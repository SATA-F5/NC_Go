"""
pulid_api.py
------------
NapCat (NapNeko) OneBot HTTP API client.

Log policy:
- Success: debug level (only shown when verbose=True)
- Connection failure / non-200: warning by default, debug if quiet=True
- get_login_info / get_status use quiet=True because NapCat returns errors
  before QQ login, which is normal and should not be logged as warning.
"""

import asyncio
from typing import Optional, Dict, Any

import aiohttp
from astrbot.api import logger


class NapCatAPI:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6099,
        token: str = "",
        timeout: float = 10.0,
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.verbose = verbose
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
        quiet: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Send a request to NapCat.

        quiet=True: suppress warnings on non-200 / connection failures.
                    Use for endpoints where failure is expected
                    (e.g. get_login_info before QQ login).
        """
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        session = await self._get_session()
        try:
            async with session.request(
                method.upper(),
                url,
                params=params,
                json=json_data,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    if self.verbose:
                        logger.debug(f"NapCatAPI {method} {path} -> OK")
                    return await resp.json(content_type=None)

                # Non-200
                if not quiet:
                    logger.warning(f"NapCatAPI {method} {path} -> HTTP {resp.status}")
                elif self.verbose:
                    logger.debug(f"NapCatAPI {method} {path} -> HTTP {resp.status} (quiet)")
                return None

        except aiohttp.ClientConnectorError as e:
            if not quiet:
                logger.warning(f"NapCatAPI {method} {path} -> connection failed: {e}")
            elif self.verbose:
                logger.debug(f"NapCatAPI {method} {path} -> connection failed (quiet)")
            return None
        except asyncio.TimeoutError:
            if not quiet:
                logger.warning(f"NapCatAPI {method} {path} -> timeout")
            return None
        except Exception as e:
            if not quiet:
                logger.warning(f"NapCatAPI {method} {path} -> error: {e}")
            return None

    async def get_login_info(self) -> Optional[Dict[str, Any]]:
        # quiet=True: NapCat returns non-200 or error payload before QQ login,
        # this is expected and should not spam warnings.
        return await self.request("POST", "/get_login_info", quiet=True)

    async def get_status(self) -> Optional[Dict[str, Any]]:
        # quiet=True: called frequently to poll status, failure is not critical.
        return await self.request("POST", "/get_status", quiet=True)

    async def ping(self) -> bool:
        try:
            session = await self._get_session()
            async with session.post(f"{self.base_url}/get_status") as resp:
                return resp.status == 200
        except Exception:
            return False