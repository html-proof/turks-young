from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


import re

SEO_KEY_PATTERN = r"^[a-zA-Z0-9\-_.%]+$"


def _list_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    unique: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = str(item).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            unique.append(normalized)
            seen.add(key)
    return unique


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    languages: list[str] | None = Field(default=None, max_length=10)
    favorite_genres: list[str] | None = Field(default=None, max_length=20)
    favorite_artists: list[str] | None = Field(default=None, max_length=30)
    onboarding_completed: bool | None = None
    streaming_quality_wifi: str | None = Field(default=None, max_length=20)
    streaming_quality_mobile: str | None = Field(default=None, max_length=20)
    download_quality: str | None = Field(default=None, max_length=20)
    data_saver_enabled: bool | None = None
    autoplay_enabled: bool | None = None
    push_notifications_enabled: bool | None = None
    pulse_followed_releases_enabled: bool | None = None
    pulse_selected_releases_enabled: bool | None = None
    pulse_trending_enabled: bool | None = None
    pulse_recommendations_enabled: bool | None = None
    explicit_content_enabled: bool | None = None
    equalizer_preset: str | None = Field(default=None, max_length=30)

    @field_validator("languages", "favorite_genres", "favorite_artists", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str] | None:
        return None if value is None else _list_from_value(value)


class TrackSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    track_id: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=300)
    artists: list[str] = Field(default_factory=list, max_length=20)
    artist_ids: list[str] = Field(default_factory=list, max_length=20)
    genres: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=50)
    album: str = Field(default="", max_length=300)
    album_id: str = Field(default="", max_length=100)
    album_seokey: str = Field(default="", max_length=200)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_.%]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("track_id", "title", "language", "album", "album_id", "album_seokey", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}

    @field_validator("artists", "artist_ids", "genres", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return _list_from_value(value)


class ListeningEvent(TrackSnapshot):
    played_seconds: int = Field(default=0, ge=0, le=86400)
    completed: bool = False
    source: str = Field(default="playback", min_length=1, max_length=50)

    @field_validator("played_seconds", mode="before")
    @classmethod
    def normalize_played_seconds(cls, value: Any) -> int:
        if value is None:
            return 0
        try:
            return max(0, min(86400, int(float(value))))
        except (ValueError, TypeError):
            return 0

    @field_validator("completed", mode="before")
    @classmethod
    def normalize_completed(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: Any) -> str:
        if not value:
            return "playback"
        return str(value).strip()[:50] or "playback"


class ArtistSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    artist_id: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=300)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_.%]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("artist_id", "name", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}


class AlbumSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    album_id: str = Field(default="", max_length=100)
    title: str = Field(min_length=1, max_length=300)
    artists: list[str] = Field(default_factory=list, max_length=20)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_.%]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("album_id", "title", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}

    @field_validator("artists", mode="before")
    @classmethod
    def normalize_artists(cls, value: Any) -> list[str]:
        return _list_from_value(value)


class UserPlaylistCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    is_public: bool = False


class UserPlaylistUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_public: bool | None = None


class TrackOrderUpdate(BaseModel):
    seokeys: list[str] = Field(min_length=1, max_length=500)


class OnboardingUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    languages: list[str] | None = Field(default=None, max_length=10)
    favorite_artists: list[str] | None = Field(default=None, max_length=30)
    completed: bool | None = None
    step: str | None = Field(default=None, max_length=50)

    @field_validator("languages", "favorite_artists", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str] | None:
        return None if value is None else _list_from_value(value)


class PulsePostCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(default="", max_length=1000)
    track: dict[str, Any] | None = None
    album: dict[str, Any] | None = None
    playlist_id: str | None = Field(default=None, max_length=36)


class PulseCommentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1, max_length=1000)


class DeviceRegister(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=1, max_length=500)
    platform: str = Field(min_length=1, max_length=20)
    device_name: str | None = Field(default=None, max_length=150)


class PlayerSessionUpdate(BaseModel):
    track: dict[str, Any] | None = None
    queue: list[Any] = Field(default_factory=list)
    position_ms: int = Field(default=0, ge=0)
    playing: bool = False
    repeat_mode: str = Field(default="off", pattern="^(off|one|all)$")
    shuffle_enabled: bool = False
    duration_ms: int = Field(default=0, ge=0)
    device_id: str | None = Field(default=None, max_length=200)


class PreferenceIds(BaseModel):
    """IDs submitted by onboarding/settings; identity comes from the token."""
    language_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    artist_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("language_ids", "artist_ids")
    @classmethod
    def unique_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not result:
            raise ValueError("At least one ID is required")
        return result


class RecommendationEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=50)
    song_id: str | None = Field(default=None, max_length=200)
    artist_id: str | None = Field(default=None, max_length=200)
    album_id: str | None = Field(default=None, max_length=200)
    playlist_id: str | None = Field(default=None, max_length=200)
    position_ms: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    source: str | None = Field(default=None, max_length=50)
    context_id: str | None = Field(default=None, max_length=100)
    query: str | None = Field(default=None, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
