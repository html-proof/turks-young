from typing import Literal

from pydantic import BaseModel, Field


class LyricLine(BaseModel):
    startMs: int = Field(ge=0)
    endMs: int | None = Field(default=None, ge=0)
    text: str


class LyricsResponse(BaseModel):
    trackId: str
    provider: str | None = None
    providerId: str | None = None
    status: Literal["available", "not_found", "instrumental"]
    synced: bool
    instrumental: bool
    plainLyrics: str | None = None
    lines: list[LyricLine] = Field(default_factory=list)

