"""
Simple in-process circuit breaker.

States:
  CLOSED   – requests flow normally
  OPEN     – requests are blocked; cached/503 is returned immediately
  HALF_OPEN – one probe request is allowed through to test recovery
"""
import asyncio
import time
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        recovery_timeout: float,
        window: float,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.window = window

        self._state = CBState.CLOSED
        self._failure_times: list[float] = []
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CBState:
        return self._state

    def _prune_old_failures(self) -> None:
        cutoff = time.monotonic() - self.window
        self._failure_times = [t for t in self._failure_times if t >= cutoff]

    async def call(self, coro):
        """Wrap an awaitable. Raises CircuitOpenError when the circuit is open."""
        async with self._lock:
            now = time.monotonic()

            if self._state == CBState.OPEN:
                if now - self._opened_at >= self.recovery_timeout:
                    self._state = CBState.HALF_OPEN
                    logger.info("circuit_breaker name=%s state=half_open", self.name)
                else:
                    if hasattr(coro, "close"):
                        coro.close()
                    raise CircuitOpenError(self.name)

        try:
            result = await coro
            await self._on_success()
            return result
        except Exception:
            await self._on_failure()
            raise

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CBState.HALF_OPEN:
                self._state = CBState.CLOSED
                self._failure_times.clear()
                logger.info("circuit_breaker name=%s state=closed (recovered)", self.name)

    async def _on_failure(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._failure_times.append(now)
            self._prune_old_failures()

            if self._state == CBState.HALF_OPEN or (
                len(self._failure_times) >= self.failure_threshold
            ):
                self._state = CBState.OPEN
                self._opened_at = now
                logger.warning(
                    "circuit_breaker name=%s state=open failures=%d",
                    self.name,
                    len(self._failure_times),
                )


class CircuitOpenError(Exception):
    def __init__(self, name: str):
        super().__init__(f"Circuit '{name}' is OPEN")
        self.name = name
