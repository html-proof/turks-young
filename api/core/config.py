import os

# ── Supabase PostgreSQL ────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")  # postgresql://user:pass@host/db

# ── Firebase ───────────────────────────────────────────────────────────────────
# Paste the entire service-account JSON string from:
# Firebase Console → Project Settings → Service Accounts → Generate new private key
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# ── Upstash Redis ──────────────────────────────────────────────────────────────
UPSTASH_REDIS_REST_URL   = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "5"))
UPSTREAM_MAX_RETRIES = int(os.getenv("UPSTREAM_MAX_RETRIES", "2"))

RETRY_DELAYS = [0.2, 0.5]

CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_RECOVERY_TIMEOUT  = int(os.getenv("CB_RECOVERY_TIMEOUT", "30"))
CB_WINDOW            = int(os.getenv("CB_WINDOW", "60"))

TTL_SONG         = int(os.getenv("TTL_SONG",         "21600"))
TTL_ALBUM        = int(os.getenv("TTL_ALBUM",        "21600"))
TTL_ARTIST       = int(os.getenv("TTL_ARTIST",       "21600"))
TTL_PLAYLIST     = int(os.getenv("TTL_PLAYLIST",     "3600"))
TTL_SEARCH       = int(os.getenv("TTL_SEARCH",       "900"))
TTL_CHARTS       = int(os.getenv("TTL_CHARTS",       "600"))
TTL_TRENDING     = int(os.getenv("TTL_TRENDING",     "600"))
TTL_NEW_RELEASES = int(os.getenv("TTL_NEW_RELEASES", "900"))
TTL_SIMILAR      = int(os.getenv("TTL_SIMILAR",      "3600"))

# ── Lyrics ────────────────────────────────────────────────────────────────────
LYRICS_PROVIDER = os.getenv("LYRICS_PROVIDER", "lrclib")
LYRICS_USER_AGENT = os.getenv(
    "LYRICS_USER_AGENT", "MusicHub/1.0 (https://github.com/html-proof/turks-young)"
)
LYRICS_TIMEOUT = float(os.getenv("LYRICS_TIMEOUT", "10"))
TTL_LYRICS = int(os.getenv("TTL_LYRICS", "2592000"))
TTL_LYRICS_NOT_FOUND = int(os.getenv("TTL_LYRICS_NOT_FOUND", "21600"))

# JSON array owned by the backend deployment. An empty value intentionally
# exposes an empty catalogue instead of inventing onboarding choices.
MUSIC_LANGUAGES_JSON = os.getenv("MUSIC_LANGUAGES_JSON", "")
