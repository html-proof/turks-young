import asyncio
import logging
import random
import aiohttp
from api.songs.songs import Songs
from api.albums.albums import Albums
from api.artists.artists import Artists
from api.trending.trending import Trending
from api.newreleases.newreleases import NewReleases
from api.charts.charts import Charts
from api.playlists.playlists import Playlists
from api import endpoints
from api.functions import Functions
from api.errors import Errors
from api.discovery.discovery import Discovery
from api.core import config
from api.core.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


class GaanaPy(Songs, Albums, Artists, Trending, NewReleases, Charts, Playlists, Discovery):
    def __init__(self):
        self.aiohttp = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.UPSTREAM_TIMEOUT)
        )
        self.api_endpoints = endpoints
        self.functions = Functions()
        self.errors = Errors()
        self._circuit_breaker = CircuitBreaker(
            name="gaana",
            failure_threshold=config.CB_FAILURE_THRESHOLD,
            recovery_timeout=config.CB_RECOVERY_TIMEOUT,
            window=config.CB_WINDOW,
        )

    async def _do_request(self, method: str, url: str, **kwargs) -> dict:
        """Single attempt — raises on any failure."""
        if method == "GET":
            response = await self.aiohttp.get(url, **kwargs)
        else:
            response = await self.aiohttp.post(url, **kwargs)
        if response.status != 200:
            raise aiohttp.ClientResponseError(
                response.request_info, response.history, status=response.status
            )
        result = await response.json()
        if not isinstance(result, dict):
            raise ValueError("Unexpected response format")
        return result

    async def _safe_request(self, method: str, url: str, **kwargs) -> dict:
        """Retry with backoff, guarded by a circuit breaker."""
        delays = config.RETRY_DELAYS
        last_exc: Exception | None = None

        for attempt in range(config.UPSTREAM_MAX_RETRIES + 1):
            if attempt > 0:
                base = delays[min(attempt - 1, len(delays) - 1)]
                await asyncio.sleep(base + random.uniform(0, base * 0.25))

            try:
                return await self._circuit_breaker.call(
                    self._do_request(method, url, **kwargs)
                )
            except CircuitOpenError:
                logger.warning("upstream circuit open url=%s", url)
                return await self.errors.no_results()
            except aiohttp.ClientResponseError as exc:
                # Authentication, missing resources, and malformed requests
                # are deterministic and must not be retried as network loss.
                if exc.status not in {408, 429, 500, 502, 503, 504}:
                    logger.info("upstream non-retryable status=%s url=%s", exc.status, url)
                    return await self.errors.no_results()
                last_exc = exc
                logger.warning("upstream retryable status=%s attempt=%d/%d url=%s", exc.status, attempt + 1, config.UPSTREAM_MAX_RETRIES + 1, url)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                last_exc = exc
                logger.warning(
                    "upstream attempt=%d/%d url=%s error=%s",
                    attempt + 1,
                    config.UPSTREAM_MAX_RETRIES + 1,
                    url,
                    exc,
                )

        logger.error("upstream exhausted retries url=%s last_error=%s", url, last_exc)
        return await self.errors.no_results()
