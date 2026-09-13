import asyncio
import json
import re
import socket
import random
import platform
import zipfile
import shutil
import subprocess
import webbrowser
import ssl
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Any, Set

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from astrbot.api import AstrBotConfig
except ImportError:
    AstrBotConfig = dict  # 兼容旧版

from .pulid_api import NapCatAPI
from .astrbot_api import AstrBotAPI

# 尝试导入 certifi，用于修复 SSL 证书验证失败问题
try:
    import certifi
    _HAS_CERTIFI = True
except ImportError:
    _HAS_CERTIFI = False

PLUGIN_NAME = "pulid_napcat_go_to_astrbot"
NAPCAT_RELEASE_BASE = "https://github.com/NapNeko/NapCatQQ/releases/download"
DEFAULT_NAPCAT_VERSION = "v4.17.32"
DEFAULT_DOWNLOAD_MIRROR = "https://ghfast.top/"

DEFAULT_CONFIG = {
    "reverse_ws_host": "127.0.0.1",
    "reverse_ws_port_min": 6100,
    "reverse_ws_port_max": 6200,
    "reverse_ws_token": "",
    "napcat_port": 6099,
    "auto_start": False,
    "auto_restart": True,
    "astrbot_api_key": "",
    "astrbot_api_base": "http://127.0.0.1:6185",
    "qq_number": "",
    "napcat_version": DEFAULT_NAPCAT_VERSION,
    "napcat_download_mirror": DEFAULT_DOWNLOAD_MIRROR,
}

CREATE_NO_WINDOW = 0x08000000


# ==================== SSL Context ====================

def make_ssl_context() -> ssl.SSLContext:
    """
    构造一个用于 aiohttp 的 SSLContext。
    优先使用 certifi 提供的 CA bundle；若 certifi 不可用，则降级为不校验证书（打印警告）。
    """
    if _HAS_CERTIFI:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception as e:
            logger.warning(f"使用 certifi 构造 SSLContext 失败: {e}，降级为不校验")

    logger.warning(
        "未安装 certifi，HTTPS 证书验证将被禁用（存在安全风险，建议 pip install certifi）"
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ==================== QQ 检测 ====================

def is_qq_installed() -> bool:
    """检测 Windows 是否安装了 QQNT"""
    if platform.system() != "Windows":
        return True

    try:
        import winreg
    except ImportError:
        return True

    key_paths = [
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
    ]

    for path in key_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                uninstall_str, _ = winreg.QueryValueEx(key, "UninstallString")
                if uninstall_str:
                    return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False


# ==================== 进程工具 ====================

def list_pids_by_name(name: str) -> Set[int]:
    """列出所有匹配名称的进程 PID（Windows，其它平台返回空集）"""
    if platform.system() != "Windows":
        return set()

    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        ).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"tasklist {name} 失败: {e}")
        return set()

    pids: Set[int] = set()
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
    return pids


def kill_pid_tree(pid: int):
    """按进程树强杀（Windows）"""
    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
        logger.info(f"已强杀进程树 PID={pid}")
    except Exception as e:
        logger.warning(f"taskkill PID {pid} 失败: {e}")


def kill_pids(pids: Set[int]):
    for pid in pids:
        kill_pid_tree(pid)


# ==================== NapCat 管理器 ====================

