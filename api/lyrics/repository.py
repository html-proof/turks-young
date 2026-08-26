from typing import Any


class PostgresLyricsRepository:
    def __init__(self, pool):
        self.pool = pool

    async def get(
        self,
        track_id: str,
        max_age_seconds: int,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT provider, provider_lyrics_id, synced_lyrics, plain_lyrics,
                       instrumental, status
                  FROM lyrics_cache
                 WHERE track_id = $1
                   AND fetched_at >= now() - ($2 * interval '1 second')
                   AND ($3::text IS NULL OR status = $3)
                """,
                track_id,
                max_age_seconds,
                status,
            )
        return dict(row) if row else None

    async def put(self, track_id: str, data: dict[str, Any]) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO lyrics_cache (
                    track_id, provider, provider_lyrics_id, synced_lyrics,
                    plain_lyrics, instrumental, status, fetched_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
                ON CONFLICT (track_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    provider_lyrics_id = EXCLUDED.provider_lyrics_id,
                    synced_lyrics = EXCLUDED.synced_lyrics,
                    plain_lyrics = EXCLUDED.plain_lyrics,
                    instrumental = EXCLUDED.instrumental,
                    status = EXCLUDED.status,
                    fetched_at = now(),
                    updated_at = now()
                """,
                track_id,
                data.get("provider", "lrclib"),
                str(data["id"]) if data.get("id") is not None else None,
                data.get("syncedLyrics"),
                data.get("plainLyrics"),
                bool(data.get("instrumental")),
                data.get("status", "available"),
            )
