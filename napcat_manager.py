import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, List

import aiohttp

from astrbot.api import logger

from .utils import (
    IS_WINDOWS, IS_LINUX,
    DEFAULT_NAPCAT_JSON, DEFAULT_NAPCAT_ACCOUNT_JSON,
    DEFAULT_NAPCAT_PROTOCOL_JSON, DEFAULT_WEBUI_JSON,
    DEFAULT_ONEBOT11_TEMPLATE,
    DEFAULT_CONFIG,
    REMNANT_PATTERN, REMNANT_SCAN_RE, QQ_LOGIN_ID_RE,
    CREATE_NO_WINDOW,
    resolve_persistent_dir, make_ssl_context,
    list_pids_by_name, kill_pids,
    build_docker_run_args,
    detect_docker_binary,
    get_linux_qq_version, get_linux_qq_build_code,
    is_linux_qq_version_compatible,
    NAPCAT_SUPPORTED_QQ_MIN, NAPCAT_SUPPORTED_QQ_MAX,
)
from .download import DownloadMixin


class NapCatManager(DownloadMixin):
    def __init__(self, plugin, deploy_mode: str = "auto"):
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(self.context.plugin_dir) if hasattr(self.context, "plugin_dir") else Path(__file__).parent
        self.persistent_dir = resolve_persistent_dir(self.context)
        self.napcat_dir = self.persistent_dir / "napcat"
        self.legacy_napcat_dir = self.plugin_dir / "napcat"
        self.napcat_dir.mkdir(parents=True, exist_ok=True)

        self.deploy_mode = deploy_mode
        self.docker_data_dir = self.persistent_dir / "docker-data"
        self.docker_config_dir = self.docker_data_dir / "config"
        self.docker_qq_dir = self.docker_data_dir / "QQ"

        self.process: Optional[asyncio.subprocess.Process] = None
        self.napcat_token: str = ""
        self.napcat_webui_url: str = ""
        self.napcat_port: int = DEFAULT_CONFIG["napcat_port"]
        self.napcat_qq_number: str = ""    # 从日志里抓到的 QQ 号
        self._log_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._downloading = False
        self._qq_logged_in = False
        self._auto_link_done = False
        self._pre_existing_qq_pids: Set[int] = set()
        self._launched_pids: Set[int] = set()
        self._launch_fake_bin: Optional[Path] = None

        # NapCatAPI 由外部注入（napcat 插件目录下的 pulid_api.py）
        try:
            from .pulid_api import NapCatAPI
            self.api = NapCatAPI(host="127.0.0.1", port=self.napcat_port, token="")
        except Exception:
            self.api = None
        self.log_lines: deque = deque(maxlen=500)

        self.download_state: Dict[str, Any] = {
            "downloading": False, "percent": 0, "downloaded_mb": 0.0,
            "total_mb": 0.0, "speed_kbps": 0.0, "current_mirror": "",
            "current_index": 0, "total_mirrors": 0, "phase": "idle", "message": "",
        }

        self._migrate_legacy()

    def _notice(self, msg: str, level: str = "warning"):
        self.log_lines.append(msg)
        try:
            if level == "warning":
                logger.warning(f"[NapCat_Go] {msg}")
            elif level == "error":
                logger.error(f"[NapCat_Go] {msg}")
            else:
                logger.info(f"[NapCat_Go] {msg}")
        except Exception:
            pass

    def _is_new_style_install(self, d: Path) -> bool:
        try:
            return (d / "libnapcat_launcher.so").exists() and (d / "launcher.sh").exists()
        except Exception:
            return False

    def _find_new_style_root(self) -> Optional[Path]:
        if self._is_new_style_install(self.napcat_dir):
            return self.napcat_dir
        for sub in [self.napcat_dir / "napcat", self.napcat_dir / "NapCat"]:
            if self._is_new_style_install(sub):
                return sub
        return None

    def _get_config_dir(self) -> Path:
        candidates = [
            self.napcat_dir / "config",
            self.napcat_dir / "napcat" / "config",
            self.napcat_dir / "NapCat" / "config",
        ]
        for c in candidates:
            try:
                if c.exists() and c.is_dir():
                    if (c / "webui.json").exists() or (c / "napcat.json").exists():
                        return c
            except Exception:
                continue
        for c in candidates:
            try:
                if c.exists() and c.is_dir():
                    return c
            except Exception:
                continue
        root = self._find_new_style_root()
        if root:
            d = root / "config"
            d.mkdir(parents=True, exist_ok=True)
            return d
        d = self.napcat_dir / "config"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_base_configs(self, qq_number: str = ""):
        cfg_dir = self._get_config_dir()
        if not qq_number:
            qq_number = str(self.plugin.config.get("qq_number", "") or "").strip()
        if not qq_number:
            qq_number = self.napcat_qq_number

        # 1. napcat.json
        try:
            p = cfg_dir / "napcat.json"
            data = dict(DEFAULT_NAPCAT_JSON)
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        old = json.load(f)
                    if isinstance(old, dict):
                        data.update(old)
                except Exception:
                    pass
            data["packetBackend"] = "auto"
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.log_lines.append(f"[config] 写入 {p.name}")
        except Exception as e:
            self.log_lines.append(f"[config] ⚠ 写入 napcat.json 失败: {e}")

        if qq_number:
            try:
                p = cfg_dir / f"napcat_{qq_number}.json"
                data = dict(DEFAULT_NAPCAT_ACCOUNT_JSON)
                if p.exists():
                    try:
                        with open(p, "r", encoding="utf-8-sig") as f:
                            old = json.load(f)
                        if isinstance(old, dict):
                            data.update(old)
                    except Exception:
                        pass
                data["packetBackend"] = "auto"
                data.setdefault("autoTimeSync", True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self.log_lines.append(f"[config] 写入 {p.name}")
            except Exception as e:
                self.log_lines.append(f"[config] ⚠ 写入 napcat_{qq_number}.json 失败: {e}")

            try:
                p = cfg_dir / f"napcat_protocol_{qq_number}.json"
                data = dict(DEFAULT_NAPCAT_PROTOCOL_JSON)
                if p.exists():
                    try:
                        with open(p, "r", encoding="utf-8-sig") as f:
                            old = json.load(f)
                        if isinstance(old, dict):
                            data.update(old)
                    except Exception:
                        pass
                data["enable"] = False
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                self.log_lines.append(f"[config] 写入 {p.name}")
            except Exception as e:
                self.log_lines.append(f"[config] ⚠ 写入 napcat_protocol_{qq_number}.json 失败: {e}")

            try:
                p = cfg_dir / f"onebot11_{qq_number}.json"
                if not p.exists():
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(DEFAULT_ONEBOT11_TEMPLATE, f, indent=2, ensure_ascii=False)
                    self.log_lines.append(f"[config] 创建 {p.name}")
                else:
                    self.log_lines.append(f"[config] 已存在 {p.name}")
            except Exception as e:
                self.log_lines.append(f"[config] ⚠ 创建 onebot11_{qq_number}.json 失败: {e}")

        try:
            p = cfg_dir / "webui.json"
            data = dict(DEFAULT_WEBUI_JSON)
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        old = json.load(f)
                    if isinstance(old, dict):
                        data.update(old)
                except Exception:
                    pass
            if qq_number:
                data["autoLoginAccount"] = qq_number
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            self.log_lines.append(f"[config] 写入 {p.name}")
        except Exception as e:
            self.log_lines.append(f"[config] ⚠ 写入 webui.json 失败: {e}")

    def _find_qq_binary(self, root: Path) -> Tuple[Optional[str], Optional[str]]:
        cand_which = shutil.which("qq")
        if cand_which:
            try:
                real = os.path.realpath(cand_which)
                if os.path.exists(real) and os.access(real, os.X_OK):
                    self.log_lines.append(f"[start] which qq -> {real}")
                    return real, str(Path(real).parent)
            except Exception as e:
                self.log_lines.append(f"[start] ⚠ 检查 which qq 失败: {e}")

        for cand in [
            root / "QQ" / "qq", root / "qq", Path("/opt/QQ/qq"),
            Path("/usr/local/bin/qq"), Path("/usr/bin/qq"), Path("/usr/lib/qq/qq"),
        ]:
            try:
                if not cand.exists():
                    continue
                real = os.path.realpath(cand)
                if os.path.exists(real) and os.access(real, os.X_OK):
                    self.log_lines.append(f"[start] 找到 qq: {real}")
                    return real, str(Path(real).parent)
            except Exception:
                continue
        return None, None

    def _refresh_launched_pids(self):
        try:
            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            cur = list_pids_by_name(qq_name)
            self._launched_pids.update(cur - self._pre_existing_qq_pids)
            if IS_WINDOWS:
                self._launched_pids.update(list_pids_by_name("NapCatWinBootMain.exe"))
            else:
                self._launched_pids.update(list_pids_by_name("NapCat"))
                self._launched_pids.update(list_pids_by_name("Xvfb"))
        except Exception:
            pass

    def _cleanup_processes(self):
        self._refresh_launched_pids()
        if self._launched_pids:
            kill_pids(self._launched_pids)
            self._launched_pids.clear()

    def _sudo_pkill(self, pattern: str, sig: str = "KILL") -> None:
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if IS_WINDOWS or not pw or not shutil.which("sudo"):
            return
        try:
            subprocess.run(
                ["sudo", "-S", "pkill", f"-{sig}", "-f", pattern],
                input=(pw + "\n").encode(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except Exception:
            pass

    def _sudo_kill_pids(self, pids: List[int], sig: str = "KILL") -> None:
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if IS_WINDOWS or not pw or not shutil.which("sudo") or not pids:
            return
        try:
            subprocess.run(
                ["sudo", "-S", "kill", f"-{sig}", *[str(p) for p in pids]],
                input=(pw + "\n").encode(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except Exception:
            pass

    def _list_remnant_pids(self) -> List[Tuple[int, int, str, str]]:
        if IS_WINDOWS:
            return []
        try:
            r = subprocess.run(
                ["ps", "-eo", "pid,ppid,stat,args"],
                capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                return []
        except Exception:
            return []

        me = os.getpid()
        out: List[Tuple[int, int, str, str]] = []
        for line in r.stdout.splitlines()[1:]:
            line = line.rstrip()
            if not line.strip():
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            stat = parts[2]
            args = parts[3]
            if pid == me:
                continue
            if "grep" in args or args.startswith("ps ") or " ps -eo" in args:
                continue
            if REMNANT_SCAN_RE.search(args):
                out.append((pid, ppid, stat, args))
        return out

    def _kill_root_remnants(self) -> None:
        if IS_WINDOWS:
            return
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw or not shutil.which("sudo"):
            return

        self.log_lines.append("[stop] 开始清理所有 NapCat/QQ/Xvfb 残留进程 ...")

        for attempt in range(1, 4):
            self._sudo_pkill(REMNANT_PATTERN, "KILL")
            time.sleep(1.0)

            rems = self._list_remnant_pids()
            if not rems:
                self.log_lines.append(f"[stop] ✔ 残留进程已清理干净（第 {attempt} 轮）")
                return

            alive = [(p, pp, st, ar) for (p, pp, st, ar) in rems if "Z" not in st]
            zombies = [(p, pp, st, ar) for (p, pp, st, ar) in rems if "Z" in st]

            self.log_lines.append(
                f"[stop] ⚠ 第 {attempt}/3 轮仍有残留："
                f"活进程 {[p for p, _, _, _ in alive]}，"
                f"僵尸 {[p for p, _, _, _ in zombies]}"
            )

            if alive:
                self._sudo_kill_pids([p for p, _, _, _ in alive], "KILL")
            for _pid, ppid, _stat, _args in zombies:
                if ppid and ppid > 1:
                    self._sudo_kill_pids([ppid], "KILL")
            time.sleep(1.0)

        rems = self._list_remnant_pids()
        if not rems:
            self.log_lines.append("[stop] ✔ 残留进程已清理干净")
            return

        alive = [(p, pp, st, ar) for (p, pp, st, ar) in rems if "Z" not in st]
        zombies = [(p, pp, st, ar) for (p, pp, st, ar) in rems if "Z" in st]
        if zombies and not alive:
            self.log_lines.append(
                f"[stop] ⚠ 仅剩僵尸进程 {[p for p, _, _, _ in zombies]}，"
                f"kill -9 无效；等父进程回收或重启 AstrBot 即可"
            )
        else:
            self.log_lines.append(
                f"[stop] ✘ 残留进程清理失败："
                f"活进程 {[p for p, _, _, _ in alive]}，"
                f"僵尸 {[p for p, _, _, _ in zombies]}"
            )

    def _reset_download_state(self):
        self.download_state.update({
            "downloading": False, "percent": 0, "downloaded_mb": 0.0,
            "total_mb": 0.0, "speed_kbps": 0.0, "current_mirror": "",
            "current_index": 0, "total_mirrors": 0, "phase": "idle", "message": "",
        })

    def _build_fake_sudo(self, tag: str, env: Dict[str, str]) -> Optional[Path]:
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw:
            return None
        real_sudo = shutil.which("sudo") or "/usr/bin/sudo"
        try:
            fake_bin = self.napcat_dir / f".ncgo_bin_{tag}"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.chmod(fake_bin, 0o700)
            w = fake_bin / "sudo"
            safe = pw.replace("'", "'\\''")
            w.write_text("#!/bin/sh\n" f"printf '%s\\n' '{safe}' | '{real_sudo}' -S \"$@\"\n")
            os.chmod(w, 0o700)
            env["PATH"] = f"{fake_bin}:{env.get('PATH', os.environ.get('PATH', ''))}"
            return fake_bin
        except Exception as e:
            self.log_lines.append(f"[deps] ⚠ 创建 sudo 包装脚本({tag})失败: {e}")
            return None

    def _migrate_legacy(self):
        if not self.legacy_napcat_dir.exists():
            return
        try:
            new_has_content = any(self.napcat_dir.iterdir())
        except Exception:
            new_has_content = False
        if new_has_content:
            return
        try:
            shutil.copytree(self.legacy_napcat_dir, self.napcat_dir, dirs_exist_ok=True)
            self.log_lines.append(f"[migrate] {self.legacy_napcat_dir} -> {self.napcat_dir}")
        except Exception as e:
            logger.error(f"[NapCat_Go] migration failed: {e}")

    # ---------- Docker 命令 ----------

    async def _docker_cmd(self, *args: str, timeout: int = 120) -> Tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.persistent_dir),
            )
        except FileNotFoundError:
            return -1, "docker 未安装"
        except Exception as e:
            return -1, str(e)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "docker 命令超时"
        out = stdout.decode("utf-8", errors="ignore") if stdout else ""
        return proc.returncode or 0, out

    async def docker_available(self) -> bool:
        if not detect_docker_binary():
            return False
        code, _ = await self._docker_cmd("info", timeout=15)
        return code == 0

    async def docker_image_present(self, ref: str) -> bool:
        code, _ = await self._docker_cmd("image", "inspect", ref, timeout=20)
        return code == 0

    async def docker_container_present(self, name: str) -> bool:
        code, out = await self._docker_cmd("ps", "-a", "--format", "{{.Names}}", timeout=15)
        if code != 0:
            return False
        return name in [ln.strip() for ln in out.splitlines() if ln.strip()]

    async def docker_container_running(self, name: str) -> bool:
        code, out = await self._docker_cmd("ps", "--format", "{{.Names}}", timeout=15)
        if code != 0:
            return False
        return name in [ln.strip() for ln in out.splitlines() if ln.strip()]

    def _docker_sync_names(self, *args: str) -> Set[str]:
        try:
            r = subprocess.run(["docker", *args], capture_output=True, text=True,
                               timeout=8, stdin=subprocess.DEVNULL)
        except Exception:
            return set()
        if r.returncode != 0:
            return set()
        return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}

    def _docker_sync_check(self, *args: str) -> bool:
        try:
            r = subprocess.run(["docker", *args], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=8)
            return r.returncode == 0
        except Exception:
            return False

    def docker_image_ref(self) -> str:
        img = str(self.plugin.config.get("docker_image", DEFAULT_CONFIG.get("docker_image", "mlikiowa/napcat-docker"))
                  or "mlikiowa/napcat-docker").strip()
        tag = str(self.plugin.config.get("docker_tag", "latest")
                  or "latest").strip() or "latest"
        return f"{img}:{tag}" if ":" not in img else img

    def docker_container_name(self) -> str:
        return str(self.plugin.config.get("docker_container_name", "napcat-go")
                   or "napcat-go").strip() or "napcat-go"

    # ---------- Windows 入口 ----------

    def _find_entry(self) -> Optional[str]:
        for name in ["launcher-win10-user.bat", "launcher-win10.bat",
                     "launcher.bat", "launcher-user.bat",
                     "launcher-user.sh", "launcher.sh",
                     "napcat.mjs", "index.js"]:
            if (self.napcat_dir / name).exists():
                return name
        return None

    # ---------- 启动 / 停止 ----------

    async def _start_native(self) -> bool:
        async with self._lock:
            if self.process and self.process.returncode is None:
                return True

            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            self._pre_existing_qq_pids = list_pids_by_name(qq_name)
            self._launched_pids.clear()
            qq_number = str(self.plugin.config.get("qq_number", "") or "").strip()

            env_extra: Dict[str, str] = {}
            pw = ""

            if IS_WINDOWS:
                if not self._find_entry():
                    if not await self.download_napcat():
                        self.log_lines.append("[start] ✘ 下载失败")
                        return False
                    if not self._find_entry():
                        self.log_lines.append("[start] 下载完成但入口文件仍缺失")
                        return False

                from .utils import is_qq_installed
                if not is_qq_installed():
                    self.log_lines.append("[start] 未检测到 QQ 客户端，请先安装 QQNT")
                    return False

                launcher = None
                for name in ["launcher-win10-user.bat", "launcher-win10.bat",
                             "launcher.bat", "launcher-user.bat"]:
                    p = self.napcat_dir / name
                    if p.exists():
                        launcher = p
                        break
                if not launcher:
                    self.log_lines.append(f"[start] 未找到启动器: {self.napcat_dir}")
                    return False
                cmd = ["cmd", "/c", launcher.name]
                if qq_number:
                    cmd += ["-q", qq_number]
                cwd = str(self.napcat_dir)
                creationflags = CREATE_NO_WINDOW
                self.log_lines.append(f"[start] 启动 NapCat: {launcher.name}")
            else:
                root = self._find_new_style_root()

                if not root:
                    self.log_lines.append("[start] 未检测到新方案，开始安装 NapCat ...")
                    if not await self.install_linux_napcat():
                        return False
                    root = self._find_new_style_root()
                    if not root:
                        self.log_lines.append("[start] ✘ 安装后仍未找到")
                        return False

                self.log_lines.append(f"[start] ⚡ 使用新方案目录: {root}")

                pw = (self.plugin.config.get("sudo_password", "") or "").strip()
                if not pw or not shutil.which("sudo"):
                    self.log_lines.append("[start] ✘ 未配置 sudo 密码或系统无 sudo")
                    return False

                qq_ok = await self.ensure_linux_qq_compatible()
                if not qq_ok:
                    self.log_lines.append("[start] ✘ LinuxQQ 版本不兼容或安装失败，无法继续")
                    self.log_lines.append(f"[start] 兼容 build 范围: [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}]")
                    return False
                self.log_lines.append(
                    f"[start] ✔ LinuxQQ 就绪: {get_linux_qq_version()} "
                    f"(build {get_linux_qq_build_code()})"
                )

                if not shutil.which("Xvfb"):
                    self.log_lines.append("[start] ⚠ 未检测到 Xvfb")

                qq_real, qq_dir = self._find_qq_binary(root)
                if not qq_dir:
                    self.log_lines.append("[start] ✘ 找不到可用的 qq 命令")
                    return False

                base_path = os.environ.get("PATH", "") or \
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                path_parts = base_path.split(":")
                if qq_dir not in path_parts:
                    base_path = f"{qq_dir}:{base_path}"

                user_home = os.environ.get("HOME") or ""
                if not user_home:
                    try:
                        import pwd
                        user_home = pwd.getpwuid(os.getuid()).pw_dir
                    except Exception:
                        user_home = str(Path.home())

                user_name = os.environ.get("USER") or os.environ.get("LOGNAME") or ""

                self._ensure_base_configs(qq_number)

                launch_path = base_path
                env_extra["PATH"] = launch_path
                env_extra["HOME"] = user_home
                if user_name:
                    env_extra["USER"] = user_name
                env_extra["NAPCAT_BOOTMAIN"] = str(root)
                env_extra["DISPLAY"] = ":1"

                real_sudo = shutil.which("sudo") or "sudo"
                cmd = [real_sudo, "-S", "-E", "env",
                       f"PATH={launch_path}", f"HOME={user_home}"]
                if user_name:
                    cmd.append(f"USER={user_name}")
                cmd += [f"NAPCAT_BOOTMAIN={root}", f"DISPLAY=:1", "bash", "launcher.sh"]
                cwd = str(root)
                creationflags = 0

                try:
                    subprocess.run(["pkill", "-f", "Xvfb :1"], timeout=5,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

                self.log_lines.append(f"[start] qq: {qq_real}")
                self.log_lines.append("[start] 启动命令: sudo -E env ... bash launcher.sh")

            self._auto_link_done = False
            self._qq_logged_in = False
            launch_env = {**os.environ}
            launch_env.update(env_extra)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd, creationflags=creationflags, env=launch_env,
                )
                if pw:
                    try:
                        self.process.stdin.write((pw + "\n").encode())
                        await self.process.stdin.drain()
                        self.process.stdin.close()
                    except Exception:
                        pass
            except Exception as e:
                self.log_lines.append(f"[start] 启动失败: {e}")
                return False

            self._log_task = asyncio.create_task(self._read_logs())
            await asyncio.sleep(3)
            self._refresh_launched_pids()
            return True

    async def _start_docker(self) -> bool:
        if not await self.docker_available():
            self._notice("[docker] ✘ docker 不可用", "error")
            return False

        ref = self.docker_image_ref()
        name = self.docker_container_name()

        if self._log_task and not self._log_task.done():
            if await self.docker_container_running(name):
                return True

        if not await self.docker_image_present(ref):
            self.log_lines.append(f"[docker] 拉取镜像 {ref} ...")
            self.download_state["downloading"] = True
            self.download_state["phase"] = "downloading"
            self.download_state["message"] = "正在拉取 Docker 镜像"
            self.download_state["current_mirror"] = ref
            code, out = await self._docker_cmd("pull", ref, timeout=900)
            self.download_state["downloading"] = False
            if code != 0:
                self.log_lines.append(f"[docker] ✘ 拉取失败: {out[-200:]}")
                self.download_state["phase"] = "failed"
                return False
            self.download_state["phase"] = "done"
            self.download_state["percent"] = 100
            self.download_state["message"] = "镜像就绪"

        self.docker_config_dir.mkdir(parents=True, exist_ok=True)
        self.docker_qq_dir.mkdir(parents=True, exist_ok=True)

        if not await self.docker_container_present(name):
            try:
                uid = os.getuid()
                gid = os.getgid()
            except AttributeError:
                uid, gid = 0, 0
            port = int(self.plugin.config.get("napcat_port", 6099) or 6099)
            args = build_docker_run_args(
                container=name, image_ref=ref,
                host_config_dir=str(self.docker_config_dir),
                host_qq_dir=str(self.docker_qq_dir),
                uid=uid, gid=gid, host_webui_port=port,
            )
            code, out = await self._docker_cmd(*args, timeout=180)
            if code != 0:
                self.log_lines.append(f"[docker] ✘ 创建容器失败: {out[-300:]}")
                return False
        else:
            running = await self.docker_container_running(name)
            if not running:
                code, out = await self._docker_cmd("start", name, timeout=90)
                if code != 0:
                    self.log_lines.append(f"[docker] ✘ 启动容器失败: {out[-200:]}")
                    return False

        if self._log_task and not self._log_task.done():
            self._log_task.cancel()
            try:
                await self._log_task
            except (asyncio.CancelledError, Exception):
                pass

        self._auto_link_done = False
        self._qq_logged_in = False
        self._log_task = asyncio.create_task(self._read_docker_logs(name))
        await asyncio.sleep(2)
        return True

    async def _read_docker_logs(self, container: str):
        webui_url_pattern = re.compile(r"WebUi\s+User\s+Panel\s+Url:\s*(https?://\S+)", re.IGNORECASE)
        token_pattern = re.compile(r"(?:token|access[_-]?token|WebUi\s*Token|WebUI\s*Token)[=:\s]+([A-Za-z0-9\-_]+)", re.IGNORECASE)
        login_ok_pattern = re.compile(r"(Login Success|login success|登录成功|快速登录成功)", re.IGNORECASE)

        def _usable(url: str) -> bool:
            return "[::]" not in url and "0.0.0.0" not in url

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "-f", "--tail", "200", container,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            self.log_lines.append(f"[docker] 日志流启动失败: {e}")
            return
        self.process = proc
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.CancelledError, RuntimeError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            if "接收 <-" not in text and "发送 ->" not in text:
                self.log_lines.append(text)
            if not self.napcat_qq_number:
                m_qq = QQ_LOGIN_ID_RE.search(text)
                if m_qq:
                    self.napcat_qq_number = m_qq.group(1)
                    logger.info(f"[NapCat_Go] 识别到 QQ 号: {self.napcat_qq_number}")
            m = webui_url_pattern.search(text)
            if m:
                url = m.group(1).rstrip(".,;，。；")
                if not _usable(url):
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm and not self.napcat_token:
                        self.napcat_token = tm.group(1)
                        if self.api:
                            self.api.set_token(self.napcat_token)
                    continue
                if url != self.napcat_webui_url:
                    self.napcat_webui_url = url
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm:
                        self.napcat_token = tm.group(1)
                        if self.api:
                            self.api.set_token(self.napcat_token)
                continue
            if not self.napcat_webui_url:
                tm = token_pattern.search(text)
                if tm and tm.group(1) != self.napcat_token:
                    self.napcat_token = tm.group(1)
                    if self.api:
                        self.api.set_token(self.napcat_token)
            if not self._qq_logged_in and login_ok_pattern.search(text):
                self._qq_logged_in = True
                if self.plugin.config.get("auto_sync", True) and not self._auto_link_done:
                    self._auto_link_done = True
                    asyncio.create_task(self._auto_sync_later())
        self.process = None

    async def start(self) -> bool:
        if self.deploy_mode == "docker":
            return await self._start_docker()
        ok = await self._start_native()
        if ok:
            return True
        user_mode = str(self.plugin.config.get("deploy_mode", "auto")).strip().lower()
        if not IS_WINDOWS and user_mode == "auto" and detect_docker_binary():
            self._notice("[fallback] ⚠ 本地部署失败，回退到 Docker", "warning")
            self.plugin.config["deploy_mode"] = "docker"
            self.deploy_mode = "docker"
            self.plugin._save_config(self.plugin.config)
            return await self._start_docker()
        return False

    async def stop(self):
        if self.deploy_mode == "docker":
            if self._log_task and not self._log_task.done():
                self._log_task.cancel()
                try:
                    await self._log_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._log_task = None
            self.process = None
            name = self.docker_container_name()
            code, out = await self._docker_cmd("stop", name, timeout=90)
            self.log_lines.append(f"[docker] 停止容器: {name} ({'ok' if code == 0 else out[-120:]})")
            self.napcat_webui_url = ""
            self.napcat_token = ""
            self._qq_logged_in = False
            return

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
            self._kill_root_remnants()

            if self._launch_fake_bin and self._launch_fake_bin.exists():
                try:
                    shutil.rmtree(self._launch_fake_bin, ignore_errors=True)
                except Exception:
                    pass
                self._launch_fake_bin = None
            await asyncio.sleep(1.5)
            self.napcat_webui_url = ""
            self.napcat_token = ""
            self._qq_logged_in = False

    async def restart(self):
        if self.deploy_mode == "docker":
            name = self.docker_container_name()
            if await self.docker_container_running(name):
                await self._docker_cmd("restart", name, timeout=120)
            else:
                await self._start_docker()
            await asyncio.sleep(2)
            return
        await self.stop()
        await asyncio.sleep(1)
        await self.start()

    def is_running(self) -> bool:
        if self.deploy_mode == "docker":
            return self.docker_container_name() in self._docker_sync_names("ps")
        return self.process is not None and self.process.returncode is None

    def is_installed(self) -> bool:
        if self.deploy_mode == "docker":
            return self._docker_sync_check("image", "inspect", self.docker_image_ref())
        if IS_WINDOWS:
            return self._find_entry() is not None
        return self._find_new_style_root() is not None

    def cleanup(self):
        if self.deploy_mode == "docker":
            return
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
            if not self.napcat_qq_number:
                m_qq = QQ_LOGIN_ID_RE.search(text)
                if m_qq:
                    self.napcat_qq_number = m_qq.group(1)
                    logger.info(f"[NapCat_Go] 识别到 QQ 号: {self.napcat_qq_number}")
            m = webui_url_pattern.search(text)
            if m:
                url = m.group(1).rstrip(".,;，。；")
                if not _usable(url):
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm and not self.napcat_token:
                        self.napcat_token = tm.group(1)
                        if self.api:
                            self.api.set_token(self.napcat_token)
                    continue
                if url != self.napcat_webui_url:
                    self.napcat_webui_url = url
                    tm = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if tm:
                        self.napcat_token = tm.group(1)
                        if self.api:
                            self.api.set_token(self.napcat_token)
                    logger.info("NapCat WebUI URL detected")
                continue
            if not self.napcat_webui_url:
                tm = token_pattern.search(text)
                if tm and tm.group(1) != self.napcat_token:
                    self.napcat_token = tm.group(1)
                    if self.api:
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
