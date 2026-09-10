from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import exceptions as firebase_exceptions
from pydantic import BaseModel, ConfigDict

from api.firebase import FirebaseRuntime


bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uid: str
    email: str | None = None
    display_name: str | None = None
    photo_url: str | None = None
    provider: str | None = None


def account_error(code: str, message: str, status_code: int = status.HTTP_401_UNAUTHORIZED) -> HTTPException:
    """Return the stable error shape consumed by the client auth interceptor."""
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


def get_firebase_runtime(request: Request) -> FirebaseRuntime:
    runtime = getattr(request.app.state, "firebase", None)
    if not runtime or not runtime.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase is not configured for this deployment",
        )
    return runtime


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A Firebase ID token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    runtime = get_firebase_runtime(request)
    try:
        claims: dict[str, Any] = await runtime.verify_id_token(credentials.credentials)
    except (ValueError, firebase_exceptions.FirebaseError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked Firebase ID token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase token does not contain a user ID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    firebase_claims = claims.get("firebase") or {}
    return AuthenticatedUser(
        uid=uid,
        email=claims.get("email"),
        display_name=claims.get("name"),
        photo_url=claims.get("picture"),
        provider=firebase_claims.get("sign_in_provider"),
    )


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser | None:
    """Optionally authenticate a user from Firebase ID token, returning None for guest calls."""
    if not credentials or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        return None
    runtime = getattr(request.app.state, "firebase", None)
    if not runtime or not runtime.enabled:
        return None
    try:
        claims: dict[str, Any] = await runtime.verify_id_token(credentials.credentials)
        uid = claims.get("uid") or claims.get("sub")
        if not uid:
            return None
        firebase_claims = claims.get("firebase") or {}
        return AuthenticatedUser(
            uid=uid,
            email=claims.get("email"),
            display_name=claims.get("name"),
            photo_url=claims.get("picture"),
            provider=firebase_claims.get("sign_in_provider"),
        )
    except Exception:
        return None

