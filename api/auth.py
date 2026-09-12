from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import auth as firebase_auth
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
    auth_time: int = 0
    admin: bool = False


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


def invalid_account(reason: str) -> HTTPException:
    return HTTPException(401, detail={"code": "ACCOUNT_INVALID", "reason": reason})


async def get_firebase_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise invalid_account("INVALID_TOKEN")

    runtime = get_firebase_runtime(request)
    try:
        claims: dict[str, Any] = await runtime.verify_id_token(credentials.credentials)
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(401, detail={"code": "TOKEN_EXPIRED"}) from None
    except firebase_auth.RevokedIdTokenError:
        raise invalid_account("TOKEN_REVOKED") from None
    except firebase_auth.UserDisabledError:
        raise invalid_account("ACCOUNT_DISABLED") from None
    except firebase_auth.UserNotFoundError:
        raise invalid_account("USER_NOT_FOUND") from None
    except (ValueError, firebase_auth.InvalidIdTokenError):
        raise invalid_account("INVALID_TOKEN") from None
    except firebase_exceptions.FirebaseError:
        raise HTTPException(503, "Authentication service unavailable") from None

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
        auth_time=int(claims.get("auth_time", 0)),
        admin=claims.get("admin") is True,
    )


async def get_current_user(
    request: Request,
    user: AuthenticatedUser = Depends(get_firebase_user),
) -> AuthenticatedUser:
    repository = getattr(request.app.state, "user_repository", None)
    if repository is None:
        raise HTTPException(503, "Database is unavailable")
    account = await repository.get_session_account(user.uid)
    if account is None:
        raise invalid_account("USER_NOT_FOUND")
    state = account.get("account_status", "").lower()
    if state != "active":
        raise invalid_account("ACCOUNT_DISABLED" if state in ("disabled", "suspended") else "ACCOUNT_DELETED")
    request.state.account = account
    return user


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser | None:
    """Optionally authenticate a user from Firebase ID token, returning None for guest calls."""
    if not credentials or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        return None
    user = await get_firebase_user(request, credentials)
    return await get_current_user(request, user)

