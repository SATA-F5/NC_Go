import asyncio
import json
import re
import socket
import random
import platform
import zipfile
import subprocess
import webbrowser
import ssl
import os
import signal
import time
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

PLUGIN_NAME = "pulid_napcat_go_to_astrbot"
NAPCAT_RELEASE_BASE = "https://github.com/NapNeko/NapCatQQ/releases/download"
NAPCAT_LINUX_INSTALL_URL = "https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh"
DEFAULT_NAPCAT_VERSION = "v4.17.32"
DEFAULT_DOWNLOAD_MIRROR = "https://gh.zwy.one/"

# Mirror candidates, ordered from fastest to slowest (real-world tests)
NAPCAT_MIRROR_CANDIDATES = [
    "https://gh.zwy.one/",
    "https://raw.ihtw.moe/",
    "https://gh.llkk.cc/",
    "https://gh.xxooo.cf/",
    "https://ghfile.geekertao.top/",
    "https://ghproxy.cxkpro.top/",
    "https://git.yylx.win/",
    "https://gh.h233.eu.org/",
    "https://cdn.crashmc.com/",
    "https://githubproxy.cc/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
]

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"
IS_UNIX = IS_LINUX or IS_MACOS

DEFAULT_CONFIG = {
    "auto_sync": True,
    "auto_start": False,
    "napcat_port": 6099,
    "napcat_version": DEFAULT_NAPCAT_VERSION,
    "napcat_download_mirror": DEFAULT_DOWNLOAD_MIRROR,
    "napcat_config_dir": "",
    "astrbot_config_path": "",
    "qq_number": "",
}

CREATE_NO_WINDOW = 0x08000000


# ============================================================
# Common utilities
# ============================================================

def make_ssl_context() -> ssl.SSLContext:
    # Disable cert verification to avoid SSL_CERTIFICATE_VERIFY_FAILED
    # on Python environments without a proper CA bundle.
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

    import shutil
    for p in [
        "/opt/QQ/qq",
        "/opt/QQ/QQ",
        "/usr/bin/qq",
        "/usr/local/bin/qq",
        os.path.expanduser("~/Applications/QQ.app/Contents/MacOS/QQ"),
        "/Applications/QQ.app/Contents/MacOS/QQ",
    ]:
        if os.path.exists(p):
            return True
    return shutil.which("qq") is not None


