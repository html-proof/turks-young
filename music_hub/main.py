from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from music_hub.api.v1 import router as api_v1_router
from music_hub.auth import FirebaseVerifier
from music_hub.cache import RedisCache
from music_hub.cache.lyrics_cache import LyricsCache
from music_hub.config import Settings, get_settings
from music_hub.container import Container
from music_hub.database import Database
from music_hub.errors import (
    ForbiddenOperation,
    InfrastructureUnavailable,
    ProviderUnavailable,
    ResourceNotFound,
)
from music_hub.middleware import RedisRateLimitMiddleware
from music_hub.providers.gaana import GaanaProvider
from music_hub.providers.lyrics import (
    LicensedHttpLyricsProvider,
    LrclibLyricsProvider,
    LyricsOvhProvider,
    LyricsProvider,
    UnsupportedLyricsProvider,
)
from music_hub.lyrics.matcher import LyricsMatcher
from music_hub.recommendations.candidate_generator import CandidateGenerator
from music_hub.recommendations.cursor import InvalidCursor
from music_hub.recommendations.engine import RecommendationEngine
from music_hub.repositories import (
    DeviceRepository,
    HistoryRepository,
    LibraryRepository,
    PlaylistRepository,
    PreferenceRepository,
    RecommendationRepository,
    SettingsRepository,
    UserRepository,
)
from music_hub.services.history import HistoryService
from music_hub.services.devices import DeviceService
from music_hub.services.home import HomeService
from music_hub.services.library import LibraryService
from music_hub.services.lyrics import LyricsService
from music_hub.services.music import MusicService
from music_hub.services.onboarding import OnboardingService
from music_hub.services.playlists import PlaylistService
from music_hub.services.search import SearchService
from music_hub.services.settings import SettingsService
from music_hub.services.users import UserService


logger = logging.getLogger("music_hub")


def _build_lyrics_providers(settings: Settings) -> list[LyricsProvider]:
    """Return the lyrics providers in the order they should be consulted.

    Sources that expose their own catalogue metadata come first, because only
    those can be scored against the requested recording. The unverifiable
    plain-text fallback goes last and never produces synchronised lyrics.
    """
    providers: list[LyricsProvider] = []
    if settings.lrclib_enabled:
        providers.append(
            LrclibLyricsProvider(
                settings.lrclib_base_url,
                settings.lyrics_user_agent,
                settings.lyrics_request_timeout_seconds,
            )
        )
    if settings.lyrics_api_base_url and settings.lyrics_api_token:
        providers.append(
            LicensedHttpLyricsProvider(
                settings.lyrics_api_base_url,
                settings.lyrics_api_token.get_secret_value(),
                settings.lyrics_request_timeout_seconds,
            )
        )
    if settings.lyrics_ovh_enabled:
        providers.append(
            LyricsOvhProvider(
                settings.lyrics_ovh_base_url,
                settings.lyrics_request_timeout_seconds,
                settings.lyrics_user_agent,
            )
        )
    return providers or [UnsupportedLyricsProvider()]


