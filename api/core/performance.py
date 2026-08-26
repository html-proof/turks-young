import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)
_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=500))


def record(endpoint: str, duration_ms: float) -> None:
    values = _samples[endpoint]
    values.append(duration_ms)
    if len(values) >= 20:
        ordered = sorted(values)
        def percentile(percent: float) -> float:
            index = min(len(ordered) - 1, int((len(ordered) - 1) * percent))
            return ordered[index]
        logger.info(
            "performance endpoint=%s samples=%d p50_ms=%.1f p95_ms=%.1f p99_ms=%.1f",
            endpoint, len(values), percentile(.50), percentile(.95), percentile(.99),
        )


def now() -> float:
    return time.perf_counter()
