from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from api.auth import AuthenticatedUser, get_current_user
from api.personalization.models import PulseCommentCreate, PulsePostCreate
from api.personalization.repository import PostgresUserRepository


router = APIRouter(prefix="/pulse", tags=["Pulse"])


def _repo(request: Request) -> PostgresUserRepository:
    repo = getattr(request.app.state, "user_repository", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return repo


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
    if not await _repo(request).like_post(user.uid, post_id):
        raise HTTPException(status_code=404, detail="Post not found")


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
    comment = await _repo(request).add_comment(user.uid, post_id, data)
    if comment is None:
        raise HTTPException(status_code=404, detail="Post not found")
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
