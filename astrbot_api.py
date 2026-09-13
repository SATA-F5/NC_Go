"""
astrbot_api.py
--------------
AstrBot 自身 HTTP API 的轻量封装。
用于在 NapCat 反向 WS 配置完成后，自动在 AstrBot 中创建 aiocqhttp 机器人实例。
"""

from typing import Optional, Dict, Any, List

import aiohttp
from astrbot.api import logger


class AstrBotAPI:
    """AstrBot HTTP API 客户端"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:6185",
        api_key: str = "",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Optional[Dict[str, Any]] = None,
        retries: int = 1,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        session = await self._get_session()
        for attempt in range(retries + 1):
            try:
                async with session.request(
                    method.upper(), url, json=json_data, headers=headers
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    elif resp.status == 403:
                        logger.error("AstrBot API 返回 403，请检查 API Key scope")
                        return None
                    else:
                        text = await resp.text()
                        logger.warning(
                            f"AstrBot API {path} 返回 {resp.status}: {text[:200]}"
                        )
                        return None
            except aiohttp.ClientError as e:
                if attempt < retries:
                    continue
                logger.error(f"AstrBot API {path} 请求异常: {e}")
                return None
            except Exception as e:
                logger.error(f"AstrBot API {path} 未知异常: {e}")
                return None
        return None

    async def list_bots(self) -> Optional[List[Dict[str, Any]]]:
        resp = await self._request("GET", "/api/v1/bots")
        if resp and isinstance(resp, dict):
            return resp.get("data") or resp.get("bots") or []
        return None

    async def create_bot(
        self, platform: str, name: str, config: Dict[str, Any], enable: bool = True
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "platform": platform,
            "name": name,
            "config": config,
            "enable": enable,
        }
        return await self._request("POST", "/api/v1/bots", json_data=payload)

    async def update_bot(
        self, bot_id: str, config: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        return await self._request(
            "PUT", f"/api/v1/bots/{bot_id}", json_data={"config": config}
        )

    async def delete_bot(self, bot_id: str) -> Optional[Dict[str, Any]]:
        return await self._request("DELETE", f"/api/v1/bots/{bot_id}")