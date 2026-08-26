from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from api.core import config
from api.lyrics.models import LyricsResponse
from api.lyrics.provider import LyricsProviderError, LyricsRateLimited


router = APIRouter(tags=["lyrics"])
SEO_KEY = r"^[a-z0-9\-]+$"


async def _lyrics_for_track(request: Request, track_id: str):
    cache = getattr(request.app.state, "cache", None)
    cache_key = f"songs:info:{track_id}"
    tracks = await cache.get(cache_key) if cache else None
    if tracks is None:
        tracks = await request.app.state.gaanapy.get_track_info([track_id])
        if cache and not (isinstance(tracks, dict) and "error" in tracks):
            await cache.set(cache_key, tracks, config.TTL_SONG)
    if (isinstance(tracks, dict) and "error" in tracks) or not tracks:
        raise HTTPException(status_code=404, detail="Track not found")
    try:
        return await request.app.state.lyrics_service.get_lyrics(tracks[0])
    except LyricsRateLimited as exc:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content={"detail": "Lyrics provider rate limit reached", "status": "rate_limited"},
        )
    except LyricsProviderError as exc:
        raise HTTPException(status_code=503, detail="Lyrics service is temporarily unavailable") from exc


@router.get(
    "/api/v1/tracks/{track_id}/lyrics",
    response_model=LyricsResponse,
    summary="Retrieve normalized lyrics for a track.",
)
async def get_track_lyrics(
    request: Request,
    track_id: str = Path(..., min_length=1, max_length=200, pattern=SEO_KEY),
):
    return await _lyrics_for_track(request, track_id)


@router.get(
    "/lyrics/{track_id}",
    response_model=LyricsResponse,
    include_in_schema=False,
)
async def get_track_lyrics_legacy(
    request: Request,
    track_id: str = Path(..., min_length=1, max_length=200, pattern=SEO_KEY),
):
    return await _lyrics_for_track(request, track_id)