def list_pids_by_name(name: str) -> Set[int]:
    pids: Set[int] = set()

    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
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
            out = subprocess.check_output(
                ["pgrep", "-f", name],
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode()
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except Exception:
            pass
    return pids


def kill_pid_tree(pid: int):
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
            logger.info(f"Killed process tree PID={pid}")
        except Exception as e:
            logger.warning(f"taskkill PID {pid} failed: {e}")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            logger.info(f"Terminated process PID={pid}")
        except Exception as e:
            logger.warning(f"kill PID {pid} failed: {e}")


def kill_pids(pids: Set[int]):
    for pid in pids:
        kill_pid_tree(pid)


# ============================================================
# NapCat process manager
# ============================================================

class NapCatManager:
    def __init__(self, plugin: "NapCatGoPlugin"):
        self.plugin = plugin
        self.context = plugin.context
        if hasattr(self.context, "plugin_dir"):
            self.plugin_dir = Path(self.context.plugin_dir)
        else:
            self.plugin_dir = Path(__file__).parent
        self.napcat_dir = self.plugin_dir / "napcat"

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
            "downloading": False,
            "percent": 0,
            "downloaded_mb": 0.0,
            "total_mb": 0.0,
            "speed_kbps": 0.0,
            "current_mirror": "",
            "current_index": 0,
            "total_mirrors": 0,
            "phase": "idle",
            "message": "",
        }

    def _find_entry(self) -> Optional[str]:
        candidates = [
            "launcher-win10-user.bat",
            "launcher-win10.bat",
            "launcher.bat",
            "launcher-user.sh",
            "launcher.sh",
            "napcat.mjs",
            "index.js",
        ]
        for name in candidates:
            if (self.napcat_dir / name).exists():
                return name
        return None

    def _find_linux_napcat_install(self) -> Optional[Path]:
        for c in [
            Path("/usr/local/napcat"),
            Path("/opt/napcat"),
            Path.home() / "napcat",
        ]:
            try:
                if c.exists() and (c / "NapCat").exists():
                    return c
            except Exception:
                continue
        return None

    def _refresh_launched_pids(self):
        try:
            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            current_qq = list_pids_by_name(qq_name)
            self._launched_pids.update(current_qq - self._pre_existing_qq_pids)
            if IS_WINDOWS:
                self._launched_pids.update(list_pids_by_name("NapCatWinBootMain.exe"))
            else:
                self._launched_pids.update(list_pids_by_name("NapCat"))
        except Exception as e:
            logger.warning(f"Refresh launched pids failed: {e}")

    def _cleanup_processes(self):
        self._refresh_launched_pids()
        if self._launched_pids:
            logger.info(f"Cleaning up launched pids: {self._launched_pids}")
            kill_pids(self._launched_pids)
            self._launched_pids.clear()

    def _reset_download_state(self):
        self.download_state.update({
            "downloading": False,
            "percent": 0,
            "downloaded_mb": 0.0,
            "total_mb": 0.0,
            "speed_kbps": 0.0,
            "current_mirror": "",
            "current_index": 0,
            "total_mirrors": 0,
            "phase": "idle",
            "message": "",
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

        try:
            version = self.plugin.config.get("napcat_version", DEFAULT_NAPCAT_VERSION)
            filename = "NapCat.Shell.zip"

            local_candidates = [
                self.plugin_dir / "NapCat.Shell.zip",
                self.plugin_dir / "napcat.shell.zip",
                self.plugin_dir / "napcat.Shell.zip",
                self.plugin_dir / "NapCat.shell.zip",
            ]
            for local_zip in local_candidates:
                if local_zip.exists():
                    logger.info(f"Found local archive: {local_zip}")
                    self.download_state["phase"] = "extracting"
                    self.download_state["message"] = f"Extracting {local_zip.name}"
                    return await self._extract_napcat(local_zip)

            official = f"{NAPCAT_RELEASE_BASE}/{version}/{filename}"

            user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()

            mirror_prefixes: List[str] = []
            if user_mirror:
                mirror_prefixes.append(user_mirror)
            for m in NAPCAT_MIRROR_CANDIDATES:
                if m not in mirror_prefixes:
                    mirror_prefixes.append(m)

            urls: List[Tuple[str, str]] = []
            for prefix in mirror_prefixes:
                urls.append((f"{prefix.rstrip('/')}/{official}", prefix))
            urls.append((official, "official"))

            download_path = self.plugin_dir / filename
            total_mirrors = len(urls)
            self.download_state["total_mirrors"] = total_mirrors

            for idx, (url, label) in enumerate(urls, 1):
                logger.info(f"[{idx}/{total_mirrors}] Trying {label}: {url}")
                self.download_state["current_mirror"] = label
                self.download_state["current_index"] = idx
                self.download_state["percent"] = 0
                self.download_state["downloaded_mb"] = 0.0
                self.download_state["total_mb"] = 0.0
                self.download_state["speed_kbps"] = 0.0
                self.download_state["message"] = f"Downloading from {label}"

                ssl_ctx = make_ssl_context()
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                session = aiohttp.ClientSession(connector=connector)

                try:
                    async with session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(
                                total=None,
                                sock_connect=10,
                                sock_read=30,
                            ),
                        ) as resp:
                            if resp.status != 200:
                                logger.warning(f"{label} returned HTTP {resp.status}, next")
                                continue

                            total = resp.content_length or 0
                            downloaded = 0
                            last_percent = -1
                            start_time = time.time()
                            last_update_time = start_time

                            with open(download_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 128):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    if now - last_update_time >= 0.4:
                                        elapsed = now - start_time
                                        speed_kbps = downloaded / elapsed / 1024 if elapsed > 0 else 0
                                        self.download_state["downloaded_mb"] = downloaded / 1024 / 1024
                                        self.download_state["speed_kbps"] = speed_kbps
                                        if total > 0:
                                            self.download_state["total_mb"] = total / 1024 / 1024
                                            self.download_state["percent"] = downloaded * 100 // total
                                        last_update_time = now

                                        if total > 0:
                                            p = downloaded * 100 // total
                                            if p != last_percent and p % 20 == 0:
                                                last_percent = p
                                                logger.info(f"Progress: {p}% ({speed_kbps:.0f} KB/s)")

                    logger.info(f"Download finished: {download_path}")
                    self.download_state["percent"] = 100
                    self.download_state["phase"] = "extracting"
                    self.download_state["message"] = "Downloaded, extracting"
                    success = await self._extract_napcat(download_path)
                    if success:
                        try:
                            download_path.unlink()
                        except Exception:
                            pass
                        logger.info(f"OK from {label}")
                        self.download_state["phase"] = "done"
                        self.download_state["message"] = f"Downloaded from {label}"
                    else:
                        self.download_state["phase"] = "failed"
                        self.download_state["message"] = "Extraction failed"
                    return success

                except asyncio.TimeoutError:
                    logger.warning(f"{label} timeout, next")
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    logger.warning(f"{label} failed: {e}, next")
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue

            logger.error(
                "All download sources failed. Please download NapCat.Shell.zip manually:\n"
                f"  {official}\n"
                f"Place it at:\n"
                f"  {self.plugin_dir / filename}"
            )
            self.download_state["phase"] = "failed"
            self.download_state["message"] = "All sources failed"
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
                logger.warning(
                    f"Entry file not found after extract, contents: "
                    f"{[p.name for p in self.napcat_dir.iterdir()][:20]}"
                )
                return False
            logger.info(f"Extracted, entry: {self._find_entry()}")
            return True
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return False

    async def install_linux_napcat(self) -> bool:
        cmd = f"curl -o /tmp/napcat.sh {NAPCAT_LINUX_INSTALL_URL} && bash /tmp/napcat.sh"
        logger.info(f"Running Linux install script: {cmd}")
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.download_state["message"] = "Running Linux install script"

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode("utf-8", errors="ignore") if stdout else ""
            for line in output.splitlines()[-30:]:
                if line.strip():
                    self.log_lines.append(f"[install] {line.strip()}")

            if proc.returncode == 0 and self._find_linux_napcat_install():
                logger.info("Linux NapCat installed successfully")
                self.download_state["phase"] = "done"
                self.download_state["message"] = "Install done"
                return True

            logger.warning(
                f"Auto install failed (rc={proc.returncode}). "
                f"Run manually:\n"
                f"  sudo bash -c 'curl -o /tmp/napcat.sh "
                f"{NAPCAT_LINUX_INSTALL_URL} && bash /tmp/napcat.sh'"
            )
            self.download_state["phase"] = "failed"
            self.download_state["message"] = "Auto install failed"
            return False
        except Exception as e:
            logger.error(f"Install script exception: {e}")
            self.download_state["phase"] = "failed"
            self.download_state["message"] = f"Exception: {e}"
            return False
        finally:
            self.download_state["downloading"] = False

    async def start(self):
        async with self._lock:
            if self.process and self.process.returncode is None:
                logger.warning("NapCat is already running")
                return

            if not is_qq_installed():
                logger.error("QQ client not detected")
                return

            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            self._pre_existing_qq_pids = list_pids_by_name(qq_name)
            logger.info(f"Pre-existing {qq_name} pids: {self._pre_existing_qq_pids}")
            self._launched_pids.clear()

            qq_number = str(self.plugin.config.get("qq_number", "") or "").strip()

            if IS_WINDOWS:
                if not self._find_entry():
                    logger.info("NapCat not found, downloading...")
                    if not await self.download_napcat():
                        logger.error("NapCat download failed")
                        return

                launcher = None
                for name in [
                    "launcher-win10-user.bat",
                    "launcher-win10.bat",
                    "launcher.bat",
                    "launcher-user.bat",
                ]:
                    p = self.napcat_dir / name
                    if p.exists():
                        launcher = p
                        break
                if not launcher:
                    logger.error(f"Windows launcher not found in {self.napcat_dir}")
                    return

                cmd = ["cmd", "/c", launcher.name]
                if qq_number:
                    cmd += ["-q", qq_number]
                    logger.info(f"Quick login: QQ={qq_number}")
                else:
                    logger.info("No QQ number configured, will show QR code")

                cwd = str(self.napcat_dir)
                creationflags = CREATE_NO_WINDOW
                logger.info(f"Starting NapCat (Windows): {' '.join(cmd)}")

            else:
                linux_dir = self._find_linux_napcat_install()
                if not linux_dir:
                    logger.warning("NapCat not installed, trying auto install...")
                    await self.install_linux_napcat()
                    linux_dir = self._find_linux_napcat_install()
                    if not linux_dir:
                        logger.error(
                            "NapCat not installed. Run manually:\n"
                            f"  sudo bash -c 'curl -o /tmp/napcat.sh "
                            f"{NAPCAT_LINUX_INSTALL_URL} && bash /tmp/napcat.sh'"
                        )
                        return

                napcat_bin = linux_dir / "NapCat"
                if not napcat_bin.exists():
                    logger.error(f"NapCat binary not found: {napcat_bin}")
                    return

                try:
                    napcat_bin.chmod(0o755)
                except Exception:
                    pass

                if qq_number:
                    cmd = ["xvfb-run", "-a", str(napcat_bin), "-q", qq_number]
                    logger.info(f"Quick login: QQ={qq_number}")
                else:
                    cmd = ["xvfb-run", "-a", str(napcat_bin)]
                    logger.info("No QQ number configured, will show QR code")

                cwd = str(linux_dir)
                creationflags = 0
                logger.info(f"Starting NapCat (Linux): {' '.join(cmd)}")

            self._auto_link_done = False
            self._qq_logged_in = False

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    creationflags=creationflags,
                )
            except Exception as e:
                logger.error(f"Failed to start NapCat: {e}")
                return

            self._log_task = asyncio.create_task(self._read_logs())
            await asyncio.sleep(2)
            self._refresh_launched_pids()
            logger.info(f"New pids this run: {self._launched_pids}")

    async def stop(self):
        async with self._lock:
            if self.process and self.process.returncode is None:
                logger.info("Stopping NapCat...")
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
            logger.info("NapCat stopped")

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
        webui_url_pattern = re.compile(
            r"WebUi\s+User\s+Panel\s+Url:\s*(https?://\S+)", re.IGNORECASE
        )
        token_pattern = re.compile(
            r"(?:token|access[_-]?token|WebUi\s*Token|WebUI\s*Token)[=:\s]+([A-Za-z0-9\-_]+)",
            re.IGNORECASE,
        )
        login_ok_pattern = re.compile(
            r"(Login Success|login success|已登录|登录成功|快速登录成功)", re.IGNORECASE
        )

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
                continue

            if not self.napcat_webui_url:
                tm = token_pattern.search(text)
                if tm and tm.group(1) != self.napcat_token:
                    self.napcat_token = tm.group(1)
                    self.api.set_token(self.napcat_token)

            if not self._qq_logged_in and login_ok_pattern.search(text):
                self._qq_logged_in = True
                logger.info("QQ login detected")
                if self.plugin.config.get("auto_sync", True) and not self._auto_link_done:
                    self._auto_link_done = True
                    logger.info("Trigger auto sync")
                    asyncio.create_task(self._auto_sync_later())

        self.process = None

    async def _auto_sync_later(self):
        try:
            await asyncio.sleep(2)
            ok, msg = self.plugin.syncer.sync()
            logger.info(f"Auto sync result: {ok} {msg}")
        except Exception as e:
            logger.error(f"Auto sync exception: {e}")


