import pytest
from unittest.mock import AsyncMock

from api import endpoints
from api.errors import Errors
from api.playlists.playlists import Playlists


class FakePlaylists(Playlists):
    def __init__(self, response):
        self.api_endpoints = endpoints
        self.errors = Errors()
        self._safe_request = AsyncMock(return_value=response)
        self.get_track_info = AsyncMock(return_value=[{"seokey": "track-one"}])
        self.format_json_songs = AsyncMock(
            side_effect=lambda item, resolve_stream=False: {
                "seokey": item["seokey"],
                "title": item.get("title") or item.get("track_title"),
            }
        )


@pytest.mark.asyncio
async def test_success_error_marker_does_not_reject_playlist():
    playlist = FakePlaylists({
        "error": "SUCCESS",
        "count": 1,
        "tracks": [{"seokey": "track-one"}],
    })

    result = await playlist.get_playlist_info("playlist-one")

    assert result == [{"seokey": "track-one"}]
    playlist.get_track_info.assert_awaited_once_with(["track-one"])


@pytest.mark.asyncio
async def test_real_upstream_error_is_preserved():
    playlist = FakePlaylists({"error": "Playlist unavailable"})

    result = await playlist.get_playlist_info("missing-playlist")

    assert result == {"error": "Playlist unavailable"}
    playlist.get_track_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedded_track_with_numeric_id_is_not_dropped():
    playlist = FakePlaylists({
        "error": "SUCCESS",
        "count": 1,
        "tracks": [{"track_id": 42, "title": "Playlist Song"}],
    })

    result = await playlist.get_playlist_info("malayalam-fast-numbers")

    assert result == [{"seokey": "42", "title": "Playlist Song"}]
    playlist.format_json_songs.assert_awaited_once()
    playlist.get_track_info.assert_not_awaited()
