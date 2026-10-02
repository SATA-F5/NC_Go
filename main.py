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
import hashlib
import base64
import uuid
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

PLUGIN_CODE_VERSION = "2026-10-02-v70-github-qq-mirror"
PLUGIN_NAME = "pulid_napcat_go_to_astrbot"
CONFIG_VERSION = 4

#所有下载均使用官方或镜像链接
NAPCAT_RELEASE_BASE = "https://github.com/NapNeko/NapCatQQ/releases/download"
NAPCAT_INSTALL_SH_ORIGINAL = "https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/refs/heads/main/install.sh"
NAPCAT_INSTALL_SH_LEGACY = "https://raw.githubusercontent.com/NapNeko/napcat-linux-installer/main/install.sh"
DEFAULT_NAPCAT_VERSION = "v4.18.28"
DEFAULT_DOWNLOAD_MIRROR = "https://gh.zwy.one/"

NAPCAT_MIRROR_CANDIDATES = [
    "https://gh.zwy.one/", "https://raw.ihtw.moe/", "https://gh.llkk.cc/",
    "https://gh.xxooo.cf/", "https://ghfile.geekertao.top/", "https://ghproxy.cxkpro.top/",
    "https://git.yylx.win/", "https://gh.h233.eu.org/", "https://cdn.crashmc.com/",
    "https://githubproxy.cc/", "https://gh-proxy.com/", "https://ghproxy.net/",
    "https://ghfast.top/",
]

# ============ QQ 版本兼容性 ============
# NapCat PacketBackend 对 Linux QQ build 号的限制。
# 历史经验：
#   3.2.22-251203 -> build 251203 超上限（QQ 服务端也会提示版本过低）
#   3.2.29-260528 -> build 260528 落在旧范围 [28498, 36580] 内
#   3.2.32-52194  -> build 52194 超出旧范围上限
#   3.2.34-53644  -> build 53644 超出旧范围上限，但从 SATA-F5/NC_Go 分发的
#                    3.2.34 版本实际可用，因此这里放宽上限。
# 如某天 NapCat 对新版 QQ 又拒绝，把 MAX 调回 36580 即可恢复严格模式。
NAPCAT_SUPPORTED_QQ_MIN = 28498
NAPCAT_SUPPORTED_QQ_MAX = 999999
RECOMMENDED_LINUXQQ_VERSION = "3.2.34"

# LinuxQQ deb 下载源（按顺序尝试，GitHub 链接会自动套 NAPCAT_MIRROR_CANDIDATES 的全部加速前缀）
LINUXQQ_DEB_URLS = [
    # 主源：SATA-F5/NC_Go 仓库托管的 3.2.34 amd64 deb
    "https://github.com/SATA-F5/NC_Go/releases/download/data-v0.0.0/QQ_3.2.34_Linux_amd64.deb",
    # 兜底：Rodert/qq-versions 归档的 3.2.28 amd64 deb
    "https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260429/QQ_3.2.28_260429_amd64_01.deb",
]

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MACOS = platform.system() == "Darwin"

DEPLOY_MODES = {"auto", "windows", "docker", "native"}
DEFAULT_DEPLOY_MODE = "auto"
DEFAULT_DOCKER_IMAGE = "mlikiowa/napcat-docker"
DEFAULT_DOCKER_TAG = "latest"
DEFAULT_DOCKER_CONTAINER = "napcat-go"
DOCKER_NAPCAT_CONFIG_PATH = "/app/napcat/config"
DOCKER_QQ_DATA_PATH = "/app/.config/QQ"
DOCKER_WEBUI_PORT = 6099

DEFAULT_CONFIG = {
    "auto_sync": True,
    "auto_start": True,
    "napcat_port": 6099,
    "napcat_version": DEFAULT_NAPCAT_VERSION,
    "napcat_download_mirror": DEFAULT_DOWNLOAD_MIRROR,
    "napcat_config_dir": "",
    "astrbot_config_path": "",
    "qq_number": "",
    "deploy_mode": DEFAULT_DEPLOY_MODE,
    "docker_image": DEFAULT_DOCKER_IMAGE,
    "docker_tag": DEFAULT_DOCKER_TAG,
    "docker_container_name": DEFAULT_DOCKER_CONTAINER,
    "sudo_password": "",
}

CREATE_NO_WINDOW = 0x08000000

# 残留进程扫描/清理匹配模式（覆盖 native 模式下的 root 进程）
REMNANT_PATTERN = "napcat|NapCat|Xvfb|launcher.sh|/opt/QQ"
REMNANT_SCAN_RE = re.compile(r"napcat|NapCat|Xvfb|launcher\.sh|/opt/QQ", re.IGNORECASE)


def _read_os_release() -> Dict[str, str]:
    info: Dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip().strip('"').strip("'")
            if info:
                break
        except Exception:
            continue
    return info


def _detect_distro_family() -> str:
    osr = _read_os_release()
    ids = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).lower()
    if not ids.strip():
        if os.path.exists("/etc/arch-release") or shutil.which("pacman"):
            return "arch"
        if os.path.exists("/etc/debian_version") or shutil.which("apt-get"):
            return "debian"
        if os.path.exists("/etc/redhat-release") or shutil.which("dnf"):
            return "fedora"
        return "unknown"
    if "arch" in ids:
        return "arch"
    if "debian" in ids or "ubuntu" in ids:
        return "debian"
    if "fedora" in ids or "rhel" in ids or "centos" in ids:
        return "fedora"
    return "unknown"


def detect_docker_binary() -> Optional[str]:
    return shutil.which("docker")


def resolve_deploy_mode(mode: str, *, is_windows: bool = IS_WINDOWS,
                        docker_binary_available: bool = True) -> str:
    m = str(mode or DEFAULT_DEPLOY_MODE).strip().lower()
    if m not in DEPLOY_MODES:
        m = DEFAULT_DEPLOY_MODE
    if is_windows:
        return "windows"
    if m == "auto":
        return "docker" if docker_binary_available else "native"
    if m == "windows":
        return "native"
    return m


def build_docker_run_args(container, image_ref, host_config_dir, host_qq_dir,
                          uid, gid, host_webui_port, extra_publish="3001:3001"):
    return [
        "run", "-d", "--name", container, "--restart", "unless-stopped",
        "-e", f"NAPCAT_UID={uid}", "-e", f"NAPCAT_GID={gid}",
        "-p", f"{host_webui_port}:{DOCKER_WEBUI_PORT}", "-p", extra_publish,
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{host_config_dir}:{DOCKER_NAPCAT_CONFIG_PATH}",
        "-v", f"{host_qq_dir}:{DOCKER_QQ_DATA_PATH}",
        image_ref,
    ]


def reverse_ws_host(deploy_mode: str, astrbot_host: str) -> str:
    return "host.docker.internal" if deploy_mode == "docker" else astrbot_host


_ASCII_ART_CHARS = set("┌┐└┘─│█║═╔╗╚╝")
_INSTALL_NOISE_SUBSTR = (
    "下面是 NapCat", "Powered by", "接下来，您可以", "1. Docker", "2. 本地",
    "可视化安装", "Shell 安装", "您可以选择", "TUI-CLI", "使用 --help",
    "开始 Shell", "检查旧版本", "未检测到旧版本",
)
_INSTALL_KEEP_SUBSTR = (
    "失败", "错误", "警告", "error", "Error", "ERROR", "failed", "Failed",
    "无法", "No such", "not found", "未找到", "安装完成", "安装失败",
    "成功", "完成", "开始", "下载", "解压", "拷贝", "安装到", "安装目录",
    "✔", "✘", "launcher", "launcher.sh", "Xvfb", "sudo", "密码", "chown",
    "qq", "command not found", "文件已存在", "重命名",
)
_TS_INFO = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d\d:\d\d:\d\d\]\s*:")


