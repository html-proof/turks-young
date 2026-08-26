from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from api.auth import AuthenticatedUser, get_current_user
from api.personalization.models import (
    AlbumSnapshot,
    ArtistSnapshot,
    ListeningEvent,
    ProfileUpdate,
    TrackSnapshot,
    UserPlaylistCreate,
    UserPlaylistUpdate,
)
from api.personalization.repository import PostgresUserRepository as FirebaseUserRepository
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
            detail="Database is unavailable",
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


@router.post(
    "/recommendations/refresh",
    summary="Rebuild taste signals from full listening history.",
    status_code=status.HTTP_200_OK,
)
async def refresh_signals(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
    service: PersonalizedMusicService = Depends(get_personalization_service),
) -> dict[str, Any]:
    """
    Replays the user's complete listening history and rebuilds the implicit
    taste signals from scratch.  Call this after importing history or when
    recommendations feel stale.
    """
    count = await service.rebuild_signals_from_history(user.uid)
    return {"rebuilt": True, "events_processed": count}


@router.get(
    "/taste",
    summary="Show the computed music taste profile used for recommendations.",
)
async def get_taste(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    """
    Returns the merged preference-score map (artists, genres, languages) that
    drives recommendations — useful for debugging and the 'Edit taste' UI.
    """
    import asyncio as _asyncio
    from api.personalization.scorer import build_preference_scores, preferred_languages

    profile, favorites, history, signals = await _asyncio.gather(
        repository.get_profile(user.uid),
        repository.list_favorites(user.uid),
        repository.list_history(user.uid, 100),
        repository.get_signals(user.uid),
    )

    prefs = build_preference_scores(profile, favorites, history, signals)
    langs = preferred_languages(profile, prefs, max_languages=5)

    def top_n(bucket: dict[str, float], n: int = 10) -> list[dict[str, Any]]:
        return [
            {"name": name.title(), "score": round(score, 1)}
            for name, score in sorted(bucket.items(), key=lambda x: x[1], reverse=True)[:n]
        ]

    return {
        "preferred_languages": langs,
        "top_artists": top_n(prefs["artists"]),
        "top_genres":  top_n(prefs["genres"]),
        "is_cold_start": sum(sum(v.values()) for v in prefs.values()) < 10.0,
    }


@router.get("/playlists", summary="List the user's playlists.")
async def list_user_playlists(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> list[dict[str, Any]]:
    return await repository.list_playlists(user.uid)


@router.post("/playlists", status_code=status.HTTP_201_CREATED, summary="Create a playlist.")
async def create_user_playlist(
    playlist: UserPlaylistCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return await repository.create_playlist(user.uid, playlist)


@router.get("/playlists/{playlist_id}", summary="Get one user playlist.")
async def get_user_playlist(
    playlist_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    playlist = await repository.get_playlist(user.uid, playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


@router.patch("/playlists/{playlist_id}", summary="Update a user playlist.")
async def update_user_playlist(
    playlist_id: UUID,
    update: UserPlaylistUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    playlist = await repository.update_playlist(user.uid, playlist_id, update)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


@router.delete("/playlists/{playlist_id}", summary="Delete a user playlist.")
async def delete_user_playlist(
    playlist_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    if not await repository.delete_playlist(user.uid, playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"deleted": True}


@router.post("/playlists/{playlist_id}/tracks", summary="Add a track to a playlist.")
async def add_user_playlist_track(
    playlist_id: UUID,
    track: TrackSnapshot,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    playlist = await repository.add_playlist_track(user.uid, playlist_id, track)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


@router.delete("/playlists/{playlist_id}/tracks/{seokey}", summary="Remove a playlist track.")
async def remove_user_playlist_track(
    playlist_id: UUID,
    seokey: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    playlist = await repository.remove_playlist_track(user.uid, playlist_id, seokey)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


@router.get("/artists", summary="List followed artists.")
async def list_followed_artists(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> list[dict[str, Any]]:
    return await repository.list_followed_artists(user.uid)


@router.post("/artists", status_code=status.HTTP_201_CREATED, summary="Follow an artist.")
async def follow_artist(
    artist: ArtistSnapshot,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return await repository.follow_artist(user.uid, artist)


@router.delete("/artists/{seokey}", summary="Unfollow an artist.")
async def unfollow_artist(
    seokey: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    if not await repository.unfollow_artist(user.uid, seokey):
        raise HTTPException(status_code=404, detail="Followed artist not found")
    return {"deleted": True}


@router.get("/albums", summary="List saved albums.")
async def list_saved_albums(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> list[dict[str, Any]]:
    return await repository.list_saved_albums(user.uid)


@router.post("/albums", status_code=status.HTTP_201_CREATED, summary="Save an album.")
async def save_album(
    album: AlbumSnapshot,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, Any]:
    return await repository.save_album(user.uid, album)


@router.delete("/albums/{seokey}", summary="Remove a saved album.")
async def remove_saved_album(
    seokey: str = Path(..., min_length=1, max_length=200, pattern=r"^[a-z0-9\-]+$"),
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    if not await repository.remove_saved_album(user.uid, seokey):
        raise HTTPException(status_code=404, detail="Saved album not found")
    return {"deleted": True}


@router.delete("/account", summary="Permanently delete the authenticated account.")
async def delete_account(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: FirebaseUserRepository = Depends(get_user_repository),
) -> dict[str, bool]:
    await repository.delete_account(user.uid)
    await request.app.state.firebase.delete_user(user.uid)
    return {"deleted": True}
