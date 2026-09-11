import logging
import logging.config
import os
import re
import uuid
import time
from collections import deque
from typing import Optional

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from firebase_admin import exceptions as firebase_exceptions

from api.cache.redis_cache import RedisCache
from api.catalog.labels import PostgresLabelRepository, label_registry
from api.catalog.routes import router as catalog_router
from api.catalog.service import CatalogService, LanguageCatalog
from api.core import config
from api.db.connection import create_pool
from api.firebase import FirebaseRuntime
from api.gaanapy import GaanaPy
from api.lyrics.provider import LRCLibProvider
from api.lyrics.repository import PostgresLyricsRepository
from api.lyrics.routes import router as lyrics_router
from api.lyrics.service import LyricsService
from api.personalization.repository import PostgresUserRepository
from api.personalization.routes import router as personalization_router
from api.personalization.routes import users_router
from api.personalization.v1_routes import router as v1_personalization_router
from api.pulse.routes import router as pulse_router, api_router as personalized_pulse_router
from api.personalization.service import PersonalizedMusicService
from api.core.performance import record as record_performance
from api.stream_fallback import StreamFallbackResolver

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "fmt": '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
})

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="GaanaPy", version="1.0")
app.include_router(personalization_router)
app.include_router(users_router)
app.include_router(pulse_router)
app.include_router(personalized_pulse_router)
app.include_router(lyrics_router)
app.include_router(catalog_router)
app.include_router(v1_personalization_router)

# Native mobile clients do not use CORS. Browser origins must be configured
# explicitly in production instead of allowing every website by default.
cors_origins = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Keep a small, bounded per-process request limiter as a safety net for the
# public API and its upstream music providers. Deployments with multiple
# instances should additionally enforce limits at Cloudflare/load-balancer.
_RATE_LIMIT_REQUESTS = max(1, int(os.getenv("RATE_LIMIT_REQUESTS", "120")))
_RATE_LIMIT_WINDOW_SECONDS = max(1, int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")))
_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_RATE_LIMIT_MAX_BUCKETS = 10_000


def _rate_limit_key(request: Request) -> str:
    forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:128]
    return (request.client.host if request.client else "unknown")[:128]


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in {"/health", "/ready"}:
        return await call_next(request)
    now = time.monotonic()
    key = _rate_limit_key(request)
    bucket = _RATE_LIMIT_BUCKETS.get(key)
    if bucket is None:
        if len(_RATE_LIMIT_BUCKETS) >= _RATE_LIMIT_MAX_BUCKETS:
            stale_before = now - _RATE_LIMIT_WINDOW_SECONDS
            for candidate, timestamps in list(_RATE_LIMIT_BUCKETS.items()):
                if not timestamps or timestamps[-1] <= stale_before:
                    _RATE_LIMIT_BUCKETS.pop(candidate, None)
                    if len(_RATE_LIMIT_BUCKETS) < _RATE_LIMIT_MAX_BUCKETS:
                        break
        bucket = _RATE_LIMIT_BUCKETS.setdefault(key, deque())
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_REQUESTS:
        return JSONResponse(
            status_code=429,
            content={"error": "Too many requests. Please try again shortly."},
            headers={"Retry-After": str(_RATE_LIMIT_WINDOW_SECONDS)},
        )
    bucket.append(now)
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------
_SAFE_RID = re.compile(r"[^a-zA-Z0-9\-_]")

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    raw = request.headers.get("X-Request-ID") or ""
    # Sanitize: strip non-alphanumeric/dash/underscore chars and cap length
    rid = _SAFE_RID.sub("", raw)[:64] or str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response

