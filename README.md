# turks-young

A FastAPI backend for a Spotify-style music streaming app, wrapping the Gaana music catalog.

## Features

- Song, album, artist, playlist search and metadata
- Trending, new releases, and charts endpoints
- Redis caching with per-resource TTLs
- Retry + exponential backoff on upstream failures
- Circuit breaker — stops hammering a failing upstream
- Firebase-backed auth and personalization
- Structured JSON request logging with request IDs
- Synced/plain lyrics through LRCLIB, normalized for player highlighting and tap-to-seek
- PostgreSQL lyrics cache with separate successful and not-found expiry windows
- Backend-owned onboarding languages and artist selection with provider artwork
- Canonical typed song, artist, album, and playlist responses
- Categorized/paginated search, discovery, recent searches, and personalized home feed
- `/health` and `/ready` readiness probes

## Stack

- **FastAPI** + **uvicorn**
- **aiohttp** for async upstream calls
- **Redis** (`redis[asyncio]`) for caching
- **Firebase Admin SDK** for auth / personalization

## Quick start

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `UPSTASH_REDIS_REST_URL` | empty | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | empty | Upstash Redis REST token |
| `UPSTREAM_TIMEOUT` | `5` | Gaana API timeout (seconds) |
| `UPSTREAM_MAX_RETRIES` | `2` | Retry attempts before giving up |
| `CB_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_RECOVERY_TIMEOUT` | `30` | Seconds before circuit re-tests |
| `TTL_SONG` | `21600` | Song cache TTL (seconds) |
| `TTL_SEARCH` | `900` | Search cache TTL (seconds) |
| `TTL_TRENDING` | `600` | Trending cache TTL (seconds) |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LYRICS_USER_AGENT` | `MusicHub/1.0 (...)` | Identifies Music Hub to LRCLIB; set this to a real app/contact URL |
| `LYRICS_TIMEOUT` | `10` | LRCLIB request timeout (seconds) |
| `TTL_LYRICS` | `2592000` | Successful lyrics cache lifetime (seconds) |
| `TTL_LYRICS_NOT_FOUND` | `21600` | Missing lyrics cache lifetime (seconds) |
| `MUSIC_LANGUAGES_JSON` | empty | Backend-owned JSON language catalogue; empty means onboarding returns an empty state |

## API docs

Visit `/docs` after starting the server.

The frontend-facing lyrics endpoint is `GET /api/v1/tracks/{seokey}/lyrics`.
It returns timestamped lines when synchronization is available, plain lyrics as
a fallback, and distinct `not_found` and `instrumental` states. LRCLIB is useful
for prototyping; verify lyric redistribution rights before a commercial release.

## Backend-driven client API

All new client endpoints use `{ "data": ..., "meta": ..., "error": null }`.
Catalogue items contain an explicit `type` and use the same canonical shape
across search, home, onboarding, artist, and album responses.

| Endpoint | Purpose |
|---|---|
| `GET /api/languages` | Backend-configured onboarding choices |
| `GET /api/artists?languages=id1,id2` | Provider artists relevant to selected languages |
| `PUT /api/me/preferences/languages` | Persist validated language IDs |
| `PUT /api/me/preferences/artists` | Persist validated artist IDs and complete onboarding |
| `GET /api/home?type=all&limit=24&cursor=...` | Personalized typed, paginated home sections |
| `GET /api/home?refresh=true` | Refresh and replace the cached home response |
| `GET /api/search?q=...` | Categorized All results |
| `GET /api/search?q=...&type=song&page=1&limit=20` | Typed pagination |
| `GET /api/search/discover` | Backend discovery content for an empty search |
| `GET /api/artists/{id}` | Artist details and separated content sections |
| `GET /api/albums/{id}` | Album details and provider tracklist |
| `/api/me/recent-searches` | Cross-device recent-search CRUD |

`MUSIC_LANGUAGES_JSON` must be a JSON array of objects with `id`, `name`,
`native_name`, and optional `image_url`. No language is selected or invented
when this setting is empty. Apply Supabase migration
`20260826000003_backend_driven_catalog.sql` before deploying these endpoints.

Home feed pages return `sections`, `next_cursor`, `has_more`, and `content_type`.
Supported types are `all`, `song`, `album`, `artist`, and `playlist`. Cached
content is served while stale data is revalidated, and concurrent requests for
the same page are coalesced per API worker.

## Keep Render Web Service Awake

Render free tier instances go to sleep after 15 minutes of inactivity. To prevent cold starts and keep the API active 24/7, a GitHub Actions cron trigger is configured at `.github/workflows/render-keep-alive.yml`.

- **Frequency**: Runs automatically every 10 minutes (`*/10 * * * *`).
- **Endpoint**: Pings `GET /health` (falling back to `GET /`).
- **Manual Trigger**: Can also be dispatched manually via the **Actions** tab in GitHub.
- **Custom URL (Optional)**: Set the repository Secret or Variable `RENDER_SERVICE_URL` in GitHub Repository Settings -> Secrets and variables -> Actions if your service URL changes.

