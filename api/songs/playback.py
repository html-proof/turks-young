"""Playback resolution helpers: stream validation, identity scoring, cache keys.

Gaana stream URLs are signed and short-lived, and a decrypted URL is not proof
that the CDN will serve audio. These helpers let the per-song info endpoint
verify the URL it hands to the player and recover a playable source for the
same recording when the primary one is dead.
"""
import html
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from rapidfuzz import fuzz

from api.core import config
from api.lyrics.service import duration_seconds

logger = logging.getLogger(__name__)

NO_VALID_SOURCE = "NO_VALID_SOURCE"
DECRYPT_FAILED = "DECRYPT_FAILED"

IDENTITY_MATCH_THRESHOLD = 0.88
DURATION_TOLERANCE_SECONDS = 3
_WEIGHTS = {"title": 0.40, "artist": 0.25, "album": 0.20, "duration": 0.10, "year": 0.05}

_PROBE_BYTES = 2048
_STREAM_CONTENT_TYPES = (
    "application/octet-stream",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "video/mp4",
    "video/mp2t",
)


def song_info_cache_key(seokey: str) -> str:
    return f"songs:info:v2:{seokey}"


def playback_cache_key(seokey: str) -> str:
    return f"playback:v2:gaana:{seokey}"


def redact_url(url: str | None) -> str:
    """Drop query string and fragment so signatures never reach the logs."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""


def stream_expiry(url: str | None) -> int | None:
    if not url:
        return None
    try:
        values = parse_qs(urlsplit(url).query).get("exp")
    except ValueError:
        return None
    if not values:
        match = re.search(r"[?&~]exp=(\d+)", url)
        values = [match.group(1)] if match else []
    try:
        return int(values[0]) if values else None
    except ValueError:
        return None


def playback_cache_ttl(url: str | None, now: float | None = None) -> int:
    """Seconds a validated URL may be cached; 0 means do not cache."""
    ttl = config.TTL_PLAYBACK
    expiry = stream_expiry(url)
    if expiry is not None:
        remaining = expiry - int(now if now is not None else time.time()) - 60
        ttl = min(ttl, remaining)
    return max(0, int(ttl))


@dataclass(frozen=True)
class StreamCheck:
    ok: bool
    status: int | None = None
    content_type: str = ""
    error: str = ""


def _is_stream_content(content_type: str, body: bytes) -> bool:
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype.startswith("audio/") or ctype in _STREAM_CONTENT_TYPES:
        return True
    if not body:
        return False
    # CDNs sometimes omit the type; an HTML/JSON/text body is an error page.
    return not ctype or not (ctype.startswith("text/") or "json" in ctype or "xml" in ctype)


async def validate_stream(client: httpx.AsyncClient, url: str) -> StreamCheck:
    """Fetch the first bytes of a stream with a ranged GET.

    HEAD is deliberately not used: several CDNs reject or mis-answer HEAD for
    signed media URLs that serve GET correctly.
    """
    if not url:
        return StreamCheck(ok=False, error="empty_url")
    try:
        async with client.stream(
            "GET",
            url,
            headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}"},
            follow_redirects=True,
            timeout=config.STREAM_VALIDATION_TIMEOUT,
        ) as response:
            content_type = response.headers.get("content-type", "")
            if response.status_code not in (200, 206):
                return StreamCheck(ok=False, status=response.status_code, content_type=content_type)
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= _PROBE_BYTES:
                    break
            return StreamCheck(
                ok=_is_stream_content(content_type, body),
                status=response.status_code,
                content_type=content_type,
            )
    except httpx.HTTPError as exc:
        return StreamCheck(ok=False, error=type(exc).__name__)


def normalize_identity_text(value: Any) -> str:
    text = html.unescape(html.unescape(str(value or "")))
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)
    text = re.sub(r"\b(?:feat|ft|featuring)\.?\s.*$", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _artist_names(value: Any) -> list[str]:
    if isinstance(value, list):
        names = [v.get("name") if isinstance(v, dict) else v for v in value]
    else:
        names = re.split(r",|&|\band\b", str(value or ""))
    return [n for n in (normalize_identity_text(name) for name in names) if n]


def _year(value: Any) -> int | None:
    match = re.match(r"\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return fuzz.ratio(left, right) / 100.0


def identity_score(target: dict[str, Any], candidate: dict[str, Any]) -> float:
    """Weighted fingerprint similarity between two formatted song records."""
    score = _WEIGHTS["title"] * _similarity(
        normalize_identity_text(target.get("title")),
        normalize_identity_text(candidate.get("title")),
    )

    target_artists = _artist_names(target.get("artists"))
    candidate_artists = _artist_names(candidate.get("artists"))
    if target_artists and candidate_artists:
        matched = sum(
            1 for name in target_artists
            if max(_similarity(name, other) for other in candidate_artists) >= 0.9
        )
        score += _WEIGHTS["artist"] * matched / len(target_artists)

    score += _WEIGHTS["album"] * _similarity(
        normalize_identity_text(target.get("album")),
        normalize_identity_text(candidate.get("album")),
    )

    target_duration = duration_seconds(target.get("duration"))
    candidate_duration = duration_seconds(candidate.get("duration"))
    if target_duration and candidate_duration and abs(target_duration - candidate_duration) <= DURATION_TOLERANCE_SECONDS:
        score += _WEIGHTS["duration"]

    target_year = _year(target.get("release_date"))
    if target_year is not None and target_year == _year(candidate.get("release_date")):
        score += _WEIGHTS["year"]

    return round(score, 4)


class PlaybackResolver:
    """Centralized playback resolver.
    
    1. Verifies stream with ranged GET requests (Range: bytes=0-2047, status 200/206).
    2. Follows redirects.
    3. Resolves primary provider.
    4. If primary fails, resolves alternate variants or alternate providers.
    5. Recovers by high-confidence identity search (threshold >= 0.88).
    6. Manages versioned short-lived playback caching (playback:v2:gaana:{id}).
    7. Emits structured observability logs.
    """

    def __init__(self, cache: Any = None, http_client: Any = None, gaana: Any = None) -> None:
        self.cache = cache
        self.http_client = http_client
        self.gaana = gaana

    async def resolve(
        self,
        track_or_id: dict[str, Any] | str,
        *,
        refresh: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        track_id = ""
        track_meta: dict[str, Any] = {}
        if isinstance(track_or_id, str):
            track_id = track_or_id.strip()
            track_meta = {"track_id": track_id, "seokey": track_id}
        elif isinstance(track_or_id, dict):
            track_meta = track_or_id
            track_id = str(
                track_meta.get("track_id")
                or track_meta.get("seokey")
                or track_meta.get("id")
                or ""
            ).strip()

        title = track_meta.get("title") or ""
        primary_source = track_meta.get("source") or "gaana"
        logger.info(
            "PLAYBACK_RESOLVE_STARTED track_id=%s title=%s primary_source=%s",
            track_id, title or "(pending)", primary_source,
        )

        key = playback_cache_key(track_id)
        if self.cache and hasattr(self.cache, "get") and not refresh and track_id:
            try:
                cached = await self.cache.get(key)
                if isinstance(cached, dict) and cached.get("playable") and cached.get("stream", {}).get("url"):
                    stream_url = cached["stream"]["url"]
                    expiry = stream_expiry(stream_url)
                    if expiry is None or (expiry - int(time.time())) > 30:
                        return cached
                    else:
                        # Expired cached URL: invalidate it
                        if hasattr(self.cache, "delete"):
                            await self.cache.delete(key)
            except Exception:
                pass

        if self.gaana is None:
            # Standalone validation on given track metadata
            stream_url = track_meta.get("stream_url") or track_meta.get("streamUrl") or ""
            if stream_url:
                async with httpx.AsyncClient(follow_redirects=True, timeout=config.STREAM_VALIDATION_TIMEOUT) as local_client:
                    check = await validate_stream(client or local_client, stream_url)
                    if check.ok:
                        ttl = playback_cache_ttl(stream_url)
                        res = {
                            "track_id": track_id,
                            "playable": True,
                            "stream": {
                                "url": stream_url,
                                "expires_at": str(stream_expiry(stream_url)) if stream_expiry(stream_url) else None,
                                "quality": "96kbps",
                            },
                            "artwork": {
                                "url": track_meta.get("imageUrl") or track_meta.get("image_url") or "",
                                "source": "track",
                            },
                            "playback_source": {"provider": primary_source, "id": track_id},
                        }
                        if self.cache and hasattr(self.cache, "set") and ttl > 0:
                            try:
                                await self.cache.set(key, res, ttl)
                            except Exception:
                                pass
                        return res

            return {
                "track_id": track_id,
                "playable": False,
                "reason": NO_VALID_SOURCE,
            }

        # Resolve through Gaana/catalog adapter
        resolved_list = await self.gaana.resolve_song_playback(track_id, refresh=refresh)
        if isinstance(resolved_list, list) and resolved_list:
            resolved_track = resolved_list[0]
            if resolved_track.get("playable") and resolved_track.get("stream_url"):
                stream_url = resolved_track["stream_url"]
                source_info = resolved_track.get("playback_source") or {"provider": "gaana", "id": track_id}
                ttl = playback_cache_ttl(stream_url)
                expires_at = stream_expiry(stream_url)
                result = {
                    "track_id": track_id,
                    "playable": True,
                    "stream": {
                        "url": stream_url,
                        "expires_at": str(expires_at) if expires_at else None,
                        "quality": "96kbps",
                    },
                    "artwork": {
                        "url": resolved_track.get("imageUrl") or resolved_track.get("image_url") or "",
                        "source": "album" if resolved_track.get("album") else "track",
                    },
                    "playback_source": source_info,
                    "canonical_track": resolved_track,
                }
                logger.info(
                    "PLAYBACK_RESOLVED track_id=%s source=%s provider=%s url=%s latency_ms=%.1f",
                    track_id, source_info.get("provider", "gaana"), primary_source,
                    redact_url(stream_url), (time.perf_counter() - started) * 1000,
                )
                if self.cache and hasattr(self.cache, "set") and ttl > 0:
                    try:
                        await self.cache.set(key, result, ttl)
                    except Exception:
                        pass
                return result
            else:
                reason = resolved_track.get("unavailable_reason") or NO_VALID_SOURCE
                logger.warning(
                    "PLAYBACK_UNAVAILABLE track_id=%s reason=%s latency_ms=%.1f",
                    track_id, reason, (time.perf_counter() - started) * 1000,
                )
                return {
                    "track_id": track_id,
                    "playable": False,
                    "reason": reason,
                }

        logger.warning(
            "PLAYBACK_UNAVAILABLE track_id=%s reason=%s latency_ms=%.1f",
            track_id, NO_VALID_SOURCE, (time.perf_counter() - started) * 1000,
        )
        return {
            "track_id": track_id,
            "playable": False,
            "reason": NO_VALID_SOURCE,
        }