# ---------------------------------------------------------------------------
# Access log middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    rid = getattr(request.state, "request_id", "-")
    record_performance(request.url.path, duration_ms)
    response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
    logger.info(
        '"method":"%s","path":"%s","status":%d,"duration_ms":%.1f,"request_id":"%s"',
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        rid,
    )
    return response

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        '"msg":"unhandled exception","path":"%s","error":"%s","request_id":"%s"',
        request.url.path,
        exc,
        getattr(request.state, "request_id", "-"),
    )
    return JSONResponse(
        status_code=502,
        content={"error": "An unexpected error occurred. Please try again."},
    )


@app.exception_handler(firebase_exceptions.FirebaseError)
async def firebase_exception_handler(request: Request, exc: firebase_exceptions.FirebaseError):
    return JSONResponse(
        status_code=503,
        content={"error": "Personalization data source is temporarily unavailable"},
    )

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
firebase_runtime = FirebaseRuntime.from_environment()
app.state.firebase = firebase_runtime
app.state.gaanapy = None
app.state.user_repository = None
app.state.personalization_service = None
app.state.cache = None
app.state.db_pool = None
app.state.lyrics_service = None
app.state.catalog_service = None
app.state.fallback_resolver = None


@app.on_event("startup")
async def startup_event():
    language_catalog = LanguageCatalog.from_json(config.MUSIC_LANGUAGES_JSON)
    gaanapy = GaanaPy()
    app.state.gaanapy = gaanapy
    app.state.catalog_service = CatalogService(gaanapy, language_catalog)

    if config.LYRICS_PROVIDER != "lrclib":
        raise RuntimeError(f"Unsupported LYRICS_PROVIDER: {config.LYRICS_PROVIDER}")
    lyrics_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.LYRICS_TIMEOUT),
    )
    app.state.lyrics_session = lyrics_session
    lyrics_provider = LRCLibProvider(
        lyrics_session,
        user_agent=config.LYRICS_USER_AGENT,
        timeout=config.LYRICS_TIMEOUT,
    )
    app.state.catalog_service.lyrics_provider = lyrics_provider

    cache = RedisCache(config.UPSTASH_REDIS_REST_URL, config.UPSTASH_REDIS_REST_TOKEN)
    await cache.connect()
    app.state.cache = cache
    app.state.catalog_service.cache = cache

    fallback_resolver = StreamFallbackResolver(cache=cache)
    app.state.fallback_resolver = fallback_resolver
    gaanapy._fallback_resolver = fallback_resolver
    if hasattr(gaanapy, "songs"):
        gaanapy.songs._fallback_resolver = fallback_resolver
    if hasattr(gaanapy, "albums"):
        gaanapy.albums._fallback_resolver = fallback_resolver
    gaanapy.cache = cache
    from api.stream_fallback import set_stream_fallback_resolver
    set_stream_fallback_resolver(fallback_resolver)

    firebase_runtime.initialize()  # still needed for JWT verification

    app.state.db_error = None
    if config.DATABASE_URL:
        try:
            pool = await create_pool(config.DATABASE_URL)
            app.state.db_pool = pool
            if app.state.catalog_service:
                app.state.catalog_service.db_pool = pool
            repo = PostgresUserRepository(pool)
            app.state.user_repository = repo
            app.state.personalization_service = PersonalizedMusicService(repo)
            label_repo = PostgresLabelRepository(pool)
            label_registry.repository = label_repo
            await label_registry.load_from_db()
            logger.info('"msg":"postgres connected"')
        except Exception as exc:
            app.state.db_error = f"{type(exc).__name__}: {exc}"
            logger.error('"msg":"postgres unavailable","error":"%s"', exc)
    else:
        app.state.db_error = "DATABASE_URL environment variable is missing / empty"
        logger.warning('"msg":"DATABASE_URL not set; postgres disabled"')

    app.state.lyrics_service = LyricsService(
        provider=lyrics_provider,
        repository=PostgresLyricsRepository(app.state.db_pool),
        success_ttl=config.TTL_LYRICS,
        not_found_ttl=config.TTL_LYRICS_NOT_FOUND,
        cache=cache,
    )


