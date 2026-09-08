import logging

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from api.core import config
from api.lyrics.models import LyricsResponse
from api.lyrics.provider import LyricsProviderError, LyricsRateLimited

logger = logging.getLogger(__name__)
router = APIRouter(tags=["lyrics"])
SEO_KEY = r"^[a-zA-Z0-9\-_./%\[\]()+@]+$"


async def _lyrics_for_track(
    request: Request,
    track_id: str,
    title: str | None = None,
    artist: str | None = None,
):
    cache = getattr(request.app.state, "cache", None)
    cache_key = f"songs:info:{track_id}"
    tracks = await cache.get(cache_key) if cache else None
    if tracks is None:
        try:
            tracks = await request.app.state.gaanapy.get_track_info([track_id])
            if cache and not (isinstance(tracks, dict) and "error" in tracks):
                await cache.set(cache_key, tracks, config.TTL_SONG)
        except Exception:
            tracks = None

    track_data = None
    if tracks and not (isinstance(tracks, dict) and "error" in tracks):
        track_data = tracks[0] if isinstance(tracks, list) and tracks else tracks

    if not track_data and (title or track_id):
        track_data = {
            "seokey": track_id,
            "title": title or track_id.replace("-", " ").title(),
            "artists": artist or "",
            "album": "",
            "duration": 0,
        }

    if not track_data:
        raise HTTPException(status_code=404, detail="Track not found")

    if title and not track_data.get("title"):
        track_data["title"] = title
    if artist and not (track_data.get("artists") or track_data.get("artist")):
        track_data["artists"] = artist

    try:
        return await request.app.state.lyrics_service.get_lyrics(track_data)
    except LyricsRateLimited as exc:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content={"detail": "Lyrics provider rate limit reached", "status": "rate_limited"},
        )
    except LyricsProviderError as exc:
        cause = exc.__cause__
        cause_msg = f" (cause: {cause.__class__.__name__}: {cause})" if cause else ""
        logger.warning("Lyrics provider error for %s: %s%s", track_id, exc, cause_msg)
        fallback = {
            "trackId": track_id,
            "provider": None,
            "providerId": None,
            "status": "not_found",
            "synced": False,
            "instrumental": False,
            "plainLyrics": None,
            "lines": [],
        }
        if cache:
            try:
                await cache.set(f"lyrics:{track_id}", fallback, 60)
            except Exception:
                pass
        return fallback


@router.get(
    "/api/v1/tracks/{track_id}/lyrics",
    response_model=LyricsResponse,
    summary="Retrieve normalized lyrics for a track.",
)
async def get_track_lyrics(
    request: Request,
    track_id: str = Path(..., min_length=1, max_length=200, pattern=SEO_KEY),
    title: str | None = None,
    artist: str | None = None,
):
    return await _lyrics_for_track(request, track_id, title=title, artist=artist)


@router.get(
    "/lyrics/{track_id}",
    response_model=LyricsResponse,
    include_in_schema=False,
)
async def get_track_lyrics_legacy(
    request: Request,
    track_id: str = Path(..., min_length=1, max_length=200, pattern=SEO_KEY),
    title: str | None = None,
    artist: str | None = None,
):
    return await _lyrics_for_track(request, track_id, title=title, artist=artist)
