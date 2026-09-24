"""Playback resolution for /songs/info/: validation, fallbacks and caching.

All provider and CDN traffic is mocked; no request leaves the process.
"""
import time
from urllib.parse import unquote

import httpx
import pytest
from starlette.testclient import TestClient

from api import endpoints
from api.errors import Errors
from api.functions import Functions
from api.songs.playback import (
    identity_score,
    playback_cache_key,
    playback_cache_ttl,
    redact_url,
    song_info_cache_key,
    validate_stream,
)
from api.songs.songs import Songs

AUDIO = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64


class FakeCache:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl):
        self.store[key] = value
        self.ttls[key] = ttl


class FakeFunctions(Functions):
    """Decryption is keyed by message so tests control every decrypted URL."""

    def __init__(self, decrypted):
        super().__init__()
        self._decrypted = decrypted

    async def decryptLink(self, encrypted_data):
        return self._decrypted.get(encrypted_data, "")


class FakeGaana(Songs):
    def __init__(self, details, cdn, *, search=None, decrypted=None):
        self.api_endpoints = endpoints
        self.functions = FakeFunctions(decrypted or {})
        self.errors = Errors()
        self.cache = FakeCache()
        self._details = details
        self._search = search or []
        self.requests = []
        self.stream_http_client = httpx.AsyncClient(transport=httpx.MockTransport(cdn))

    async def _safe_request(self, method, url, **kwargs):
        self.requests.append((method, url))
        if url.startswith(endpoints.song_details_url):
            seokey = unquote(url[len(endpoints.song_details_url):])
            tracks = self._details.get(seokey)
            return {"tracks": tracks} if tracks else await self.errors.no_results()
        if url.startswith(endpoints.search_songs_url):
            return {"gr": [{"gd": self._search}]} if self._search else await self.errors.no_results()
        return await self.errors.no_results()


def raw_track(seokey, message, **overrides):
    track = {
        "seokey": seokey,
        "track_id": "1001",
        "track_title": "Kesariya",
        "artist": [{"name": "Arijit Singh", "seokey": "arijit-singh", "artist_id": "1"}],
        "album_title": "Brahmastra",
        "albumseokey": "brahmastra",
        "duration": "268",
        "release_date": "2022-07-17",
        "artwork_large": "https://a10.gaanacdn.com/images/albums/1/1/crop_480x480_1.jpg",
        "urls": {"medium": {"message": message}} if message else {},
    }
    track.update(overrides)
    return track


def audio_response(request, status=206, content_type="audio/mp4"):
    return httpx.Response(status, headers={"content-type": content_type}, content=AUDIO, request=request)


def signed(path, expires_in=3600):
    return f"https://stream.test/{path}?exp={int(time.time()) + expires_in}&sig=secret"


@pytest.mark.asyncio
async def test_valid_primary_stream_is_playable_and_cached_briefly():
    primary = signed("hls/song/128.mp4")
    seen = []

    def cdn(request):
        seen.append(request)
        return audio_response(request)

    gaana = FakeGaana({"kesariya": [raw_track("kesariya", "m1")]}, cdn, decrypted={"m1": primary})
    result = await gaana.resolve_song_playback("kesariya")

    assert result[0]["playable"] is True
    assert result[0]["unavailable_reason"] is None
    assert result[0]["stream_url"] == primary
    assert seen[0].method == "GET"
    assert seen[0].headers["range"] == "bytes=0-2047"
    key = playback_cache_key("kesariya")
    assert gaana.cache.store[key][0]["stream_url"] == primary
    assert 0 < gaana.cache.ttls[key] <= 300


@pytest.mark.asyncio
async def test_expired_primary_falls_back_to_decrypted_bitrate_variant():
    primary = signed("hls/song/128.mp4")

    def cdn(request):
        if request.url.path.endswith("/128.mp4"):
            return httpx.Response(403, headers={"content-type": "text/html"}, content=b"expired", request=request)
        return audio_response(request, status=200, content_type="application/octet-stream")

    gaana = FakeGaana({"kesariya": [raw_track("kesariya", "m1")]}, cdn, decrypted={"m1": primary})
    result = await gaana.resolve_song_playback("kesariya")

    track = result[0]
    assert track["playable"] is True
    assert track["stream_url"].startswith("https://stream.test/hls/song/64.mp4")
    assert track["stream_urls"]["urls"]["high_quality"] == track["stream_url"]


@pytest.mark.asyncio
async def test_all_sources_failing_reports_unplayable_and_is_not_cached():
    def cdn(request):
        return httpx.Response(404, request=request)

    gaana = FakeGaana(
        {"kesariya": [raw_track("kesariya", "m1")]},
        cdn,
        decrypted={"m1": signed("hls/song/128.mp4")},
    )
    result = await gaana.resolve_song_playback("kesariya")

    track = result[0]
    assert track["playable"] is False
    assert track["unavailable_reason"] == "NO_VALID_SOURCE"
    assert track["stream_url"] == ""
    assert "high_quality" in track["stream_urls"]["urls"]
    assert gaana.cache.store == {}