@app.on_event("shutdown")
async def shutdown_event():
    await label_registry.flush_pending_to_db()
    gaanapy: GaanaPy | None = app.state.gaanapy
    if gaanapy and hasattr(gaanapy, "aiohttp"):
        await gaanapy.aiohttp.close()
    lyrics_session: aiohttp.ClientSession | None = getattr(app.state, "lyrics_session", None)
    if lyrics_session and not lyrics_session.closed:
        await lyrics_session.close()
    cache: RedisCache | None = app.state.cache
    if cache:
        await cache.close()
    fallback_resolver = getattr(app.state, "fallback_resolver", None)
    if fallback_resolver:
        await fallback_resolver.close()
    if app.state.db_pool:
        await app.state.db_pool.close()
    await firebase_runtime.close()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
MAX_LIMIT, MIN_LIMIT, DEFAULT_LIMIT = 100, 1, 10
MIN_PAGE, DEFAULT_PAGE, MAX_PAGE = 1, 1, 1000

MAX_QUERY_LENGTH = 200
MAX_SEOKEY_LENGTH = 200
MAX_ARTIST_ID_LENGTH = 20
MAX_LANGUAGE_LENGTH = 50

SEO_KEY_BASE_PATTERN = r"^[a-zA-Z0-9\-_./%\[\]()+@:]+$"
ARTIST_ID_PATTERN = r"^[0-9]+$"
LANGUAGE_PATTERN = r"^[a-zA-Z]+(?:\s[a-zA-Z]+)*$"
SEARCH_QUERY_PATTERN = r"^[^<>\r\n]+$"


def validate_seokey(
    seokey: str = Query(
        ...,
        min_length=1,
        max_length=MAX_SEOKEY_LENGTH,
        pattern=SEO_KEY_BASE_PATTERN,
        description="The `seokey` of the resource.",
    )
):
    return seokey


def _gaana(request: Request) -> GaanaPy:
    return request.app.state.gaanapy


def _cache(request: Request) -> RedisCache:
    return request.app.state.cache


def _is_stream_expired(data) -> bool:
    if not isinstance(data, (list, dict)):
        return False
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict):
            url = item.get("stream_url") or ""
            if not url and isinstance(item.get("stream_urls"), dict):
                urls = item["stream_urls"].get("urls", {})
                url = urls.get("high_quality") or urls.get("medium_quality") or urls.get("very_high_quality") or ""
            if url:
                match = re.search(r"exp=(\d+)", url)
                if match:
                    exp_time = int(match.group(1))
                    if exp_time <= int(time.time()) + 60:
                        return True
    return False


async def _cached(cache: RedisCache, key: str, ttl: int, loader, force_fresh: bool = False):
    """Return cached value when present, otherwise await coro, cache result, and return."""
    if not force_fresh:
        hit = await cache.get(key)
        if hit is not None and not _is_stream_expired(hit):
            return hit
    result = await loader()
    if not (isinstance(result, dict) and "error" in result):
        await cache.set(key, result, ttl)
    return result

# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready(request: Request):
    issues = []
    cache: RedisCache = request.app.state.cache
    if not cache or not cache.available:
        issues.append("redis_unavailable")
    if request.app.state.gaanapy is None:
        issues.append("gaana_client_not_initialized")
    if getattr(request.app.state, "db_pool", None) is None:
        issues.append("database_unavailable")
    if issues:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "issues": issues,
                "db_error": getattr(request.app.state, "db_error", None),
            },
        )
    return {"status": "ready"}


