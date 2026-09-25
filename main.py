import asyncio
import json
import re
import platform
import zipfile
import subprocess
import webbrowser
import ssl
import os
import signal
import time
import shutil
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Set, List, Tuple

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from astrbot.api import AstrBotConfig
except ImportError:
    AstrBotConfig = dict

from .pulid_api import NapCatAPI

PLUGIN_CODE_VERSION = "2026-09-25-v18-versionfix"
PLUGIN_NAME = "pulid_napcat_go_to_astrbot"
CONFIG_VERSION = 3

NAPCAT_RELEASE_BASE = "https://github.com/NapNeko/NapCatQQ/releases/download"
NAPCAT_LINUX_INSTALL_URL = "https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh"
DEFAULT_NAPCAT_VERSION = "v4.18.28"
DEFAULT_DOWNLOAD_MIRROR = "https://gh.zwy.one/"

NAPCAT_MIRROR_CANDIDATES = [
    "https://gh.zwy.one/", "https://raw.ihtw.moe/", "https://gh.llkk.cc/",
    "https://gh.xxooo.cf/", "https://ghfile.geekertao.top/", "https://ghproxy.cxkpro.top/",
    "https://git.yylx.win/", "https://gh.h233.eu.org/", "https://cdn.crashmc.com/",
    "https://githubproxy.cc/", "https://gh-proxy.com/", "https://ghproxy.net/",
    "https://ghfast.top/",
]

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"

DEFAULT_CONFIG = {
    "auto_sync": True,
    "auto_start": True,
    "napcat_port": 6099,
    "napcat_version": DEFAULT_NAPCAT_VERSION,
    "napcat_download_mirror": DEFAULT_DOWNLOAD_MIRROR,
    "napcat_config_dir": "",
    "astrbot_config_path": "",
    "qq_number": "",
}

CREATE_NO_WINDOW = 0x08000000


# ============================================================
# Path resolution
# ============================================================

def resolve_persistent_dir(context: Any) -> Path:
    candidate: Optional[Path] = None
    if hasattr(context, "get_data_dir"):
        try:
            candidate = Path(context.get_data_dir())
        except Exception:
            candidate = None
    if candidate is None:
        candidate = Path("data") / "plugin_data" / PLUGIN_NAME
    else:
        if candidate.name != PLUGIN_NAME:
            candidate = candidate / PLUGIN_NAME
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ============================================================
# Utility
# ============================================================

def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_qq_installed() -> bool:
    if IS_WINDOWS:
        try:
            import winreg
        except ImportError:
            return True
        for path in [
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        ]:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                    s, _ = winreg.QueryValueEx(key, "UninstallString")
                    if s:
                        return True
            except Exception:
                continue
        return False
    for p in ["/opt/QQ/qq", "/opt/QQ/QQ", "/usr/bin/qq", "/usr/local/bin/qq",
              os.path.expanduser("~/Applications/QQ.app/Contents/MacOS/QQ"),
              "/Applications/QQ.app/Contents/MacOS/QQ"]:
        if os.path.exists(p):
            return True
    return shutil.which("qq") is not None


def list_pids_by_name(name: str) -> Set[int]:
    pids: Set[int] = set()
    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=10,
            ).decode("utf-8", errors="ignore")
        except Exception:
            return pids
        for line in out.splitlines():
            line = line.strip().strip('"')
            if not line or line.startswith("INFO:"):
                continue
            parts = line.split('","')
            if len(parts) >= 2:
                try:
                    pids.add(int(parts[1].strip('"')))
                except ValueError:
                    continue
    else:
        try:
            out = subprocess.check_output(["pgrep", "-f", name], stderr=subprocess.DEVNULL, timeout=10).decode()
            for line in out.splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        except Exception:
            pass
    return pids


def kill_pid_tree(pid: int):
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=10)
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except Exception:
            pass


def kill_pids(pids: Set[int]):
    for pid in pids:
        kill_pid_tree(pid)


# ============================================================
# NapCat manager
# ============================================================

