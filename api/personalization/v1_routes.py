from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.auth import AuthenticatedUser, account_error, get_current_user
from api.core.home_state import HOME_STALE, coalesce, invalidate_home
from api.personalization.models import AlbumSnapshot, ArtistSnapshot, PreferenceIds, RecommendationEvent
from api.personalization.routes import get_personalization_service, get_user_repository

router = APIRouter(prefix="/api/v1", tags=["Music Hub v1"])


@router.post("/auth/google")
async def google_auth(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    """Exchange a verified Firebase/Google identity for a Music Hub account session.

    Firebase owns the token/session; this endpoint provisions the database record once
    and returns the authoritative bootstrap state.
    """
    repository = getattr(request.app.state, "user_repository", None)
    if repository is None:
        raise HTTPException(503, "Database is unavailable")
    existing = await repository.get_account(user.uid)
    if existing and existing["account_status"] != "deleted":
        if existing["account_status"] != "active":
            raise account_error("ACCOUNT_UNAVAILABLE", "This account is not active.", 403)
    elif existing and existing["account_status"] == "deleted":
        # Deliberate recreation starts a fresh account; cascades remove the old
        # private data and onboarding state before provisioning the new record.
        await repository.recreate_user(user)
    else:
        await repository.ensure_user(user)
    return await repository.bootstrap(user)


@router.get("/me/bootstrap")
async def bootstrap(user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    return await repository.bootstrap(user)


def _catalog(request: Request):
    service = getattr(request.app.state, "catalog_service", None)
    if service is None:
        raise HTTPException(503, "Music catalog is unavailable")
    return service


@router.get("/languages")
async def languages(request: Request):
    values = [item.model_dump(mode="json") for item in _catalog(request).languages.languages]
    return {"languages": values}


@router.get("/onboarding/artists")
async def onboarding_artists(request: Request, language_ids: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=50), cursor: str | None = Query(None, max_length=100)):
    ids = list(dict.fromkeys(value.strip() for value in language_ids.split(",") if value.strip()))
    catalog = _catalog(request)
    unknown = [value for value in ids if not catalog.languages.resolve([value])]
    if unknown:
        raise HTTPException(422, {"unknown_language_ids": unknown})
    result = await catalog.artist_page(catalog.languages.normalize_ids(ids), limit, cursor)
    return {"artists": result["items"], "nextCursor": result["next_cursor"], "hasMore": result["has_more"]}


@router.put("/me/preferences/languages")
async def save_languages(data: PreferenceIds, request: Request, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    if not data.language_ids:
        raise HTTPException(422, "language_ids is required")
    catalog = _catalog(request)
    unknown = [value for value in data.language_ids if not catalog.languages.resolve([value])]
    if unknown:
        raise HTTPException(422, {"unknown_language_ids": unknown})
    normalized_ids = catalog.languages.normalize_ids(data.language_ids)
    await repository.replace_language_preferences(user.uid, normalized_ids, [catalog.languages.by_id[value].name for value in normalized_ids])
    cache = getattr(request.app.state, "cache", None)
    if cache:
        invalidate_home(user.uid)
    return {"language_ids": normalized_ids}


@router.put("/me/preferences/artists")
async def save_artists(data: PreferenceIds, request: Request, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    if not data.artist_ids:
        raise HTTPException(422, "artist_ids is required")
    names = data.artist_ids
    try:
        raw = await request.app.state.gaanapy.get_artist_info(data.artist_ids, False)
        if isinstance(raw, list):
            by_id = {str(item.get("seokey")): str(item.get("name")) for item in raw if item.get("seokey") and item.get("name")}
            names = [by_id.get(artist_id, artist_id) for artist_id in data.artist_ids]
    except Exception:
        pass
    await repository.replace_selected_artists(user.uid, data.artist_ids, names)
    cache = getattr(request.app.state, "cache", None)
    if cache:
        invalidate_home(user.uid)
    return {"artist_ids": data.artist_ids}


@router.post("/me/onboarding/complete")
async def complete_onboarding(user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    try:
        state = await repository.complete_onboarding(user.uid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"onboarding_completed": state["completed"]}


@router.delete("/me")
async def delete_current_account(request: Request, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    deleted = await repository.delete_account(user.uid)
    if not deleted:
        raise account_error("ACCOUNT_NOT_FOUND", "This account is no longer available.")
    # Firebase deletion is best effort: the database status invalidates the account
    # immediately, including existing ID tokens.
    try:
        await request.app.state.firebase.delete_user(user.uid)
    except Exception:
        pass
    return {"deleted": True}


@router.get("/home")
async def home(request: Request, refresh: bool = False, limit: int = Query(24, ge=1, le=50), cursor: str | None = Query(None, max_length=100), type: str = Query("all", pattern="^(all|song|album|artist|playlist)$"), user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository), recommendations=Depends(get_personalization_service)):
    cache = getattr(request.app.state, "cache", None)
    key = f"home:{user.uid}:{type}:{limit}:{cursor or '0'}"
    if cache and not refresh:
        cached = await cache.get(key)
        if cached is not None:
            return cached
    try:
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="cursor must be a non-negative offset") from exc

    async def build():
        page = await _catalog(request).home_page(user.uid, repository, recommendations, limit, offset, type)
        return {"greeting": "Good morning", **page, "algorithm_version": "rec_v1"}

    try:
        data = await coalesce(key, build)
        HOME_STALE[key] = data
        if cache:
            await cache.set(key, data, 900)
        return data
    except Exception:
        stale = HOME_STALE.get(key)
        if stale is not None:
            return {**stale, "stale": True}
        raise


@router.post("/events")
async def events(event: RecommendationEvent, request: Request, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    result = await repository.record_recommendation_event(user.uid, event)
    cache = getattr(request.app.state, "cache", None)
    if cache and event.event_type in {"song_like", "song_unlike", "song_completed", "song_replay", "artist_follow", "album_save"}:
        invalidate_home(user.uid)
    return result


@router.get("/me/recently-played")
async def recently_played(limit: int = Query(20, ge=1, le=100), user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    return {"items": await repository.list_recently_played(user.uid, limit)}


@router.put("/me/liked-songs/{song_id}")
async def like_song(song_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.set_song_like(user.uid, song_id, True)
    return {"song_id": song_id, "liked": True}


@router.delete("/me/liked-songs/{song_id}")
async def unlike_song(song_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.set_song_like(user.uid, song_id, False)
    return {"song_id": song_id, "liked": False}


@router.get("/me/liked-songs")
async def liked_songs(user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    return {"song_ids": await repository.list_liked_song_ids(user.uid)}


@router.put("/me/following/artists/{artist_id}")
async def follow_artist(artist_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.follow_artist(user.uid, ArtistSnapshot(seokey=artist_id, name=artist_id))
    return {"artist_id": artist_id, "following": True}


@router.delete("/me/following/artists/{artist_id}")
async def unfollow_artist(artist_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.unfollow_artist(user.uid, artist_id)
    return {"artist_id": artist_id, "following": False}


@router.get("/me/following/artists")
async def followed_artists(user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    return {"artists": await repository.list_followed_artists(user.uid)}


@router.put("/me/albums/{album_id}")
async def save_album(album_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.save_album(user.uid, AlbumSnapshot(seokey=album_id, title=album_id))
    return {"album_id": album_id, "saved": True}


@router.delete("/me/albums/{album_id}")
async def unsave_album(album_id: str, user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    await repository.remove_saved_album(user.uid, album_id)
    return {"album_id": album_id, "saved": False}


@router.get("/me/albums")
async def saved_albums(user: AuthenticatedUser = Depends(get_current_user), repository=Depends(get_user_repository)):
    return {"albums": await repository.list_saved_albums(user.uid)}
