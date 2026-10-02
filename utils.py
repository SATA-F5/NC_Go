import os
import re
import ssl
import json
import time
import base64
import hashlib
import platform
import subprocess
import shutil
import signal
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Set, List, Tuple

from astrbot.api import logger

PLUGIN_NAME = "pulid_napcat_go_to_astrbot"
CONFIG_VERSION = 4

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

# ============ 默认基础配置 ============
DEFAULT_NAPCAT_JSON = {
    "fileLog": False, "consoleLog": True,
    "fileLogLevel": "debug", "consoleLogLevel": "info",
    "packetBackend": "auto", "packetServer": "", "o3HookMode": 1,
    "bypass": {
        "hook": False, "window": False, "module": False,
        "process": False, "container": False, "js": False
    }
}

DEFAULT_NAPCAT_ACCOUNT_JSON = {**DEFAULT_NAPCAT_JSON, "autoTimeSync": True}

DEFAULT_NAPCAT_PROTOCOL_JSON = {
    "enable": False,
    "network": {
        "httpServers": [], "websocketServers": [], "websocketClients": []
    }
}

DEFAULT_WEBUI_JSON = {
    "host": "::", "port": 6099, "token": "5503af8581de",
    "loginRate": 10, "autoLoginAccount": "", "disableWebUI": False,
    "accessControlMode": "none", "ipWhitelist": [], "ipBlacklist": [],
    "enableXForwardedFor": False, "enable2FA": False, "totpSecret": ""
}

DEFAULT_ONEBOT11_TEMPLATE = {
    "network": {
        "httpServers": [], "httpSseServers": [], "httpClients": [],
        "websocketServers": [], "websocketClients": [], "plugins": []
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
    "imageDownloadProxy": "",
    "timeout": {
        "baseTimeout": 10000, "uploadSpeedKBps": 256,
        "downloadSpeedKBps": 256, "maxTimeout": 1800000
    }
}

NAPCAT_SUPPORTED_QQ_MIN = 28498
NAPCAT_SUPPORTED_QQ_MAX = 999999
RECOMMENDED_LINUXQQ_VERSION = "3.2.34"

LINUXQQ_DEB_URLS = [
    "https://github.com/SATA-F5/NC_Go/releases/download/data-v0.0.0/QQ_3.2.34_Linux_amd64.deb",
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
    "auto_sync": True, "auto_start": True, "napcat_port": 6099,
    "napcat_version": DEFAULT_NAPCAT_VERSION,
    "napcat_download_mirror": DEFAULT_DOWNLOAD_MIRROR,
    "napcat_config_dir": "", "astrbot_config_path": "", "qq_number": "",
    "deploy_mode": DEFAULT_DEPLOY_MODE,
    "docker_image": DEFAULT_DOCKER_IMAGE,
    "docker_tag": DEFAULT_DOCKER_TAG,
    "docker_container_name": DEFAULT_DOCKER_CONTAINER,
    "sudo_password": "",
}

CREATE_NO_WINDOW = 0x08000000
REMNANT_PATTERN = "napcat|NapCat|Xvfb|launcher.sh|/opt/QQ"
REMNANT_SCAN_RE = re.compile(r"napcat|NapCat|Xvfb|launcher\.sh|/opt/QQ", re.IGNORECASE)

# 从 NapCat 日志里抓 QQ 号
QQ_LOGIN_ID_RE = re.compile(
    r"(?:快速登录成功|登录成功|Login\s+Success|login\s+success)[：:\s]*(\d{5,12})",
    re.IGNORECASE,
)

_ASCII_ART_CHARS = set("┌┐└┘─│█║═╔╗╚╝")
_INSTALL_KEEP_SUBSTR = (
    "失败", "错误", "警告", "error", "Error", "ERROR", "failed", "Failed",
    "无法", "No such", "not found", "未找到", "安装完成", "安装失败",
    "成功", "完成", "开始", "下载", "解压", "拷贝", "安装到", "安装目录",
    "✔", "✘", "launcher", "launcher.sh", "Xvfb", "sudo", "密码", "chown",
    "qq", "command not found", "文件已存在", "重命名",
)
_TS_INFO = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d\d:\d\d:\d\d\]\s*:")
_SECRET_SALT = b"ncgo-sudo-secret-v1"


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


def get_linux_qq_version() -> Optional[str]:
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
    return _parse_build_from_version(get_linux_qq_version())


def is_linux_qq_version_compatible() -> bool:
    code = get_linux_qq_build_code()
    if code is None:
        return False
    return NAPCAT_SUPPORTED_QQ_MIN <= code <= NAPCAT_SUPPORTED_QQ_MAX


def probe_deb_version(deb_path: Path) -> Optional[str]:
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
            out = subprocess.check_output(["pgrep", "-f", name],
                                          stderr=subprocess.DEVNULL, timeout=10).decode()
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