# ============================================================
# Config syncer
# ============================================================

class ConfigSyncer:
    def __init__(self, plugin: "NapCatGoPlugin"):
        self.plugin = plugin
        self.context = plugin.context
        if hasattr(self.context, "plugin_dir"):
            self.plugin_dir = Path(self.context.plugin_dir)
        else:
            self.plugin_dir = Path(__file__).parent

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
            p = Path(manual)
            logger.info(f"Using manual AstrBot config path: {p}")
            return p

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
        candidates.append(Path("data/cmd_config.json"))
        candidates.append(Path("../data/cmd_config.json"))
        candidates.append(Path("../../data/cmd_config.json"))

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
                logger.info(f"Auto found AstrBot config: {ap}")
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
            return {
                "id": p.get("id", ""),
                "host": host,
                "port": int(port),
                "path": p.get("ws_reverse_path", "/ws/") or "/ws/",
                "token": str(token),
            }
        return None

    def _find_napcat_config_dir(self) -> Optional[Path]:
        # Priority 1: manual
        manual = str(self.plugin.config.get("napcat_config_dir", "") or "").strip()
        if manual:
            p = Path(manual)
            if p.exists() and p.is_dir():
                logger.info(f"[1/3 manual] Using: {p}")
                return p
            logger.warning(f"[1/3 manual] Path not exists: {p}")
            return p

        # Priority 2: plugin dir
        plugin_candidate = self.plugin_dir / "napcat" / "config"
        if plugin_candidate.exists() and plugin_candidate.is_dir():
            if list(plugin_candidate.glob("onebot11*.json")):
                logger.info(f"[2/3 plugin dir] Using: {plugin_candidate}")
                return plugin_candidate
            logger.info(f"[2/3 plugin dir] No onebot11*.json in {plugin_candidate}")
        else:
            logger.info(f"[2/3 plugin dir] {plugin_candidate} not exists")

        # Priority 3: system default
        system = platform.system()
        logger.info(f"[3/3 system default] System: {system}")

        home = Path.home()
        candidates: List[Path] = []

        if system == "Windows":
            candidates += [
                home / "NapCat" / "config",
                home / "Documents" / "NapCat" / "config",
                Path("C:/NapCat/config"),
                Path("D:/NapCat/config"),
                Path("C:/NapNeko/NapCat/config"),
                Path("D:/NapNeko/NapCat/config"),
            ]
        elif system == "Darwin":
            candidates += [
                home / "NapCat" / "config",
                home / "Applications" / "NapCat" / "config",
                home / "Library" / "Application Support" / "NapCat" / "config",
                Path("/Applications/NapCat/config"),
                Path("/usr/local/napcat/config"),
            ]
        else:
            candidates += [
                Path("/usr/local/napcat/config"),
                Path("/opt/napcat/config"),
                Path("/root/napcat/config"),
                home / "napcat" / "config",
                home / "NapCat" / "config",
            ]

        env_dir = os.environ.get("NAPCAT_CONFIG_DIR")
        if env_dir:
            candidates.insert(0, Path(env_dir))
            logger.info(f"[3/3 system default] env NAPCAT_CONFIG_DIR={env_dir}")

        for c in candidates:
            try:
                if c.exists() and c.is_dir() and list(c.glob("onebot11*.json")):
                    logger.info(f"[3/3 system default] Found: {c}")
                    return c
            except Exception:
                continue

        logger.warning("NapCat config dir not found. Please set it manually.")
        return None

    def _find_onebot11_file(self) -> Optional[Path]:
        if not self.napcat_config_dir:
            return None
        if not self.napcat_config_dir.exists():
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
                logger.warning(f"{target} not found, using latest onebot11 file")
            return max(cs, key=lambda p: p.stat().st_mtime)
        except Exception as e:
            logger.error(f"Find onebot11 file failed: {e}")
            return None

    def scan_candidates(self) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen = set()
        candidates: List[Path] = [self.plugin_dir / "napcat" / "config"]

        home = Path.home()
        system = platform.system()

        if system == "Windows":
            candidates += [
                home / "NapCat" / "config",
                home / "Documents" / "NapCat" / "config",
                Path("C:/NapCat/config"),
                Path("D:/NapCat/config"),
                Path("C:/NapNeko/NapCat/config"),
                Path("D:/NapNeko/NapCat/config"),
            ]
        elif system == "Darwin":
            candidates += [
                home / "NapCat" / "config",
                home / "Applications" / "NapCat" / "config",
                home / "Library" / "Application Support" / "NapCat" / "config",
                Path("/Applications/NapCat/config"),
                Path("/usr/local/napcat/config"),
            ]
        else:
            candidates += [
                Path("/usr/local/napcat/config"),
                Path("/opt/napcat/config"),
                Path("/root/napcat/config"),
                home / "napcat" / "config",
                home / "NapCat" / "config",
            ]

        env_dir = os.environ.get("NAPCAT_CONFIG_DIR")
        if env_dir:
            candidates.insert(0, Path(env_dir))

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
            return self._fail("AstrBot config file cmd_config.json not found")
        if not self.astrbot_config_path.exists():
            return self._fail(f"AstrBot config not exists: {self.astrbot_config_path}")
        if not self.astrbot_bot:
            return self._fail("No enabled OneBot v11 bot in AstrBot")
        if not self.napcat_config_dir:
            return self._fail("NapCat config dir not found, please set manually")
        if not self.napcat_config_dir.exists():
            return self._fail(f"NapCat config dir not exists: {self.napcat_config_dir}")
        if not self.napcat_config_file:
            return self._fail(f"No onebot11_*.json in {self.napcat_config_dir}")

        bot = self.astrbot_bot
        ws_url = f"ws://{bot['host']}:{bot['port']}{bot['path']}"

        try:
            with open(self.napcat_config_file, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
        except Exception as e:
            return self._fail(f"Read {self.napcat_config_file.name} failed: {e}")

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
            return self._fail(f"Write {self.napcat_config_file.name} failed: {e}")

        try:
            with open(self.napcat_config_file, "r", encoding="utf-8-sig") as f:
                verify = json.load(f)
            vc = verify.get("network", {}).get("websocketClients", [])
            ok = any(isinstance(c, dict) and c.get("url") == ws_url for c in vc)
        except Exception:
            ok = False

        if not ok:
            return self._fail(f"Verify after write failed: {self.napcat_config_file.name}")

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
        logger.warning(f"Sync failed: {msg}")
        return False, msg

    def status_dict(self) -> Dict[str, Any]:
        bot = self.astrbot_bot or {}
        return {
            "platform": platform.system(),
            "is_windows": IS_WINDOWS,
            "is_linux": IS_LINUX,
            "is_macos": IS_MACOS,
            "astrbot_config_path": str(self.astrbot_config_path) if self.astrbot_config_path else None,
            "astrbot_bot_found": self.astrbot_bot is not None,
            "astrbot_bot_id": bot.get("id"),
            "astrbot_bot_host": bot.get("host"),
            "astrbot_bot_port": bot.get("port"),
            "astrbot_bot_token": bot.get("token", ""),
            "napcat_config_dir": str(self.napcat_config_dir) if self.napcat_config_dir else None,
            "napcat_config_file": str(self.napcat_config_file) if self.napcat_config_file else None,
            "last_sync_ok": self.last_sync_ok,
            "last_sync_msg": self.last_sync_msg,
            "last_sync_time": self.last_sync_time,
            "config": self.plugin.config,
        }


# ============================================================
# Plugin entry
# ============================================================

class NapCatGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.astrbot_config = config
        self.config = self._load_config()
        self.syncer = ConfigSyncer(self)
        self.manager = NapCatManager(self)

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

        @filter.command("napcat")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def napcat_cmd(event: AstrMessageEvent):
            yield event.plain_result(self._status_text())

        if self.config.get("auto_start", False):
            logger.info("auto_start=True, starting NapCat")
            asyncio.create_task(self.manager.start())
        elif self.config.get("auto_sync", True):
            logger.info("auto_sync=True, trying sync")
            try:
                ok, msg = self.syncer.sync()
                if ok:
                    logger.info(f"Auto sync ok: {msg}")
                else:
                    logger.warning(f"Auto sync failed: {msg}")
            except Exception as e:
                logger.error(f"Auto sync exception: {e}")

    async def api_status(self):
        try:
            self.syncer.refresh()
        except Exception as e:
            logger.warning(f"Status refresh failed: {e}")

        d = self.syncer.status_dict()
        d["running"] = self.manager.is_running()
        d["napcat_installed"] = self.manager.is_installed()
        d["qq_installed"] = is_qq_installed()
        d["napcat_webui_url"] = self.manager.napcat_webui_url
        d["napcat_token"] = self.manager.napcat_token
        d["napcat_port"] = self.manager.napcat_port
        d["download_state"] = dict(self.manager.download_state)

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
            except Exception as e:
                logger.debug(f"Query login info failed: {e}")

        d["qq_logged_in"] = qq_logged_in
        d["qq_user_id"] = qq_user_id
        d["qq_nickname"] = qq_nickname
        return json_response(d)

    async def api_sync(self):
        try:
            ok, msg = self.syncer.sync()
            return json_response({"ok": ok, "message": msg, "status": self.syncer.status_dict()})
        except Exception as e:
            logger.error(f"Sync exception: {e}")
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
            return json_response({
                "ok": True,
                "sync_ok": ok,
                "sync_msg": msg,
                "status": self.syncer.status_dict(),
            })
        except Exception as e:
            return json_response({
                "ok": True,
                "sync_ok": False,
                "sync_msg": f"Sync exception: {e}",
                "status": self.syncer.status_dict(),
            })

    async def api_start(self):
        await self.manager.start()
        return json_response({"ok": True})

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
            if ok:
                return json_response({"ok": True, "message": "NapCat installed"})
            return error_response(
                "Auto install failed (needs root). Run manually:\n"
                f"sudo bash -c 'curl -o /tmp/napcat.sh "
                f"{NAPCAT_LINUX_INSTALL_URL} && bash /tmp/napcat.sh'"
            )
        except Exception as e:
            return error_response(f"Install failed: {e}")

    async def api_get_logs(self):
        since = request.query.get("since", 0, type=int)
        lines = list(self.manager.log_lines)
        return json_response({"lines": lines[since:], "total": len(lines)})

    async def api_open_webui(self):
        url = self.manager.napcat_webui_url
        if not url:
            return error_response("WebUI URL not available, start NapCat first")
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

    def _load_config(self) -> Dict[str, Any]:
        if hasattr(self.context, "get_data_dir"):
            data_dir = Path(self.context.get_data_dir())
        else:
            data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = data_dir / f"{PLUGIN_NAME}_config.json"

        config = DEFAULT_CONFIG.copy()

        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as f:
                    user = json.load(f)
                for k in DEFAULT_CONFIG.keys():
                    if k in user:
                        config[k] = user[k]
            except Exception as e:
                logger.error(f"Read local config failed: {e}")

        try:
            if self.astrbot_config is not None and hasattr(self.astrbot_config, "get"):
                for k in DEFAULT_CONFIG.keys():
                    v = self.astrbot_config.get(k)
                    if v is None:
                        continue
                    if k in ("auto_sync", "auto_start"):
                        config[k] = bool(v)
                    elif k == "napcat_port":
                        try:
                            config[k] = int(v)
                        except (TypeError, ValueError):
                            pass
                    else:
                        config[k] = str(v or "").strip()
        except Exception as e:
            logger.warning(f"Read AstrBot config failed: {e}")

        if not self.config_path.exists():
            self._save_config(config)

        return config

    def _save_config(self, config: Dict[str, Any] = None):
        if config is None:
            config = self.config
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save config failed: {e}")

    def _status_text(self) -> str:
        s = self.syncer.status_dict()
        lines = ["NapCat_Go Status", "-" * 20, f"Platform: {s['platform']}"]
        lines.append(f"NapCat process: {'running' if self.manager.is_running() else 'stopped'}")
        lines.append(f"QQ login: {'yes' if self.manager._qq_logged_in else 'no'}")
        if s["astrbot_bot_found"]:
            lines.append(f"AstrBot bot: {s['astrbot_bot_id']}")
            lines.append(f"Reverse WS: ws://{s['astrbot_bot_host']}:{s['astrbot_bot_port']}/ws/")
        else:
            lines.append("AstrBot bot: not found")
        lines.append(f"NapCat config dir: {s['napcat_config_dir'] or 'not found'}")
        lines.append(f"Last sync: {s['last_sync_time'] or 'never'}")
        lines.append(f"Status: {'OK' if s['last_sync_ok'] else 'FAIL'} {s['last_sync_msg']}")
        return "\n".join(lines)

    async def terminate(self):
        logger.info("NapCat_Go plugin unloading...")
        try:
            await self.manager.stop()
        except Exception as e:
            logger.error(f"Stop NapCat failed: {e}")
            try:
                self.manager.cleanup()
            except Exception:
                pass
        logger.info("NapCat_Go plugin unloaded")