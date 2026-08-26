import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from api.auth import AuthenticatedUser, get_current_user
from api.firebase import FirebaseRuntime
from api.personalization.models import PulseCommentCreate, PulsePostCreate
from api.personalization.repository import PostgresUserRepository


router = APIRouter(prefix="/pulse", tags=["Pulse"])
api_router = APIRouter(prefix="/api", tags=["Personalized Pulse"])


def _repo(request: Request) -> PostgresUserRepository:
    repo = getattr(request.app.state, "user_repository", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return repo


def _firebase(request: Request) -> FirebaseRuntime:
    return request.app.state.firebase


@api_router.get("/pulse", summary="Get the authenticated user's personalized Pulse feed.")
async def personalized_feed(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = Query(None, max_length=200),
    refresh: bool = Query(False),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    repo = _repo(request)
    account = await repo.get_account(user.uid)
    if account is None:
        await repo.ensure_user(user)
        account = await repo.get_account(user.uid)
    if not account or account.get("account_status") != "active":
        raise HTTPException(status_code=401, detail="Account is unavailable")
    cache = getattr(request.app.state, "cache", None)
    key = f"pulse:user:{user.uid}:{limit}:{cursor or '0'}"
    if cache and not refresh:
        cached = await cache.get(key)
        if cached is not None:
            return {**cached, "cached": True}
    try:
        data = await repo.get_personalized_pulse(user.uid, limit, cursor)
        if cache:
            await cache.set_with_stale(key, data, 120, 900)
        return {**data, "cached": False}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api_router.post("/pulse/{item_id}/state", summary="Update a Pulse item's read state.")
async def update_pulse_state(
    item_id: UUID,
    request: Request,
    state: str = Query(..., pattern="^(seen|opened|read)$"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, bool]:
    if not await _repo(request).mark_pulse_state(user.uid, item_id, state):
        raise HTTPException(status_code=404, detail="Pulse item not found")
    return {"updated": True}


async def _notify(
    request: Request,
    target_uid: str,
    actor_name: str,
    type_: str,
    title: str,
    body: str,
    data: dict[str, str],
) -> None:
    """Save an in-app notification and send an FCM push. Fire-and-forget."""
    repo = _repo(request)
    firebase = _firebase(request)
    try:
        await repo.create_notification(target_uid, type_, title, body, data)
        tokens = await repo.get_device_tokens(target_uid)
        if tokens:
            await firebase.send_push_notification(tokens, title, body, data)
    except Exception:
        pass  # notifications are best-effort


@router.get("/feed", summary="Get Pulse feed.")
async def get_feed(
    request: Request,
    feed_type: str = Query("foryou", pattern="^(foryou|following)$"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return await _repo(request).get_pulse_feed(user.uid, limit, offset, feed_type)


@router.post("/posts", status_code=status.HTTP_201_CREATED, summary="Create a Pulse post.")
async def create_post(
    request: Request,
    data: PulsePostCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    return await _repo(request).create_pulse_post(user.uid, data)


@router.get("/posts/{post_id}", summary="Get a Pulse post.")
async def get_post(
    request: Request,
    post_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    post = await _repo(request).get_pulse_post(post_id, user.uid)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.delete("/posts/{post_id}", summary="Delete a Pulse post.")
async def delete_post(
    request: Request,
    post_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, bool]:
    if not await _repo(request).delete_pulse_post(user.uid, post_id):
        raise HTTPException(status_code=404, detail="Post not found")
    return {"deleted": True}


@router.post("/posts/{post_id}/likes", status_code=status.HTTP_204_NO_CONTENT, summary="Like a post.")
async def like_post(
    request: Request,
    post_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    repo = _repo(request)
    post = await repo.get_pulse_post(post_id, user.uid)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    await repo.like_post(user.uid, post_id)
    if post["uid"] != user.uid:
        actor = user.display_name or "Someone"
        asyncio.create_task(_notify(
            request, post["uid"], actor,
            "pulse_like",
            f"{actor} liked your post",
            post.get("body", "")[:80] or "Check it out",
            {"type": "pulse_like", "post_id": str(post_id)},
        ))


@router.delete("/posts/{post_id}/likes", status_code=status.HTTP_204_NO_CONTENT, summary="Unlike a post.")
async def unlike_post(
    request: Request,
    post_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    await _repo(request).unlike_post(user.uid, post_id)


@router.get("/posts/{post_id}/comments", summary="Get post comments.")
async def get_comments(
    request: Request,
    post_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return await _repo(request).get_post_comments(post_id, limit, offset)


@router.post(
    "/posts/{post_id}/comments",
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment.",
)
async def add_comment(
    request: Request,
    post_id: UUID,
    data: PulseCommentCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    repo = _repo(request)
    comment = await repo.add_comment(user.uid, post_id, data)
    if comment is None:
        raise HTTPException(status_code=404, detail="Post not found")
    post = await repo.get_pulse_post(post_id, user.uid)
    if post and post["uid"] != user.uid:
        actor = user.display_name or "Someone"
        asyncio.create_task(_notify(
            request, post["uid"], actor,
            "pulse_comment",
            f"{actor} commented on your post",
            data.body[:80],
            {"type": "pulse_comment", "post_id": str(post_id)},
        ))
    return comment


@router.delete("/comments/{comment_id}", summary="Delete a comment.")
async def delete_comment(
    request: Request,
    comment_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, bool]:
    if not await _repo(request).delete_comment(user.uid, comment_id):
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"deleted": True}
