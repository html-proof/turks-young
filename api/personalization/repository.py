import hashlib
import json
import uuid
import base64
import math
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

    async def get_account(self, uid: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT uid, email, display_name, photo_url, provider, account_status, "
                "onboarding_completed, onboarding_completed_at, deleted_at FROM users WHERE uid=$1",
                uid,
            )
        return dict(row) if row else None

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
                      provider     = EXCLUDED.provider,
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

    async def recreate_user(self, user: AuthenticatedUser) -> None:
        """Create a clean account after deliberate prior account deletion."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM users WHERE uid=$1 AND account_status='deleted'", user.uid)
        await self.ensure_user(user)

    async def bootstrap(self, user: AuthenticatedUser) -> dict[str, Any]:
        account = await self.get_account(user.uid)
        if account is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        onboarding = await self.get_onboarding(user.uid)
        profile = await self.get_profile(user.uid)
        return {
            "authenticated": True,
            "account": {
                "id": account["uid"], "email": account["email"],
                "display_name": account["display_name"], "avatar_url": account["photo_url"],
                "onboarding_completed": bool(account["onboarding_completed"]),
                "onboarding_completed_at": account["onboarding_completed_at"].isoformat() if account["onboarding_completed_at"] else None,
                "account_status": account["account_status"],
            },
            "preferences": {
                "languages": [{"id": i, "name": n} for i, n in zip(profile.get("language_ids", []), profile.get("languages", []))],
                "artists": [{"id": i, "name": n} for i, n in zip(profile.get("favorite_artist_ids", []), profile.get("favorite_artists", []))],
            },
            "onboarding": onboarding | {
                "completed": bool(account["onboarding_completed"]),
                "next_step": "none" if account["onboarding_completed"] else onboarding["step"] + "s" if onboarding["step"] in {"language", "artist"} else "languages",
            },
        }

    async def get_profile(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT p.display_name, p.languages, p.language_ids, p.favorite_genres, p.favorite_artists, "
                "p.favorite_artist_ids, u.onboarding_completed, p.streaming_quality_wifi, "
                "streaming_quality_mobile, download_quality, data_saver_enabled, "
                "autoplay_enabled, push_notifications_enabled, explicit_content_enabled, "
                "pulse_followed_releases_enabled, pulse_selected_releases_enabled, pulse_trending_enabled, pulse_recommendations_enabled, "
                "equalizer_preset, p.created_at, p.updated_at FROM user_profiles p "
                "JOIN users u ON u.uid = p.uid WHERE p.uid = $1",
                uid,
            )
        if row is None:
            return {}
        return {
            "display_name": row["display_name"],
            "languages": list(row["languages"]),
            "language_ids": list(row["language_ids"]),
            "favorite_genres": list(row["favorite_genres"]),
            "favorite_artists": list(row["favorite_artists"]),
            "favorite_artist_ids": list(row["favorite_artist_ids"]),
            "onboarding_completed": row["onboarding_completed"],
            "streaming_quality_wifi": row["streaming_quality_wifi"],
            "streaming_quality_mobile": row["streaming_quality_mobile"],
            "download_quality": row["download_quality"],
            "data_saver_enabled": row["data_saver_enabled"],
            "autoplay_enabled": row["autoplay_enabled"],
            "push_notifications_enabled": row["push_notifications_enabled"],
            "pulse_followed_releases_enabled": row["pulse_followed_releases_enabled"],
            "pulse_selected_releases_enabled": row["pulse_selected_releases_enabled"],
            "pulse_trending_enabled": row["pulse_trending_enabled"],
            "pulse_recommendations_enabled": row["pulse_recommendations_enabled"],
            "explicit_content_enabled": row["explicit_content_enabled"],
            "equalizer_preset": row["equalizer_preset"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def update_profile(self, uid: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "display_name", "languages", "language_ids", "favorite_genres", "favorite_artists",
            "favorite_artist_ids",
            "onboarding_completed",
            "streaming_quality_wifi", "streaming_quality_mobile", "download_quality",
            "data_saver_enabled", "autoplay_enabled", "push_notifications_enabled",
            "pulse_followed_releases_enabled", "pulse_selected_releases_enabled", "pulse_trending_enabled",
            "pulse_recommendations_enabled",
            "explicit_content_enabled", "equalizer_preset",
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

    # ── Versioned recommendation API persistence ───────────────────────────

    async def replace_language_preferences(self, uid: str, language_ids: list[str], names: list[str]) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for language_id, name in zip(language_ids, names):
                    await conn.execute(
                        "INSERT INTO languages (id, name, native_name) VALUES ($1,$2,$2) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name",
                        language_id, name,
                    )
                await conn.execute("DELETE FROM user_languages WHERE user_id=$1", uid)
                for language_id in language_ids:
                    await conn.execute(
                        "INSERT INTO user_languages (user_id, language_id, weight) VALUES ($1,$2,1) ON CONFLICT (user_id,language_id) DO UPDATE SET updated_at=now()",
                        uid, language_id,
                    )
                await conn.execute(
                    "UPDATE user_profiles SET languages=$2, language_ids=$3, onboarding_step='artist', updated_at=now() WHERE uid=$1",
                    uid, names, language_ids,
                )

    async def replace_selected_artists(self, uid: str, artist_ids: list[str], artist_names: list[str] | None = None) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM user_selected_artists WHERE user_id=$1", uid)
                for artist_id in artist_ids:
                    await conn.execute(
                        "INSERT INTO user_selected_artists (user_id, artist_id, source) VALUES ($1,$2,'onboarding') ON CONFLICT (user_id,artist_id) DO UPDATE SET source='onboarding'",
                        uid, artist_id,
                    )
                await conn.execute(
                    "UPDATE user_profiles SET favorite_artist_ids=$2, favorite_artists=$3, onboarding_step='complete', updated_at=now() WHERE uid=$1",
                    uid, artist_ids, artist_names or artist_ids,
                )

    async def complete_onboarding(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT language_ids, favorite_artist_ids FROM user_profiles WHERE uid=$1 FOR UPDATE", uid
                )
                if not row or not row["language_ids"] or not row["favorite_artist_ids"]:
                    raise ValueError("Languages and artists must be saved before onboarding can complete")
                await conn.execute(
                    "UPDATE user_profiles SET onboarding_completed=TRUE, onboarding_step='complete', updated_at=now() WHERE uid=$1",
                    uid,
                )
                await conn.execute(
                    "UPDATE users SET onboarding_completed=TRUE, onboarding_completed_at=now(), updated_at=now() WHERE uid=$1",
                    uid,
                )
        return await self.get_onboarding(uid)

    async def record_recommendation_event(self, uid: str, event: Any) -> dict[str, Any]:
        data = event.model_dump(mode="json")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO user_events (user_id,event_type,song_id,artist_id,album_id,playlist_id,position_ms,duration_ms,source,context_id,query,payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)",
                    uid, data["event_type"], data.get("song_id"), data.get("artist_id"), data.get("album_id"),
                    data.get("playlist_id"), data["position_ms"], data["duration_ms"], data.get("source"),
                    data.get("context_id"), data.get("query"), json.dumps(data.get("payload") or {}),
                )
                if data["event_type"] in {"song_play", "song_completed", "song_replay"} and data.get("song_id"):
                    duration = data["duration_ms"]
                    position = data["position_ms"]
                    completion = min(100.0, (position / duration * 100.0) if duration else 0.0)
                    await conn.execute(
                        "INSERT INTO playback_history (user_id,song_id,artist_id,album_id,duration_ms,position_ms,completion_percentage,source,context_id,metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
                        uid, data["song_id"], data.get("artist_id"), data.get("album_id"), duration, position,
                        completion, data.get("source"), data.get("context_id"), json.dumps(data.get("payload") or {}),
                    )
                    # Keep the established recommendation reader in sync while
                    # the normalized playback table is the durable source.
                    history_record = {
                        "seokey": data["song_id"],
                        "artists": [data["artist_id"]] if data.get("artist_id") else [],
                        "artist_ids": [data["artist_id"]] if data.get("artist_id") else [],
                        "album_seokey": data.get("album_id") or "",
                        "played_seconds": round(position / 1000),
                        "completed": data["event_type"] == "song_completed" or completion >= 80,
                        "source": data.get("source") or "api",
                    }
                    await conn.execute(
                        "INSERT INTO user_history (uid,event,played_at) VALUES ($1,$2::jsonb,now())",
                        uid, json.dumps(history_record),
                    )
        return {"accepted": True, "event_type": data["event_type"]}

    async def list_recently_played(self, uid: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT song_id,artist_id,album_id,started_at,position_ms,duration_ms,completion_percentage,source,context_id,metadata FROM playback_history WHERE user_id=$1 ORDER BY started_at DESC LIMIT $2",
                uid, limit,
            )
        return [dict(row) | {"started_at": row["started_at"].isoformat()} for row in rows]

    async def set_song_like(self, uid: str, song_id: str, liked: bool, snapshot: dict[str, Any] | None = None) -> None:
        async with self._pool.acquire() as conn:
            if liked:
                await conn.execute("INSERT INTO user_liked_songs (user_id,song_id,song) VALUES ($1,$2,$3::jsonb) ON CONFLICT (user_id,song_id) DO UPDATE SET song=EXCLUDED.song", uid, song_id, json.dumps(snapshot or {"id": song_id}))
            else:
                await conn.execute("DELETE FROM user_liked_songs WHERE user_id=$1 AND song_id=$2", uid, song_id)

    async def list_liked_song_ids(self, uid: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT song_id FROM user_liked_songs WHERE user_id=$1 ORDER BY created_at DESC", uid)
        return [row["song_id"] for row in rows]

    async def delete_account(self, uid: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET account_status='deleted', deleted_at=now(), updated_at=now() WHERE uid=$1 AND account_status <> 'deleted'",
                uid,
            )
        return result == "UPDATE 1"

    # ── Recent searches ───────────────────────────────────────────────────────

    async def list_recent_searches(self, uid: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, query, result_type, item, searched_at FROM recent_searches "
                "WHERE uid = $1 ORDER BY searched_at DESC LIMIT $2",
                uid, limit,
            )
        return [self._recent_search_record(row) for row in rows]

    async def save_recent_search(self, uid: str, data: Any) -> dict[str, Any]:
        # Keep one current entry per exact query/type for a compact cross-device list.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM recent_searches WHERE uid = $1 AND lower(query) = lower($2) "
                    "AND result_type IS NOT DISTINCT FROM $3",
                    uid, data.query, data.result_type,
                )
                row = await conn.fetchrow(
                    "INSERT INTO recent_searches (uid, query, result_type, item) "
                    "VALUES ($1, $2, $3, $4::jsonb) "
                    "RETURNING id, query, result_type, item, searched_at",
                    uid, data.query, data.result_type,
                    json.dumps(data.item) if data.item is not None else None,
                )
        return self._recent_search_record(row)

    async def delete_recent_search(self, uid: str, search_id: uuid.UUID) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM recent_searches WHERE uid = $1 AND id = $2", uid, search_id
            )
        return result == "DELETE 1"

    async def clear_recent_searches(self, uid: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM recent_searches WHERE uid = $1", uid)

    @staticmethod
    def _recent_search_record(row: Any) -> dict[str, Any]:
        raw_item = row["item"]
        item = json.loads(raw_item) if isinstance(raw_item, str) else raw_item
        return {
            "id": str(row["id"]),
            "query": row["query"],
            "result_type": row["result_type"],
            "item": item,
            "searched_at": row["searched_at"].isoformat(),
        }

    # ── Onboarding ─────────────────────────────────────────────────────────────

    async def get_onboarding(self, uid: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT p.languages, p.language_ids, p.favorite_artists, p.favorite_artist_ids, "
                "u.onboarding_completed, p.onboarding_step "
                "FROM users u LEFT JOIN user_profiles p ON p.uid = u.uid WHERE u.uid = $1",
                uid,
            )
        if row is None:
            return {"completed": False, "step": "language", "languages": [], "language_ids": [], "favorite_artists": [], "favorite_artist_ids": []}
        return {
            "completed": row["onboarding_completed"],
            "step": row["onboarding_step"],
            "languages": list(row["languages"] or []),
            "language_ids": list(row["language_ids"] or []),
            "favorite_artists": list(row["favorite_artists"] or []),
            "favorite_artist_ids": list(row["favorite_artist_ids"] or []),
        }

    async def update_onboarding(self, uid: str, data: OnboardingUpdate) -> dict[str, Any]:
        column_map = {
            "languages": "languages",
            "favorite_artists": "favorite_artists",
            "completed": "onboarding_completed",
            "step": "onboarding_step",
        }
        raw = data.model_dump(exclude_unset=True)
        if raw.get("completed") is True:
            raise ValueError("Use the onboarding completion endpoint after saving preferences")
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

    async def upsert_music_release(self, release: dict[str, Any]) -> dict[str, Any]:
        """Upsert provider data; ingestion is deliberately separate from feed reads."""
        required = ("id", "type", "title")
        if any(not release.get(key) for key in required):
            raise ValueError("release requires id, type and title")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO music_releases
                (id,type,title,artist_ids,artist_names,album_id,album_name,languages,image_url,stream_url,release_date,popularity,trending_score,metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
                ON CONFLICT (id) DO UPDATE SET type=EXCLUDED.type,title=EXCLUDED.title,
                  artist_ids=EXCLUDED.artist_ids,artist_names=EXCLUDED.artist_names,album_id=EXCLUDED.album_id,
                  album_name=EXCLUDED.album_name,languages=EXCLUDED.languages,image_url=EXCLUDED.image_url,
                  stream_url=EXCLUDED.stream_url,release_date=EXCLUDED.release_date,popularity=EXCLUDED.popularity,
                  trending_score=EXCLUDED.trending_score,metadata=EXCLUDED.metadata,updated_at=now()
                RETURNING id,type,title,artist_ids,artist_names,album_id,album_name,languages,image_url,stream_url,release_date,popularity,trending_score,created_at""",
                str(release["id"]), release["type"], release["title"], release.get("artist_ids", []),
                release.get("artist_names", []), release.get("album_id"), release.get("album_name"),
                release.get("languages", []), release.get("image_url"), release.get("stream_url"),
                release.get("release_date"), float(release.get("popularity", 0)),
                float(release.get("trending_score", 0)), json.dumps(release.get("metadata", {})),
            )
        return self._release_record(row)

    @staticmethod
    def _release_record(row: Any) -> dict[str, Any]:
        result = dict(row)
        for key in ("release_date", "created_at"):
            if result.get(key): result[key] = result[key].isoformat()
        return result

    async def get_personalized_pulse(self, uid: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
        """Rank normalized releases using backend preferences and activity only."""
        profile, followed, history = await __import__("asyncio").gather(
            self.get_profile(uid), self.list_followed_artists(uid), self.list_history(uid, 100)
        )
        selected_ids = {str(x) for x in profile.get("favorite_artist_ids", []) if x}
        followed_ids = {str((x.get("artist") or {}).get("artist_id") or (x.get("artist") or {}).get("id") or "") for x in followed}
        history_ids = {str(x.get("artist_id")) for x in history if x.get("artist_id")}
        languages = {str(x).casefold() for x in profile.get("languages", []) if x}
        offset = 0
        if cursor:
            try: offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception: raise ValueError("invalid cursor")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id,type,title,artist_ids,artist_names,album_id,album_name,languages,image_url,stream_url,
                   release_date,popularity,trending_score,created_at
                   FROM music_releases WHERE release_date > now() - interval '90 days'
                   OR trending_score > 0 ORDER BY GREATEST(COALESCE(release_date, created_at), created_at) DESC LIMIT 500"""
            )
        ranked = []
        now = datetime.now(timezone.utc)
        for row in rows:
            item = self._release_record(row)
            release_type = item.get("type")
            artist_ids = {str(x) for x in (item.get("artist_ids") or [])}
            release_langs = {str(x).casefold() for x in (item.get("languages") or [])}
            selected = bool(artist_ids & selected_ids) and profile.get("pulse_selected_releases_enabled", True)
            followed_match = bool(artist_ids & followed_ids) and profile.get("pulse_followed_releases_enabled", True)
            language_match = bool(release_langs & languages) and profile.get("pulse_trending_enabled", True)
            release_at = row["release_date"] or row["created_at"]
            age_hours = max(0.0, (now - release_at).total_seconds() / 3600)
            freshness = 20 * math.exp(-age_hours / (24 * 14))
            score = (40 if selected else 0) + (35 if followed_match else 0) + (25 if language_match else 0)
            score += freshness + min(15.0, float(item.get("trending_score") or 0))
            score += min(10.0, float(item.get("popularity") or 0) / 10)
            history_match = bool(artist_ids & history_ids) and profile.get("pulse_recommendations_enabled", True)
            score += 15 if history_match else 0
            if not (selected or followed_match or language_match or history_match):
                continue
            release_kind = {"song": "new_song", "album": "new_album", "single": "new_single", "ep": "artist_release"}.get(release_type, "artist_release")
            if selected: reason, kind = "New release from an artist you selected", release_kind
            elif followed_match: reason, kind = "New release from an artist you follow", release_kind
            elif language_match and freshness > 10: reason, kind = f"New release in {next(iter(release_langs & languages)).title()}", release_kind
            elif language_match: reason, kind = f"Trending in {next(iter(release_langs & languages)).title()}", "song_trending" if release_type == "song" else "album_trending"
            else: reason, kind = "Because you listen to this artist", "recommended_release"
            item.update({"type": kind, "reason": reason, "score": round(score, 3), "is_new": age_hours <= 72,
                         "song": item if release_type == "song" else None,
                         "album": item if release_type in {"album", "ep"} else None,
                         "artist": {"ids": item.get("artist_ids"), "names": item.get("artist_names")} })
            ranked.append(item)
        ranked.sort(key=lambda x: (-x["score"], x.get("release_date") or x.get("created_at") or ""))
        # Keep the feed varied without suppressing the first few strong matches.
        result, artist_counts = [], {}
        for item in ranked[offset:]:
            key = (item.get("artist_ids") or [item["id"]])[0]
            if artist_counts.get(key, 0) >= 4: continue
            artist_counts[key] = artist_counts.get(key, 0) + 1
            result.append(item)
            if len(result) >= limit + 1: break
        has_more = len(result) > limit
        result = result[:limit]
        next_cursor = base64.urlsafe_b64encode(str(offset + limit).encode()).decode() if has_more else None
        return {"items": result, "next_cursor": next_cursor, "has_more": has_more,
                "unread_count": await self.count_unread_pulse(uid)}

    async def count_unread_pulse(self, uid: str) -> int:
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM user_pulse_items WHERE user_id=$1 AND state <> 'read'", uid) or 0)

    async def mark_pulse_state(self, uid: str, item_id: uuid.UUID, state: str) -> bool:
        if state not in {"seen", "opened", "read"}: raise ValueError("invalid pulse state")
        async with self._pool.acquire() as conn:
            result = await conn.execute("UPDATE user_pulse_items SET state=$1 WHERE id=$2 AND user_id=$3", state, item_id, uid)
        return result == "UPDATE 1"

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
                "SELECT track, queue, position_ms, playing, repeat_mode, shuffle_enabled, duration_ms, device_id, updated_at "
                "FROM player_sessions WHERE uid = $1",
                uid,
            )
        if row is None:
            return None
        track = row["track"] if isinstance(row["track"], dict) else (
            json.loads(row["track"]) if row["track"] else None)
        queue = row["queue"] if isinstance(row["queue"], list) else json.loads(row["queue"])
        return {"track": track, "queue": queue, "position_ms": row["position_ms"],
                "playing": row["playing"], "repeat_mode": row["repeat_mode"],
                "shuffle_enabled": row["shuffle_enabled"], "duration_ms": row["duration_ms"],
                "device_id": row["device_id"],
                "updated_at": row["updated_at"].isoformat()}

    async def put_player_session(self, uid: str, data: PlayerSessionUpdate) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO player_sessions (uid, track, queue, position_ms, playing, repeat_mode, shuffle_enabled, duration_ms, device_id) "
                "VALUES ($1, $2::jsonb, $3::jsonb, $4, $5, $6, $7, $8, $9) "
                "ON CONFLICT (uid) DO UPDATE SET track = $2::jsonb, queue = $3::jsonb, "
                "position_ms = $4, playing = $5, repeat_mode = $6, shuffle_enabled = $7, duration_ms = $8, device_id = $9, updated_at = now() "
                "RETURNING track, queue, position_ms, playing, repeat_mode, shuffle_enabled, duration_ms, device_id, updated_at",
                uid,
                json.dumps(data.track) if data.track else None,
                json.dumps(data.queue),
                data.position_ms, data.playing, data.repeat_mode, data.shuffle_enabled, data.duration_ms, data.device_id,
            )
        track = row["track"] if isinstance(row["track"], dict) else (
            json.loads(row["track"]) if row["track"] else None)
        queue = row["queue"] if isinstance(row["queue"], list) else json.loads(row["queue"])
        return {"track": track, "queue": queue, "position_ms": row["position_ms"],
                "playing": row["playing"], "repeat_mode": row["repeat_mode"],
                "shuffle_enabled": row["shuffle_enabled"], "duration_ms": row["duration_ms"],
                "device_id": row["device_id"],
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
