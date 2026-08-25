"""
Upstash Redis-backed cache with transparent JSON serialisation.

Uses upstash_redis.asyncio (HTTP/REST) so no TCP connection is needed —
works on Render free tier without a sidecar Redis process.

Gracefully degrades to a no-op when Upstash is not configured so the API
keeps working without a cache layer.
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self, url: str, token: str):
        self._url = url
        self._token = token
        self._client = None
        self._available = False

    async def connect(self) -> bool:
        if not self._url or not self._token:
            logger.warning("cache status=disabled reason=UPSTASH_REDIS_REST_URL or TOKEN not set")
            return False
        try:
            from upstash_redis.asyncio import Redis
            self._client = Redis(url=self._url, token=self._token)
            await self._client.ping()
            self._available = True
            logger.info("cache status=connected url=%s", self._url)
        except Exception as exc:
            self._available = False
            logger.warning("cache status=unavailable reason=%s", exc)
        return self._available

    async def close(self) -> None:
        self._client = None

    async def get(self, key: str) -> Any | None:
        if not self._available or not self._client:
            return None
        try:
            raw = await self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.debug("cache get key=%s error=%s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if not self._available or not self._client:
            return
        try:
            await self._client.set(key, json.dumps(value), ex=ttl)
        except Exception as exc:
            logger.debug("cache set key=%s error=%s", key, exc)

    async def delete(self, key: str) -> None:
        if not self._available or not self._client:
            return
        try:
            await self._client.delete(key)
        except Exception as exc:
            logger.debug("cache delete key=%s error=%s", key, exc)

    @property
    def available(self) -> bool:
        return self._available