class NapCatManager:
    """NapCat 进程与反向 WS 管理器"""

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
        self.reverse_ws_port: Optional[int] = None
        self._log_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._downloading = False

        self._pre_existing_qq_pids: Set[int] = set()
        self._launched_pids: Set[int] = set()

        self.api = NapCatAPI(host="127.0.0.1", port=self.napcat_port, token="")
        self.astrbot_api = AstrBotAPI(
            base_url=self.plugin.config.get(
                "astrbot_api_base", "http://127.0.0.1:6185"
            ),
            api_key=self.plugin.config.get("astrbot_api_key", ""),
        )
        self._created_bot_id: Optional[str] = None

        self.log_lines: deque = deque(maxlen=500)

    # ---------- 工具 ----------

    def _find_entry(self) -> Optional[str]:
        """检测 NapCat 是否已解压"""
        candidates = [
            "launcher-win10-user.bat",
            "launcher-win10.bat",
            "launcher.bat",
            "launcher-user.bat",
            "napcat.bat",
            "NapCatWinBootMain.exe",
            "napcat.mjs",
            "index.js",
        ]
        for name in candidates:
            if (self.napcat_dir / name).exists():
                return name
        return None

    def _refresh_launched_pids(self):
        if platform.system() != "Windows":
            return
        try:
            current_qq = list_pids_by_name("QQ.exe")
            self._launched_pids.update(current_qq - self._pre_existing_qq_pids)
            self._launched_pids.update(list_pids_by_name("NapCatWinBootMain.exe"))
        except Exception as e:
            logger.warning(f"刷新进程 PID 失败: {e}")

    def _cleanup_windows_processes(self):
        if platform.system() != "Windows":
            return

        self._refresh_launched_pids()
        if self._launched_pids:
            logger.info(f"清理启动进程 PID: {self._launched_pids}")
            kill_pids(self._launched_pids)
            self._launched_pids.clear()

        leftover_boot = list_pids_by_name("NapCatWinBootMain.exe")
        if leftover_boot:
            logger.info(f"清理残留 NapCatWinBootMain.exe: {leftover_boot}")
            kill_pids(leftover_boot)

    # ---------- 下载与解压 ----------

    async def _download_napcat(self) -> bool:
        if self._downloading:
            logger.warning("NapCat 正在下载中，请稍候")
            return False

        self._downloading = True
        try:
            version = self.plugin.config.get("napcat_version", DEFAULT_NAPCAT_VERSION)
            filename = "NapCat.Shell.zip"

            # 1) 本地文件优先
            local_zip = self.plugin_dir / filename
            if local_zip.exists():
                logger.info(f"检测到本地压缩包，直接解压: {local_zip}")
                self._append_log(f"检测到本地压缩包 {filename}，跳过下载")
                return await self._extract_napcat(local_zip)

            # 2) 构造下载 URL 列表：镜像优先，官方兜底
            mirror = (self.plugin.config.get("napcat_download_mirror") or "").strip()
            official = f"{NAPCAT_RELEASE_BASE}/{version}/{filename}"
            urls = []
            if mirror:
                urls.append((f"{mirror.rstrip('/')}/{official}", "镜像"))
            urls.append((official, "官方"))

            download_path = self.plugin_dir / filename

            for url, label in urls:
                logger.info(f"尝试从{label}下载: {url}")
                self._append_log(f"正在从{label}下载 NapCat {version}...")

                # 每一轮都用独立的 SSLContext + Connector + Session
                ssl_ctx = make_ssl_context()
                connector = aiohttp.TCPConnector(ssl=ssl_ctx)
                session = aiohttp.ClientSession(connector=connector)

                try:
                    async with session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(
                                total=None, sock_connect=15, sock_read=120
                            ),
                        ) as resp:
                            if resp.status != 200:
                                logger.warning(f"{label} 返回 HTTP {resp.status}")
                                self._append_log(f"{label} 返回 HTTP {resp.status}")
                                continue

                            total = resp.content_length or 0
                            downloaded = 0
                            last_percent = -1
                            with open(download_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 256):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if total > 0:
                                        percent = downloaded * 100 // total
                                        if percent != last_percent and percent % 20 == 0:
                                            last_percent = percent
                                            logger.info(f"下载进度：{percent}%")

                    logger.info(f"下载完成：{download_path}")
                    self._append_log("NapCat 下载完成，正在解压...")
                    success = await self._extract_napcat(download_path)
                    if success:
                        try:
                            download_path.unlink()
                        except Exception:
                            pass
                        self._append_log("NapCat 解压完成")
                    return success

                except Exception as e:
                    logger.warning(f"{label} 下载失败: {e}")
                    self._append_log(f"{label} 下载失败: {e}")
                    continue

            # 3) 全部失败
            logger.error(
                "所有下载源均失败。请手动下载 NapCat.Shell.zip 放到插件目录下，"
                "然后点「重新下载 NapCat」或「启动」。"
            )
            self._append_log(
                "所有下载源均失败。请手动下载 NapCat.Shell.zip 放入插件目录。"
            )
            return False
        finally:
            self._downloading = False

    async def _extract_napcat(self, archive_path: Path) -> bool:
        """
        解压 NapCat.Shell.zip 到 self.napcat_dir。
        压缩包根目录就是 launcher.bat 等文件，直接解压即可。
        """
        try:
            self.napcat_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(self.napcat_dir)

            if not self._find_entry():
                logger.warning(
                    f"解压后未在 {self.napcat_dir} 找到 NapCat 入口文件，"
                    f"目录内容: {[p.name for p in self.napcat_dir.iterdir()][:20]}"
                )
                return False
            logger.info(f"解压完成，入口文件: {self._find_entry()}")
            return True
        except Exception as e:
            logger.error(f"解压 NapCat 失败: {e}")
            return False

    # ---------- 生命周期 ----------

    async def start(self):
        async with self._lock:
            if self.process and self.process.returncode is None:
                logger.warning("NapCat 已在运行中")
                return

            if not is_qq_installed():
                logger.error("未检测到 QQ 客户端。请安装 QQNT 9.9.27+ 后再启动。")
                return

            if not self._find_entry():
                logger.info("未检测到 NapCat，开始自动下载...")
                success = await self._download_napcat()
                if not success:
                    logger.error("NapCat 下载失败")
                    return

            system = platform.system()

            if system == "Windows":
                self._pre_existing_qq_pids = list_pids_by_name("QQ.exe")
                logger.info(f"启动前已有 QQ.exe PIDs: {self._pre_existing_qq_pids}")
                self._launched_pids.clear()

            if system == "Windows":
                launcher = self.napcat_dir / "launcher-win10-user.bat"
                if not launcher.exists():
                    for name in ["launcher-win10.bat", "launcher.bat", "launcher-user.bat"]:
                        candidate = self.napcat_dir / name
                        if candidate.exists():
                            launcher = candidate
                            break
                    else:
                        logger.error(f"未找到任何 launcher 脚本: {self.napcat_dir}")
                        return

                cmd = ["cmd", "/c", str(launcher.name)]
                logger.info(f"启动 NapCat (Windows): {' '.join(cmd)}")
            else:
                entry = self._find_entry()
                if not entry:
                    logger.error("未找到 NapCat 入口文件")
                    return
                cmd = ["node", entry]
                logger.info(f"启动 NapCat (Unix): {' '.join(cmd)}")

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self.napcat_dir),
                    creationflags=CREATE_NO_WINDOW if system == "Windows" else 0,
                )
            except Exception as e:
                logger.error(f"启动 NapCat 失败: {e}")
                return

            self._log_task = asyncio.create_task(self._read_logs())

            if system == "Windows":
                await asyncio.sleep(2)
                self._refresh_launched_pids()
                logger.info(f"本次启动新增 PID: {self._launched_pids}")

    async def stop(self):
        async with self._lock:
            if self.process and self.process.returncode is None:
                logger.info("正在停止 NapCat...")
                try:
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("顶层进程未在 5 秒内退出，尝试强杀")
                    try:
                        self.process.kill()
                        await self.process.wait()
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"terminate 异常: {e}")
                self.process = None

            if self._log_task and not self._log_task.done():
                self._log_task.cancel()
                try:
                    await self._log_task
                except asyncio.CancelledError:
                    pass
                self._log_task = None

            if platform.system() == "Windows":
                self._cleanup_windows_processes()
                await asyncio.sleep(1.5)

            self.napcat_webui_url = ""
            self.napcat_token = ""
            logger.info("NapCat 已停止")

    async def restart(self):
        await self.stop()
        await asyncio.sleep(1)
        await self.start()

    # ---------- 日志与 token ----------

    def _append_log(self, text: str):
        self.log_lines.append(text)

    async def _read_logs(self):
        assert self.process and self.process.stdout

        webui_url_pattern = re.compile(
            r"WebUi\s+User\s+Panel\s+Url:\s*(https?://\S+)",
            re.IGNORECASE,
        )
        token_pattern = re.compile(
            r"(?:token|access[_-]?token|WebUi\s*Token|WebUI\s*Token)[=:\s]+([A-Za-z0-9\-_]+)",
            re.IGNORECASE,
        )

        def _is_usable_url(url: str) -> bool:
            return "[::]" not in url and "0.0.0.0" not in url

        while True:
            try:
                line = await self.process.stdout.readline()
            except (asyncio.CancelledError, RuntimeError):
                break
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                logger.info(f"[NapCat] {text}")
                self._append_log(text)

            m_url = webui_url_pattern.search(text)
            if m_url:
                url = m_url.group(1).rstrip("，。,.")

                if not _is_usable_url(url):
                    logger.info(f"忽略通配 WebUI URL: {url}")
                    token_match = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if token_match and not self.napcat_token:
                        self.napcat_token = token_match.group(1)
                        self.api.set_token(self.napcat_token)
                    continue

                if url != self.napcat_webui_url:
                    self.napcat_webui_url = url
                    token_match = re.search(r"[?&]token=([A-Za-z0-9\-_]+)", url)
                    if token_match:
                        self.napcat_token = token_match.group(1)
                        self.api.set_token(self.napcat_token)
                    logger.info(f"提取到 NapCat WebUI URL: {url}")

                    if (
                        self.plugin.config.get("auto_restart", True)
                        and not self.reverse_ws_port
                    ):
                        await self.configure_reverse_ws()
                continue

            if not self.napcat_webui_url:
                m_token = token_pattern.search(text)
                if m_token and m_token.group(1) != self.napcat_token:
                    self.napcat_token = m_token.group(1)
                    self.api.set_token(self.napcat_token)
                    logger.info(f"提取到 NapCat WebUI token: {self.napcat_token[:4]}...")

        if self.process:
            logger.info(f"NapCat 进程已退出，返回码: {self.process.returncode}")
            self.process = None

    # ---------- 反向 WS 配置 ----------

    async def configure_reverse_ws(self):
        min_port = self.plugin.config.get("reverse_ws_port_min", 6100)
        max_port = self.plugin.config.get("reverse_ws_port_max", 6200)
        port = await self._select_available_port(min_port, max_port)
        if not port:
            logger.error("无法在指定端口范围内找到可用端口")
            return
        self.reverse_ws_port = port

        host = self.plugin.config.get("reverse_ws_host", "127.0.0.1")
        token = self.plugin.config.get("reverse_ws_token", "")
        path = "/ws/"

        await self._ensure_astrbot_bot(host, port, path, token)
        await self._update_astrbot_adapter(host, port, path, token)
        await self._write_napcat_config(host, port, path, token)

    async def _write_napcat_config(self, host: str, port: int, path: str, token: str):
        config_dir = self.napcat_dir / "config"
        if not config_dir.exists():
            logger.warning(f"未找到 NapCat 配置目录: {config_dir}")
            return

        candidates = list(config_dir.glob("onebot11_*.json")) + list(config_dir.glob("onebot11.json"))
        if not candidates:
            logger.warning(f"未在 {config_dir} 中找到 onebot11*.json")
            return

        config_file = max(candidates, key=lambda p: p.stat().st_mtime)
        ws_url = f"ws://{host}:{port}{path}"

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                napcat_config = json.load(f)

            network = napcat_config.setdefault("network", {})
            ws_clients = network.setdefault("websocketClients", [])

            if any(c.get("url") == ws_url for c in ws_clients):
                logger.info(f"反向 WS 配置已存在: {ws_url}")
            else:
                ws_clients.append({"enable": True, "url": ws_url, "token": token})
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(napcat_config, f, indent=2, ensure_ascii=False)
                logger.info(f"已写入反向 WS 配置: {ws_url}，重启 NapCat 后生效")
                await self.restart()
        except Exception as e:
            logger.error(f"修改 NapCat 配置文件失败: {e}")

    async def _ensure_astrbot_bot(self, host: str, port: int, path: str, token: str) -> bool:
        astrbot_api_key = self.plugin.config.get("astrbot_api_key", "")
        if not astrbot_api_key:
            logger.warning("未配置 AstrBot API Key，跳过机器人实例创建")
            return False

        bot_config = {
            "enable_ws_reverse": True,
            "ws_reverse_host": host,
            "ws_reverse_port": port,
            "ws_reverse_path": path,
            "access_token": token,
            "enable": True,
        }

        if self._created_bot_id:
            resp = await self.astrbot_api.update_bot(self._created_bot_id, bot_config)
            if resp:
                logger.info(f"已更新 AstrBot 机器人实例: {self._created_bot_id}")
                return True
            self._created_bot_id = None

        resp = await self.astrbot_api.create_bot(
            platform="aiocqhttp",
            name=f"NapCat_Go_{self.napcat_port}",
            config=bot_config,
            enable=True,
        )
        if resp:
            bot_id = resp.get("id") or resp.get("bot_id") or resp.get("data", {}).get("id")
            if bot_id:
                self._created_bot_id = str(bot_id)
            logger.info("AstrBot 机器人实例创建成功")
            return True
        logger.error("AstrBot 机器人实例创建失败")
        return False

    async def _update_astrbot_adapter(self, host: str, port: int, path: str, token: str):
        config_data = {
            "type": "aiocqhttp",
            "enable_ws_reverse": True,
            "ws_reverse_host": host,
            "ws_reverse_port": port,
            "ws_reverse_path": path,
            "access_token": token,
            "enable": True,
        }

        if hasattr(self.context, "set_adapter_config"):
            try:
                await self.context.set_adapter_config("aiocqhttp", config_data)
                logger.info("已通过 AstrBot Context 更新 aiocqhttp 适配器配置")
                return
            except Exception as e:
                logger.warning(f"通过 Context 更新适配器失败: {e}")

        config_path = Path("data/config/aiocqhttp_config.json")
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(config_data, indent=2, ensure_ascii=False)
            )
            logger.info(f"已写入适配器配置文件: {config_path}")
        except Exception as e:
            logger.error(f"写入适配器配置失败: {e}")

    # ---------- 端口选择 ----------

    async def _select_available_port(self, min_port: int, max_port: int) -> Optional[int]:
        ports = list(range(min_port, max_port + 1))
        random.shuffle(ports)
        for port in ports:
            if self._is_port_available(port):
                return port
        return None

    @staticmethod
    def _is_port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False


