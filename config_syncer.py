import json
import os
import re
import shutil
import subprocess
import platform
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from astrbot.api import logger

from .utils import (
    IS_WINDOWS, IS_LINUX, IS_MACOS,
    DEFAULT_ONEBOT11_TEMPLATE,
    _mask_token, reverse_ws_host,
    resolve_persistent_dir,
)


class ConfigSyncer:
    def __init__(self, plugin):
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
        candidates += [Path("data/cmd_config.json"),
                       Path("../data/cmd_config.json"),
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
        try:
            return self.plugin.manager._get_config_dir()
        except Exception:
            pass
        for base in [self.napcat_dir, self.napcat_dir / "napcat"]:
            for sub in [base / "config", base]:
                try:
                    if sub.exists() and sub.is_dir():
                        if sub.name == "config" or list(sub.glob("onebot11*.json")):
                            return sub
                except Exception:
                    continue
        return None

    def _resolve_qq_number(self) -> str:
        try:
            cached = getattr(self.plugin.manager, "napcat_qq_number", "") or ""
            if cached:
                return cached
        except Exception:
            pass

        qq = str(self.plugin.config.get("qq_number", "") or "").strip()
        if qq:
            return qq

        scan_dirs = [
            self.napcat_dir / "config",
            self.napcat_dir / "napcat" / "config",
            self.napcat_dir / "NapCat" / "config",
        ]
        if self.napcat_config_dir:
            scan_dirs.insert(0, self.napcat_config_dir)
        seen = set()
        for base in scan_dirs:
            try:
                if not base.exists() or not base.is_dir():
                    continue
                ap = base.resolve()
                if ap in seen:
                    continue
                seen.add(ap)
                for f in base.glob("napcat_*.json"):
                    m = re.match(r"napcat_(\d+)\.json$", f.name)
                    if m:
                        return m.group(1)
                for f in base.glob("onebot11_*.json"):
                    m = re.match(r"onebot11_(\d+)\.json$", f.name)
                    if m:
                        return m.group(1)
            except Exception:
                continue

        for base in scan_dirs:
            try:
                p = base / "webui.json"
                if p.exists():
                    with open(p, "r", encoding="utf-8-sig") as f:
                        d = json.load(f)
                    v = str(d.get("autoLoginAccount", "") or "").strip()
                    if v:
                        return v
            except Exception:
                continue
        return ""

    def _find_onebot11_file(self) -> Optional[Path]:
        if not self.napcat_config_dir or not self.napcat_config_dir.exists():
            return None
        try:
            qq = self._resolve_qq_number()
            if qq:
                return self.napcat_config_dir / f"onebot11_{qq}.json"
            cs = list(self.napcat_config_dir.glob("onebot11_*.json"))
            if cs:
                return max(cs, key=lambda p: p.stat().st_mtime)
            return self.napcat_config_dir / "onebot11.json"
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

    def _backup_file(self, path: Path) -> None:
        try:
            if path.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = path.with_suffix(path.suffix + f".bak.{ts}")
                shutil.copy2(path, bak)
                logger.info(f"[NapCat_Go] 备份 {path.name} -> {bak.name}")
        except Exception as e:
            logger.warning(f"[NapCat_Go] 备份 {path} 失败: {e}")

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

        config: Dict[str, Any]
        if self.napcat_config_file.exists():
            try:
                with open(self.napcat_config_file, "r", encoding="utf-8-sig") as f:
                    config = json.load(f)
                if not isinstance(config, dict):
                    config = dict(DEFAULT_ONEBOT11_TEMPLATE)
            except Exception as e:
                logger.warning(f"[NapCat_Go] 解析 {self.napcat_config_file} 失败: {e}，使用默认模板")
                config = dict(DEFAULT_ONEBOT11_TEMPLATE)
        else:
            self.napcat_config_dir.mkdir(parents=True, exist_ok=True)
            config = dict(DEFAULT_ONEBOT11_TEMPLATE)

        network = config.setdefault("network", {})
        if not isinstance(network, dict):
            network = {}
            config["network"] = network
        for key in ("httpServers", "httpSseServers", "httpClients",
                    "websocketServers", "websocketClients", "plugins"):
            if key not in network or not isinstance(network[key], list):
                network[key] = []

        config.setdefault("musicSignUrl", "")
        config.setdefault("enableLocalFile2Url", False)
        config.setdefault("parseMultMsg", False)
        config.setdefault("imageDownloadProxy", "")
        if "timeout" not in config or not isinstance(config["timeout"], dict):
            config["timeout"] = {
                "baseTimeout": 10000, "uploadSpeedKBps": 256,
                "downloadSpeedKBps": 256, "maxTimeout": 1800000
            }

        bot = self.astrbot_bot
        ws_host = reverse_ws_host(getattr(self.plugin, "deploy_mode", None), bot["host"])
        ws_url = f"ws://{ws_host}:{bot['port']}{bot['path']}"

        # ⚡ 关键修复：一次写全所有 SSL 相关字段，覆盖 NapCat 各版本读取的键名
        network["websocketClients"] = [{
            "enable": True,
            "name": "astrbot",
            "url": ws_url,
            "reportSelfMessage": False,
            "messagePostFormat": "array",
            "token": bot["token"],
            "debug": False,
            "heartInterval": 30000,
            "reconnectInterval": 3000,
            # SSL 关闭（各版本键名兼容）
            "verifyCertificate": False,   # 新版 NapCat
            "sslVerify": False,            # 旧版 NapCat
            "sslCertVerify": False,        # 兼容
            "ssl": False,                  # 关闭 wss
            "sslCert": "",
            "sslKey": "",
        }]

        self._backup_file(self.napcat_config_file)
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