@app.get("/health/db", tags=["ops"])
@app.get("/api/health/db", tags=["ops"])
async def health_db(request: Request):
    pool = getattr(request.app.state, "db_pool", None)
    db_error = getattr(request.app.state, "db_error", None)
    has_url = bool(config.DATABASE_URL)
    masked_url = (
        f"{config.DATABASE_URL.split('@')[0].split(':')[0]}://***@{config.DATABASE_URL.split('@')[-1]}"
        if "@" in config.DATABASE_URL
        else (config.DATABASE_URL[:15] + "..." if config.DATABASE_URL else "NOT_SET")
    )
    if pool is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "disconnected",
                "has_database_url": has_url,
                "db_target": masked_url,
                "error": db_error or "Database pool not initialized",
                "user_repository_ready": False,
            },
        )
    try:
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1;")
            user_count = await conn.fetchval("SELECT count(*) FROM users;")
            return {
                "status": "connected",
                "alive": val == 1,
                "total_users": user_count,
                "user_repository_ready": getattr(request.app.state, "user_repository", None) is not None,
            }
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "has_database_url": has_url,
                "db_target": masked_url,
            },
        )


@app.get("/health/redis", tags=["ops"])
@app.get("/api/health/redis", tags=["ops"])
async def health_redis(request: Request):
    cache = getattr(request.app.state, "cache", None)
    has_url = bool(config.UPSTASH_REDIS_REST_URL)
    has_token = bool(config.UPSTASH_REDIS_REST_TOKEN)
    token_valid = (
        config.UPSTASH_REDIS_REST_TOKEN != "<paste-your-token-in-render-ui>"
        and len(config.UPSTASH_REDIS_REST_TOKEN) > 20
    )
    if not cache or not cache.available:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "has_url": has_url,
                "has_token": has_token,
                "token_is_valid_format": token_valid,
                "error": "Redis cache is unavailable or UPSTASH_REDIS_REST_TOKEN is invalid/unset",
            },
        )
    return {
        "status": "connected",
        "url": config.UPSTASH_REDIS_REST_URL,
        "available": True,
    }


@app.api_route("/", methods=["GET", "HEAD"], tags=["ops"])
async def home():
    return {"docs": "/docs", "github": "https://github.com/html-proof/turks-young"}

# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------
@app.get("/songs/search/", summary="Search for songs.")
async def songs_search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, pattern=SEARCH_QUERY_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"songs:search:{query}:{limit}"
    result = await _cached(cache, key, config.TTL_SEARCH, lambda: gaana.search_songs(query, limit))
    if isinstance(result, dict) and "error" in result:
        return []
    return result