@pytest.mark.asyncio
async def test_decrypt_failure_is_reported_and_not_cached_anywhere():
    def cdn(request):
        return audio_response(request)

    gaana = FakeGaana({"kesariya": [raw_track("kesariya", "undecryptable")]}, cdn)
    info = await gaana.get_track_info(["kesariya"])
    assert info[0]["playable"] is False
    assert info[0]["unavailable_reason"] == "DECRYPT_FAILED"
    assert song_info_cache_key("kesariya") not in gaana.cache.store

    result = await gaana.resolve_song_playback("kesariya")
    assert result[0]["unavailable_reason"] == "DECRYPT_FAILED"
    assert gaana.cache.store == {}


@pytest.mark.asyncio
async def test_redirect_is_followed_during_validation():
    def cdn(request):
        if request.url.host == "stream.test":
            return httpx.Response(302, headers={"location": "https://edge.test/final/128.mp4"}, request=request)
        return audio_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn)) as client:
        check = await validate_stream(client, signed("hls/song/128.mp4"))
    assert check.ok is True
    assert check.status == 206


@pytest.mark.asyncio
async def test_head_blocked_but_ranged_get_succeeds():
    def cdn(request):
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        return audio_response(request, status=200, content_type="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn)) as client:
        check = await validate_stream(client, signed("hls/song/128.mp4"))
    assert check.ok is True


@pytest.mark.asyncio
async def test_html_error_page_with_200_is_rejected():
    def cdn(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>denied</html>", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn)) as client:
        check = await validate_stream(client, signed("hls/song/128.mp4"))
    assert check.ok is False


@pytest.mark.asyncio
async def test_identity_recovery_accepts_same_recording():
    dead = signed("dead/128.mp4")
    alive = signed("alive/128.mp4")

    def cdn(request):
        if request.url.path.startswith("/alive/"):
            return audio_response(request)
        return httpx.Response(410, request=request)

    gaana = FakeGaana(
        {
            "kesariya": [raw_track("kesariya", "dead")],
            "kesariya-1": [raw_track("kesariya-1", "alive", track_id="2002")],
        },
        cdn,
        search=[{"seo": "kesariya"}, {"seo": "kesariya-1"}],
        decrypted={"dead": dead, "alive": alive},
    )
    result = await gaana.resolve_song_playback("kesariya")

    track = result[0]
    assert track["playable"] is True
    assert track["stream_url"] == alive
    assert track["seokey"] == "kesariya"
    assert track["playback_source"]["id"] == "kesariya-1"
    assert track["playback_source"]["match_score"] >= 0.88


@pytest.mark.asyncio
async def test_identity_recovery_rejects_title_only_match():
    def cdn(request):
        if request.url.path.startswith("/alive/"):
            return audio_response(request)
        return httpx.Response(410, request=request)

    cover = raw_track(
        "kesariya-cover",
        "alive",
        artist=[{"name": "Some Cover Band", "seokey": "cover-band", "artist_id": "9"}],
        album_title="Acoustic Covers",
        duration="201",
        release_date="2023-02-01",
    )
    gaana = FakeGaana(
        {"kesariya": [raw_track("kesariya", "dead")], "kesariya-cover": [cover]},
        cdn,
        search=[{"seo": "kesariya-cover"}],
        decrypted={"dead": signed("dead/128.mp4"), "alive": signed("alive/128.mp4")},
    )
    result = await gaana.resolve_song_playback("kesariya")

    assert result[0]["playable"] is False
    assert result[0]["unavailable_reason"] == "NO_VALID_SOURCE"
    assert "playback_source" not in result[0]
    assert gaana.cache.store == {}


def test_identity_score_weights():
    target = {"title": "Kesariya", "artists": "Arijit Singh", "album": "Brahmastra",
              "duration": "268", "release_date": "2022-07-17"}
    assert identity_score(target, dict(target)) == 1.0
    title_only = {"title": "Kesariya (From &quot;Brahmastra&quot;)", "artists": "Other",
                  "album": "Other", "duration": "100", "release_date": "2022"}
    assert identity_score(target, title_only) < 0.88


def test_cache_ttl_respects_exp_and_logs_redact_signatures():
    now = 1_700_000_000
    assert playback_cache_ttl(f"https://s.test/a.mp4?exp={now + 120}", now=now) == 60
    assert playback_cache_ttl(f"https://s.test/a.mp4?exp={now + 30}", now=now) == 0
    assert playback_cache_ttl("https://s.test/a.mp4", now=now) == 300
    assert redact_url("https://s.test/a/128.mp4?exp=1&sig=secret") == "https://s.test/a/128.mp4"


def test_songs_info_endpoint_exposes_playable_fields():
    from app import app

    def cdn(request):
        return httpx.Response(404, request=request)

    previous = app.state.gaanapy
    app.state.gaanapy = FakeGaana(
        {"kesariya": [raw_track("kesariya", "m1")]}, cdn, decrypted={"m1": signed("x/128.mp4")},
    )
    try:
        response = TestClient(app).get("/songs/info/?seokey=kesariya")
    finally:
        app.state.gaanapy = previous
    assert response.status_code == 200
    track = response.json()[0]
    assert track["playable"] is False
    assert track["unavailable_reason"] == "NO_VALID_SOURCE"
    for field in ("seokey", "title", "stream_url", "stream_urls", "images"):
        assert field in track
