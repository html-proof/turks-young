from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from api.auth import AuthenticatedUser, get_current_user
from api.catalog.models import IdSelection, RecentSearchCreate, envelope
from api.personalization.models import OnboardingUpdate
from api.personalization.routes import get_personalization_service, get_user_repository


router = APIRouter(prefix="/api", tags=["Music Hub Catalog"])


def _service(request: Request):
    service = getattr(request.app.state, "catalog_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Music catalog is unavailable")
    return service


@router.get("/languages", summary="List backend-configured onboarding languages.")
async def languages(request: Request):
    service = _service(request)
    values = [value.model_dump(mode="json") for value in service.languages.languages]
    return envelope({"items": values}, count=len(values))


@router.get("/artists", summary="List artists relevant to selected languages.")
async def relevant_artists(
    request: Request,
    languages: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(30, ge=1, le=50),
):
    ids = list(dict.fromkeys(item.strip() for item in languages.split(",") if item.strip()))
    configured = _service(request).languages
    unknown = [item for item in ids if item not in configured.by_id]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_language_ids": unknown})
    values = await _service(request).artists_for_languages(ids, limit)
    return envelope({"items": values}, count=len(values), language_ids=ids)


@router.get("/search", summary="Search the catalog with separated result types.")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    type: Literal["song", "artist", "album", "playlist"] | None = None,
    page: int = Query(1, ge=1, le=1000),
    limit: int = Query(20, ge=1, le=50),
):
    result = await _service(request).search(q.strip(), type, page, limit)
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
    unknown = [item for item in selection.language_ids if item not in configured.by_id]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_language_ids": unknown})
    names = [configured.by_id[item].name for item in selection.language_ids]
    await repository.update_profile(
        user.uid, {"languages": names, "language_ids": selection.language_ids}
    )
    state = await repository.update_onboarding(
        user.uid,
        OnboardingUpdate(step="artist", completed=False),
    )
    return envelope(state)


@router.put("/me/preferences/artists", summary="Persist selected backend artist IDs.")
async def save_artists(
    request: Request,
    selection: IdSelection,
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
):
    if selection.artist_ids is None:
        raise HTTPException(status_code=422, detail="artist_ids is required")
    result = await request.app.state.gaanapy.get_artist_info(selection.artist_ids, False)
    valid_items = {
        item.get("seokey"): item for item in result
        if isinstance(item, dict) and item.get("seokey")
    } if isinstance(result, list) else {}
    valid_ids = set(valid_items)
    unknown = [item for item in selection.artist_ids if item not in valid_ids]
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_artist_ids": unknown})
    names = [valid_items[item].get("name", "") for item in selection.artist_ids]
    await repository.update_profile(
        user.uid, {"favorite_artists": names, "favorite_artist_ids": selection.artist_ids}
    )
    state = await repository.update_onboarding(
        user.uid,
        OnboardingUpdate(step="complete", completed=True),
    )
    return envelope(state)


@router.get("/home", summary="Get a personalized, backend-composed home feed.")
async def home(
    request: Request,
    refresh: bool = False,
    limit: int = Query(20, ge=1, le=50),
    user: AuthenticatedUser = Depends(get_current_user),
    repository=Depends(get_user_repository),
    recommendations=Depends(get_personalization_service),
):
    cache = getattr(request.app.state, "cache", None)
    key = f"home:{user.uid}:{limit}"
    if cache and not refresh:
        cached = await cache.get(key)
        if cached is not None:
            return envelope(cached, cached=True)
    sections = await _service(request).home(user.uid, repository, recommendations, limit)
    data = {"sections": sections}
    if cache:
        await cache.set(key, data, 300)
    return envelope(data, cached=False)


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
