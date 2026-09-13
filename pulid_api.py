"""
pulid_api.py
------------
NapCat (NapNeko) OneBot HTTP API 的轻量封装。
NapCat 运行后，通过 OneBot 标准接口与之交互。
"""

from typing import Optional, Dict, Any

import aiohttp
from astrbot.api import logger


class NapCatAPI:
    """NapCat OneBot HTTP API 客户端"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6099,
        token: str = "",
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    def update_endpoint(self, host: Optional[str] = None, port: Optional[int] = None):
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port

    def set_token(self, token: str):
        self.token = token or ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        session = await self._get_session()
        try:
            async with session.request(
                method.upper(), url, params=params, json=json_data, headers=headers
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning(f"NapCat API {path} 返回状态码 {resp.status}")
                return None
        except Exception as e:
            logger.error(f"请求 NapCat API 异常: {e}")
            return None

    async def get_login_info(self) -> Optional[Dict[str, Any]]:
        return await self.request("POST", "/get_login_info")

    async def get_status(self) -> Optional[Dict[str, Any]]:
        return await self.request("POST", "/get_status")

    async def ping(self) -> bool:
        try:
            session = await self._get_session()
            async with session.post(f"{self.base_url}/get_status") as resp:
                return resp.status == 200
        except Exception:
            return False