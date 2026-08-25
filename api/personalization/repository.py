import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from api.auth import AuthenticatedUser
from api.personalization.models import ListeningEvent, TrackSnapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signal_key(value: str) -> str:
    return hashlib.sha256(value.casefold().encode()).hexdigest()[:24]


class PostgresUserRepository:
    """User-data repository backed by Supabase PostgreSQL (asyncpg)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_user(self, user: AuthenticatedUser) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (uid, email, display_name, photo_url, provider, last_seen_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (uid) DO UPDATE
                  SET email        = EXCLUDED.email,
                      display_name = EXCLUDED.display_name,
                      photo_url    = EXCLUDED.photo_url,
                      last_seen_at = now()
                """,
                user.uid, user.email, user.display_name, user.photo_url, user.provider,
            )
            await conn.execute(
                """
                INSERT INTO user_profiles (uid, display_name)
                VALUES ($1, $2)
                ON CONFLICT (uid) DO NOTHING
                """,
                user.uid, user.display_name or "",
            )

    async def get_profile(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT display_name, languages, favorite_genres, favorite_artists, "
                "created_at, updated_at FROM user_profiles WHERE uid = $1",
                uid,
            )
        if row is None:
            return {}
        return {
            "display_name": row["display_name"],
            "languages": list(row["languages"]),
            "favorite_genres": list(row["favorite_genres"]),
            "favorite_artists": list(row["favorite_artists"]),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def update_profile(self, uid: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"display_name", "languages", "favorite_genres", "favorite_artists"}
        filtered = {k: v for k, v in changes.items() if k in allowed}
        if not filtered:
            return await self.get_profile(uid)

        # Build SET clause dynamically
        parts = []
        values: list[Any] = []
        for i, (col, val) in enumerate(filtered.items(), start=1):
            parts.append(f"{col} = ${i}")
            values.append(val)

        # updated_at
        parts.append(f"updated_at = ${len(values) + 1}")
        values.append(datetime.now(timezone.utc))
        # WHERE uid
        values.append(uid)

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE user_profiles SET {', '.join(parts)} WHERE uid = ${len(values)}",
                *values,
            )
        return await self.get_profile(uid)

    async def list_favorites(self, uid: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT track, favorited_at FROM user_favorites "
                "WHERE uid = $1 ORDER BY favorited_at DESC",
                uid,
            )
        result = []
        for row in rows:
            item = json.loads(row["track"]) if isinstance(row["track"], str) else dict(row["track"])
            item["favorited_at"] = row["favorited_at"].isoformat()
            result.append(item)
        return result

    async def save_favorite(self, uid: str, track: TrackSnapshot) -> tuple[dict[str, Any], bool]:
        record = track.model_dump(mode="json")
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT 1 FROM user_favorites WHERE uid = $1 AND seokey = $2",
                uid, track.seokey,
            )
            created = existing is None
            await conn.execute(
                """
                INSERT INTO user_favorites (uid, seokey, track, favorited_at)
                VALUES ($1, $2, $3::jsonb, now())
                ON CONFLICT (uid, seokey) DO UPDATE
                  SET track = EXCLUDED.track, favorited_at = now()
                """,
                uid, track.seokey, json.dumps(record),
            )
        if created:
            await self.adjust_signals(uid, track, 5.0)
        record["favorited_at"] = _utc_now()
        return record, created

    async def remove_favorite(self, uid: str, seokey: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM user_favorites WHERE uid = $1 AND seokey = $2 RETURNING track",
                uid, seokey,
            )
        if row is None:
            return False
        track_data = json.loads(row["track"]) if isinstance(row["track"], str) else dict(row["track"])
        snap = TrackSnapshot.model_validate(track_data)
        await self.adjust_signals(uid, snap, -5.0)
        return True

    async def add_history(self, uid: str, event: ListeningEvent) -> dict[str, Any]:
        record = event.model_dump(mode="json")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO user_history (uid, event, played_at) VALUES ($1, $2::jsonb, now()) "
                "RETURNING id, played_at",
                uid, json.dumps(record),
            )
        record["event_id"] = str(row["id"])
        record["played_at"] = row["played_at"].isoformat()
        weight = 2.0 if event.completed else 1.0
        await self.adjust_signals(uid, event, weight)
        return record

    async def list_history(self, uid: str, limit: int = 25) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, event, played_at FROM user_history "
                "WHERE uid = $1 ORDER BY played_at DESC LIMIT $2",
                uid, limit,
            )
        result = []
        for row in rows:
            item = json.loads(row["event"]) if isinstance(row["event"], str) else dict(row["event"])
            item["event_id"] = str(row["id"])
            item["played_at"] = row["played_at"].isoformat()
            result.append(item)
        return result

    async def clear_history(self, uid: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM user_history WHERE uid = $1", uid)

    async def get_signals(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT bucket, key, value, score FROM user_signals WHERE uid = $1",
                uid,
            )
        result: dict[str, Any] = {}
        for row in rows:
            bucket = result.setdefault(row["bucket"], {})
            bucket[row["key"]] = {"value": row["value"], "score": row["score"]}
        return result

    async def clear_signals(self, uid: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM user_signals WHERE uid = $1", uid)

    async def adjust_signals(self, uid: str, track: TrackSnapshot, amount: float) -> None:
        buckets = {
            "artists": track.artists,
            "genres": track.genres,
            "languages": [track.language] if track.language else [],
        }
        async with self._pool.acquire() as conn:
            for bucket, items in buckets.items():
                for value in items:
                    key = _signal_key(value)
                    if amount > 0:
                        await conn.execute(
                            """
                            INSERT INTO user_signals (uid, bucket, key, value, score)
                            VALUES ($1, $2, $3, $4, $5)
                            ON CONFLICT (uid, bucket, key) DO UPDATE
                              SET score = GREATEST(0, user_signals.score + $5)
                            """,
                            uid, bucket, key, value, amount,
                        )
                    else:
                        # Decrement and remove if score hits zero
                        await conn.execute(
                            """
                            UPDATE user_signals
                            SET score = GREATEST(0, score + $4)
                            WHERE uid = $1 AND bucket = $2 AND key = $3
                            """,
                            uid, bucket, key, amount,
                        )
                        await conn.execute(
                            "DELETE FROM user_signals WHERE uid = $1 AND bucket = $2 AND key = $3 AND score = 0",
                            uid, bucket, key,
                        )


# Keep the old name as an alias so nothing else needs updating
FirebaseUserRepository = PostgresUserRepository
