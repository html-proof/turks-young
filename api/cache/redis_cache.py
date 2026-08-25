"""
Redis-backed cache with transparent JSON serialisation.

Gracefully degrades to a no-op when Redis is unavailable so the API
keeps working without a cache layer.
"""
import json
import logging
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_SENTINEL = object()


class RedisCache:
    def __init__(self, redis_url: str):
        self._url = redis_url
        self._client: aioredis.Redis | None = None
        self._available = False

    async def connect(self) -> bool:
        try:
            self._client = aioredis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await self._client.ping()
            self._available = True
            logger.info("cache status=connected url=%s", self._url)
        except Exception as exc:
            self._available = False
            logger.warning("cache status=unavailable reason=%s", exc)
        return self._available

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def get(self, key: str) -> Any | None:
        if not self._available or not self._client:
            return None
        try:
            raw = await self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
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
