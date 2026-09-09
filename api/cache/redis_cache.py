"""
Upstash Redis-backed cache with transparent JSON serialisation.

Uses upstash_redis.asyncio (HTTP/REST) so no TCP connection is needed —
works on Render free tier without a sidecar Redis process.

Gracefully degrades to a no-op when Upstash is not configured so the API
keeps working without a cache layer.
"""
import json
import logging
import asyncio
import time
from typing import Any

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self, url: str, token: str):
        self._url = url
        self._token = token
        self._client = None
        self._available = False
        self._local: dict[str, tuple[float, float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
            self._client = None
            logger.warning("cache status=unavailable reason=%s", exc)
        return self._available

    async def close(self) -> None:
        self._client = None

    async def get(self, key: str) -> Any | None:
        value, fresh = await self.get_with_stale(key)
        return value if fresh else None

    async def get_with_stale(self, key: str) -> tuple[Any | None, bool]:
        """Return (value, is_fresh), retaining a short stale window locally/redis-side."""
        local = self._local.get(key)
        now = time.monotonic()
        if local:
            fresh_until, stale_until, value = local
            if now < stale_until:
                return value, now < fresh_until
            self._local.pop(key, None)
        if not self._available or not self._client:
            return None, False
        try:
            raw = await self._client.get(key)
            if raw is None:
                return None, False
            value = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(value, dict) and "__music_hub_cache" in value:
                record = value["__music_hub_cache"]
                fresh_until = float(record["fresh_until"])
                stale_until = float(record["stale_until"])
                payload = record["value"]
                if time.time() < stale_until:
                    return payload, time.time() < fresh_until
                return None, False
            return value, True
        except Exception as exc:
            logger.debug("cache get key=%s error=%s", key, exc)
            return None, False

    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self.set_with_stale(key, value, ttl)

    async def set_with_stale(self, key: str, value: Any, ttl: int, stale_ttl: int | None = None) -> None:
        stale_ttl = stale_ttl if stale_ttl is not None else max(ttl, 60)
        now_wall = time.time()
        now_mono = time.monotonic()
        self._local[key] = (now_mono + ttl, now_mono + ttl + stale_ttl, value)
        if not self._available or not self._client:
            return
        try:
            record = {"__music_hub_cache": {
                "fresh_until": now_wall + ttl,
                "stale_until": now_wall + ttl + stale_ttl,
                "value": value,
            }}
            await self._client.set(key, json.dumps(record), ex=ttl + stale_ttl)
        except Exception as exc:
            logger.debug("cache set key=%s error=%s", key, exc)

    async def get_or_set(self, key: str, loader, ttl: int, stale_ttl: int | None = None) -> Any:
        try:
            value, fresh = await self.get_with_stale(key)
            if value is not None:
                if not fresh:
                    logger.info("cache status=stale key=%s", key)
                    lock = self._locks.setdefault(key, asyncio.Lock())
                    if not lock.locked():
                        asyncio.create_task(self._refresh(key, loader, ttl, stale_ttl, lock))
                else:
                    logger.info("cache status=hit key=%s", key)
                return value
            logger.info("cache status=miss key=%s", key)
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                value, _ = await self.get_with_stale(key)
                if value is not None:
                    return value
                value = await loader()
                await self.set_with_stale(key, value, ttl, stale_ttl)
                return value
        except Exception as exc:
            logger.warning("cache get_or_set fallback key=%s error=%s", key, exc)
            return await loader()

    async def _refresh(self, key: str, loader, ttl: int, stale_ttl: int | None, lock: asyncio.Lock) -> None:
        async with lock:
            try:
                value, fresh = await self.get_with_stale(key)
                if value is not None and fresh:
                    return
                value = await loader()
                await self.set_with_stale(key, value, ttl, stale_ttl)
            except Exception as exc:
                logger.warning("cache refresh failed key=%s error=%s", key, exc)

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