@app.get("/songs/info/", summary="Retrieve detailed information on a song.")
async def songs_info(
    request: Request,
    seokey: Optional[str] = Query(None, min_length=1, max_length=MAX_SEOKEY_LENGTH, pattern=SEO_KEY_BASE_PATTERN),
    query: Optional[str] = Query(None, min_length=1, max_length=MAX_SEOKEY_LENGTH, pattern=SEO_KEY_BASE_PATTERN),
    title: Optional[str] = Query(None, max_length=300),
    artist: Optional[str] = Query(None, max_length=300),
    refresh: bool = Query(False),
):
    # Older clients used `query`; keep it as a compatible alias for the
    # provider's seokey without weakening the validation rules.
    seokey = seokey or query
    if not seokey and not title:
        raise HTTPException(status_code=422, detail="seokey, query, or title is required")
    seokey = seokey or (title.replace(" ", "-").lower() if title else "")
    gaana = _gaana(request)
    cache = _cache(request)
    fallback_res = getattr(request.app.state, "fallback_resolver", None)
    if not fallback_res:
        from api.stream_fallback import get_stream_fallback_resolver
        fallback_res = get_stream_fallback_resolver()

    key = f"songs:info:{seokey}"

    # If seokey is explicitly a JioSaavn identifier, resolve it immediately via fallback_resolver
    if seokey and (seokey.startswith("saavn:") or seokey.startswith("saavn-")):
        if fallback_res:
            fb = await fallback_res.resolve_stream(seokey, (artist or "").strip())
            if fb and fb.get("stream_url"):
                saavn_track = {
                    "seokey": fb.get("seokey") or seokey,
                    "id": fb.get("id") or seokey,
                    "track_id": fb.get("track_id") or seokey,
                    "title": fb.get("title") or (title or "").strip() or seokey,
                    "artists": fb.get("artist") or (artist or "").strip(),
                    "album": fb.get("album") or "",
                    "duration": fb.get("duration") or "180",
                    "stream_url": fb["stream_url"],
                    "stream_urls": fb.get("stream_urls", {}),
                    "images": fb.get("images", {"urls": {"large_artwork": "", "medium_artwork": "", "small_artwork": ""}}),
                }
                await cache.set(key, [saavn_track], config.TTL_SONG)
                return [saavn_track]

    result = await _cached(
        cache,
        key,
        config.TTL_SONG,
        lambda: gaana.get_track_info([seokey], force_refresh=refresh),
        force_fresh=refresh,
    )
    if isinstance(result, dict) and "error" in result:
        if fallback_res:
            effective_title = (title or "").strip() or (seokey.replace("-", " ").title() if not seokey.startswith("saavn") else "")
            effective_artist = (artist or "").strip()
            fb = await fallback_res.resolve_stream(effective_title, effective_artist)
            if fb and fb.get("stream_url"):
                synthetic_track = {
                    "seokey": seokey,
                    "id": seokey,
                    "track_id": seokey,
                    "title": fb.get("title") or effective_title,
                    "artists": fb.get("artist") or effective_artist,
                    "album": fb.get("album") or "",
                    "duration": fb.get("duration") or "180",
                    "stream_url": fb["stream_url"],
                    "stream_urls": fb.get("stream_urls", {}),
                    "images": fb.get("images", {"urls": {"large_artwork": "", "medium_artwork": "", "small_artwork": ""}}),
                }
                await cache.set(key, [synthetic_track], config.TTL_SONG)
                return [synthetic_track]
        raise HTTPException(status_code=404, detail=result["error"])

    if isinstance(result, list) and result and not (result[0].get("stream_url") or "").strip():
        if fallback_res:
            song_title = (title or "").strip() or result[0].get("title") or ""
            song_artist = (artist or "").strip() or result[0].get("artists") or ""
            fb = await fallback_res.resolve_stream(song_title, song_artist)
            if fb and fb.get("stream_url"):
                result[0]["stream_url"] = fb["stream_url"]
                if fb.get("stream_urls"):
                    result[0]["stream_urls"] = fb["stream_urls"]
                await cache.set(key, result, config.TTL_SONG)

    return result

# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------
@app.get("/albums/search/", summary="Search for albums.")
async def albums_search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, pattern=SEARCH_QUERY_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"albums:search:{query}:{limit}"
    result = await _cached(cache, key, config.TTL_SEARCH, lambda: gaana.search_albums(query, limit))
    if isinstance(result, dict) and "error" in result:
        return []
    return result