def _filter_install_output(text: str) -> List[str]:
    out: List[str] = []
    prev = None
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*[mK]", "", raw).strip()
        if not line:
            continue
        if any(ch in _ASCII_ART_CHARS for ch in line):
            continue
        if line in ("--", "-" * 20) or re.match(r"^[-=━─_*]{10,}$", line):
            continue
        if _TS_INFO.match(line):
            if line != prev:
                out.append(line)
            prev = line
            continue
        if any(k in line for k in _INSTALL_KEEP_SUBSTR):
            if line != prev:
                out.append(line)
        prev = line
    return out


def resolve_persistent_dir(context: Any) -> Path:
    candidate = None
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
    if not candidate.is_absolute():
        try:
            candidate = (Path.cwd() / candidate).resolve()
        except Exception:
            candidate = candidate.absolute()
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def make_ssl_context(strict: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not strict:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _mask_token(t: Optional[str]) -> str:
    s = str(t or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "..." + s[-4:]


_SECRET_SALT = b"ncgo-sudo-secret-v1"


def _machine_master_key() -> bytes:
    parts: List[str] = []
    try:
        parts.append(str(uuid.getnode()))
    except Exception:
        pass
    for getter in (platform.node, lambda: str(Path.home().resolve())):
        try:
            parts.append(getter())
        except Exception:
            pass
    raw = "|".join(parts).encode("utf-8", errors="ignore")
    return hashlib.pbkdf2_hmac("sha256", raw, _SECRET_SALT, 200_000, dklen=32)


def _encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    data = plain.encode("utf-8")
    try:
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(_machine_master_key())
        return "enc:fernet:" + Fernet(key).encrypt(data).decode("ascii")
    except Exception:
        salt = os.urandom(16)
        stream = b""
        cnt = 0
        while len(stream) < len(data):
            stream += hashlib.pbkdf2_hmac("sha256", _machine_master_key(),
                                          salt + cnt.to_bytes(4, "big"), 1, dklen=32)
            cnt += 1
        out = bytes(b ^ stream[i] for i, b in enumerate(data))
        return "enc:v1:" + base64.b64encode(salt + out).decode("ascii")


def _decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        if stored.startswith("enc:fernet:"):
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(_machine_master_key())
            return Fernet(key).decrypt(stored[len("enc:fernet:"):].encode("ascii")).decode("utf-8")
        if stored.startswith("enc:v1:"):
            blob = base64.b64decode(stored[len("enc:v1:"):])
            salt, enc = blob[:16], blob[16:]
            stream = b""
            cnt = 0
            while len(stream) < len(enc):
                stream += hashlib.pbkdf2_hmac("sha256", _machine_master_key(),
                                              salt + cnt.to_bytes(4, "big"), 1, dklen=32)
                cnt += 1
            return bytes(b ^ stream[i] for i, b in enumerate(enc)).decode("utf-8")
    except Exception:
        return ""
    return stored


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
    for p in ["/opt/QQ/qq", "/opt/QQ/QQ"]:
        try:
            if os.path.exists(p) and os.access(p, os.X_OK):
                return True
        except Exception:
            continue
    cand = shutil.which("qq")
    if cand:
        try:
            real = os.path.realpath(cand)
            if os.path.exists(real) and os.access(real, os.X_OK):
                return True
        except Exception:
            pass
    for p in ["/usr/bin/qq", "/usr/local/bin/qq",
              "/opt/linuxqq/qq", "/usr/lib/qq/qq"]:
        try:
            if os.path.exists(p) and os.access(p, os.X_OK):
                return True
        except Exception:
            continue
    return False


# ============ Linux QQ 版本检测 ============

def get_linux_qq_version() -> Optional[str]:
    """读取已安装 LinuxQQ 的完整版本号，例如 '3.2.34-53644'。"""
    if IS_WINDOWS:
        return None

    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "linuxqq"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            return r.stdout.strip()
    except Exception:
        pass

    for qq_bin in ("/opt/QQ/qq", "/usr/bin/qq"):
        try:
            if not os.path.exists(qq_bin):
                continue
            r = subprocess.run(
                [qq_bin, "--version"],
                capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
            out = (r.stdout or "") + (r.stderr or "")
            m = re.search(r"(\d+\.\d+\.\d+)[-_](\d+)", out)
            if m:
                return f"{m.group(1)}-{m.group(2)}"
        except Exception:
            continue
    return None


def _parse_build_from_version(v: Optional[str]) -> Optional[int]:
    """从任意版本字符串里提取末尾数字 build 号。"""
    if not v:
        return None
    m = re.search(r"[-_](\d+)$", v)
    if m:
        return int(m.group(1))
    parts = re.split(r"[-_.]", v)
    for p in reversed(parts):
        if p.isdigit():
            return int(p)
    return None


def get_linux_qq_build_code() -> Optional[int]:
    """从已安装版本号里提取 build 号。"""
    return _parse_build_from_version(get_linux_qq_version())


def is_linux_qq_version_compatible() -> bool:
    """检查当前 LinuxQQ 的 build 号是否在 NapCat PacketBackend 支持范围内。"""
    code = get_linux_qq_build_code()
    if code is None:
        return False
    return NAPCAT_SUPPORTED_QQ_MIN <= code <= NAPCAT_SUPPORTED_QQ_MAX


def probe_deb_version(deb_path: Path) -> Optional[str]:
    """用 dpkg-deb 从 deb 包内部读取 Version 字段。

    返回例如 '3.2.34-53644'。读取失败返回 None。
    """
    try:
        r = subprocess.run(
            ["dpkg-deb", "-f", str(deb_path), "Version"],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            v = (r.stdout or "").strip()
            if v:
                return v
    except Exception:
        pass
    return None


def is_deb_build_compatible(deb_path: Path) -> Tuple[bool, Optional[str], Optional[int]]:
    """检查一个 deb 包内部的 build 号是否在支持范围内。

    返回 (ok, version_str, build_code)。
    """
    v = probe_deb_version(deb_path)
    if not v:
        return False, None, None
    code = _parse_build_from_version(v)
    if code is None:
        return False, v, None
    return (NAPCAT_SUPPORTED_QQ_MIN <= code <= NAPCAT_SUPPORTED_QQ_MAX), v, code


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
            out = subprocess.check_output(["pgrep", "-f", name], stderr=subprocess.DEVNULL,
                                          timeout=10).decode()
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


class NapCatManager:
    def __init__(self, plugin: "NapCatGoPlugin", deploy_mode: str = "auto"):
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
        self._log_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._downloading = False
        self._qq_logged_in = False
        self._auto_link_done = False
        self._pre_existing_qq_pids: Set[int] = set()
        self._launched_pids: Set[int] = set()
        self._launch_fake_bin: Optional[Path] = None

        self.api = NapCatAPI(host="127.0.0.1", port=self.napcat_port, token="")
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

    def _find_linux_qq_root(self) -> Optional[Path]:
        for c in [Path("/opt/QQ"), Path("/usr/share/qq"),
                  Path("/opt/linuxqq"), Path("/usr/lib/qq")]:
            try:
                if (c / "resources" / "app").exists():
                    return c
            except Exception:
                continue
        return None

    def _find_qq_binary(self, root: Path) -> Tuple[Optional[str], Optional[str]]:
        cand_which = shutil.which("qq")
        if cand_which:
            try:
                real = os.path.realpath(cand_which)
                if os.path.exists(real) and os.access(real, os.X_OK):
                    self.log_lines.append(f"[start] which qq -> {real}")
                    return real, str(Path(real).parent)
                else:
                    self.log_lines.append(
                        f"[start] ⚠ which qq={cand_which} 是断链或不可执行，跳过"
                    )
            except Exception as e:
                self.log_lines.append(f"[start] ⚠ 检查 which qq 失败: {e}")

        for cand in [
            root / "QQ" / "qq",
            root / "qq",
            Path("/opt/QQ/qq"),
            Path("/usr/local/bin/qq"),
            Path("/usr/bin/qq"),
            Path("/usr/lib/qq/qq"),
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

    # ============== sudo / 残留进程处理 ==============

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
                self.log_lines.append(
                    f"[stop] ✔ 残留进程已清理干净（第 {attempt} 轮）"
                )
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

    # ==================== Windows ====================

    def _find_entry(self) -> Optional[str]:
        for name in ["launcher-win10-user.bat", "launcher-win10.bat",
                     "launcher.bat", "launcher-user.bat",
                     "launcher-user.sh", "launcher.sh",
                     "napcat.mjs", "index.js"]:
            if (self.napcat_dir / name).exists():
                return name
        return None

    async def _extract_napcat(self, archive_path: Path) -> bool:
        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(self.napcat_dir)
            if not self._find_entry():
                return False
            return True
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return False

    async def download_napcat(self) -> bool:
        if not IS_WINDOWS:
            return True
        if self._downloading:
            return False
        self._downloading = True
        self._reset_download_state()
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.download_state["message"] = "正在下载 NapCat.Shell.zip"
        self.download_state["current_mirror"] = "准备下载"
        self.log_lines.append("[download] 开始下载 NapCat.Shell.zip ...")

        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log_lines.append(f"[download] ✘ 无法创建目录 {self.napcat_dir}: {e}")
            self._downloading = False
            self.download_state["downloading"] = False
            self.download_state["phase"] = "failed"
            return False

        try:
            version = self.plugin.config.get("napcat_version", DEFAULT_NAPCAT_VERSION)
            if "/" in version:
                fixed = version.split("/", 1)[0].strip()
                if fixed:
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
                    self.log_lines.append(f"[download] 发现本地压缩包: {local_zip}")
                    ok = await self._extract_napcat(local_zip)
                    if ok:
                        self.download_state["phase"] = "done"
                        self.download_state["percent"] = 100
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

            urls: List[Tuple[str, str]] = []
            for p in prefixes:
                urls.append((f"{p.rstrip('/')}/{official}", p))
            urls.append((official, "official"))

            download_path = self.napcat_dir / filename
            self.download_state["total_mirrors"] = len(urls)
            self.log_lines.append(f"[download] 共 {len(urls)} 个镜像候选，依次尝试")

            for idx, (url, label) in enumerate(urls, 1):
                self.log_lines.append(f"[download] [{idx}/{len(urls)}] 尝试: {label}")
                self.download_state["current_mirror"] = label
                self.download_state["current_index"] = idx
                ssl_ctx = make_ssl_context(strict=(label == "official"))
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
                        self.download_state["percent"] = 100
                        self.log_lines.append(f"[download] ✔ 下载并解压成功（来源: {label}）")
                        return True
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    self.log_lines.append(f"[download] [{idx}/{len(urls)}] {label} 失败: {e}")
                    try:
                        if download_path.exists():
                            download_path.unlink()
                    except Exception:
                        pass
                    continue

            self.log_lines.append("[download] ✘ 所有镜像均失败")
            self.download_state["phase"] = "failed"
            return False
        finally:
            self._downloading = False
            self.download_state["downloading"] = False

    # ==================== Docker ====================

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
        img = str(self.plugin.config.get("docker_image", DEFAULT_DOCKER_IMAGE)
                  or DEFAULT_DOCKER_IMAGE).strip()
        tag = str(self.plugin.config.get("docker_tag", DEFAULT_DOCKER_TAG)
                  or DEFAULT_DOCKER_TAG).strip() or "latest"
        return f"{img}:{tag}" if ":" not in img else img

    def docker_container_name(self) -> str:
        return str(self.plugin.config.get("docker_container_name", DEFAULT_DOCKER_CONTAINER)
                   or DEFAULT_DOCKER_CONTAINER).strip() or DEFAULT_DOCKER_CONTAINER

    # ==================== Linux QQ 自动安装 ====================

    async def _download_file(self, url: str, dest: Path,
                             connect_timeout: int = 15, read_timeout: int = 60,
                             progress_label: str = "",
                             progress_index: int = 0,
                             progress_total: int = 0,
                             max_time: int = 900) -> bool:
        try:
            ssl_ctx = make_ssl_context(strict=False)
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=max_time, sock_connect=connect_timeout,
                        sock_read=read_timeout),
                ) as resp:
                    if resp.status != 200:
                        self.log_lines.append(
                            f"[qq-install] {progress_label} HTTP {resp.status}"
                        )
                        return False
                    total = resp.content_length or 0
                    got = 0
                    t0 = time.time()
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            f.write(chunk)
                            got += len(chunk)
                            elapsed = time.time() - t0
                            if elapsed > 0:
                                self.download_state["downloaded_mb"] = got / 1024 / 1024
                                self.download_state["speed_kbps"] = got / elapsed / 1024
                                if total > 0:
                                    self.download_state["total_mb"] = total / 1024 / 1024
                                    self.download_state["percent"] = min(100, got * 100 // total)
                                else:
                                    self.download_state["total_mb"] = 0.0
                                self.download_state["current_mirror"] = progress_label
                                self.download_state["current_index"] = progress_index
                                self.download_state["total_mirrors"] = progress_total
                    return True
        except Exception as e:
            self.log_lines.append(f"[qq-install] {progress_label} 异常: {e}")
            return False

    async def _validate_deb(self, path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                magic = f.read(7)
            return magic == b"!<arch>"
        except Exception:
            return False

    def _build_linuxqq_candidate_urls(self) -> List[Tuple[str, str]]:
        candidates: List[Tuple[str, str]] = []
        seen = set()

        def add(u: str, label: str):
            if u and u not in seen:
                seen.add(u)
                candidates.append((u, label))

        user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
        mirror_order: List[str] = []
        if user_mirror:
            mirror_order.append(user_mirror)
        for m in NAPCAT_MIRROR_CANDIDATES:
            if m not in mirror_order:
                mirror_order.append(m)

        for raw in LINUXQQ_DEB_URLS:
            if "github.com/" in raw or "githubusercontent.com/" in raw:
                for m in mirror_order:
                    add(f"{m.rstrip('/')}/{raw}", m)
                add(raw, "github-original")
            else:
                add(raw, "qq-cdn")
        return candidates

    async def _purge_linuxqq(self) -> None:
        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw or not shutil.which("sudo"):
            return
        env = {**os.environ}
        fake_bin = self._build_fake_sudo("purgeqq", env)
        try:
            for args in (["dpkg", "-r", "linuxqq"], ["dpkg", "--purge", "linuxqq"]):
                self.log_lines.append(f"[qq-compat] 执行: sudo {' '.join(args)}")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", *args,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                    )
                    try:
                        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
                        text = (out or b"").decode("utf-8", errors="ignore")
                        for ln in text.splitlines()[-5:]:
                            if ln.strip():
                                self.log_lines.append(f"[qq-compat] {ln.strip()}")
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        self.log_lines.append("[qq-compat] ⚠ dpkg 卸载超时")
                except Exception as e:
                    self.log_lines.append(f"[qq-compat] ⚠ dpkg 卸载异常: {e}")
        finally:
            if fake_bin and fake_bin.exists():
                shutil.rmtree(fake_bin, ignore_errors=True)

    async def ensure_linux_qq_compatible(self) -> bool:
        cur_ver = get_linux_qq_version()
        cur_code = get_linux_qq_build_code()

        if is_qq_installed() and is_linux_qq_version_compatible():
            self.log_lines.append(
                f"[qq-compat] ✔ 当前 QQ {cur_ver} (build {cur_code}) "
                f"在支持范围 [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] 内，跳过"
            )
            return True

        if is_qq_installed():
            self.log_lines.append(
                f"[qq-compat] ⚠ 当前 QQ {cur_ver} (build {cur_code}) 不兼容："
                f"PacketBackend 要求 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}]"
            )
            self.log_lines.append(
                f"[qq-compat] 将自动卸载并安装推荐版本 {RECOMMENDED_LINUXQQ_VERSION}"
            )
            await self._purge_linuxqq()

            if is_qq_installed():
                self.log_lines.append(
                    "[qq-compat] ⚠ purge 后仍检测到 QQ，可能卸载失败，继续尝试安装"
                )
        else:
            self.log_lines.append("[qq-compat] 未检测到 LinuxQQ，准备安装推荐版本 ...")

        for stale in list(self.napcat_dir.glob("linuxqq*.deb")) + \
                     list(self.napcat_dir.glob("QQ*.deb")):
            try:
                self.log_lines.append(f"[qq-compat] 移除旧 deb 缓存: {stale.name}")
                stale.unlink()
            except Exception:
                pass

        ok = await self.ensure_linux_qq()
        if not ok:
            return False

        new_ver = get_linux_qq_version()
        new_code = get_linux_qq_build_code()
        if is_linux_qq_version_compatible():
            self.log_lines.append(
                f"[qq-compat] ✔ 安装完成，当前 QQ {new_ver} (build {new_code}) 已兼容"
            )
            return True
        self.log_lines.append(
            f"[qq-compat] ✘ 安装后 QQ {new_ver} (build {new_code}) 仍不兼容，"
            f"请手动下载 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] "
            f"的 deb 放到: {self.napcat_dir}/"
        )
        return False

    async def ensure_linux_qq(self) -> bool:
        if is_qq_installed():
            self.log_lines.append("[qq-install] ✔ LinuxQQ 已安装，跳过")
            return True

        self.log_lines.append(
            f"[qq-install] 开始安装 LinuxQQ（目标版本 {RECOMMENDED_LINUXQQ_VERSION}）..."
        )

        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw:
            self.log_lines.append("[qq-install] ✘ 未配置 sudo 密码，无法安装 QQ")
            return False
        if not shutil.which("sudo"):
            self.log_lines.append("[qq-install] ✘ 系统无 sudo 命令")
            return False

        env_base = {
            **os.environ,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }

        deb_path = self.napcat_dir / "linuxqq.deb"
        downloaded = False

        manual = list(self.napcat_dir.glob("linuxqq*.deb")) + \
                 list(self.napcat_dir.glob("QQ*.deb"))
        if manual:
            for m in manual:
                if not await self._validate_deb(m):
                    self.log_lines.append(
                        f"[qq-install] ⚠ 本地 deb 无效: {m}，忽略"
                    )
                    continue
                ok_build, ver_str, build_code = is_deb_build_compatible(m)
                if ok_build:
                    deb_path = m
                    self.log_lines.append(
                        f"[qq-install] ✔ 使用本地 deb: {deb_path} "
                        f"(version={ver_str}, build={build_code})"
                    )
                    downloaded = True
                    break
                else:
                    self.log_lines.append(
                        f"[qq-install] ⚠ 本地 deb 的 build 不兼容，跳过: "
                        f"{m.name} (version={ver_str}, build={build_code})"
                    )

        if not downloaded:
            candidates = self._build_linuxqq_candidate_urls()
            total_urls = len(candidates)
            self.download_state["downloading"] = True
            self.download_state["phase"] = "downloading"
            self.download_state["message"] = "正在下载 LinuxQQ"
            self.download_state["current_mirror"] = "下载 LinuxQQ"
            self.download_state["total_mirrors"] = total_urls
            self.download_state["percent"] = 0

            for idx, (url, label) in enumerate(candidates, 1):
                short = url[:90] + ("..." if len(url) > 90 else "")
                self.log_lines.append(
                    f"[qq-install] [{idx}/{total_urls}] 尝试 ({label}): {short}"
                )
                self.download_state["current_index"] = idx
                self.download_state["current_mirror"] = f"LinuxQQ ({idx}/{total_urls}) - {label}"
                try:
                    if deb_path.exists():
                        try:
                            deb_path.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass

                ok = await self._download_file(
                    url, deb_path,
                    connect_timeout=15, read_timeout=60,
                    progress_label=f"LinuxQQ ({idx}/{total_urls})",
                    progress_index=idx, progress_total=total_urls,
                    max_time=900,
                )
                if not ok:
                    continue
                if not deb_path.exists() or deb_path.stat().st_size < 1024:
                    self.log_lines.append(
                        f"[qq-install] [{idx}/{total_urls}] ✘ 文件过小"
                    )
                    continue
                if not await self._validate_deb(deb_path):
                    self.log_lines.append(
                        f"[qq-install] [{idx}/{total_urls}] ✘ 不是有效 deb，换下一个"
                    )
                    try:
                        deb_path.unlink()
                    except Exception:
                        pass
                    continue

                ok_build, ver_str, build_code = is_deb_build_compatible(deb_path)
                if not ok_build:
                    if ver_str is None:
                        self.log_lines.append(
                            f"[qq-install] [{idx}/{total_urls}] ✘ 无法读取 deb 版本，换下一个"
                        )
                    else:
                        self.log_lines.append(
                            f"[qq-install] [{idx}/{total_urls}] ✘ deb 内 build 不兼容，"
                            f"跳过: version={ver_str} build={build_code} "
                            f"(要求 ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}])"
                        )
                    try:
                        deb_path.unlink()
                    except Exception:
                        pass
                    continue

                size_mb = deb_path.stat().st_size / 1024 / 1024
                self.log_lines.append(
                    f"[qq-install] [{idx}/{total_urls}] ✔ 下载成功 "
                    f"({size_mb:.1f} MB, version={ver_str}, build={build_code})"
                )
                downloaded = True
                break

            self.download_state["downloading"] = False

        if not downloaded:
            self.log_lines.append(
                "[qq-install] ✘ 所有 LinuxQQ 下载源均失败或均不兼容。"
                "请手动下载 build 号 ∈ ["
                f"{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}] 的 deb 放到: "
                + str(self.napcat_dir)
            )
            self.log_lines.append(
                "[qq-install] 手动下载地址（浏览器打开，任选其一）："
            )
            self.log_lines.append(
                "[qq-install]   https://github.com/SATA-F5/NC_Go/releases"
            )
            self.log_lines.append(
                "[qq-install]   https://github.com/Rodert/qq-versions/releases"
            )
            self.download_state["phase"] = "failed"
            self.download_state["message"] = "LinuxQQ 下载失败"
            return False

        self.download_state["phase"] = "extracting"
        self.download_state["message"] = "正在安装 LinuxQQ"
        self.download_state["current_mirror"] = "安装 LinuxQQ"
        self.download_state["percent"] = 0

        env = dict(env_base)
        fake_bin = self._build_fake_sudo("qq", env)
        if not fake_bin:
            self.log_lines.append("[qq-install] ✘ 创建 sudo 包装脚本失败")
            self.download_state["phase"] = "failed"
            return False

        try:
            self.log_lines.append("[qq-install] 执行: sudo apt-get update")
            proc = await asyncio.create_subprocess_exec(
                "sudo", "apt-get", "update",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass

            self.log_lines.append(f"[qq-install] 执行: sudo dpkg -i {deb_path.name}")
            proc = await asyncio.create_subprocess_exec(
                "sudo", "dpkg", "-i", str(deb_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                self.log_lines.append("[qq-install] ✘ dpkg 安装超时")
                self.download_state["phase"] = "failed"
                self.download_state["message"] = "dpkg 超时"
                return False

            dpkg_rc = proc.returncode or 0
            text = (out or b"").decode("utf-8", errors="ignore")
            for ln in text.splitlines()[-8:]:
                if ln.strip():
                    self.log_lines.append(f"[qq-install] {ln.strip()}")

            if dpkg_rc != 0:
                self.log_lines.append(
                    "[qq-install] dpkg 返回非 0，执行 apt-get -f install -y 补依赖 ..."
                )
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "apt-get", "-f", "install", "-y",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    self.log_lines.append("[qq-install] ✘ apt-get -f install 超时")
                    self.download_state["phase"] = "failed"
                    self.download_state["message"] = "apt-get 超时"
                    return False
                text = (out or b"").decode("utf-8", errors="ignore")
                for ln in text.splitlines()[-8:]:
                    if ln.strip():
                        self.log_lines.append(f"[qq-install] {ln.strip()}")

            if is_qq_installed():
                installed_ver = get_linux_qq_version()
                installed_code = get_linux_qq_build_code()
                self.log_lines.append(
                    f"[qq-install] ✔ LinuxQQ 安装成功: {installed_ver} (build {installed_code})"
                )
                try:
                    deb_path.unlink()
                except Exception:
                    pass
                self.download_state["phase"] = "done"
                self.download_state["downloading"] = False
                self.download_state["percent"] = 100
                self.download_state["current_mirror"] = ""
                self.download_state["message"] = "LinuxQQ 已安装"
                return True
            else:
                self.log_lines.append(
                    "[qq-install] ✘ 安装后仍未检测到 /opt/QQ/qq"
                )
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "LinuxQQ 安装失败"
                return False
        finally:
            if fake_bin and fake_bin.exists():
                shutil.rmtree(fake_bin, ignore_errors=True)

    # ==================== Linux install.sh ====================

    def _build_install_sh_urls(self) -> List[Tuple[str, str]]:
        user_mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
        prefixes: List[str] = []
        if user_mirror:
            prefixes.append(user_mirror)
        for m in NAPCAT_MIRROR_CANDIDATES:
            if m not in prefixes:
                prefixes.append(m)
        urls: List[Tuple[str, str]] = []
        for p in prefixes:
            urls.append((f"{p.rstrip('/')}/{NAPCAT_INSTALL_SH_ORIGINAL}", p))
        urls.append((NAPCAT_INSTALL_SH_ORIGINAL, "github-raw"))
        urls.append((NAPCAT_INSTALL_SH_LEGACY, "legacy"))
        return urls

    async def _backup_inner_napcat_if_needed(self) -> bool:
        inner = self.napcat_dir / "napcat"
        if not inner.exists():
            return True
        try:
            has_content = any(inner.iterdir())
        except Exception:
            has_content = False
        if not has_content:
            return True

        if self._is_new_style_install(inner) or self._is_new_style_install(self.napcat_dir):
            self.log_lines.append(
                f"[install] {inner} 已包含 launcher，跳过备份"
            )
            return True

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = self.napcat_dir / f"napcat.bak.{ts}"
        try:
            shutil.move(str(inner), str(bak))
            self.log_lines.append(
                f"[install] ⚠ 检测到 {inner} 已存在且不为空，"
                f"已备份为 {bak.name}"
            )
            return True
        except Exception as e:
            self.log_lines.append(f"[install] ⚠ 备份 {inner} 失败: {e}")
            self.log_lines.append(f"[install] 尝试直接删除 {inner} ...")
            try:
                shutil.rmtree(inner)
                self.log_lines.append(f"[install] ✔ 已删除 {inner}")
                return True
            except Exception as e2:
                self.log_lines.append(f"[install] ✘ 删除 {inner} 也失败: {e2}")
                self.log_lines.append("[install] 请手动执行：")
                self.log_lines.append(f"[install]   rm -rf {inner}")
                self.log_lines.append("[install] 或带 sudo：")
                self.log_lines.append(f"[install]   sudo rm -rf {inner}")
                return False

    async def install_linux_napcat(self) -> bool:
        self.napcat_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.napcat_dir / "install.sh"

        self._reset_download_state()
        self.download_state["downloading"] = True
        self.download_state["phase"] = "downloading"
        self.download_state["message"] = "正在准备安装"
        self.download_state["current_mirror"] = "准备中"

        pw = (self.plugin.config.get("sudo_password", "") or "").strip()
        if not pw:
            self.log_lines.append("[install] ✘ 未配置 sudo 密码")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "未配置 sudo 密码"
            return False
        if not shutil.which("sudo"):
            self.log_lines.append("[install] ✘ 系统无 sudo 命令")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "系统无 sudo"
            return False

        if not await self._backup_inner_napcat_if_needed():
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "无法清理旧的 napcat 目录，请手动删除"
            return False

        sh_urls = self._build_install_sh_urls()
        self.log_lines.append(f"[install] 目标目录: {self.napcat_dir}")
        self.log_lines.append(f"[install] 共 {len(sh_urls)} 个 install.sh 下载源，依次尝试")

        self.download_state["total_mirrors"] = len(sh_urls)
        self.download_state["current_index"] = 0
        self.download_state["current_mirror"] = "下载 install.sh"
        self.download_state["message"] = "正在下载 install.sh"

        downloaded = False
        for idx, (url, label) in enumerate(sh_urls, 1):
            self.log_lines.append(f"[install] [{idx}/{len(sh_urls)}] 下载 install.sh: {label}")
            self.download_state["current_mirror"] = label
            self.download_state["current_index"] = idx
            try:
                ssl_ctx = make_ssl_context(strict=False)
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                async with aiohttp.ClientSession(connector=connector) as session:
                    try:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(
                                total=None, sock_connect=15, sock_read=30),
                        ) as resp:
                            if resp.status != 200:
                                self.log_lines.append(
                                    f"[install] [{idx}/{len(sh_urls)}] {label} HTTP {resp.status}"
                                )
                                continue
                            total = resp.content_length or 0
                            got = 0
                            t0 = time.time()
                            with open(script_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(8192):
                                    f.write(chunk)
                                    got += len(chunk)
                                    elapsed = time.time() - t0
                                    if elapsed > 0:
                                        self.download_state["downloaded_mb"] = got / 1024 / 1024
                                        self.download_state["speed_kbps"] = got / elapsed / 1024
                                        if total > 0:
                                            self.download_state["total_mb"] = total / 1024 / 1024
                                            self.download_state["percent"] = min(100, got * 100 // total)
                                        else:
                                            self.download_state["total_mb"] = 0.0
                    except Exception as e:
                        self.log_lines.append(f"[install] [{idx}/{len(sh_urls)}] {label} 下载异常: {e}")
                        continue

                if script_path.exists() and script_path.stat().st_size > 100:
                    size = script_path.stat().st_size
                    self.log_lines.append(
                        f"[install] [{idx}/{len(sh_urls)}] {label} ✔ 下载成功 ({size} 字节)"
                    )
                    self.download_state["downloaded_mb"] = size / 1024 / 1024
                    self.download_state["total_mb"] = size / 1024 / 1024
                    self.download_state["percent"] = 100
                    downloaded = True
                    break
                else:
                    self.log_lines.append(
                        f"[install] [{idx}/{len(sh_urls)}] {label} ✘ 文件异常"
                    )
            except Exception as e:
                self.log_lines.append(f"[install] [{idx}/{len(sh_urls)}] {label} 异常: {e}")
                continue

        if not downloaded:
            self.log_lines.append("[install] ✘ install.sh 所有来源均下载失败")
            self.download_state["phase"] = "failed"
            self.download_state["downloading"] = False
            self.download_state["message"] = "install.sh 下载失败"
            return False

        try:
            os.chmod(script_path, 0o755)
        except Exception:
            pass

        self.download_state["phase"] = "extracting"
        self.download_state["percent"] = 0
        self.download_state["downloaded_mb"] = 0.0
        self.download_state["total_mb"] = 0.0
        self.download_state["speed_kbps"] = 0.0
        self.download_state["current_mirror"] = "install.sh 执行中 00:00"
        self.download_state["message"] = "正在执行 install.sh"

        started_at = time.time()
        stop_flag = asyncio.Event()

        async def _tick():
            while not stop_flag.is_set():
                try:
                    await asyncio.wait_for(stop_flag.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    pass
                elapsed = time.time() - started_at
                mm = int(elapsed) // 60
                ss = int(elapsed) % 60
                fake_pct = min(95, int(elapsed / 600 * 95))
                self.download_state["percent"] = fake_pct
                self.download_state["current_mirror"] = f"install.sh 执行中 {mm:02d}:{ss:02d}"

        tick_task = asyncio.create_task(_tick())

        env = {
            **os.environ,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }
        fake_bin = self._build_fake_sudo("install", env)

        self.log_lines.append(
            f"[install] 开始执行 install.sh（sudo，cwd={self.napcat_dir}）..."
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "bash", str(script_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.napcat_dir),
                env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                self.log_lines.append("[install] ✘ install.sh 执行超时")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "install.sh 执行超时"
                return False

            output = stdout.decode("utf-8", errors="ignore") if stdout else ""
            filtered = _filter_install_output(output)
            for line in filtered:
                self.log_lines.append(f"[install] {line}")
            self.log_lines.append(f"[install] install.sh 退出码: {proc.returncode}")

            low = output.lower()
            if ("incorrect password" in low or "sorry, try again" in low
                    or "authentication failure" in low):
                self.log_lines.append("[install] ✘ sudo 密码错误")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "sudo 密码错误"
                return False

            ok = self._is_new_style_install(self.napcat_dir) or \
                 self._is_new_style_install(self.napcat_dir / "napcat")

            if ok:
                self.log_lines.append("[install] ✔ 新方案安装成功")
                self.download_state["phase"] = "done"
                self.download_state["downloading"] = False
                self.download_state["percent"] = 100
                self.download_state["current_mirror"] = ""
                self.download_state["message"] = "安装完成"
            else:
                self.log_lines.append("[install] ✘ 未检测到 libnapcat_launcher.so + launcher.sh")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "安装完成但未找到 launcher"
            return ok
        finally:
            stop_flag.set()
            try:
                await tick_task
            except Exception:
                pass
            if fake_bin and fake_bin.exists():
                shutil.rmtree(fake_bin, ignore_errors=True)

    # ==================== Native 启动 ====================

    async def _start_native(self) -> bool:
        async with self._lock:
            if self.process and self.process.returncode is None:
                return True

            qq_name = "QQ.exe" if IS_WINDOWS else "qq"
            self._pre_existing_qq_pids = list_pids_by_name(qq_name)
            self._launched_pids.clear()
            qq_number = str(self.plugin.config.get("qq_number", "") or "").strip()

            env_extra: Dict[str, str] = {}

            if IS_WINDOWS:
                if not self._find_entry():
                    if not await self.download_napcat():
                        self.log_lines.append("[start] ✘ 下载失败")
                        return False
                    if not self._find_entry():
                        self.log_lines.append("[start] 下载完成但入口文件仍缺失")
                        return False

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
                    self.log_lines.append("[start] 提示：可手动下载兼容版本 deb 放到:")
                    self.log_lines.append(f"[start]   {self.napcat_dir}/")
                    self.log_lines.append(
                        f"[start] 兼容 build 范围: [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}]"
                    )
                    self.log_lines.append("[start] 然后重试启动")
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
                    self.log_lines.append("[start] 诊断建议：")
                    self.log_lines.append("[start]   ls -l /opt/QQ/qq")
                    self.log_lines.append("[start]   ls -l /usr/bin/qq   # 若指向已删除的文件则是断链")
                    self.log_lines.append("[start] 若 /usr/bin/qq 是断链，执行：")
                    self.log_lines.append("[start]   sudo rm -f /usr/bin/qq")
                    self.log_lines.append(f"[start] 或建软链：ln -sf /opt/QQ/qq {root}/qq")
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

                launch_path = base_path
                env_extra["PATH"] = launch_path
                env_extra["HOME"] = user_home
                if user_name:
                    env_extra["USER"] = user_name
                env_extra["NAPCAT_BOOTMAIN"] = str(root)
                env_extra["DISPLAY"] = ":1"

                real_sudo = shutil.which("sudo") or "sudo"
                cmd = [
                    real_sudo, "-S", "-E", "env",
                    f"PATH={launch_path}",
                    f"HOME={user_home}",
                ]
                if user_name:
                    cmd.append(f"USER={user_name}")
                cmd += [
                    f"NAPCAT_BOOTMAIN={root}",
                    f"DISPLAY=:1",
                    "bash", "launcher.sh",
                ]
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


class ConfigSyncer:
    def __init__(self, plugin: "NapCatGoPlugin"):
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(self.context.plugin_dir) if hasattr(self.context, "plugin_dir") else Path(__file__).parent
        self.persistent_dir = resolve_persistent_dir(self.context)
        self.napcat_dir = self.persistent_dir / "napcat"

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
        candidates += [Path("data/cmd_config.json"), Path("../data/cmd_config.json"),
                       Path("../../data/cmd_config.json")]
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

        if getattr(self.plugin, "deploy_mode", None) == "docker":
            d = self.persistent_dir / "docker-data" / "config"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return d

        if IS_WINDOWS:
            d = self.napcat_dir / "config"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return d

        for base in [self.napcat_dir, self.napcat_dir / "napcat"]:
            for sub in [base / "config", base]:
                try:
                    if sub.exists() and sub.is_dir():
                        if sub.name == "config" or list(sub.glob("onebot11*.json")):
                            return sub
                except Exception:
                    continue
        try:
            root = self.plugin.manager._find_new_style_root()
            if root:
                d = root / "config"
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    return d
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _find_onebot11_file(self) -> Optional[Path]:
        if not self.napcat_config_dir or not self.napcat_config_dir.exists():
            return None
        try:
            cs = list(self.napcat_config_dir.glob("onebot11_*.json"))
            cs += list(self.napcat_config_dir.glob("onebot11.json"))
            if not cs:
                f = self.napcat_config_dir / "onebot11.json"
                return f
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
        candidates = [self.napcat_dir / "config",
                      self.napcat_dir / "napcat" / "config"]
        try:
            root = self.plugin.manager._find_new_style_root()
            if root:
                candidates += [root / "config", root / "napcat" / "config"]
        except Exception:
            pass
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

    def _pre_sync_chown(self) -> None:
        if IS_WINDOWS:
            return
        if not self.napcat_config_dir:
            return
        try:
            uid = os.getuid()
            gid = os.getgid()
        except Exception:
            return
        if uid == 0:
            return

        env = {**os.environ}
        fake_bin = self.plugin.manager._build_fake_sudo("prechown", env)
        if not fake_bin and not shutil.which("sudo"):
            return

        try:
            r = subprocess.run(
                ["sudo", "chown", "-R", f"{uid}:{gid}", str(self.napcat_config_dir)],
                env=env, capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                logger.info(f"[NapCat_Go] pre-sync chown ok: {self.napcat_config_dir}")
            else:
                logger.warning(
                    f"[NapCat_Go] pre-sync chown rc={r.returncode}: "
                    f"{(r.stderr or '').strip()[:120]}"
                )
        except Exception as e:
            logger.warning(f"[NapCat_Go] pre-sync chown failed: {e}")
        finally:
            if fake_bin and fake_bin.exists():
                shutil.rmtree(fake_bin, ignore_errors=True)

        try:
            parent = self.napcat_config_dir.parent
            if parent and parent.exists() and parent != self.napcat_config_dir:
                env2 = {**os.environ}
                fake2 = self.plugin.manager._build_fake_sudo("prechown2", env2)
                subprocess.run(
                    ["sudo", "chown", "-R", f"{uid}:{gid}", str(parent)],
                    env=env2, timeout=30, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if fake2 and fake2.exists():
                    shutil.rmtree(fake2, ignore_errors=True)
        except Exception:
            pass

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

        self._pre_sync_chown()

        if not self.napcat_config_file.exists():
            try:
                self.napcat_config_dir.mkdir(parents=True, exist_ok=True)
                with open(self.napcat_config_file, "w", encoding="utf-8") as f:
                    json.dump({"network": {"websocketClients": []}}, f,
                              indent=2, ensure_ascii=False)
            except Exception as e:
                return self._fail(f"Create empty onebot11 failed: {e}")

        bot = self.astrbot_bot
        ws_host = reverse_ws_host(getattr(self.plugin, "deploy_mode", None), bot["host"])
        ws_url = f"ws://{ws_host}:{bot['port']}{bot['path']}"

        try:
            with open(self.napcat_config_file, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
        except Exception as e:
            return self._fail(f"Read failed: {e}")

        network = config.setdefault("network", {})
        new_clients = [{
            "name": "astrbot",
            "enable": True,
            "url": ws_url,
            "token": bot["token"],
            "reportSelfMessage": False,
            "messageFormat": "array",
            "debug": False,
            "heartInterval": 30000,
            "reconnectInterval": 3000,
            "sslVerify": False,
            "ssl": False,
            "sslCertVerify": False,
        }]
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
            "astrbot_config_path": str(self.astrbot_config_path) if self.astrbot_config_path else None,
            "astrbot_bot_found": self.astrbot_bot is not None,
            "astrbot_bot_id": bot.get("id"), "astrbot_bot_host": bot.get("host"),
            "astrbot_bot_port": bot.get("port"),
            "astrbot_bot_token": _mask_token(bot.get("token", "")),
            "napcat_config_dir": str(self.napcat_config_dir) if self.napcat_config_dir else None,
            "napcat_config_file": str(self.napcat_config_file) if self.napcat_config_file else None,
            "last_sync_ok": self.last_sync_ok, "last_sync_msg": self.last_sync_msg,
            "last_sync_time": self.last_sync_time,
            "config": {**self.plugin.config, "sudo_password": ""},
        }


class NapCatGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.astrbot_config = config

        logger.info(f"[NapCat_Go] ============ CODE VERSION: {PLUGIN_CODE_VERSION} ============")

        self.persistent_dir = resolve_persistent_dir(context)
        logger.info(f"[NapCat_Go] persistent dir: {self.persistent_dir}")

        self.config = self._load_config()
        self.syncer = ConfigSyncer(self)

        self.deploy_mode = resolve_deploy_mode(
            self.config.get("deploy_mode", DEFAULT_DEPLOY_MODE),
            docker_binary_available=detect_docker_binary() is not None,
        )
        logger.info(f"[NapCat_Go] resolved deploy_mode = {self.deploy_mode}")
        self.manager = NapCatManager(self, deploy_mode=self.deploy_mode)
        logger.info(f"[NapCat_Go] NapCat dir: {self.manager.napcat_dir}")

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

        _final_log = {**self.config, "sudo_password": "***" if self.config.get("sudo_password") else ""}
        logger.info(f"[NapCat_Go] FINAL CONFIG: {_final_log}")

        auto_start = bool(self.config.get("auto_start", True))
        installed = self.manager.is_installed()
        logger.info(f"[NapCat_Go] auto_start={auto_start} installed={installed}")

        if auto_start:
            self._auto_start_task = asyncio.ensure_future(self._auto_start_wrapper())

    @filter.command("napcat")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def napcat_cmd(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._status_text())
        except Exception as e:
            logger.error(f"[NapCat_Go] napcat_cmd failed: {e}", exc_info=True)

    async def _auto_start_wrapper(self):
        try:
            await asyncio.sleep(3)
            ok = await self.manager.start()
            if ok and self.manager.is_running():
                logger.info("[NapCat_Go] auto_start: NapCat is running")
            else:
                logger.warning(f"[NapCat_Go] auto_start: failed (returned {ok})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[NapCat_Go] auto_start failed: {e}", exc_info=True)

    async def api_status(self):
        try:
            self.syncer.refresh()
        except Exception:
            pass
        d = self.syncer.status_dict()
        d["running"] = self.manager.is_running()
        d["napcat_installed"] = self.manager.is_installed()
        d["qq_installed"] = True if self.deploy_mode == "docker" else is_qq_installed()
        d["deploy_mode"] = self.manager.deploy_mode
        d["distro_family"] = _detect_distro_family() if IS_LINUX else None
        d["docker_available"] = detect_docker_binary() is not None
        d["docker_container"] = self.manager.docker_container_name()
        d["docker_image"] = self.manager.docker_image_ref()
        d["napcat_webui_url"] = self.manager.napcat_webui_url
        d["napcat_token"] = _mask_token(self.manager.napcat_token)
        d["napcat_port"] = self.manager.napcat_port
        d["download_state"] = dict(self.manager.download_state)
        d["log_count"] = len(self.manager.log_lines)

        if IS_LINUX:
            d["qq_version"] = get_linux_qq_version()
            d["qq_build"] = get_linux_qq_build_code()
            d["qq_version_compatible"] = is_linux_qq_version_compatible()
            d["qq_supported_build_min"] = NAPCAT_SUPPORTED_QQ_MIN
            d["qq_supported_build_max"] = NAPCAT_SUPPORTED_QQ_MAX
            d["qq_recommended_version"] = RECOMMENDED_LINUXQQ_VERSION
        else:
            d["qq_version"] = None
            d["qq_build"] = None
            d["qq_version_compatible"] = None

        try:
            root = self.manager._find_new_style_root()
            d["new_style_root"] = str(root) if root else None
        except Exception:
            d["new_style_root"] = None
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
        return json_response({**self.config, "sudo_password": ""})

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("Request body must be JSON")
        deploy_mode = str(payload.get("deploy_mode", "") or "").strip().lower() or self.config.get("deploy_mode", DEFAULT_DEPLOY_MODE)
        if deploy_mode not in DEPLOY_MODES:
            deploy_mode = DEFAULT_DEPLOY_MODE
        self.config.update({
            "napcat_config_dir": str(payload.get("napcat_config_dir", "") or "").strip(),
            "astrbot_config_path": str(payload.get("astrbot_config_path", "") or "").strip(),
            "qq_number": str(payload.get("qq_number", "") or "").strip(),
            "deploy_mode": deploy_mode,
            "docker_image": str(payload.get("docker_image", self.config.get("docker_image", DEFAULT_DOCKER_IMAGE)) or "").strip(),
            "docker_tag": str(payload.get("docker_tag", self.config.get("docker_tag", DEFAULT_DOCKER_TAG)) or "").strip(),
            "docker_container_name": str(payload.get("docker_container_name", self.config.get("docker_container_name", DEFAULT_DOCKER_CONTAINER)) or "").strip(),
            "napcat_download_mirror": str(payload.get("napcat_download_mirror", self.config.get("napcat_download_mirror", DEFAULT_DOWNLOAD_MIRROR))),
        })
        new_pw = str(payload.get("sudo_password", "") or "")
        if new_pw.strip():
            self.config["sudo_password"] = new_pw.strip()
        self.deploy_mode = resolve_deploy_mode(
            self.config.get("deploy_mode", DEFAULT_DEPLOY_MODE),
            docker_binary_available=detect_docker_binary() is not None,
        )
        self.manager.deploy_mode = self.deploy_mode
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
        if self.manager.deploy_mode == "docker":
            try:
                ok = await self.manager._start_docker()
                return json_response({"ok": ok})
            except Exception as e:
                return error_response(f"Docker install failed: {e}")
        if IS_WINDOWS:
            try:
                ok = await self.manager.download_napcat()
                return json_response({"ok": ok})
            except Exception as e:
                return error_response(f"Download failed: {e}")
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
            if self.manager.deploy_mode == "docker":
                name = self.manager.docker_container_name()
                code, out = await self.manager._docker_cmd("rm", "-f", name, timeout=60)
                if self.manager._log_task and not self.manager._log_task.done():
                    self.manager._log_task.cancel()
                    try:
                        await self.manager._log_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self.manager._log_task = None
                self.manager.process = None
                self.manager.napcat_webui_url = ""
                self.manager.napcat_token = ""
                self.manager._qq_logged_in = False
                return json_response({"ok": code == 0, "message": "容器已移除"})
            await self.manager.stop()
            self.manager.cleanup()
            return json_response({"ok": True, "message": "Cleaned"})
        except Exception as e:
            return error_response(f"Cleanup failed: {e}")

    def _load_config(self) -> Dict[str, Any]:
        self.config_path = self.persistent_dir / "config.json"
        config = DEFAULT_CONFIG.copy()

        persisted_user: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8-sig") as f:
                    persisted_user = json.load(f)
            except Exception:
                persisted_user = {}

        persisted_sudo_pw = ""
        if persisted_user.get("sudo_password"):
            persisted_sudo_pw = _decrypt_secret(str(persisted_user["sudo_password"]))

        for k in DEFAULT_CONFIG:
            if k == "sudo_password":
                continue
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
            except Exception:
                pass

        panel_cfg: Dict[str, Any] = {}
        panel_path = self._find_panel_config_path()
        if panel_path:
            try:
                with open(panel_path, "r", encoding="utf-8-sig") as f:
                    panel_cfg = json.load(f)
                if not isinstance(panel_cfg, dict):
                    panel_cfg = {}
            except Exception:
                panel_cfg = {}

        merged = dict(config)
        for k, v in obj_cfg.items():
            if k == "sudo_password":
                continue
            merged[k] = v
        for k, v in panel_cfg.items():
            if k == "sudo_password":
                continue
            if k in DEFAULT_CONFIG and v is not None:
                merged[k] = v

        final = DEFAULT_CONFIG.copy()
        for k in DEFAULT_CONFIG:
            if k == "sudo_password":
                continue
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

        if persisted_sudo_pw:
            final["sudo_password"] = persisted_sudo_pw
        else:
            panel_sudo_pw = ""
            for source in (panel_cfg, obj_cfg):
                v = source.get("sudo_password")
                if v and str(v).strip():
                    panel_sudo_pw = str(v).strip()
                    break
            if panel_sudo_pw:
                final["sudo_password"] = panel_sudo_pw

        to_save = dict(final)
        to_save["__config_version__"] = CONFIG_VERSION
        if to_save.get("sudo_password"):
            to_save["sudo_password"] = _encrypt_secret(to_save["sudo_password"])
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
            if to_save.get("sudo_password"):
                to_save["sudo_password"] = _encrypt_secret(to_save["sudo_password"])
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save config failed: {e}")

    def _status_text(self) -> str:
        s = self.syncer.status_dict()
        lines = ["NapCat_Go Status", "-" * 20, f"Platform: {s['platform']}"]
        lines.append(f"deploy_mode: {self.deploy_mode}")
        if IS_LINUX:
            lines.append(f"distro: {_detect_distro_family()}")
            lines.append(f"QQ version: {get_linux_qq_version() or '(未安装)'}")
            lines.append(f"QQ build: {get_linux_qq_build_code()}")
            lines.append(
                f"QQ compatible: {'yes' if is_linux_qq_version_compatible() else 'NO'} "
                f"(需要 build ∈ [{NAPCAT_SUPPORTED_QQ_MIN}, {NAPCAT_SUPPORTED_QQ_MAX}])"
            )
            try:
                root = self.manager._find_new_style_root()
                lines.append(f"new_style_root: {root or '(未找到)'}")
            except Exception:
                pass
        lines.append(f"NapCat: {'running' if self.manager.is_running() else 'stopped'}")
        lines.append(f"QQ login: {'yes' if self.manager._qq_logged_in else 'no'}")
        lines.append(f"NapCat dir: {self.manager.napcat_dir}")
        return "\n".join(lines)

    async def terminate(self):
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

    @property
    def plugin_dir(self) -> Path:
        if hasattr(self.context, "plugin_dir"):
            return Path(self.context.plugin_dir)
        return Path(__file__).parent
#（注：内容由AI生成）