# ==================== 插件主体 ====================

class NapCatGoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.astrbot_config = config
        self.config = self._load_config()
        self.manager = NapCatManager(self)

        context.register_web_api(
            f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "获取 NapCat 运行状态"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/start", self.api_start, ["POST"], "启动 NapCat"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/stop", self.api_stop, ["POST"], "停止 NapCat"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/restart", self.api_restart, ["POST"], "重启 NapCat"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "获取插件配置"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/config/save", self.api_save_config, ["POST"], "保存插件配置"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/logs", self.api_get_logs, ["GET"], "获取 NapCat 日志"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/download-napcat",
            self.api_download_napcat,
            ["POST"],
            "手动下载 NapCat",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/open-webui",
            self.api_open_webui,
            ["POST"],
            "在浏览器中打开 NapCat WebUI",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/open-qq-download",
            self.api_open_qq_download,
            ["POST"],
            "打开 QQ 下载页面",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/cleanup",
            self.api_cleanup,
            ["POST"],
            "强制清理 NapCat 残留进程",
        )

        @filter.command("napcat")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def napcat_status(event: AstrMessageEvent):
            yield event.plain_result(self._get_status_text())

        if self.config.get("auto_start", False):
            logger.info("[NapCat_Go] auto_start=True，准备自动启动 NapCat")
            asyncio.create_task(self.manager.start())

    # ---------- API ----------

    async def api_status(self):
        running = self.manager.process is not None and self.manager.process.returncode is None
        return json_response({
            "running": running,
            "napcat_installed": self.manager._find_entry() is not None,
            "qq_installed": is_qq_installed(),
            "napcat_port": self.manager.napcat_port,
            "napcat_token": self.manager.napcat_token,
            "napcat_webui_url": self.manager.napcat_webui_url,
            "reverse_ws_port": self.manager.reverse_ws_port,
            "reverse_ws_host": self.config.get("reverse_ws_host", "127.0.0.1"),
            "reverse_ws_token": self.config.get("reverse_ws_token", ""),
            "config": self.config,
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

    async def api_download_napcat(self):
        success = await self.manager._download_napcat()
        if success:
            return json_response({"ok": True, "message": "NapCat 下载并解压完成"})
        return error_response("NapCat 下载失败，请查看日志或手动放入压缩包")

    async def api_open_webui(self):
        url = self.manager.napcat_webui_url
        if not url:
            return error_response("WebUI 地址尚未获取，请先启动 NapCat 并等待日志输出")
        try:
            webbrowser.open(url)
            logger.info(f"已在浏览器打开: {url}")
            return json_response({"ok": True, "url": url})
        except Exception as e:
            logger.error(f"打开浏览器失败: {e}")
            return error_response(f"打开浏览器失败: {e}")

    async def api_open_qq_download(self):
        try:
            webbrowser.open("https://im.qq.com")
            return json_response({"ok": True})
        except Exception as e:
            logger.error(f"打开浏览器失败: {e}")
            return error_response(f"打开浏览器失败: {e}")

    async def api_cleanup(self):
        try:
            await self.manager.stop()
            self.manager._cleanup_windows_processes()
            return json_response({"ok": True, "message": "已清理残留进程"})
        except Exception as e:
            logger.error(f"清理失败: {e}")
            return error_response(f"清理失败: {e}")

    async def api_get_config(self):
        return json_response(self.config)

    async def api_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        try:
            min_port = int(payload.get("reverse_ws_port_min", 6100))
            max_port = int(payload.get("reverse_ws_port_max", 6200))
        except (TypeError, ValueError):
            return error_response("端口必须是整数")
        if min_port >= max_port:
            return error_response("最小端口必须小于最大端口")
        if min_port < 1 or max_port > 65535:
            return error_response("端口范围必须在 1-65535 之间")

        self.config.update({
            "reverse_ws_port_min": min_port,
            "reverse_ws_port_max": max_port,
            "reverse_ws_token": str(payload.get("reverse_ws_token", "")),
            "napcat_port": int(payload.get("napcat_port", 6099)),
            "astrbot_api_key": str(payload.get("astrbot_api_key", "")),
            "qq_number": str(payload.get("qq_number", "")),
            "napcat_version": str(
                payload.get("napcat_version", self.config.get("napcat_version", ""))
            ),
            "napcat_download_mirror": str(
                payload.get(
                    "napcat_download_mirror",
                    self.config.get("napcat_download_mirror", DEFAULT_DOWNLOAD_MIRROR),
                )
            ),
        })
        self._save_config(self.config)
        logger.info("配置已更新")
        return json_response({"ok": True, "config": self.config})

    async def api_get_logs(self):
        since = request.query.get("since", 0, type=int)
        lines = list(self.manager.log_lines)
        return json_response({
            "lines": lines[since:],
            "total": len(lines),
        })

    # ---------- 内部 ----------

    def _load_config(self) -> Dict[str, Any]:
        if hasattr(self.context, "get_data_dir"):
            data_dir = Path(self.context.get_data_dir())
        else:
            data_dir = Path("data")
        self.config_path = data_dir / f"{PLUGIN_NAME}_config.json"

        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    user_config = json.load(f)
                config = {**DEFAULT_CONFIG, **user_config}
            except Exception as e:
                logger.error(f"读取本地配置失败: {e}")
                config = DEFAULT_CONFIG.copy()
        else:
            config = DEFAULT_CONFIG.copy()
            self._save_config(config)

        # 从 AstrBot 配置面板读取 auto_start，覆盖本地配置
        try:
            if self.astrbot_config is not None:
                # AstrBotConfig 继承自 dict，可直接用 get
                if hasattr(self.astrbot_config, "get"):
                    astrbot_auto_start = self.astrbot_config.get("auto_start")
                else:
                    astrbot_auto_start = getattr(
                        self.astrbot_config, "auto_start", None
                    )

                if astrbot_auto_start is not None:
                    config["auto_start"] = bool(astrbot_auto_start)
                    logger.info(
                        f"[NapCat_Go] 从 AstrBot 配置读取 auto_start = {config['auto_start']}"
                    )
            else:
                logger.info("[NapCat_Go] 未收到 AstrBot 配置，使用本地配置")
        except Exception as e:
            logger.warning(f"读取 AstrBot 配置失败（auto_start 使用本地配置）: {e}")

        return config

    def _save_config(self, config: Dict[str, Any] = None):
        if config is None:
            config = self.config
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    async def terminate(self):
        logger.info("NapCat_Go 插件正在卸载，清理进程...")
        try:
            await self.manager.stop()
        except Exception as e:
            logger.error(f"停止 NapCat 失败: {e}")
            try:
                self.manager._cleanup_windows_processes()
            except Exception as e2:
                logger.error(f"强制清理失败: {e2}")

        try:
            await self.manager.api.close()
        except Exception:
            pass

        try:
            await self.manager.astrbot_api.close()
        except Exception:
            pass

        logger.info("NapCat_Go 插件已卸载")

    def _get_status_text(self) -> str:
        running = self.manager.process is not None and self.manager.process.returncode is None
        installed = self.manager._find_entry() is not None
        qq_ok = is_qq_installed()

        if running:
            status = "运行中"
        elif not qq_ok:
            status = "未安装 QQ"
        elif not installed:
            status = "未安装 NapCat"
        else:
            status = "已安装"

        token_masked = ""
        if self.manager.napcat_token:
            token_masked = (
                f"{self.manager.napcat_token[:4]}...{self.manager.napcat_token[-4:]}"
                if len(self.manager.napcat_token) > 8 else "****"
            )

        reverse_ws = (
            f"ws://{self.config.get('reverse_ws_host', '127.0.0.1')}:"
            f"{self.manager.reverse_ws_port or '未配置'}/ws/"
        )

        return (
            f"NapCat 状态：{status}\n"
            f"QQ 已安装：{'是' if qq_ok else '否'}\n"
            f"NapCat 已安装：{'是' if installed else '否'}\n"
            f"进程端口：{self.manager.napcat_port}\n"
            f"WebUI URL：{self.manager.napcat_webui_url or '未获取'}\n"
            f"反向 WS 地址：{reverse_ws}"
        )