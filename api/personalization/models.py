from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


SEO_KEY_PATTERN = r"^[a-z0-9\-]+$"


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

    @field_validator("artists", "artist_ids", "genres", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return _list_from_value(value)


class ListeningEvent(TrackSnapshot):
    played_seconds: int = Field(default=0, ge=0, le=86400)
    completed: bool = False
    source: str = Field(default="playback", min_length=1, max_length=50)
