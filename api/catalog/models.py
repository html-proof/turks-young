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


class AlternateSource(BaseModel):
    source: str
    source_track_id: str


class CanonicalArtwork(BaseModel):
    original: str | None = None
    high: str | None = None
    medium: str | None = None
    thumbnail: str | None = None


class CanonicalPlayback(BaseModel):
    resolved: bool = False
    stream_url: str | None = None
    expires_at: str | None = None
    quality: str | None = None


class CanonicalTrack(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    track_id: str
    title: str
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    album_id: str = ""
    duration_ms: int = 0
    language: str = ""
    year: str | int = ""
    source: str = "gaana"
    source_track_id: str = ""
    alternate_sources: list[AlternateSource] = Field(default_factory=list)
    artwork: CanonicalArtwork = Field(default_factory=CanonicalArtwork)
    playback: CanonicalPlayback = Field(default_factory=CanonicalPlayback)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalTrack":
        if not isinstance(data, dict):
            return cls(track_id="unknown", title="")
        
        # Identity
        tid = str(
            data.get("track_id")
            or data.get("seokey")
            or data.get("id")
            or data.get("song_id")
            or ""
        ).strip()
        
        raw_title = str(
            data.get("title")
            or data.get("name")
            or data.get("song_name")
            or data.get("track")
            or ""
        ).strip()
        
        # Artists
        artists_val = data.get("artists")
        extracted_artists: list[str] = []
        if isinstance(artists_val, list):
            for a in artists_val:
                if isinstance(a, dict):
                    name = a.get("name") or a.get("title")
                    if name:
                        extracted_artists.append(str(name).strip())
                elif isinstance(a, str) and a.strip():
                    extracted_artists.append(a.strip())
        elif isinstance(artists_val, str) and artists_val.strip():
            extracted_artists = [p.strip() for p in artists_val.split(",") if p.strip()]
        
        if not extracted_artists:
            artist_val = data.get("artist")
            if isinstance(artist_val, str) and artist_val.strip():
                extracted_artists = [p.strip() for p in artist_val.split(",") if p.strip()]
            elif isinstance(artist_val, dict):
                name = artist_val.get("name") or artist_val.get("title")
                if name:
                    extracted_artists.append(str(name).strip())

        # Album
        album_val = data.get("album")
        album_name = ""
        album_id = str(data.get("album_id") or data.get("albumId") or "").strip()
        if isinstance(album_val, dict):
            album_name = str(album_val.get("name") or album_val.get("title") or "").strip()
            if not album_id:
                album_id = str(album_val.get("id") or album_val.get("seokey") or "").strip()
        elif isinstance(album_val, str):
            album_name = album_val.strip()

        # Duration
        dur_raw = (
            data.get("duration_ms")
            or data.get("durationMs")
            or data.get("duration")
            or data.get("duration_seconds")
            or 0
        )
        try:
            dur_num = float(dur_raw)
            duration_ms = int(dur_num if dur_num > 1000 else dur_num * 1000)
        except (ValueError, TypeError):
            duration_ms = 0

        # Source
        source = str(data.get("source") or "gaana").strip()
        source_track_id = str(
            data.get("source_track_id")
            or data.get("provider_id")
            or data.get("track_id")
            or tid
        ).strip()

        # Alternate sources
        alt_sources: list[AlternateSource] = []
        raw_alts = data.get("alternate_sources") or data.get("alternateSources") or []
        if isinstance(raw_alts, list):
            for alt in raw_alts:
                if isinstance(alt, dict) and "source" in alt and "source_track_id" in alt:
                    alt_sources.append(
                        AlternateSource(
                            source=str(alt["source"]),
                            source_track_id=str(alt["source_track_id"]),
                        )
                    )

        # Artwork
        art_val = data.get("artwork")
        orig_art = None
        high_art = None
        med_art = None
        thumb_art = None
        if isinstance(art_val, dict):
            orig_art = art_val.get("original")
            high_art = art_val.get("high")
            med_art = art_val.get("medium")
            thumb_art = art_val.get("thumbnail")
        
        # Fallback to legacy image fields
        candidates = data.get("artwork_candidates") or []
        fallback_img = (
            data.get("imageUrl")
            or data.get("image_url")
            or data.get("artwork_url")
            or (candidates[0] if candidates else None)
        )
        if not high_art and not orig_art and fallback_img:
            high_art = str(fallback_img)
            orig_art = str(fallback_img)

        # Playback
        pb_val = data.get("playback")
        resolved = False
        stream_url = None
        expires_at = None
        quality = None
        if isinstance(pb_val, dict):
            resolved = bool(pb_val.get("resolved"))
            stream_url = pb_val.get("stream_url")
            expires_at = pb_val.get("expires_at")
            quality = pb_val.get("quality")
        elif "stream_url" in data or "streamUrl" in data:
            s_url = data.get("stream_url") or data.get("streamUrl")
            if s_url and str(s_url).strip():
                resolved = bool(data.get("playable", True))
                stream_url = str(s_url).strip()

        return cls(
            track_id=tid,
            title=raw_title,
            artists=extracted_artists,
            album=album_name,
            album_id=album_id,
            duration_ms=duration_ms,
            language=str(data.get("language") or "").strip(),
            year=data.get("year") or data.get("release_date") or "",
            source=source,
            source_track_id=source_track_id,
            alternate_sources=alt_sources,
            artwork=CanonicalArtwork(
                original=orig_art,
                high=high_art,
                medium=med_art,
                thumbnail=thumb_art,
            ),
            playback=CanonicalPlayback(
                resolved=resolved,
                stream_url=stream_url,
                expires_at=str(expires_at) if expires_at else None,
                quality=quality,
            ),
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        primary_artist = ", ".join(self.artists) if self.artists else ""
        best_img = self.artwork.high or self.artwork.original or self.artwork.medium or self.artwork.thumbnail or ""
        candidates = [
            img for img in [self.artwork.high, self.artwork.original, self.artwork.medium, self.artwork.thumbnail]
            if img
        ]
        stream_url = self.playback.stream_url or ""
        is_playable = bool(self.playback.resolved and stream_url)

        return {
            "id": self.track_id,
            "seokey": self.track_id,
            "track_id": self.track_id,
            "title": self.title,
            "artist": primary_artist,
            "artists": [{"name": a} for a in self.artists],
            "album": self.album,
            "album_id": self.album_id,
            "album_seokey": self.album_id,
            "duration": str(self.duration_ms // 1000 if self.duration_ms else 0),
            "duration_seconds": self.duration_ms // 1000 if self.duration_ms else 0,
            "duration_ms": self.duration_ms,
            "language": self.language,
            "year": str(self.year),
            "source": self.source,
            "source_track_id": self.source_track_id,
            "alternate_sources": [a.model_dump() for a in self.alternate_sources],
            "artwork": self.artwork.model_dump(),
            "imageUrl": best_img,
            "image_url": best_img,
            "artwork_candidates": candidates,
            "playback": self.playback.model_dump(),
            "stream_url": stream_url,
            "stream_urls": {"default": stream_url} if stream_url else {},
            "playable": is_playable,
        }


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta, "error": None}


