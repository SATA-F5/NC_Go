import asyncio
import json
import os
import platform
import shutil
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request

try:
    from astrbot.api import AstrBotConfig
except ImportError:
    AstrBotConfig = dict

from .utils import (
    PLUGIN_NAME, CONFIG_VERSION, DEFAULT_CONFIG,
    IS_WINDOWS, IS_LINUX,
    _mask_token, _encrypt_secret, _decrypt_secret,
    _detect_distro_family, detect_docker_binary,
    resolve_deploy_mode, resolve_persistent_dir,
    get_linux_qq_version, get_linux_qq_build_code,
    is_linux_qq_version_compatible, is_qq_installed,
    NAPCAT_SUPPORTED_QQ_MIN, NAPCAT_SUPPORTED_QQ_MAX,
    RECOMMENDED_LINUXQQ_VERSION,
)
from .napcat_manager import NapCatManager
from .config_syncer import ConfigSyncer

PLUGIN_CODE_VERSION = "2026-10-02-v75-modular"


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
            self.config.get("deploy_mode", "auto"),
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

    # ---------- API ----------

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
        d["napcat_qq_number"] = self.manager.napcat_qq_number
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
            d["config_dir"] = str(self.manager._get_config_dir())
        except Exception:
            d["new_style_root"] = None
            d["config_dir"] = None

        qq_logged_in = self.manager._qq_logged_in
        qq_user_id = None
        qq_nickname = ""
        if d["running"] and self.manager.api:
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
                        if not self.manager.napcat_qq_number:
                            self.manager.napcat_qq_number = str(user_id)
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
        deploy_mode = str(payload.get("deploy_mode", "") or "").strip().lower() or self.config.get("deploy_mode", "auto")
        if deploy_mode not in {"auto", "windows", "docker", "native"}:
            deploy_mode = "auto"
        self.config.update({
            "napcat_config_dir": str(payload.get("napcat_config_dir", "") or "").strip(),
            "astrbot_config_path": str(payload.get("astrbot_config_path", "") or "").strip(),
            "qq_number": str(payload.get("qq_number", "") or "").strip(),
            "deploy_mode": deploy_mode,
            "docker_image": str(payload.get("docker_image", self.config.get("docker_image", "mlikiowa/napcat-docker")) or "").strip(),
            "docker_tag": str(payload.get("docker_tag", self.config.get("docker_tag", "latest")) or "").strip(),
            "docker_container_name": str(payload.get("docker_container_name", self.config.get("docker_container_name", "napcat-go")) or "").strip(),
            "napcat_download_mirror": str(payload.get("napcat_download_mirror", self.config.get("napcat_download_mirror", "https://gh.zwy.one/"))),
        })
        new_pw = str(payload.get("sudo_password", "") or "")
        if new_pw.strip():
            self.config["sudo_password"] = new_pw.strip()
        self.deploy_mode = resolve_deploy_mode(
            self.config.get("deploy_mode", "auto"),
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

    # ---------- config ----------

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
                lines.append(f"config_dir: {self.manager._get_config_dir()}")
            except Exception:
                pass
        lines.append(f"QQ number (cached): {self.manager.napcat_qq_number or '(未识别)'}")
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
