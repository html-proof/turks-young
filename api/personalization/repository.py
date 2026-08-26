import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from api.auth import AuthenticatedUser
from api.personalization.models import (
    AlbumSnapshot,
    ArtistSnapshot,
    DeviceRegister,
    ListeningEvent,
    OnboardingUpdate,
    PlayerSessionUpdate,
    PulseCommentCreate,
    PulsePostCreate,
    TrackOrderUpdate,
    TrackSnapshot,
    UserPlaylistCreate,
    UserPlaylistUpdate,
)


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
                "SELECT display_name, languages, favorite_genres, favorite_artists, onboarding_completed, "
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
            "onboarding_completed": row["onboarding_completed"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def update_profile(self, uid: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "display_name", "languages", "favorite_genres", "favorite_artists",
            "onboarding_completed",
        }
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

    async def list_playlists(self, uid: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, description, is_public, tracks, created_at, updated_at "
                "FROM user_playlists WHERE uid = $1 ORDER BY updated_at DESC",
                uid,
            )
        return [self._playlist_record(row) for row in rows]

    async def get_playlist(self, uid: str, playlist_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, description, is_public, tracks, created_at, updated_at "
                "FROM user_playlists WHERE uid = $1 AND id = $2",
                uid, playlist_id,
            )
        return self._playlist_record(row) if row else None

    async def create_playlist(self, uid: str, data: UserPlaylistCreate) -> dict[str, Any]:
        playlist_id = uuid.uuid4()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO user_playlists (id, uid, name, description, is_public) "
                "VALUES ($1, $2, $3, $4, $5) "
                "RETURNING id, name, description, is_public, tracks, created_at, updated_at",
                playlist_id, uid, data.name, data.description, data.is_public,
            )
        return self._playlist_record(row)

    async def update_playlist(
        self, uid: str, playlist_id: uuid.UUID, data: UserPlaylistUpdate,
    ) -> dict[str, Any] | None:
        changes = data.model_dump(exclude_unset=True)
        if not changes:
            return await self.get_playlist(uid, playlist_id)
        parts: list[str] = []
        values: list[Any] = []
        for index, (column, value) in enumerate(changes.items(), start=1):
            parts.append(f"{column} = ${index}")
            values.append(value)
        parts.append("updated_at = now()")
        values.extend([uid, playlist_id])
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE user_playlists SET {', '.join(parts)} "
                f"WHERE uid = ${len(values) - 1} AND id = ${len(values)} "
                "RETURNING id, name, description, is_public, tracks, created_at, updated_at",
                *values,
            )
        return self._playlist_record(row) if row else None

    async def delete_playlist(self, uid: str, playlist_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_playlists WHERE uid = $1 AND id = $2",
                uid, playlist_id,
            )
        return result == "DELETE 1"

    async def add_playlist_track(
        self, uid: str, playlist_id: uuid.UUID, track: TrackSnapshot,
    ) -> dict[str, Any] | None:
        current = await self.get_playlist(uid, playlist_id)
        if current is None:
            return None
        tracks = [item for item in current["tracks"] if item.get("seokey") != track.seokey]
        tracks.append(track.model_dump(mode="json"))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE user_playlists SET tracks = $1::jsonb, updated_at = now() "
                "WHERE uid = $2 AND id = $3 "
                "RETURNING id, name, description, is_public, tracks, created_at, updated_at",
                json.dumps(tracks), uid, playlist_id,
            )
        return self._playlist_record(row)

    async def remove_playlist_track(
        self, uid: str, playlist_id: uuid.UUID, seokey: str,
    ) -> dict[str, Any] | None:
        current = await self.get_playlist(uid, playlist_id)
        if current is None:
            return None
        tracks = [item for item in current["tracks"] if item.get("seokey") != seokey]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE user_playlists SET tracks = $1::jsonb, updated_at = now() "
                "WHERE uid = $2 AND id = $3 "
                "RETURNING id, name, description, is_public, tracks, created_at, updated_at",
                json.dumps(tracks), uid, playlist_id,
            )
        return self._playlist_record(row)

    async def list_followed_artists(self, uid: str) -> list[dict[str, Any]]:
        return await self._list_saved(uid, "user_followed_artists", "artist", "followed_at")

    async def follow_artist(self, uid: str, artist: ArtistSnapshot) -> dict[str, Any]:
        return await self._save_snapshot(
            uid, "user_followed_artists", "artist", "followed_at",
            artist.seokey, artist.model_dump(mode="json"),
        )

    async def unfollow_artist(self, uid: str, seokey: str) -> bool:
        return await self._delete_snapshot(uid, "user_followed_artists", seokey)

    async def list_saved_albums(self, uid: str) -> list[dict[str, Any]]:
        return await self._list_saved(uid, "user_saved_albums", "album", "saved_at")

    async def save_album(self, uid: str, album: AlbumSnapshot) -> dict[str, Any]:
        return await self._save_snapshot(
            uid, "user_saved_albums", "album", "saved_at",
            album.seokey, album.model_dump(mode="json"),
        )

    async def remove_saved_album(self, uid: str, seokey: str) -> bool:
        return await self._delete_snapshot(uid, "user_saved_albums", seokey)

    async def delete_account(self, uid: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE uid = $1", uid)
        return result == "DELETE 1"

    # ── Onboarding ─────────────────────────────────────────────────────────────

    async def get_onboarding(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT languages, favorite_artists, onboarding_completed, onboarding_step "
                "FROM user_profiles WHERE uid = $1",
                uid,
            )
        if row is None:
            return {"completed": False, "step": "language", "languages": [], "favorite_artists": []}
        return {
            "completed": row["onboarding_completed"],
            "step": row["onboarding_step"],
            "languages": list(row["languages"]),
            "favorite_artists": list(row["favorite_artists"]),
        }

    async def update_onboarding(self, uid: str, data: OnboardingUpdate) -> dict[str, Any]:
        column_map = {
            "languages": "languages",
            "favorite_artists": "favorite_artists",
            "completed": "onboarding_completed",
            "step": "onboarding_step",
        }
        raw = data.model_dump(exclude_unset=True)
        filtered = {column_map[k]: v for k, v in raw.items() if k in column_map}
        if not filtered:
            return await self.get_onboarding(uid)
        parts: list[str] = []
        values: list[Any] = []
        for i, (col, val) in enumerate(filtered.items(), start=1):
            parts.append(f"{col} = ${i}")
            values.append(val)
        parts.append(f"updated_at = ${len(values) + 1}")
        values.append(datetime.now(timezone.utc))
        values.append(uid)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE user_profiles SET {', '.join(parts)} WHERE uid = ${len(values)}",
                *values,
            )
        return await self.get_onboarding(uid)

    # ── Track reorder ──────────────────────────────────────────────────────────

    async def reorder_playlist_tracks(
        self, uid: str, playlist_id: uuid.UUID, data: TrackOrderUpdate,
    ) -> dict[str, Any] | None:
        current = await self.get_playlist(uid, playlist_id)
        if current is None:
            return None
        track_map = {t["seokey"]: t for t in current["tracks"] if "seokey" in t}
        ordered = [track_map[k] for k in data.seokeys if k in track_map]
        in_order = set(data.seokeys)
        for t in current["tracks"]:
            if t.get("seokey") not in in_order:
                ordered.append(t)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE user_playlists SET tracks = $1::jsonb, updated_at = now() "
                "WHERE uid = $2 AND id = $3 "
                "RETURNING id, name, description, is_public, tracks, created_at, updated_at",
                json.dumps(ordered), uid, playlist_id,
            )
        return self._playlist_record(row) if row else None

    # ── User follows ───────────────────────────────────────────────────────────

    async def follow_user(self, follower_uid: str, following_uid: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_follows (follower_uid, following_uid) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                follower_uid, following_uid,
            )

    async def unfollow_user(self, follower_uid: str, following_uid: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_follows WHERE follower_uid = $1 AND following_uid = $2",
                follower_uid, following_uid,
            )
        return result == "DELETE 1"

    async def list_following(self, uid: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.uid, u.display_name, u.photo_url, f.created_at "
                "FROM user_follows f JOIN users u ON u.uid = f.following_uid "
                "WHERE f.follower_uid = $1 ORDER BY f.created_at DESC",
                uid,
            )
        return [{"uid": r["uid"], "display_name": r["display_name"],
                 "photo_url": r["photo_url"], "followed_at": r["created_at"].isoformat()}
                for r in rows]

    async def list_followers(self, uid: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.uid, u.display_name, u.photo_url, f.created_at "
                "FROM user_follows f JOIN users u ON u.uid = f.follower_uid "
                "WHERE f.following_uid = $1 ORDER BY f.created_at DESC",
                uid,
            )
        return [{"uid": r["uid"], "display_name": r["display_name"],
                 "photo_url": r["photo_url"], "followed_at": r["created_at"].isoformat()}
                for r in rows]

    # ── Pulse ──────────────────────────────────────────────────────────────────

    _PULSE_POST_SELECT = (
        "SELECT p.id, p.uid, p.body, p.track, p.album, p.playlist_id, "
        "p.created_at, p.updated_at, "
        "u.display_name AS author_name, u.photo_url AS author_photo, "
        "(SELECT COUNT(*) FROM pulse_likes  WHERE post_id = p.id) AS like_count, "
        "(SELECT COUNT(*) FROM pulse_comments WHERE post_id = p.id) AS comment_count, "
        "EXISTS(SELECT 1 FROM pulse_likes WHERE post_id = p.id AND uid = $1) AS is_liked "
        "FROM pulse_posts p JOIN users u ON u.uid = p.uid "
    )

    @staticmethod
    def _post_record(row: Any) -> dict[str, Any]:
        track = row["track"] if isinstance(row["track"], dict) else (
            json.loads(row["track"]) if row["track"] else None)
        album = row["album"] if isinstance(row["album"], dict) else (
            json.loads(row["album"]) if row["album"] else None)
        return {
            "id": str(row["id"]),
            "uid": row["uid"],
            "author_name": row["author_name"],
            "author_photo": row["author_photo"],
            "body": row["body"],
            "track": track,
            "album": album,
            "playlist_id": str(row["playlist_id"]) if row["playlist_id"] else None,
            "like_count": row["like_count"],
            "comment_count": row["comment_count"],
            "is_liked": bool(row["is_liked"]),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    async def get_pulse_feed(
        self, uid: str, limit: int, offset: int, feed_type: str,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            if feed_type == "following":
                rows = await conn.fetch(
                    self._PULSE_POST_SELECT +
                    "JOIN user_follows f ON f.following_uid = p.uid AND f.follower_uid = $1 "
                    "ORDER BY p.created_at DESC LIMIT $2 OFFSET $3",
                    uid, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    self._PULSE_POST_SELECT +
                    "ORDER BY p.created_at DESC LIMIT $2 OFFSET $3",
                    uid, limit, offset,
                )
        return [self._post_record(r) for r in rows]

    async def create_pulse_post(self, uid: str, data: PulsePostCreate) -> dict[str, Any]:
        playlist_id = uuid.UUID(data.playlist_id) if data.playlist_id else None
        async with self._pool.acquire() as conn:
            post_id = await conn.fetchval(
                "INSERT INTO pulse_posts (uid, body, track, album, playlist_id) "
                "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5) RETURNING id",
                uid, data.body,
                json.dumps(data.track) if data.track else None,
                json.dumps(data.album) if data.album else None,
                playlist_id,
            )
            row = await conn.fetchrow(
                self._PULSE_POST_SELECT + "WHERE p.id = $2",
                uid, post_id,
            )
        return self._post_record(row)

    async def get_pulse_post(
        self, post_id: uuid.UUID, requesting_uid: str,
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                self._PULSE_POST_SELECT + "WHERE p.id = $2",
                requesting_uid, post_id,
            )
        return self._post_record(row) if row else None

    async def delete_pulse_post(self, uid: str, post_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pulse_posts WHERE id = $1 AND uid = $2", post_id, uid,
            )
        return result == "DELETE 1"

    async def like_post(self, uid: str, post_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pulse_posts WHERE id = $1", post_id,
            )
            if not exists:
                return False
            await conn.execute(
                "INSERT INTO pulse_likes (post_id, uid) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                post_id, uid,
            )
        return True

    async def unlike_post(self, uid: str, post_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pulse_likes WHERE post_id = $1 AND uid = $2", post_id, uid,
            )
        return result == "DELETE 1"

    async def get_post_comments(
        self, post_id: uuid.UUID, limit: int, offset: int,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.id, c.uid, c.body, c.created_at, c.updated_at, "
                "u.display_name, u.photo_url "
                "FROM pulse_comments c JOIN users u ON u.uid = c.uid "
                "WHERE c.post_id = $1 ORDER BY c.created_at ASC LIMIT $2 OFFSET $3",
                post_id, limit, offset,
            )
        return [{"id": str(r["id"]), "uid": r["uid"], "author_name": r["display_name"],
                 "author_photo": r["photo_url"], "body": r["body"],
                 "created_at": r["created_at"].isoformat(),
                 "updated_at": r["updated_at"].isoformat()} for r in rows]

    async def add_comment(
        self, uid: str, post_id: uuid.UUID, data: PulseCommentCreate,
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pulse_posts WHERE id = $1", post_id,
            )
            if not exists:
                return None
            row = await conn.fetchrow(
                "INSERT INTO pulse_comments (post_id, uid, body) VALUES ($1, $2, $3) "
                "RETURNING id, uid, body, created_at, updated_at",
                post_id, uid, data.body,
            )
            user_row = await conn.fetchrow(
                "SELECT display_name, photo_url FROM users WHERE uid = $1", uid,
            )
        return {"id": str(row["id"]), "uid": uid,
                "author_name": user_row["display_name"] if user_row else "",
                "author_photo": user_row["photo_url"] if user_row else None,
                "body": row["body"], "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat()}

    async def delete_comment(self, uid: str, comment_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM pulse_comments WHERE id = $1 AND uid = $2", comment_id, uid,
            )
        return result == "DELETE 1"

    # ── Devices ────────────────────────────────────────────────────────────────

    async def register_device(self, uid: str, data: DeviceRegister) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO device_tokens (uid, token, platform, device_name) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (token) DO UPDATE SET uid = $1, platform = $3, "
                "device_name = $4, last_seen_at = now() "
                "RETURNING id, platform, device_name, created_at",
                uid, data.token, data.platform, data.device_name,
            )
        return {"id": str(row["id"]), "platform": row["platform"],
                "device_name": row["device_name"],
                "created_at": row["created_at"].isoformat()}

    async def unregister_device(self, uid: str, device_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM device_tokens WHERE id = $1 AND uid = $2", device_id, uid,
            )
        return result == "DELETE 1"

    async def get_device_tokens(self, uid: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token FROM device_tokens WHERE uid = $1", uid,
            )
        return [r["token"] for r in rows]

    # ── Notifications ──────────────────────────────────────────────────────────

    async def create_notification(
        self,
        uid: str,
        type_: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO notifications (uid, type, title, body, data) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                uid, type_, title, body, json.dumps(data or {}),
            )

    async def list_notifications(self, uid: str, limit: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, type, title, body, data, is_read, created_at "
                "FROM notifications WHERE uid = $1 ORDER BY created_at DESC LIMIT $2",
                uid, limit,
            )
        return [{"id": str(r["id"]), "type": r["type"], "title": r["title"],
                 "body": r["body"],
                 "data": dict(r["data"]) if r["data"] else {},
                 "is_read": r["is_read"],
                 "created_at": r["created_at"].isoformat()} for r in rows]

    async def mark_notification_read(self, uid: str, notification_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE notifications SET is_read = TRUE WHERE id = $1 AND uid = $2",
                notification_id, uid,
            )
        return result == "UPDATE 1"

    async def mark_all_notifications_read(self, uid: str) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE notifications SET is_read = TRUE "
                "WHERE uid = $1 AND is_read = FALSE",
                uid,
            )
        return int(result.split()[-1])

    async def delete_notification(self, uid: str, notification_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM notifications WHERE id = $1 AND uid = $2",
                notification_id, uid,
            )
        return result == "DELETE 1"

    # ── Player session ─────────────────────────────────────────────────────────

    async def get_player_session(self, uid: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT track, queue, position_ms, playing, device_id, updated_at "
                "FROM player_sessions WHERE uid = $1",
                uid,
            )
        if row is None:
            return None
        track = row["track"] if isinstance(row["track"], dict) else (
            json.loads(row["track"]) if row["track"] else None)
        queue = row["queue"] if isinstance(row["queue"], list) else json.loads(row["queue"])
        return {"track": track, "queue": queue, "position_ms": row["position_ms"],
                "playing": row["playing"], "device_id": row["device_id"],
                "updated_at": row["updated_at"].isoformat()}

    async def put_player_session(self, uid: str, data: PlayerSessionUpdate) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO player_sessions (uid, track, queue, position_ms, playing, device_id) "
                "VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6) "
                "ON CONFLICT (uid) DO UPDATE SET track = $2::jsonb, queue = $3::jsonb, "
                "position_ms = $4, playing = $5, device_id = $6, updated_at = now() "
                "RETURNING track, queue, position_ms, playing, device_id, updated_at",
                uid,
                json.dumps(data.track) if data.track else None,
                json.dumps(data.queue),
                data.position_ms, data.playing, data.device_id,
            )
        track = row["track"] if isinstance(row["track"], dict) else (
            json.loads(row["track"]) if row["track"] else None)
        queue = row["queue"] if isinstance(row["queue"], list) else json.loads(row["queue"])
        return {"track": track, "queue": queue, "position_ms": row["position_ms"],
                "playing": row["playing"], "device_id": row["device_id"],
                "updated_at": row["updated_at"].isoformat()}

    async def delete_player_session(self, uid: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM player_sessions WHERE uid = $1", uid,
            )
        return result == "DELETE 1"

    async def _list_saved(
        self, uid: str, table: str, column: str, timestamp: str,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {column}, {timestamp} FROM {table} "
                f"WHERE uid = $1 ORDER BY {timestamp} DESC",
                uid,
            )
        result = []
        for row in rows:
            item = row[column] if isinstance(row[column], dict) else json.loads(row[column])
            item = dict(item)
            item[timestamp] = row[timestamp].isoformat()
            result.append(item)
        return result

    async def _save_snapshot(
        self, uid: str, table: str, column: str, timestamp: str,
        seokey: str, snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO {table} (uid, seokey, {column}, {timestamp}) "
                f"VALUES ($1, $2, $3::jsonb, now()) "
                f"ON CONFLICT (uid, seokey) DO UPDATE SET {column} = EXCLUDED.{column}, "
                f"{timestamp} = now() RETURNING {timestamp}",
                uid, seokey, json.dumps(snapshot),
            )
        result = dict(snapshot)
        result[timestamp] = row[timestamp].isoformat()
        return result

    async def _delete_snapshot(self, uid: str, table: str, seokey: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {table} WHERE uid = $1 AND seokey = $2",
                uid, seokey,
            )
        return result == "DELETE 1"

    @staticmethod
    def _playlist_record(row: Any) -> dict[str, Any]:
        tracks = row["tracks"] if isinstance(row["tracks"], list) else json.loads(row["tracks"])
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "is_public": row["is_public"],
            "tracks": tracks,
            "track_count": len(tracks),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }


# Keep the old name as an alias so nothing else needs updating
FirebaseUserRepository = PostgresUserRepository
