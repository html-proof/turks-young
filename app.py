import logging
import logging.config
import os
import re
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from firebase_admin import exceptions as firebase_exceptions

from api.cache.redis_cache import RedisCache
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
from api.pulse.routes import router as pulse_router
from api.personalization.service import PersonalizedMusicService

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
app.include_router(lyrics_router)
app.include_router(catalog_router)

cors_origins = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response

# ---------------------------------------------------------------------------
# Access log middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    response = await call_next(request)
    rid = getattr(request.state, "request_id", "-")
    logger.info(
        '"method":"%s","path":"%s","status":%d,"request_id":"%s"',
        request.method,
        request.url.path,
        response.status_code,
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
        content={"error": f"Upstream data source error: {type(exc).__name__}"},
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


@app.on_event("startup")
async def startup_event():
    language_catalog = LanguageCatalog.from_json(config.MUSIC_LANGUAGES_JSON)
    gaanapy = GaanaPy()
    app.state.gaanapy = gaanapy
    app.state.catalog_service = CatalogService(gaanapy, language_catalog)

    if config.LYRICS_PROVIDER != "lrclib":
        raise RuntimeError(f"Unsupported LYRICS_PROVIDER: {config.LYRICS_PROVIDER}")
    lyrics_provider = LRCLibProvider(
        gaanapy.aiohttp,
        user_agent=config.LYRICS_USER_AGENT,
        timeout=config.LYRICS_TIMEOUT,
    )

    cache = RedisCache(config.UPSTASH_REDIS_REST_URL, config.UPSTASH_REDIS_REST_TOKEN)
    await cache.connect()
    app.state.cache = cache

    firebase_runtime.initialize()  # still needed for JWT verification

    if config.DATABASE_URL:
        try:
            pool = await create_pool(config.DATABASE_URL)
            app.state.db_pool = pool
            repo = PostgresUserRepository(pool)
            app.state.user_repository = repo
            app.state.personalization_service = PersonalizedMusicService(repo)
            logger.info('"msg":"postgres connected"')
        except Exception as exc:
            logger.warning('"msg":"postgres unavailable","error":"%s"', exc)

    app.state.lyrics_service = LyricsService(
        provider=lyrics_provider,
        repository=PostgresLyricsRepository(app.state.db_pool),
        success_ttl=config.TTL_LYRICS,
        not_found_ttl=config.TTL_LYRICS_NOT_FOUND,
    )


@app.on_event("shutdown")
async def shutdown_event():
    gaanapy: GaanaPy | None = app.state.gaanapy
    if gaanapy and hasattr(gaanapy, "aiohttp"):
        await gaanapy.aiohttp.close()
    cache: RedisCache | None = app.state.cache
    if cache:
        await cache.close()
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

SEO_KEY_BASE_PATTERN = r"^[a-z0-9\-]+$"
ARTIST_ID_PATTERN = r"^[0-9]+$"
LANGUAGE_PATTERN = r"^[a-zA-Z]+(?:\s[a-zA-Z]+)*$"
SEARCH_QUERY_PATTERN = r"^[a-zA-Z0-9\s\-'.&]+$"


def validate_seokey(
    seokey: str = Query(
        ...,
        min_length=1,
        max_length=MAX_SEOKEY_LENGTH,
        pattern=SEO_KEY_BASE_PATTERN,
        description="The `seokey` of the resource.",
    )
):
    if not re.search(r"[a-zA-Z]", seokey):
        raise HTTPException(
            status_code=400,
            detail="seokey must contain at least one alphabetic character",
        )
    return seokey


def _gaana(request: Request) -> GaanaPy:
    return request.app.state.gaanapy


def _cache(request: Request) -> RedisCache:
    return request.app.state.cache


async def _cached(cache: RedisCache, key: str, ttl: int, coro):
    """Return cached value when present, otherwise await coro, cache result, and return."""
    hit = await cache.get(key)
    if hit is not None:
        return hit
    result = await coro
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
    if not cache.available:
        issues.append("redis_unavailable")
    if request.app.state.gaanapy is None:
        issues.append("gaana_client_not_initialized")
    if issues:
        return JSONResponse(status_code=503, content={"status": "degraded", "issues": issues})
    return {"status": "ready"}


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
    result = await _cached(cache, key, config.TTL_SEARCH, gaana.search_songs(query, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/songs/info/", summary="Retrieve detailed information on a song.")
async def songs_info(
    request: Request,
    seokey: str = Depends(validate_seokey),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"songs:info:{seokey}"
    result = await _cached(cache, key, config.TTL_SONG, gaana.get_track_info([seokey]))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
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
    result = await _cached(cache, key, config.TTL_SEARCH, gaana.search_albums(query, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/albums/info/", summary="Retrieve detailed information on an album.")
async def albums_info(
    request: Request,
    seokey: str = Depends(validate_seokey),
):
    gaana = _gaana(request)
    cache = _cache(request)
    key = f"albums:info:{seokey}"
    result = await _cached(cache, key, config.TTL_ALBUM, gaana.get_album_info([seokey], True))
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
    result = await _cached(cache, key, config.TTL_SIMILAR, gaana.get_similar_albums(album_id, limit))
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
    result = await _cached(cache, key, config.TTL_SEARCH, gaana.search_artists(query, limit))
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
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
    result = await _cached(cache, key, config.TTL_ARTIST, gaana.get_artist_info([seokey], True, limit, page))
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
    result = await _cached(cache, key, config.TTL_SIMILAR, gaana.get_similar_artists(artist_id, limit))
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
    result = await _cached(cache, key, config.TTL_ARTIST, gaana.get_artist_tracks(artist_id, limit, page))
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
    result = await _cached(cache, key, config.TTL_TRENDING, gaana.get_trending(language, limit))
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
    result = await _cached(cache, key, config.TTL_NEW_RELEASES, gaana.get_new_releases(language, limit))
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
    result = await _cached(cache, key, config.TTL_CHARTS, gaana.get_charts(limit))
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
    result = await _cached(cache, key, config.TTL_SEARCH, gaana.search_playlists(query, limit))
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
    result = await _cached(cache, key, config.TTL_PLAYLIST, gaana.get_playlist_info(seokey))
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
