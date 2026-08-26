from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from api.auth import AuthenticatedUser, get_current_user
from api.catalog.models import IdSelection, RecentSearchCreate, envelope
from api.core.home_state import HOME_STALE, coalesce
from api.personalization.models import OnboardingUpdate
from api.personalization.routes import get_personalization_service, get_user_repository
from api.core import config


router = APIRouter(prefix="/api", tags=["Music Hub Catalog"])


def _service(request: Request):
    service = getattr(request.app.state, "catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Music catalog is unavailable")
    return service


@router.get("/languages", summary="List backend-configured onboarding languages.")
async def languages(request: Request, response: Response):
    service = _service(request)
    values = [value.model_dump(mode="json") for value in service.languages.languages]
    cache = getattr(request.app.state, "cache", None)
    key = "music:languages:v1"
    if cache and hasattr(cache, "get_or_set"):
        data = await cache.get_or_set(key, lambda: _language_payload(values), config.TTL_LANGUAGES, config.STALE_CACHE_TTL)
    else:
        data = _language_payload(values)
    payload = envelope(data, count=len(data["items"]))
    response.headers["Cache-Control"] = f"public, max-age={config.TTL_LANGUAGES}, stale-while-revalidate={config.STALE_CACHE_TTL}"
    return payload


def _language_payload(values):
    return {"items": values}


@router.get("/artists", summary="List artists relevant to selected languages.")
async def relevant_artists(
    request: Request,
    languages: str | None = Query(None, min_length=1, max_length=500),
    language_ids: str | None = Query(None, min_length=1, max_length=500),
    limit: int = Query(30, ge=1, le=50),
    cursor: str | None = Query(None, max_length=100),
):
    raw_languages = languages or language_ids
    if not raw_languages:
        raise HTTPException(status_code=422, detail="languages or language_ids is required")
    ids = list(dict.fromkeys(item.strip() for item in raw_languages.split(",") if item.strip()))
    configured = _service(request).languages
    normalized_ids = configured.normalize_ids(ids)
    unknown = [item for item in ids if not configured.resolve([item])]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_language_ids": unknown})
    result = await _service(request).artist_page(normalized_ids, limit, cursor)
    return envelope(result, count=len(result["items"]), language_ids=result["language_ids"])


@router.get("/search", summary="Search the catalog with separated result types.")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    type: Literal["song", "track", "artist", "album", "playlist"] | None = None,
    kind: Literal["song", "track", "artist", "album", "playlist"] | None = None,
    page: int = Query(1, ge=1, le=1000),
    limit: int = Query(20, ge=1, le=50),
):
    requested_type = type if type is not None else kind
    result = await _service(request).search(q.strip(), "song" if requested_type == "track" else requested_type, page, limit)
    return envelope(result, query=q.strip())


@router.get("/search/discover", summary="Get backend-provided search discovery sections.")
async def search_discover(request: Request, limit: int = Query(12, ge=1, le=30)):
    sections = await _service(request).discover(limit)
    return envelope({"sections": sections}, count=len(sections))


@router.get("/artists/{artist_id}", summary="Get an artist and its separated content sections.")
async def artist_details(
    request: Request,
    artist_id: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
    limit: int = Query(20, ge=1, le=50),
):
    result = await _service(request).artist_details(artist_id, limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Artist not found")
    return envelope(result)


@router.get("/albums/{album_id}", summary="Get an album and its backend-provided tracklist.")
async def album_details(
    request: Request,
    album_id: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
):
    result = await _service(request).album_details(album_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Album not found")
    return envelope(result)


@router.get("/me", summary="Get account, profile, and authoritative onboarding state.")
async def api_me(
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    profile = await repository.get_profile(user.uid)
    onboarding = await repository.get_onboarding(user.uid)
    return envelope({
        "id": user.uid,
        "email": user.email,
        "display_name": user.display_name,
        "photo_url": user.photo_url,
        "onboarding_completed": onboarding["completed"],
        "onboarding_step": onboarding["step"],
        "profile": profile,
    })


@router.put("/me/preferences/languages", summary="Persist selected onboarding language IDs.")
async def save_languages(
    request: Request,
    selection: IdSelection,
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    if selection.language_ids is None:
        raise HTTPException(status_code=422, detail="language_ids is required")
    configured = _service(request).languages
    unknown = [item for item in selection.language_ids if not configured.resolve([item])]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_language_ids": unknown})
    normalized_ids = configured.normalize_ids(selection.language_ids)
    names = [configured.by_id[item].name for item in normalized_ids]
    await repository.replace_language_preferences(user.uid, normalized_ids, names)
    return envelope(await repository.get_onboarding(user.uid))


@router.put("/me/preferences/artists", summary="Persist selected backend artist IDs.")
async def save_artists(
    request: Request,
    selection: IdSelection,
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    if selection.artist_ids is None:
        raise HTTPException(status_code=422, detail="artist_ids is required")
    # Best-effort name resolution — these IDs already came from our own catalog
    # API so we trust them. Don't block or 422 if Gaana is slow/unavailable.
    names: dict[str, str] = {}
    try:
        result = await request.app.state.gaanapy.get_artist_info(selection.artist_ids, False)
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("seokey") and item.get("name"):
                    names[item["seokey"]] = item["name"]
    except Exception:
        pass
    await repository.replace_selected_artists(
        user.uid, selection.artist_ids, [names.get(sid, "") for sid in selection.artist_ids]
    )
    return envelope(await repository.get_onboarding(user.uid))


@router.get("/home", summary="Get a personalized, backend-composed home feed.")
async def home(
    request: Request,
    refresh: bool = False,
    limit: int = Query(24, ge=1, le=50),
    cursor: str | None = Query(None, max_length=100),
    type: str = Query("all", pattern="^(all|song|album|artist|playlist)$"),
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
    recommendations=Depends(get_personalization_service),
):
    cache = getattr(request.app.state, "cache", None)
    key = f"home:{user.uid}:{type}:{limit}:{cursor or '0'}"
    if cache and not refresh:
        cached = await cache.get(key)
        if cached is not None:
            return envelope(cached, cached=True)
    try:
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="cursor must be a non-negative offset") from exc

    async def build():
        return await _service(request).home_page(
            user.uid, repository, recommendations, limit, offset, type
        )

    try:
        data = await coalesce(key, build)
        HOME_STALE[key] = data
        if cache:
            await cache.set(key, data, 300)
        return envelope(data, cached=False)
    except Exception:
        stale = HOME_STALE.get(key)
        if stale is not None:
            return envelope(stale, cached=True, stale=True)
        raise


@router.get("/me/recent-searches", summary="List synchronized recent searches.")
async def recent_searches(
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    values = await repository.list_recent_searches(user.uid, limit)
    return envelope({"items": values}, count=len(values))


@router.post("/me/recent-searches", status_code=status.HTTP_201_CREATED, summary="Save a recent search.")
async def save_recent_search(
    data: RecentSearchCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    value = await repository.save_recent_search(user.uid, data)
    return envelope(value)


@router.delete("/me/recent-searches/{search_id}", summary="Delete one recent search.")
async def delete_recent_search(
    search_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    if not await repository.delete_recent_search(user.uid, search_id):
        raise HTTPException(status_code=404, detail="Recent search not found")
    return envelope({"deleted": True})


@router.delete("/me/recent-searches", summary="Clear all recent searches.")
async def clear_recent_searches(
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    await repository.clear_recent_searches(user.uid)
    return envelope({"deleted": True})