@app.get("/albums/info/", summary="Retrieve detailed information on an album.")
async def albums_info(
    request: Request,
    seokey: str = Depends(validate_seokey),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"albums:info:{seokey}"
    result = await _cached(cache, key, config.TTL_ALBUM, lambda: gaana.get_album_info([seokey], True))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/albums/similar/", summary="Retrieve similar albums.")
async def albums_similar(
    request: Request,
    album_id: str = Query(..., min_length=1, max_length=MAX_ARTIST_ID_LENGTH, pattern=ARTIST_ID_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"albums:similar:{album_id}:{limit}"
    result = await _cached(cache, key, config.TTL_SIMILAR, lambda: gaana.get_similar_albums(album_id, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------
@app.get("/artists/search/", summary="Search for artists.")
async def artists_search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, pattern=SEARCH_QUERY_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"artists:search:{query}:{limit}"
    result = await _cached(cache, key, config.TTL_SEARCH, lambda: gaana.search_artists(query, limit))
    if isinstance(result, dict) and "error" in result:
        return []
    return result


@app.get("/artists/info/", summary="Retrieve detailed information on an artist.")
async def artists_info(
    request: Request,
    seokey: str = Depends(validate_seokey),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    page: Optional[int] = Query(DEFAULT_PAGE, ge=MIN_PAGE, le=MAX_PAGE),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"artists:info:{seokey}:{limit}:{page}"
    result = await _cached(cache, key, config.TTL_ARTIST, lambda: gaana.get_artist_info([seokey], True, limit, page))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/artists/similar/", summary="Retrieve similar artists.")
async def artists_similar(
    request: Request,
    artist_id: str = Query(..., min_length=1, max_length=MAX_ARTIST_ID_LENGTH, pattern=ARTIST_ID_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"artists:similar:{artist_id}:{limit}"
    result = await _cached(cache, key, config.TTL_SIMILAR, lambda: gaana.get_similar_artists(artist_id, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/artists/tracks/", summary="Retrieve an artist's tracks.")
async def artists_tracks(
    request: Request,
    artist_id: str = Query(..., min_length=1, max_length=MAX_ARTIST_ID_LENGTH, pattern=ARTIST_ID_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    page: Optional[int] = Query(DEFAULT_PAGE, ge=MIN_PAGE, le=MAX_PAGE),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"artists:tracks:{artist_id}:{limit}:{page}"
    result = await _cached(cache, key, config.TTL_ARTIST, lambda: gaana.get_artist_tracks(artist_id, limit, page))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

# ---------------------------------------------------------------------------
# Trending / New Releases / Charts
# ---------------------------------------------------------------------------
@app.get("/trending", summary="Retrieve trending songs across languages.")
async def get_trending(
    request: Request,
    language: str = Query(..., min_length=1, max_length=MAX_LANGUAGE_LENGTH, pattern=LANGUAGE_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"trending:{language.lower()}:{limit}"
    result = await _cached(cache, key, config.TTL_TRENDING, lambda: gaana.get_trending(language, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/newreleases", summary="Retrieve newly released songs and albums.")
async def get_new_releases(
    request: Request,
    language: str = Query(..., min_length=1, max_length=MAX_LANGUAGE_LENGTH, pattern=LANGUAGE_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"newreleases:{language.lower()}:{limit}"
    result = await _cached(cache, key, config.TTL_NEW_RELEASES, lambda: gaana.get_new_releases(language, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/charts", summary="Retrieve the current top charts.")
async def get_charts(
    request: Request,
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"charts:{limit}"
    result = await _cached(cache, key, config.TTL_CHARTS, lambda: gaana.get_charts(limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------
@app.get("/playlists/search/", summary="Search for catalog playlists.")
async def playlists_search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, pattern=SEARCH_QUERY_PATTERN),
    limit: Optional[int] = Query(DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"playlists:search:{query}:{limit}"
    result = await _cached(cache, key, config.TTL_SEARCH, lambda: gaana.search_playlists(query, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/playlists/info/", summary="Retrieve detailed information on a playlist.")
async def playlists_info(
    request: Request,
    seokey: str = Depends(validate_seokey),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"playlists:info:{seokey}"
    result = await _cached(cache, key, config.TTL_PLAYLIST, lambda: gaana.get_playlist_info(seokey))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

# ---------------------------------------------------------------------------
# Downloads (stub — return allowed only when source permits)
# ---------------------------------------------------------------------------
@app.get("/tracks/{seokey}/download-authorization", tags=["downloads"],
         summary="Check download entitlement for a track.")
async def download_authorization(
    seokey: str = Path(..., min_length=1, max_length=MAX_SEOKEY_LENGTH, pattern=r"^[a-z0-9\-]+$"),
):
    return {"download_allowed": False}


# ---------------------------------------------------------------------------
# OpenAPI schema
# ---------------------------------------------------------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = get_openapi(
        title="GaanaPy",
        version="1.0",
        description="An unofficial Gaana API written in Python.",
        routes=app.routes,
    )
    return app.openapi_schema


app.openapi = custom_openapi
