import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_MARKDOWN_LINK = re.compile(r"^\[[^\]]*\]\(([^)]+)\)$")


def normalize_http_url(value: object) -> str | None:
    """Normalize deployment-dashboard URL values without accepting bad schemes."""
    if value is None:
        return None
    text = str(value).strip().strip("\"'").strip()
    if not text:
        return None
    markdown = _MARKDOWN_LINK.fullmatch(text)
    if markdown:
        text = markdown.group(1).strip().strip("\"'")
    text = text.strip("<>").strip()
    if text.startswith("//"):
        text = f"https:{text}"
    elif "://" not in text:
        text = f"https://{text}"
    parsed = urlsplit(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("must be a valid HTTP or HTTPS URL")
    return text.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Music Hub API"
    app_env: str = "development"
    app_debug: bool = False
    api_prefix: str = "/api/v1"

    database_url: str | None = None
    database_min_pool_size: int = 1
    database_max_pool_size: int = 10
    database_ssl: bool = True
    database_ssl_verify: bool = True

    redis_url: str | None = None
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: SecretStr | None = None
    cache_namespace: str = "music-hub"

    lyrics_api_base_url: str | None = None
    lyrics_api_token: SecretStr | None = None
    lyrics_request_timeout_seconds: float = 8
    lyrics_match_confidence: float = 0.82
    lyrics_cache_ttl: int = 604800
    lyrics_negative_cache_ttl: int = 3600

    # LRCLIB is the primary source: free, key-less, and the only one of the
    # three that returns timestamped lyrics.
    lrclib_enabled: bool = True
    lrclib_base_url: str = "https://lrclib.net/api"
    # LRCLIB asks clients to identify themselves. Point the contact at
    # something reachable before running this in production.
    lyrics_user_agent: str = "MusicHub/1.0 (https://github.com/music-hub)"
    # Plain-text last resort. Off by default: it cannot be verified against the
    # requested recording, so it is opt-in per deployment.
    lyrics_ovh_enabled: bool = False
    lyrics_ovh_base_url: str = "https://api.lyrics.ovh/v1"

    firebase_project_id: str | None = None
    firebase_credentials_path: Path | None = None
    firebase_credentials_json: SecretStr | None = None
    firebase_check_revoked: bool = True

    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    cursor_secret: SecretStr = SecretStr("development-only-change-me")
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    # Shared with the Cloudflare CDN worker (wrangler secret PROXY_SHARED_SECRET).
    # Only requests presenting it in X-Proxy-Secret may use CF-Connecting-IP as
    # their rate-limit identity; everyone else is keyed by the address the
    # platform proxy appended to X-Forwarded-For.
    proxy_shared_secret: SecretStr | None = None

    # Search responses are cached only long enough to absorb keystrokes;
    # SearchService clamps this to 30-60 seconds.
    search_cache_ttl: int = 45
    # A catalogue source is allowed to be slow, but it must never hold the
    # whole search response hostage.  This is deliberately independent from
    # the longer timeouts used by detail/playback requests.
    search_provider_timeout_seconds: float = 2.5
    artist_cache_ttl: int = 1800
    album_cache_ttl: int = 1800
    trending_cache_ttl: int = 300
    new_releases_cache_ttl: int = 600
    recommendation_cache_ttl: int = 300
    seen_songs_ttl: int = 21600

    @field_validator("firebase_credentials_path", mode="before")
    @classmethod
    def empty_credentials_path_is_none(cls, value):
        return None if value in (None, "") else value

    @field_validator("firebase_credentials_json", "proxy_shared_secret", mode="before")
    @classmethod
    def empty_secret_is_none(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("upstash_redis_rest_url", mode="before")
    @classmethod
    def normalize_upstash_rest_url(cls, value):
        return normalize_http_url(value)

    @field_validator("lyrics_api_base_url", mode="before")
    @classmethod
    def normalize_lyrics_api_base_url(cls, value):
        return normalize_http_url(value)

    @field_validator("lrclib_base_url", "lyrics_ovh_base_url", mode="before")
    @classmethod
    def normalize_free_lyrics_base_url(cls, value, info):
        # An empty override in a deployment dashboard must not blank the
        # endpoint; fall back to the documented default instead.
        return normalize_http_url(value) or cls.model_fields[info.field_name].default

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if bool(self.upstash_redis_rest_url) != bool(self.upstash_redis_rest_token):
            raise ValueError(
                "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set together"
            )
        if bool(self.lyrics_api_base_url) != bool(self.lyrics_api_token):
            raise ValueError("LYRICS_API_BASE_URL and LYRICS_API_TOKEN must be set together")
        if (
            self.app_env.casefold() == "production"
            and self.cursor_secret.get_secret_value() == "development-only-change-me"
        ):
            raise ValueError("CURSOR_SECRET must be changed in production")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
