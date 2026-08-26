from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ContentType = Literal["song", "artist", "album", "playlist", "genre"]


class Language(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100)
    native_name: str = Field(min_length=1, max_length=100)
    image_url: str | None = None


class IdSelection(BaseModel):
    language_ids: list[str] | None = Field(default=None, min_length=1, max_length=10)
    artist_ids: list[str] | None = Field(default=None, min_length=1, max_length=30)

    @field_validator("language_ids", "artist_ids")
    @classmethod
    def unique_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("At least one ID is required")
        return cleaned


class RecentSearchCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=200)
    result_type: ContentType | None = None
    item: dict[str, Any] | None = None


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta, "error": None}