class NapCatManager:
    def __init__(self, plugin: "NapCatGoPlugin"):
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(self.context.plugin_dir) if hasattr(self.context, "plugin_dir") else Path(__file__).parent

        self.persistent_dir = resolve_persistent_dir(self.context)
        self.napcat_dir = self.persistent_dir / "napcat"
        self.legacy_napcat_dir = self.plugin_dir / "napcat"
        self.napcat_dir.mkdir(parents=True, exist_ok=True)

        self.process: Optional[asyncio.subprocess.Process] = None
        self.napcat_token: str = ""
        self.napcat_webui_url: str = ""
        self.napcat_port: int = DEFAULT_CONFIG["napcat_port"]
        self._log_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._downloading = False
        self._qq_logged_in = False
        self._auto_link_done = False
        self._pre_existing_qq_pids: Set[int] = set()
        self._launched_pids: Set[int] = set()

        self.api = NapCatAPI(host="127.0.0.1", port=self.napcat_port, token="")
        self.log_lines: deque = deque(maxlen=500)

        self.download_state: Dict[str, Any] = {
            "downloading": False, "percent": 0,
            "downloaded_mb": 0.0, "total_mb": 0.0, "speed_kbps": 0.0,
            "current_mirror": "", "current_index": 0, "total_mirrors": 0,
            "phase": "idle", "message": "",
        }

        self._migrate_legacy()

    def _migrate_legacy(self):
        if not self.legacy_napcat_dir.exists():
            return
        try:
            new_has_content = any(self.napcat_dir.iterdir())
        except Exception:
            new_has_content = False
        if new_has_content:
            return
        logger.info(f"[NapCat_Go] migrating NapCat data: {self.legacy_napcat_dir} -> {self.napcat_dir}")
        try:
            shutil.copytree(self.legacy_napcat_dir, self.napcat_dir, dirs_exist_ok=True)
            logger.info("[NapCat_Go] migration OK")
            self.log_lines.append(f"[migrate] {self.legacy_napcat_dir} -> {self.napcat_dir}")
        except Exception as e:
            logger.error(f"[NapCat_Go] migration failed: {e}")

    def _find_entry(self) -> Optional[str]:
        for name in ["launcher-win10-user.bat", "launcher-win10.bat", "launcher.bat",
                     "launcher-user.sh", "launcher.sh", "napcat.mjs", "index.js"]:
            if (self.napcat_dir / name).exists():
                return name
        return None

    def _find_linux_napcat_install(self) -> Optional[Path]:
        if (self.napcat_dir / "NapCat").exists():
            return self.napcat_dir
        if (self.legacy_napcat_dir / "NapCat").exists():
            return self.legacy_napcat_dir
        for c in [Path("/usr/local/napcat"), Path("/opt/napcat"), Path.home() / "napcat"]:
            try:
                if c.exists() and (c / "NapCat").exists():
                    return c
            except Exception:
                continue
        return None

    def _refresh_launched_pids(self):
        try:
            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            cur = list_pids_by_name(qq_name)
            self._launched_pids.update(cur - self._pre_existing_qq_pids)
            if IS_WINDOWS:
                self._launched_pids.update(list_pids_by_name("NapCatWinBootMain.exe"))
            else:
                self._launched_pids.update(list_pids_by_name("NapCat"))
        except Exception:
            pass

    def _cleanup_processes(self):
        self._refresh_launched_pids()
        if self._launched_pids:
            kill_pids(self._launched_pids)
            self._launched_pids.clear()

    def _reset_download_state(self):
        self.download_state.update({
            "downloading": False, "percent": 0,
            "downloaded_mb": 0.0, "total_mb": 0.0, "speed_kbps": 0.0,
            "current_mirror": "", "current_index": 0, "total_mirrors": 0,
            "phase": "idle", "message": "",
        })

    async def download_napcat(self) -> bool:
        if not IS_WINDOWS:
            return True
        if self._downloading:
            return False
        self._downloading = True
        self._reset_download_state()
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.log_lines.append("[download] 开始下载 NapCat.Shell.zip ...")
        try:
            version = self.plugin.config.get("napcat_version", DEFAULT_NAPCAT_VERSION)
            # 兜底：万一 config.json 里存了带路径的非法值，就地纠正
            if "/" in version:
                fixed = version.split("/", 1)[0].strip()
                if fixed:
                    logger.warning(
                        f"[NapCat_Go] invalid napcat_version {version!r}, "
                        f"auto-corrected to {fixed!r}"
                    )
                    self.log_lines.append(
                        f"[download] config 里 napcat_version 非法（{version}），"
                        f"自动纠正为 {fixed}"
                    )
                    version = fixed
                    self.plugin.config["napcat_version"] = fixed
                    try:
                        self.plugin._save_config(self.plugin.config)
                    except Exception:
                        pass
                else:
                    version = DEFAULT_NAPCAT_VERSION

            filename = "NapCat.Shell.zip"

            for local_zip in [
                self.napcat_dir / filename,
                self.napcat_dir / "napcat.shell.zip",
                self.plugin_dir / filename,
                self.plugin_dir / "napcat.shell.zip",
            ]:
                if local_zip.exists():
                    logger.info(f"Found local archive: {local_zip}")
                    self.log_lines.append(f"[download] 发现本地压缩包: {local_zip}")
                    ok = await self._extract_napcat(local_zip)
                    if ok:
                        self.download_state["phase"] = "done"
                        self.log_lines.append("[download] ✔ 本地压缩包解压成功")
                    else:
                        self.download_state["phase"] = "failed"
                        self.log_lines.append("[download] ✘ 本地压缩包解压失败")
                    return ok

            official = f"{NAPCAT_RELEASE_BASE}/{version}/{filename}"
            self.log_lines.append(f"[download] 目标版本: {version}")
            user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
            prefixes: List[str] = []
            if user_mirror:
                prefixes.append(user_mirror)
            for m in NAPCAT_MIRROR_CANDIDATES:
                if m not in prefixes:
                    prefixes.append(m)

            urls = [(f"{p.rstrip('/')}/{official}", p) for p in prefixes]
            urls.append((official, "official"))
            download_path = self.napcat_dir / filename
            self.download_state["total_mirrors"] = len(urls)
            self.log_lines.append(f"[download] 共 {len(urls)} 个镜像候选，依次尝试")

            for idx, (url, label) in enumerate(urls, 1):
                logger.info(f"[{idx}/{len(urls)}] Trying {label}")
                self.log_lines.append(f"[download] [{idx}/{len(urls)}] 尝试: {label}")
                self.download_state["current_mirror"] = label
                self.download_state["current_index"] = idx
                ssl_ctx = make_ssl_context()
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                session = aiohttp.ClientSession(connector=connector)
                try:
                    async with session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
                        ) as resp:
                            if resp.status != 200:
                                self.log_lines.append(
                                    f"[download] [{idx}/{len(urls)}] {label} HTTP {resp.status}，换下一个"
                                )
                                continue
                            total = resp.content_length or 0
                            downloaded = 0
                            start = time.time()
                            last_update = start
                            with open(download_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 128):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    if now - last_update >= 0.4:
                                        elapsed = now - start
                                        speed = downloaded / elapsed / 1024 if elapsed > 0 else 0
                                        self.download_state["downloaded_mb"] = downloaded / 1024 / 1024
                                        self.download_state["speed_kbps"] = speed
                                        if total > 0:
                                            self.download_state["total_mb"] = total / 1024 / 1024
                                            self.download_state["percent"] = downloaded * 100 // total
                                        last_update = now

                    success = await self._extract_napcat(download_path)
                    if success:
                        try:
                            download_path.unlink()
                        except Exception:
                            pass
                        self.download_state["phase"] = "done"
                        self.log_lines.append(f"[download] ✔ 下载并解压成功（来源: {label}）")
                        return True
                    self.log_lines.append(
                        f"[download] [{idx}/{len(urls)}] {label} 下载完成但解压失败，换下一个"
                    )
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue

                except Exception as e:
                    logger.debug(f"{label} failed: {e}")
                    self.log_lines.append(
                        f"[download] [{idx}/{len(urls)}] {label} 失败: {e}"
                    )
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue

            self.log_lines.append("[download] ✘ 所有镜像均失败，请手动下载或更换网络")
            self.download_state["phase"] = "failed"
            return False
        finally:
            self._downloading = False
            self.download_state["downloading"] = False

    async def _extract_napcat(self, archive_path: Path) -> bool:
        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(self.napcat_dir)
            if not self._find_entry():
                logger.warning("Entry file not found after extract")
                return False
            logger.info(f"Extracted, entry: {self._find_entry()}")
            return True
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return False

    async def install_linux_napcat(self) -> bool:
        install_target = str(self.napcat_dir)
        self.napcat_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Linux NapCat install target: {install_target}")
        script_path = self.napcat_dir / "napcat_install.sh"
        cmd = (f'curl -fsSL -o "{script_path}" {NAPCAT_LINUX_INSTALL_URL} && '
               f'NAPCAT_INSTALL_DIR="{install_target}" '
               f'NAPCAT_PATH="{install_target}" '
               f'INSTALL_DIR="{install_target}" '
               f'bash "{script_path}"')
        try:
            proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.STDOUT,
                                                          cwd=str(self.napcat_dir))
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore") if stdout else ""
            for line in output.splitlines()[-40:]:
                if line.strip():
                    self.log_lines.append(f"[install] {line.strip()}")
            ok = (self.napcat_dir / "NapCat").exists()
            try:
                if script_path.exists():
                    script_path.unlink()
            except Exception:
                pass
            return proc.returncode == 0 and ok
        except Exception as e:
            logger.error(f"Install exception: {e}")
            return False

    async def start(self) -> bool:
        async with self._lock:
            if self.process and self.process.returncode is None:
                logger.warning("NapCat is already running")
                return True

            # 先确保 NapCat 本体就绪（未安装则下载），再检查 QQ
            if IS_WINDOWS and not self._find_entry():
                logger.info("NapCat not found, downloading...")
                if not await self.download_napcat():
                    logger.error("NapCat download failed")
                    return False
                if not self._find_entry():
                    logger.error("NapCat entry still missing after download")
                    self.log_lines.append("[start] 下载完成但入口文件仍缺失")
                    return False

            if not is_qq_installed():
                logger.error("QQ client not detected")
                self.log_lines.append("[start] 未检测到 QQ 客户端，请先安装 QQNT")
                return False

            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            self._pre_existing_qq_pids = list_pids_by_name(qq_name)
            self._launched_pids.clear()
            qq_number = str(self.plugin.config.get("qq_number", "") or "").strip()

            if IS_WINDOWS:
                launcher = None
                for name in ["launcher-win10-user.bat", "launcher-win10.bat", "launcher.bat", "launcher-user.bat"]:
                    p = self.napcat_dir / name
                    if p.exists():
                        launcher = p
                        break
                if not launcher:
                    logger.error(f"Windows launcher not found: {self.napcat_dir}")
                    self.log_lines.append(f"[start] 未找到启动器: {self.napcat_dir}")
                    return False
                cmd = ["cmd", "/c", launcher.name]
                if qq_number:
                    cmd += ["-q", qq_number]
                cwd = str(self.napcat_dir)
                creationflags = CREATE_NO_WINDOW
                logger.info(f"Starting NapCat (Windows): {' '.join(cmd)}")
                self.log_lines.append(f"[start] 启动 NapCat: {launcher.name}")
            else:
                if not shutil.which("xvfb-run"):
                    logger.error("xvfb-run not found")
                    return False
                linux_dir = self._find_linux_napcat_install()
                if not linux_dir:
                    await self.install_linux_napcat()
                    linux_dir = self._find_linux_napcat_install()
                    if not linux_dir:
                        logger.error("NapCat not installed")
                        return False
                napcat_bin = linux_dir / "NapCat"
                if not napcat_bin.exists():
                    return False
                try:
                    napcat_bin.chmod(0o755)
                except Exception:
                    pass
                cmd = ["xvfb-run", "-a", str(napcat_bin)]
                if qq_number:
                    cmd += ["-q", qq_number]
                cwd = str(linux_dir)
                creationflags = 0
                logger.info(f"Starting NapCat (Linux): {' '.join(cmd)}")

            self._auto_link_done = False
            self._qq_logged_in = False
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd, creationflags=creationflags,
                )
            except Exception as e:
                logger.error(f"Failed to start NapCat: {e}")
                self.log_lines.append(f"[start] 启动失败: {e}")
                return False

            self._log_task = asyncio.create_task(self._read_logs())
            await asyncio.sleep(2)
            self._refresh_launched_pids()
            return True

    async def stop(self):
        async with self._lock:
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        self.process.kill()
                        await self.process.wait()
                    except Exception:
                        pass
                except Exception:
                    pass
                self.process = None
            if self._log_task and not self._log_task.done():
                self._log_task.cancel()
                try:
                    await self._log_task
                except asyncio.CancelledError:
                    pass
                self._log_task = None
            self._cleanup_processes()
            await asyncio.sleep(1.5)
            self.napcat_webui_url = ""
            self.napcat_token = ""
            self._qq_logged_in = False

    async def restart(self):
        await self.stop()
        await asyncio.sleep(1)
        await self.start()

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def is_installed(self) -> bool:
        if IS_WINDOWS:
            return self._find_entry() is not None
        return self._find_linux_napcat_install() is not None

    def cleanup(self):
        try:
            if self.process and self.process.returncode is None:
                self.process.terminate()
        except Exception:
            pass
        self._cleanup_processes()

    async def _read_logs(self):
        assert self.process and self.process.stdout
        webui_url_pattern = re.compile(r"WebUi\s+User\s+Panel\s+Url:\s*(https?://\S+)", re.IGNORECASE)
        token_pattern = re.compile(r"(?:token|access[_-]?token|WebUi\s*Token|WebUI\s*Token)[=:\s]+([A-Za-z0-9\-_]+)", re.IGNORECASE)
        login_ok_pattern = re.compile(r"(Login Success|login success|登录成功|快速登录成功)", re.IGNORECASE)

        def _usable(url: str) -> bool:
            return "[::]" not in url and "0.0.0.0" not in url

        while True:
            try:
                line = await self.process.stdout.readline()
            except (asyncio.CancelledError, RuntimeError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            if "接收 <-" not in text and "发送 ->" not in text:
                self.log_lines.append(text)

            m = webui_url_pattern.search(text)
            if m:
                url = m.group(1).rstrip(".,;，。；")
                if not _usable(url):
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm and not self.napcat_token:
                        self.napcat_token = tm.group(1)
                        self.api.set_token(self.napcat_token)
                    continue
                if url != self.napcat_webui_url:
                    self.napcat_webui_url = url
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm:
                        self.napcat_token = tm.group(1)
                        self.api.set_token(self.napcat_token)
                    logger.info("NapCat WebUI URL detected")
                continue

            if not self.napcat_webui_url:
                tm = token_pattern.search(text)
                if tm and tm.group(1) != self.napcat_token:
                    self.napcat_token = tm.group(1)
                    self.api.set_token(self.napcat_token)

            if not self._qq_logged_in and login_ok_pattern.search(text):
                self._qq_logged_in = True
                logger.info("NapCat QQ login detected")
                if self.plugin.config.get("auto_sync", True) and not self._auto_link_done:
                    self._auto_link_done = True
                    asyncio.create_task(self._auto_sync_later())
        self.process = None

    async def _auto_sync_later(self):
        try:
            await asyncio.sleep(2)
            ok, msg = self.plugin.syncer.sync()
            if ok:
                logger.info(f"Auto sync ok: {msg}")
        except Exception as e:
            logger.error(f"Auto sync exception: {e}")


# ============================================================
# Config syncer
# ============================================================

class ConfigSyncer:
    def __init__(self, plugin: "NapCatGoPlugin"):
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(self.context.plugin_dir) if hasattr(self.context, "plugin_dir") else Path(__file__).parent
        self.persistent_dir = resolve_persistent_dir(self.context)
        self.napcat_dir = self.persistent_dir / "napcat"
        self.legacy_napcat_dir = self.plugin_dir / "napcat"

        self.astrbot_config_path: Optional[Path] = None
        self.astrbot_bot: Optional[Dict[str, Any]] = None
        self.napcat_config_dir: Optional[Path] = None
        self.napcat_config_file: Optional[Path] = None
        self.last_sync_time: Optional[str] = None
        self.last_sync_ok: bool = False
        self.last_sync_msg: str = "Not synced yet"

    def refresh(self):
        self.astrbot_config_path = self._find_astrbot_config_path()
        self.astrbot_bot = self._read_astrbot_bot()
        self.napcat_config_dir = self._find_napcat_config_dir()
        self.napcat_config_file = self._find_onebot11_file()

    def _find_astrbot_config_path(self) -> Optional[Path]:
        manual = str(self.plugin.config.get("astrbot_config_path", "") or "").strip()
        if manual:
            return Path(manual)
        candidates: List[Path] = []
        try:
            candidates.append(self.plugin_dir.parent.parent / "cmd_config.json")
        except Exception:
            pass
        if hasattr(self.context, "get_data_dir"):
            try:
                candidates.append(Path(self.context.get_data_dir()) / "cmd_config.json")
            except Exception:
                pass
        candidates += [Path("data/cmd_config.json"), Path("../data/cmd_config.json"), Path("../../data/cmd_config.json")]
        seen = set()
        for c in candidates:
            try:
                ap = c.resolve()
            except Exception:
                continue
            if ap in seen:
                continue
            seen.add(ap)
            if ap.exists() and ap.is_file():
                return ap
        return None

    def _read_astrbot_bot(self) -> Optional[Dict[str, Any]]:
        if not self.astrbot_config_path or not self.astrbot_config_path.exists():
            return None
        try:
            with open(self.astrbot_config_path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            logger.error(f"Read AstrBot config failed: {e}")
            return None
        for p in cfg.get("platform", []) or []:
            if not isinstance(p, dict):
                continue
            if p.get("type") != "aiocqhttp" or not p.get("enable", True):
                continue
            port = p.get("ws_reverse_port")
            if not port:
                continue
            host = str(p.get("ws_reverse_host", "0.0.0.0") or "0.0.0.0")
            if host in ("0.0.0.0", "[::]", "::", ""):
                host = "127.0.0.1"
            token = p.get("ws_reverse_token") or p.get("access_token") or ""
            return {"id": p.get("id", ""), "host": host, "port": int(port),
                    "path": p.get("ws_reverse_path", "/ws/") or "/ws/",
                    "token": str(token)}
        return None

    def _find_napcat_config_dir(self) -> Optional[Path]:
        manual = str(self.plugin.config.get("napcat_config_dir", "") or "").strip()
        if manual:
            return Path(manual)
        for candidate in [self.napcat_dir / "config", self.legacy_napcat_dir / "config"]:
            if candidate.exists() and candidate.is_dir():
                if list(candidate.glob("onebot11*.json")):
                    return candidate
        system = platform.system()
        home = Path.home()
        candidates: List[Path] = []
        if system == "Windows":
            candidates += [home / "NapCat" / "config", home / "Documents" / "NapCat" / "config",
                           Path("C:/NapCat/config"), Path("D:/NapCat/config")]
        elif system == "Darwin":
            candidates += [home / "NapCat" / "config", Path("/Applications/NapCat/config"),
                           Path("/usr/local/napcat/config")]
        else:
            candidates += [Path("/usr/local/napcat/config"), Path("/opt/napcat/config"),
                           Path("/root/napcat/config"), home / "napcat" / "config"]
        for c in candidates:
            try:
                if c.exists() and c.is_dir() and list(c.glob("onebot11*.json")):
                    return c
            except Exception:
                continue
        return None

    def _find_onebot11_file(self) -> Optional[Path]:
        if not self.napcat_config_dir or not self.napcat_config_dir.exists():
            return None
        try:
            cs = list(self.napcat_config_dir.glob("onebot11_*.json"))
            cs += list(self.napcat_config_dir.glob("onebot11.json"))
            if not cs:
                return None
            qq = str(self.plugin.config.get("qq_number", "") or "").strip()
            if qq:
                target = f"onebot11_{qq}.json"
                for c in cs:
                    if c.name == target:
                        return c
            return max(cs, key=lambda p: p.stat().st_mtime)
        except Exception:
            return None

    def scan_candidates(self) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen = set()
        candidates = [self.napcat_dir / "config", self.legacy_napcat_dir / "config"]
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            candidates += [home / "NapCat" / "config", home / "Documents" / "NapCat" / "config",
                           Path("C:/NapCat/config"), Path("D:/NapCat/config")]
        elif system == "Darwin":
            candidates += [home / "NapCat" / "config", Path("/Applications/NapCat/config"),
                           Path("/usr/local/napcat/config")]
        else:
            candidates += [Path("/usr/local/napcat/config"), Path("/opt/napcat/config"),
                           Path("/root/napcat/config"), home / "napcat" / "config"]
        for c in candidates:
            try:
                if not c.exists() or not c.is_dir():
                    continue
                ap = c.resolve()
                if ap in seen:
                    continue
                seen.add(ap)
                for f in c.glob("onebot11*.json"):
                    results.append({"dir": str(c), "file": str(f), "file_name": f.name})
            except Exception:
                continue
        return results

    def sync(self) -> Tuple[bool, str]:
        self.refresh()
        if not self.astrbot_config_path:
            return self._fail("AstrBot config cmd_config.json not found")
        if not self.astrbot_bot:
            return self._fail("No enabled OneBot v11 bot in AstrBot")
        if not self.napcat_config_dir:
            return self._fail("NapCat config dir not found")
        if not self.napcat_config_file:
            return self._fail("No onebot11_*.json found")
        bot = self.astrbot_bot
        ws_url = f"ws://{bot['host']}:{bot['port']}{bot['path']}"
        try:
            with open(self.napcat_config_file, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
        except Exception as e:
            return self._fail(f"Read failed: {e}")
        network = config.setdefault("network", {})
        clients = network.get("websocketClients") or []
        new_clients = []
        for c in clients:
            if not isinstance(c, dict):
                continue
            url = str(c.get("url", ""))
            if url.startswith("ws://127.0.0.1") and "/ws/" in url:
                continue
            new_clients.append(c)
        new_clients.append({"enable": True, "url": ws_url, "token": bot["token"]})
        network["websocketClients"] = new_clients
        try:
            with open(self.napcat_config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return self._fail(f"Write failed: {e}")
        msg = f"Synced to {self.napcat_config_file.name} ({ws_url})"
        self.last_sync_ok = True
        self.last_sync_msg = msg
        self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[NapCat_Go] {msg}")
        return True, msg

    def _fail(self, msg: str) -> Tuple[bool, str]:
        self.last_sync_ok = False
        self.last_sync_msg = msg
        self.last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return False, msg

    def status_dict(self) -> Dict[str, Any]:
        bot = self.astrbot_bot or {}
        return {
            "platform": platform.system(),
            "is_windows": IS_WINDOWS, "is_linux": IS_LINUX, "is_macos": IS_MACOS,
            "persistent_dir": str(self.persistent_dir),
            "napcat_data_dir": str(self.napcat_dir),
            "legacy_napcat_dir": str(self.legacy_napcat_dir) if self.legacy_napcat_dir.exists() else None,
            "astrbot_config_path": str(self.astrbot_config_path) if self.astrbot_config_path else None,
            "astrbot_bot_found": self.astrbot_bot is not None,
            "astrbot_bot_id": bot.get("id"), "astrbot_bot_host": bot.get("host"),
            "astrbot_bot_port": bot.get("port"), "astrbot_bot_token": bot.get("token", ""),
            "napcat_config_dir": str(self.napcat_config_dir) if self.napcat_config_dir else None,
            "napcat_config_file": str(self.napcat_config_file) if self.napcat_config_file else None,
            "last_sync_ok": self.last_sync_ok, "last_sync_msg": self.last_sync_msg,
            "last_sync_time": self.last_sync_time, "config": self.plugin.config,
        }


# ============================================================
# Plugin entry
# ============================================================

class NapCatGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.astrbot_config = config

        logger.info(f"[NapCat_Go] ============ CODE VERSION: {PLUGIN_CODE_VERSION} ============")

        self.persistent_dir = resolve_persistent_dir(context)
        logger.info(f"[NapCat_Go] persistent dir: {self.persistent_dir}")

        self.config = self._load_config()
        self.syncer = ConfigSyncer(self)
        self.manager = NapCatManager(self)

        logger.info(f"[NapCat_Go] NapCat data dir: {self.manager.napcat_dir}")

        # ---------- Web API ----------
        def _reg(route, handler, methods, desc=""):
            try:
                context.register_web_api(route, handler, methods, desc)
            except TypeError:
                try:
                    context.register_web_api(route, handler, methods)
                except TypeError as e:
                    logger.error(f"register_web_api failed route={route}: {e}")

        _reg(f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "Get status")
        _reg(f"/{PLUGIN_NAME}/sync", self.api_sync, ["POST"], "Sync now")
        _reg(f"/{PLUGIN_NAME}/refresh", self.api_refresh, ["POST"], "Refresh")
        _reg(f"/{PLUGIN_NAME}/scan", self.api_scan, ["POST"], "Scan dirs")
        _reg(f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "Get config")
        _reg(f"/{PLUGIN_NAME}/config/save", self.api_save_config, ["POST"], "Save config")
        _reg(f"/{PLUGIN_NAME}/start", self.api_start, ["POST"], "Start NapCat")
        _reg(f"/{PLUGIN_NAME}/stop", self.api_stop, ["POST"], "Stop NapCat")
        _reg(f"/{PLUGIN_NAME}/restart", self.api_restart, ["POST"], "Restart NapCat")
        _reg(f"/{PLUGIN_NAME}/install", self.api_install, ["POST"], "Install (Linux)")
        _reg(f"/{PLUGIN_NAME}/logs", self.api_get_logs, ["GET"], "Get logs")
        _reg(f"/{PLUGIN_NAME}/open-webui", self.api_open_webui, ["POST"], "Open WebUI")
        _reg(f"/{PLUGIN_NAME}/cleanup", self.api_cleanup, ["POST"], "Cleanup")

        logger.info(f"[NapCat_Go] FINAL CONFIG: {self.config}")

        auto_start = bool(self.config.get("auto_start", True))
        auto_sync = bool(self.config.get("auto_sync", True))
        installed = self.manager.is_installed()
        logger.info(f"[NapCat_Go] auto_start={auto_start} auto_sync={auto_sync} installed={installed}")

        if auto_start:
            logger.info("[NapCat_Go] scheduling auto start in 3s")
            self._auto_start_task = asyncio.ensure_future(self._auto_start_wrapper())
        elif auto_sync and installed:
            try:
                ok, msg = self.syncer.sync()
                logger.info(f"[NapCat_Go] immediate sync result: {ok} {msg}")
            except Exception:
                pass

    # ---------- /napcat 指令 ----------

    @filter.command("napcat")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def napcat_cmd(self, event: AstrMessageEvent):
        """显示 NapCat 插件状态"""
        try:
            yield event.plain_result(self._status_text())
        except Exception as e:
            logger.error(f"[NapCat_Go] napcat_cmd failed: {e}", exc_info=True)

    # ---------- auto start ----------

    async def _auto_start_wrapper(self):
        try:
            await asyncio.sleep(3)
            logger.info("[NapCat_Go] auto_start: beginning start sequence")
            ok = await self.manager.start()
            if ok and self.manager.is_running():
                logger.info("[NapCat_Go] auto_start: NapCat is running")
            else:
                logger.warning(f"[NapCat_Go] auto_start: failed (returned {ok})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[NapCat_Go] auto_start failed: {e}", exc_info=True)

    # ---------- API ----------

    async def api_status(self):
        try:
            self.syncer.refresh()
        except Exception:
            pass
        d = self.syncer.status_dict()
        d["running"] = self.manager.is_running()
        d["napcat_installed"] = self.manager.is_installed()
        d["qq_installed"] = is_qq_installed()
        d["napcat_webui_url"] = self.manager.napcat_webui_url
        d["napcat_token"] = self.manager.napcat_token
        d["napcat_port"] = self.manager.napcat_port
        d["download_state"] = dict(self.manager.download_state)
        d["log_count"] = len(self.manager.log_lines)
        qq_logged_in = self.manager._qq_logged_in
        qq_user_id = None
        qq_nickname = ""
        if d["running"]:
            try:
                info = await self.manager.api.get_login_info()
                if info and isinstance(info, dict):
                    data = info.get("data") or {}
                    user_id = data.get("user_id")
                    if user_id:
                        qq_logged_in = True
                        qq_user_id = user_id
                        qq_nickname = data.get("nickname", "")
                        self.manager._qq_logged_in = True
                    else:
                        qq_logged_in = False
            except Exception:
                pass
        d["qq_logged_in"] = qq_logged_in
        d["qq_user_id"] = qq_user_id
        d["qq_nickname"] = qq_nickname
        return json_response(d)

    async def api_sync(self):
        try:
            ok, msg = self.syncer.sync()
            return json_response({"ok": ok, "message": msg, "status": self.syncer.status_dict()})
        except Exception as e:
            return error_response(f"Sync exception: {e}")

    async def api_refresh(self):
        try:
            self.syncer.refresh()
            return json_response({"ok": True, "status": self.syncer.status_dict()})
        except Exception as e:
            return error_response(f"Refresh failed: {e}")

    async def api_scan(self):
        try:
            return json_response({"ok": True, "candidates": self.syncer.scan_candidates()})
        except Exception as e:
            return error_response(f"Scan failed: {e}")

    async def api_get_config(self):
        return json_response(self.config)

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("Request body must be JSON")
        self.config.update({
            "napcat_config_dir": str(payload.get("napcat_config_dir", "") or "").strip(),
            "astrbot_config_path": str(payload.get("astrbot_config_path", "") or "").strip(),
            "qq_number": str(payload.get("qq_number", "") or "").strip(),
            "napcat_download_mirror": str(
                payload.get("napcat_download_mirror",
                            self.config.get("napcat_download_mirror", DEFAULT_DOWNLOAD_MIRROR))
            ),
        })
        self._save_config(self.config)
        try:
            ok, msg = self.syncer.sync()
            return json_response({"ok": True, "sync_ok": ok, "sync_msg": msg,
                                  "status": self.syncer.status_dict()})
        except Exception as e:
            return json_response({"ok": True, "sync_ok": False,
                                  "sync_msg": f"Sync exception: {e}",
                                  "status": self.syncer.status_dict()})

    async def api_start(self):
        ok = await self.manager.start()
        return json_response({"ok": ok})

    async def api_stop(self):
        await self.manager.stop()
        return json_response({"ok": True})

    async def api_restart(self):
        await self.manager.restart()
        return json_response({"ok": True})

    async def api_install(self):
        if IS_WINDOWS:
            return error_response("Windows does not need install script")
        try:
            ok = await self.manager.install_linux_napcat()
            return json_response({"ok": ok})
        except Exception as e:
            return error_response(f"Install failed: {e}")

    async def api_get_logs(self):
        since = request.query.get("since", 0, type=int)
        lines = list(self.manager.log_lines)
        return json_response({"lines": lines[since:], "total": len(lines)})

    async def api_open_webui(self):
        url = self.manager.napcat_webui_url
        if not url:
            return error_response("WebUI URL not available")
        try:
            webbrowser.open(url)
            return json_response({"ok": True, "url": url})
        except Exception as e:
            return error_response(f"Open browser failed: {e}")

    async def api_cleanup(self):
        try:
            await self.manager.stop()
            self.manager.cleanup()
            return json_response({"ok": True, "message": "Cleaned"})
        except Exception as e:
            return error_response(f"Cleanup failed: {e}")

    # ---------- Config ----------

    def _load_config(self) -> Dict[str, Any]:
        self.config_path = self.persistent_dir / "config.json"
        config = DEFAULT_CONFIG.copy()

        persisted_user: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as f:
                    persisted_user = json.load(f)
                logger.info(f"[NapCat_Go] persistent config loaded: {persisted_user}")
            except Exception as e:
                logger.error(f"[NapCat_Go] read persistent config failed: {e}")
                persisted_user = {}

        stored_version = persisted_user.get("__config_version__", 1)
        if stored_version < CONFIG_VERSION:
            logger.info(f"[NapCat_Go] migrating config: v{stored_version} -> v{CONFIG_VERSION}")
            persisted_user["__config_version__"] = CONFIG_VERSION

        # ---- napcat_version 迁移 ----
        # 1) 老默认值（LEGACY_NAPCAT_VERSIONS 里的）自动升到新默认
        # 2) 出现 "/" 的非法值（例如 "v4.18.28/NapCat.Shell.zip"）就地纠正
        persisted_napcat_ver = str(persisted_user.get("napcat_version", "") or "").strip()
        if persisted_napcat_ver:
            if persisted_napcat_ver in LEGACY_NAPCAT_VERSIONS:
                logger.info(
                    f"[NapCat_Go] napcat_version legacy default {persisted_napcat_ver!r} "
                    f"-> {DEFAULT_NAPCAT_VERSION!r}"
                )
                persisted_user["napcat_version"] = DEFAULT_NAPCAT_VERSION
            elif "/" in persisted_napcat_ver:
                fixed = persisted_napcat_ver.split("/", 1)[0].strip()
                if fixed:
                    logger.warning(
                        f"[NapCat_Go] napcat_version invalid {persisted_napcat_ver!r}, "
                        f"auto-corrected to {fixed!r}"
                    )
                    persisted_user["napcat_version"] = fixed
                else:
                    persisted_user["napcat_version"] = DEFAULT_NAPCAT_VERSION

        for k in DEFAULT_CONFIG:
            if k in persisted_user:
                config[k] = persisted_user[k]

        obj_cfg: Dict[str, Any] = {}
        if self.astrbot_config is not None:
            try:
                if hasattr(self.astrbot_config, "keys"):
                    for k in self.astrbot_config.keys():
                        try:
                            obj_cfg[k] = self.astrbot_config[k]
                        except Exception:
                            pass
                else:
                    for k in DEFAULT_CONFIG:
                        try:
                            obj_cfg[k] = self.astrbot_config[k]
                        except Exception:
                            pass
                logger.info(f"[NapCat_Go] AstrBotConfig object: {obj_cfg}")
            except Exception as e:
                logger.warning(f"[NapCat_Go] read AstrBotConfig obj failed: {e}")

        panel_cfg: Dict[str, Any] = {}
        panel_path = self._find_panel_config_path()
        if panel_path:
            try:
                with open(panel_path, "r", encoding="utf-8-sig") as f:
                    panel_cfg = json.load(f)
                if not isinstance(panel_cfg, dict):
                    panel_cfg = {}
                logger.info(f"[NapCat_Go] panel config loaded from {panel_path}: {panel_cfg}")
            except Exception as e:
                logger.warning(f"[NapCat_Go] read panel config failed: {e}")
                panel_cfg = {}
        else:
            logger.info("[NapCat_Go] no panel config file found")

        merged = dict(config)
        merged.update(obj_cfg)
        for k, v in panel_cfg.items():
            if k in DEFAULT_CONFIG and v is not None:
                merged[k] = v

        final = DEFAULT_CONFIG.copy()
        for k in DEFAULT_CONFIG:
            if k in merged and merged[k] is not None:
                v = merged[k]
                if k in ("auto_sync", "auto_start"):
                    final[k] = bool(v)
                elif k == "napcat_port":
                    try:
                        final[k] = int(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    final[k] = str(v or "").strip()

        # 保险：final 里如果还残留非法 napcat_version，同样纠正
        if "/" in final.get("napcat_version", ""):
            final["napcat_version"] = final["napcat_version"].split("/", 1)[0].strip() or DEFAULT_NAPCAT_VERSION

        to_save = dict(final)
        to_save["__config_version__"] = CONFIG_VERSION
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        return final

    def _find_panel_config_path(self) -> Optional[Path]:
        candidates: List[Path] = []
        try:
            data_root = self.plugin_dir.parent.parent
            candidates.append(data_root / "config" / f"{PLUGIN_NAME}_config.json")
            candidates.append(data_root / f"{PLUGIN_NAME}_config.json")
            candidates.append(data_root / "config" / f"astrbot_plugin_{PLUGIN_NAME}_config.json")
        except Exception:
            pass
        if hasattr(self.context, "get_data_dir"):
            try:
                gd = Path(self.context.get_data_dir())
                candidates.append(gd / "config" / f"{PLUGIN_NAME}_config.json")
                candidates.append(gd / f"{PLUGIN_NAME}_config.json")
            except Exception:
                pass
        seen = set()
        for c in candidates:
            try:
                ap = c.resolve()
            except Exception:
                continue
            if ap in seen:
                continue
            seen.add(ap)
            if ap.exists() and ap.is_file():
                return ap
        return None

    def _save_config(self, config: Dict[str, Any] = None):
        if config is None:
            config = self.config
        try:
            to_save = dict(config)
            to_save["__config_version__"] = CONFIG_VERSION
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save config failed: {e}")

    def _status_text(self) -> str:
        s = self.syncer.status_dict()
        lines = ["NapCat_Go Status", "-" * 20, f"Platform: {s['platform']}"]
        lines.append(f"NapCat: {'running' if self.manager.is_running() else 'stopped'}")
        lines.append(f"QQ login: {'yes' if self.manager._qq_logged_in else 'no'}")
        lines.append(f"Persistent dir: {self.persistent_dir}")
        lines.append(f"NapCat dir: {self.manager.napcat_dir}")
        lines.append(f"auto_start: {self.config.get('auto_start')}")
        return "\n".join(lines)

    async def terminate(self):
        logger.info("NapCat_Go plugin unloading...")
        task = getattr(self, "_auto_start_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.manager.stop()
        except Exception as e:
            logger.error(f"Stop NapCat failed: {e}")
            try:
                self.manager.cleanup()
            except Exception:
                pass
        logger.info("NapCat_Go plugin unloaded")

    @property
    def plugin_dir(self) -> Path:
        if hasattr(self.context, "plugin_dir"):
            return Path(self.context.plugin_dir)
        return Path(__file__).parent