async def build_container(settings: Settings) -> Container:
    database = Database(
        settings.database_url,
        settings.database_min_pool_size,
        settings.database_max_pool_size,
        settings.database_ssl,
        settings.database_ssl_verify,
    )
    await database.connect()

    cache = RedisCache(
        settings.redis_url,
        settings.cache_namespace,
        settings.upstash_redis_rest_url,
        (
            settings.upstash_redis_rest_token.get_secret_value()
            if settings.upstash_redis_rest_token
            else None
        ),
    )
    try:
        await cache.connect()
    except Exception as exc:
        logger.warning(
            "Redis is unavailable; continuing without distributed cache (%s)",
            type(exc).__name__,
        )
        await cache.close()

    provider = GaanaProvider()
    lyrics_providers = _build_lyrics_providers(settings)
    firebase = FirebaseVerifier(settings)
    try:
        mode = await firebase.warm()
        logger.info("Firebase ID token verification mode: %s", mode)
    except Exception as exc:
        # Do not abort the boot: /health and /ready must stay reachable so the
        # platform can report why every authenticated route is failing.
        logger.error(
            "Firebase is unusable; every authenticated endpoint will return 503 (%s)",
            exc,
        )

    users_repository = UserRepository(database)
    preferences_repository = PreferenceRepository(database)
    history_repository = HistoryRepository(database)
    library_repository = LibraryRepository(database)
    playlists_repository = PlaylistRepository(database)
    recommendations_repository = RecommendationRepository(database)
    settings_repository = SettingsRepository(database)
    devices_repository = DeviceRepository(database)

    recommendation_engine = RecommendationEngine(
        recommendations_repository,
        CandidateGenerator(provider),
        cache,
        settings,
    )
    users = UserService(users_repository)
    onboarding = OnboardingService(preferences_repository, provider, cache)
    settings_service = SettingsService(settings_repository, cache)
    search = SearchService(
        provider,
        history_repository,
        cache,
        settings,
        settings_service,
    )
    music = MusicService(provider, cache, settings)
    lyrics = LyricsService(
        provider,
        lyrics_providers,
        LyricsCache(cache, settings.lyrics_cache_ttl, settings.lyrics_negative_cache_ttl),
        LyricsMatcher(settings.lyrics_match_confidence),
    )
    history = HistoryService(history_repository, settings_service, cache)
    library = LibraryService(library_repository, cache)
    playlists = PlaylistService(playlists_repository, cache)
    devices = DeviceService(devices_repository)
    home = HomeService(
        provider,
        music,
        recommendation_engine,
        history_repository,
        preferences_repository,
        settings_service,
    )
    return Container(
        settings=settings,
        database=database,
        cache=cache,
        firebase=firebase,
        provider=provider,
        lyrics_providers=lyrics_providers,
        users_repository=users_repository,
        preferences_repository=preferences_repository,
        history_repository=history_repository,
        library_repository=library_repository,
        playlists_repository=playlists_repository,
        recommendations_repository=recommendations_repository,
        settings_repository=settings_repository,
        devices_repository=devices_repository,
        users=users,
        onboarding=onboarding,
        search=search,
        music=music,
        lyrics=lyrics,
        history=history,
        library=library,
        playlists=playlists,
        recommendations=recommendation_engine,
        settings_service=settings_service,
        devices=devices,
        home=home,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        container = await build_container(settings)
        application.state.container = container
        try:
            yield
        finally:
            await container.provider.close()
            for lyrics_provider in container.lyrics_providers:
                await lyrics_provider.close()
            await container.cache.close()
            await container.database.close()

    application = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        debug=settings.app_debug,
        lifespan=lifespan,
        description="Authenticated, personalized music backend with a provider-independent catalog layer.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    application.add_middleware(RedisRateLimitMiddleware)
    application.include_router(api_v1_router, prefix=settings.api_prefix)

    @application.get("/", tags=["system"])
    async def root():
        return {
            "name": settings.app_name,
            "version": "1.0.0",
            "docs": "/docs",
            "api": settings.api_prefix,
        }

    @application.get("/health", tags=["system"])
    async def health():
        return {"status": "ok"}

    @application.get("/ready", tags=["system"])
    async def readiness(request: Request):
        container: Container = request.app.state.container
        database_ready = await container.database.ping()
        cache_ready = await container.cache.ping() if container.cache.configured else None
        try:
            firebase_status = container.firebase.check()
        except Exception as exc:
            # The detail can name credential paths; keep it in the logs only.
            logger.error("Firebase readiness check failed: %s", exc)
            firebase_status = "unavailable"
        ready = database_ready
        payload = {
            "status": "ready" if ready else "not_ready",
            "database": "connected" if database_ready else "unavailable",
            "firebase": firebase_status,
            "redis": (
                "connected" if cache_ready else "unavailable"
                if container.cache.configured else "not_configured"
            ),
            "provider": container.provider.name,
        }
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload,
        )

    @application.exception_handler(ResourceNotFound)
    async def not_found_handler(_: Request, exc: ResourceNotFound):
        return JSONResponse(status_code=404, content=_error("not_found", str(exc)))

    @application.exception_handler(ForbiddenOperation)
    async def forbidden_handler(_: Request, exc: ForbiddenOperation):
        return JSONResponse(status_code=403, content=_error("forbidden", str(exc)))

    @application.exception_handler(ProviderUnavailable)
    async def provider_handler(_: Request, exc: ProviderUnavailable):
        return JSONResponse(status_code=502, content=_error("provider_unavailable", str(exc)))

    @application.exception_handler(InfrastructureUnavailable)
    async def infrastructure_handler(request: Request, exc: InfrastructureUnavailable):
        logger.error("Infrastructure unavailable on %s: %s", request.url.path, exc)
        # Messages describe credentials, file paths and hostnames; never echo them.
        return JSONResponse(
            status_code=503,
            content=_error("service_unavailable", "A required backend service is unavailable"),
        )

    @application.exception_handler(InvalidCursor)
    async def cursor_handler(_: Request, exc: InvalidCursor):
        return JSONResponse(status_code=422, content=_error("invalid_cursor", str(exc)))

    @application.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception):
        logger.exception("Unhandled request failure", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=_error("internal_error", "The request could not be completed"),
        )

    return application


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


app = create_app()
from music_hub.services.devices import DeviceService
