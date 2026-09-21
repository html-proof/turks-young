import re

from fastapi import APIRouter, Depends, Response, status

from music_hub.container import Container
from music_hub.dependencies import AuthenticatedUser, get_container, require_user


router = APIRouter(prefix="/account", tags=["account"])

_GLOB_META = re.compile(r"([\\*?\[\]])")


def _glob_escape(value: str) -> str:
    """Escape Redis glob metacharacters so a user id can only match its own keys."""
    return _GLOB_META.sub(r"\\\1", value)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_account(
    current: AuthenticatedUser = Depends(require_user),
    container: Container = Depends(get_container),
):
    # The authenticated token supplies both identifiers; the client never
    # chooses which account is deleted.
    await container.firebase.delete_user(current.identity.uid)
    await container.users_repository.delete(current.id)
    await container.cache.delete_pattern(f"*:{_glob_escape(current.id)}*")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
