import os

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "5"))
UPSTREAM_MAX_RETRIES = int(os.getenv("UPSTREAM_MAX_RETRIES", "2"))

# Retry backoff delays in seconds between attempts
RETRY_DELAYS = [0.2, 0.5]

# Circuit breaker: open after this many failures within the window
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
# Seconds to keep circuit open before trying again
CB_RECOVERY_TIMEOUT = int(os.getenv("CB_RECOVERY_TIMEOUT", "30"))
# Rolling window in seconds for counting failures
CB_WINDOW = int(os.getenv("CB_WINDOW", "60"))

# Cache TTLs in seconds
TTL_SONG = int(os.getenv("TTL_SONG", "21600"))        # 6 hours
TTL_ALBUM = int(os.getenv("TTL_ALBUM", "21600"))       # 6 hours
TTL_ARTIST = int(os.getenv("TTL_ARTIST", "21600"))     # 6 hours
TTL_PLAYLIST = int(os.getenv("TTL_PLAYLIST", "3600"))  # 1 hour
TTL_SEARCH = int(os.getenv("TTL_SEARCH", "900"))       # 15 minutes
TTL_CHARTS = int(os.getenv("TTL_CHARTS", "600"))       # 10 minutes
TTL_TRENDING = int(os.getenv("TTL_TRENDING", "600"))   # 10 minutes
TTL_NEW_RELEASES = int(os.getenv("TTL_NEW_RELEASES", "900"))  # 15 minutes
TTL_SIMILAR = int(os.getenv("TTL_SIMILAR", "3600"))    # 1 hour
