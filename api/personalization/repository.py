import asyncio
import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from firebase_admin import db

from api.auth import AuthenticatedUser
from api.personalization.models import ListeningEvent, TrackSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_user_key(uid: str) -> str:
    return base64.urlsafe_b64encode(uid.encode("utf-8")).decode("ascii").rstrip("=")


def _signal_key(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:24]


class FirebaseUserRepository:
    """Async façade over the blocking Firebase Realtime Database Python API."""

    def __init__(self, firebase_app: Any) -> None:
        self.firebase_app = firebase_app
        self._ensured_users: set[str] = set()
        self._ensure_lock = asyncio.Lock()

    def _ref(self, uid: str, child: str = "") -> db.Reference:
        path = f"users/{_safe_user_key(uid)}"
        if child:
            path = f"{path}/{child.strip('/')}"
        return db.reference(path, app=self.firebase_app)

    @staticmethod
    async def _run(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(call, *args, **kwargs)

    async def ensure_user(self, user: AuthenticatedUser) -> None:
        if user.uid in self._ensured_users:
            return

        async with self._ensure_lock:
            if user.uid in self._ensured_users:
                return
            await self._create_user_if_needed(user)
            self._ensured_users.add(user.uid)

    async def _create_user_if_needed(self, user: AuthenticatedUser) -> None:
        now = utc_now()
        account = {
            "uid": user.uid,
            "email": user.email,
            "display_name": user.display_name,
            "photo_url": user.photo_url,
            "provider": user.provider,
            "last_seen_at": now,
        }
        account = {key: value for key, value in account.items() if value is not None}
        await self._run(self._ref(user.uid, "account").update, account)

        def create_profile(current: Any) -> dict[str, Any]:
            if isinstance(current, dict):
                return current
            return {
                "display_name": user.display_name or "",
                "languages": ["English"],
                "favorite_genres": [],
                "favorite_artists": [],
                "created_at": now,
                "updated_at": now,
            }

        await self._run(self._ref(user.uid, "profile").transaction, create_profile)

    async def get_profile(self, uid: str) -> dict[str, Any]:
        profile = await self._run(self._ref(uid, "profile").get)
        return profile if isinstance(profile, dict) else {}

    async def update_profile(self, uid: str, changes: dict[str, Any]) -> dict[str, Any]:
        if not changes:
            return await self.get_profile(uid)
        changes["updated_at"] = utc_now()
        await self._run(self._ref(uid, "profile").update, changes)
        return await self.get_profile(uid)

    async def list_favorites(self, uid: str) -> list[dict[str, Any]]:
        value = await self._run(self._ref(uid, "favorites/tracks").get)
        if not isinstance(value, dict):
            return []
        favorites = [item for item in value.values() if isinstance(item, dict)]
        return sorted(favorites, key=lambda item: item.get("favorited_at", ""), reverse=True)

    async def save_favorite(self, uid: str, track: TrackSnapshot) -> tuple[dict[str, Any], bool]:
        reference = self._ref(uid, f"favorites/tracks/{track.seokey}")
        existing = await self._run(reference.get)
        record = track.model_dump(mode="json")
        record["favorited_at"] = utc_now()
        await self._run(reference.set, record)
        created = not isinstance(existing, dict)
        if created:
            await self.adjust_signals(uid, track, 5.0)
        return record, created

    async def remove_favorite(self, uid: str, seokey: str) -> bool:
        reference = self._ref(uid, f"favorites/tracks/{seokey}")
        existing = await self._run(reference.get)
        if not isinstance(existing, dict):
            return False
        await self._run(reference.delete)
        await self.adjust_signals(uid, TrackSnapshot.model_validate(existing), -5.0)
        return True

    async def add_history(self, uid: str, event: ListeningEvent) -> dict[str, Any]:
        record = event.model_dump(mode="json")
        record["played_at"] = utc_now()
        reference = await self._run(self._ref(uid, "history").push, record)
        record["event_id"] = reference.key
        weight = 2.0 if event.completed else 1.0
        await self.adjust_signals(uid, event, weight)
        return record

    async def list_history(self, uid: str, limit: int = 25) -> list[dict[str, Any]]:
        query = self._ref(uid, "history").order_by_key().limit_to_last(limit)
        value = await self._run(query.get)
        if not isinstance(value, dict):
            return []
        history = [
            {**item, "event_id": event_id}
            for event_id, item in value.items()
            if isinstance(item, dict)
        ]
        return sorted(history, key=lambda item: item.get("played_at", ""), reverse=True)

    async def clear_history(self, uid: str) -> None:
        await self._run(self._ref(uid, "history").delete)

    async def get_signals(self, uid: str) -> dict[str, Any]:
        value = await self._run(self._ref(uid, "signals").get)
        return value if isinstance(value, dict) else {}

    async def clear_signals(self, uid: str) -> None:
        await self._run(self._ref(uid, "signals").delete)

    async def adjust_signals(self, uid: str, track: TrackSnapshot, amount: float) -> None:
        values = {
            "artists": track.artists,
            "genres": track.genres,
            "languages": [track.language] if track.language else [],
        }

        def update_signals(current: Any) -> dict[str, Any]:
            signals = current if isinstance(current, dict) else {}
            for bucket, items in values.items():
                bucket_data = signals.setdefault(bucket, {})
                for value in items:
                    key = _signal_key(value)
                    existing = bucket_data.get(key)
                    score = float(existing.get("score", 0)) if isinstance(existing, dict) else 0.0
                    new_score = max(0.0, score + amount)
                    if new_score == 0:
                        bucket_data.pop(key, None)
                    else:
                        bucket_data[key] = {"value": value, "score": new_score}
            return signals

        await self._run(self._ref(uid, "signals").transaction, update_signals)
