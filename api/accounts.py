"""Account lifecycle. The durable job survives process and external-service failures."""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from firebase_admin import auth as firebase_auth

from api.auth import get_current_user, get_firebase_user, AuthenticatedUser
from api.core.home_state import invalidate_home

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Account lifecycle"])


class AccountDeletionService:
    def __init__(self, pool, firebase, cache):
        self.pool, self.firebase, self.cache = pool, firebase, cache

    async def delete_account(self, firebase_uid: str) -> None:
        # Commit the access barrier and recovery job before any destructive work.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT uid FROM users WHERE firebase_uid=$1 FOR UPDATE", firebase_uid)
                await conn.execute(
                    "INSERT INTO account_deletion_jobs(firebase_uid,user_id) VALUES($1,$2) "
                    "ON CONFLICT(firebase_uid) DO NOTHING", firebase_uid, row['uid'] if row else None)
                await conn.execute("UPDATE users SET account_status='deletion_pending', updated_at=now() WHERE firebase_uid=$1", firebase_uid)
        logger.info("account deletion requested")
        await self.finish(firebase_uid)

    async def finish(self, firebase_uid: str) -> None:
        async with self.pool.acquire() as conn:
            # Row lock serializes retries across API workers; pending status was
            # already committed, so a failed external call cannot restore access.
            async with conn.transaction():
                job = await conn.fetchrow("SELECT * FROM account_deletion_jobs WHERE firebase_uid=$1 FOR UPDATE", firebase_uid)
                if not job:
                    return
                logger.info("account deletion started")
                await self.firebase.disable_and_revoke(firebase_uid)
                for identity in filter(None, [firebase_uid, job['user_id']]):
                    invalidate_home(identity)
                    if self.cache:
                        await self.cache.delete_user_data(identity)
                await conn.execute("DELETE FROM users WHERE firebase_uid=$1", firebase_uid)
                logger.info("account database cleanup completed")
                await self.firebase.delete_user(firebase_uid)
                logger.info("account auth deletion completed")
                await conn.execute("DELETE FROM account_deletion_jobs WHERE firebase_uid=$1", firebase_uid)
        logger.info("account deletion completed")

    async def run(self) -> None:
        """Retry durable jobs, including jobs created by direct database deletion."""
        cursor = ''
        while True:
            try:
                async with self.pool.acquire() as conn:
                    jobs = await conn.fetch("SELECT firebase_uid FROM account_deletion_jobs ORDER BY requested_at LIMIT 100")
                for job in jobs:
                    try:
                        await self.finish(job['firebase_uid'])
                    except Exception:
                        logger.warning("account cleanup failed; retry scheduled")
                # Firebase console deletions have no database trigger. Reconcile
                # bounded pages without treating Firebase outages as deletions.
                async with self.pool.acquire() as conn:
                    users = await conn.fetch("SELECT firebase_uid FROM users WHERE firebase_uid>$1 ORDER BY firebase_uid LIMIT 100", cursor)
                for user in users:
                    cursor = user['firebase_uid']
                    try:
                        await self.firebase.creation_time(cursor)
                    except firebase_auth.UserNotFoundError:
                        await self.delete_account(cursor)
                if len(users) < 100:
                    cursor = ''
            except Exception:
                logger.warning("account cleanup worker unavailable; retry scheduled")
            await asyncio.sleep(30)


@router.get("/session/validate")
@router.get("/api/v1/session/validate")
async def validate_session(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return {"valid": True, "user_id": request.state.account['uid'], "status": "ACTIVE"}


async def delete_self(request: Request, user: AuthenticatedUser):
    if time.time() - user.auth_time > 300:
        raise HTTPException(403, detail={"code": "REAUTHENTICATION_REQUIRED"})
    service = getattr(request.app.state, 'account_deletion', None)
    if service is None:
        raise HTTPException(503, "Account service unavailable")
    await service.delete_account(user.uid)
    return {"deleted": True}


@router.delete("/account")
async def delete_current(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return await delete_self(request, user)


@router.delete("/admin/accounts/{firebase_uid}")
async def admin_delete(firebase_uid: str, request: Request, user: AuthenticatedUser = Depends(get_firebase_user)):
    if not user.admin:
        raise HTTPException(403, "Administrator access required")
    service = getattr(request.app.state, 'account_deletion', None)
    if service is None:
        raise HTTPException(503, "Account service unavailable")
    await service.delete_account(firebase_uid)
    return {"deleted": True}
