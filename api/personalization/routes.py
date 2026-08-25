from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from api.auth import AuthenticatedUser, get_current_user
from api.personalization.models import ListeningEvent, ProfileUpdate, TrackSnapshot
from api.personalization.repository import FirebaseUserRepository
from api.personalization.service import PersonalizedMusicService


router = APIRouter(prefix="/me", tags=["Personalization"])


async def get_user_repository(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FirebaseUserRepository:
    repository = getattr(request.app.state, "user_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase Realtime Database is unavailable",
        )
    await repository.ensure_user(user)
    return repository


def get_personalization_service(request: Request) -> PersonalizedMusicService:
    service = getattr(request.app.state, "personalization_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personalization is unavailable",
        )
    return service


@router.get("", summary="Get the authenticated user's account and profile.")
async def get_me(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return {
        "account": user.model_dump(mode="json"),
        "profile": await repository.get_profile(user.uid),
    }


@router.get("/profile", summary="Get personalization preferences.")
async def get_profile(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return await repository.get_profile(user.uid)


@router.patch("/profile", summary="Update personalization preferences.")
async def update_profile(
    update: ProfileUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    changes = update.model_dump(mode="json", exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="At least one profile field is required")
    return await repository.update_profile(user.uid, changes)


@router.get("/favorites", summary="List favorite tracks.")
async def list_favorites(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> list[dict[str, Any]]:
    return await repository.list_favorites(user.uid)


@router.post("/favorites", status_code=status.HTTP_201_CREATED, summary="Save a favorite track.")
async def save_favorite(
    track: TrackSnapshot,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    favorite, created = await repository.save_favorite(user.uid, track)
    return {"created": created, "track": favorite}


@router.delete("/favorites/{seokey}", summary="Remove a favorite track.")
async def remove_favorite(
    seokey: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    deleted = await repository.remove_favorite(user.uid, seokey)
    if not deleted:
        raise HTTPException(status_code=404, detail="Favorite track not found")
    return {"deleted": True}


@router.get("/history", summary="List recent listening history.")
async def list_history(
    limit: int = Query(25, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> list[dict[str, Any]]:
    return await repository.list_history(user.uid, limit)


@router.post("/history", status_code=status.HTTP_201_CREATED, summary="Record a listening event.")
async def add_history(
    event: ListeningEvent,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return await repository.add_history(user.uid, event)


@router.delete("/history", summary="Clear listening history.")
async def clear_history(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    await repository.clear_history(user.uid)
    return {"deleted": True}


@router.delete("/signals", summary="Reset learned personalization signals.")
async def clear_signals(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    await repository.clear_signals(user.uid)
    return {"deleted": True}


@router.get("/recommendations", summary="Get personalized track recommendations.")
async def recommendations(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
    service: PersonalizedMusicService = Depends(get_personalization_service),
) -> list[dict[str, Any]]:
    catalog = getattr(request.app.state, "gaanapy", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="Music catalog is unavailable")
    return await service.recommendations(user.uid, catalog, limit)
