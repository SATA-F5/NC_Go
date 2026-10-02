import asyncio
import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger

from .utils import (
    IS_WINDOWS, IS_LINUX,
    NAPCAT_RELEASE_BASE, NAPCAT_INSTALL_SH_ORIGINAL, NAPCAT_INSTALL_SH_LEGACY,
    DEFAULT_NAPCAT_VERSION,
    NAPCAT_MIRROR_CANDIDATES,
    LINUXQQ_DEB_URLS,
    NAPCAT_SUPPORTED_QQ_MIN, NAPCAT_SUPPORTED_QQ_MAX,
    RECOMMENDED_LINUXQQ_VERSION,
    make_ssl_context, _filter_install_output,
    get_linux_qq_version, get_linux_qq_build_code,
    is_linux_qq_version_compatible, is_qq_installed,
    is_deb_build_compatible,
    DOCKER_WEBUI_PORT,
)


class DownloadMixin:
    """提供 NapCat / LinuxQQ 的下载与安装能力。

    依赖宿主类的以下属性/方法：
    - self.plugin            : NapCatGoPlugin
    - self.napcat_dir        : Path
    - self.plugin_dir        : Path
    - self.log_lines         : deque
    - self.download_state    : dict
    - self._reset_download_state()
    - self._is_new_style_install(d)
    - self._find_entry()
    - self._find_new_style_root()
    - self._build_fake_sudo(tag, env)
    """

    # ---------- 通用下载 ----------

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
                        self.log_lines.append(f"[qq-install] {progress_label} HTTP {resp.status}")
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

    # ---------- NapCat 下载（Windows 专用） ----------

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
        if getattr(self, "_downloading", False):
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

    # ---------- LinuxQQ ----------

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
            **os.environ, "TERM": "dumb", "NO_COLOR": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }

        deb_path = self.napcat_dir / "linuxqq.deb"
        downloaded = False

        manual = list(self.napcat_dir.glob("linuxqq*.deb")) + \
                 list(self.napcat_dir.glob("QQ*.deb"))
        if manual:
            for m in manual:
                if not await self._validate_deb(m):
                    self.log_lines.append(f"[qq-install] ⚠ 本地 deb 无效: {m}，忽略")
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
                self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] 尝试 ({label}): {short}")
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
                    url, deb_path, connect_timeout=15, read_timeout=60,
                    progress_label=f"LinuxQQ ({idx}/{total_urls})",
                    progress_index=idx, progress_total=total_urls, max_time=900,
                )
                if not ok:
                    continue
                if not deb_path.exists() or deb_path.stat().st_size < 1024:
                    self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] ✘ 文件过小")
                    continue
                if not await self._validate_deb(deb_path):
                    self.log_lines.append(f"[qq-install] [{idx}/{total_urls}] ✘ 不是有效 deb，换下一个")
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
                            f"跳过: version={ver_str} build={build_code}"
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
            self.log_lines.append("[qq-install] 手动下载地址（浏览器打开，任选其一）：")
            self.log_lines.append("[qq-install]   https://github.com/SATA-F5/NC_Go/releases")
            self.log_lines.append("[qq-install]   https://github.com/Rodert/qq-versions/releases")
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
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
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
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
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
                self.log_lines.append("[qq-install] dpkg 返回非 0，执行 apt-get -f install -y 补依赖 ...")
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "apt-get", "-f", "install", "-y",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
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
                self.log_lines.append("[qq-install] ✘ 安装后仍未检测到 /opt/QQ/qq")
                self.download_state["phase"] = "failed"
                self.download_state["downloading"] = False
                self.download_state["message"] = "LinuxQQ 安装失败"
                return False
        finally:
            if fake_bin and fake_bin.exists():
                shutil.rmtree(fake_bin, ignore_errors=True)

    # ---------- install.sh ----------

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
            self.log_lines.append(f"[install] {inner} 已包含 launcher，跳过备份")
            return True

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = self.napcat_dir / f"napcat.bak.{ts}"
        try:
            shutil.move(str(inner), str(bak))
            self.log_lines.append(
                f"[install] ⚠ 检测到 {inner} 已存在且不为空，已备份为 {bak.name}"
            )
            return True
        except Exception as e:
            self.log_lines.append(f"[install] ⚠ 备份 {inner} 失败: {e}")
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
                    self.log_lines.append(f"[install] [{idx}/{len(sh_urls)}] {label} ✘ 文件异常")
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
            **os.environ, "TERM": "dumb", "NO_COLOR": "1",
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
