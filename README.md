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
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `UPSTREAM_TIMEOUT` | `5` | Gaana API timeout (seconds) |
| `UPSTREAM_MAX_RETRIES` | `2` | Retry attempts before giving up |
| `CB_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_RECOVERY_TIMEOUT` | `30` | Seconds before circuit re-tests |
| `TTL_SONG` | `21600` | Song cache TTL (seconds) |
| `TTL_SEARCH` | `900` | Search cache TTL (seconds) |
| `TTL_TRENDING` | `600` | Trending cache TTL (seconds) |
| `CORS_ALLOW_ORIGINS` | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Logging level |

## API docs

Visit `/docs` after starting the server.
