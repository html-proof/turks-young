import pytest
from starlette.testclient import TestClient

from app import app


def test_songs_stream_info_endpoint():
    client = TestClient(app)
    response = client.get("/songs/test_song_123/stream-info?duration=240")
    assert response.status_code == 200
    data = response.json()
    assert data["songId"] == "test_song_123"
    assert data["duration"] == 240
    assert data["audio"]["codec"] == "aac"
    assert data["audio"]["sampleRate"] == 44100
    assert data["audio"]["bitDepth"] == 16
    assert data["audio"]["channels"] == 2

    variants = data["audio"]["variants"]
    # 240 seconds at 128 kbps -> 128 * 240 / 8 / 1000 = 3.84 MB
    var_128 = next(v for v in variants if v["bitrate"] == 128)
    assert var_128["estimatedMb"] == 3.84
    assert var_128["quality"] == "medium"

    # 240 seconds at 320 kbps -> 320 * 240 / 8 / 1000 = 9.60 MB
    var_320 = next(v for v in variants if v["bitrate"] == 320)
    assert var_320["estimatedMb"] == 9.6

    # 240 seconds at 24 kbps -> 24 * 240 / 8 / 1000 = 0.72 MB
    var_24 = next(v for v in variants if v["bitrate"] == 24)
    assert var_24["estimatedMb"] == 0.72


def test_api_songs_stream_info_endpoint():
    client = TestClient(app)
    response = client.get("/api/songs/test_song_456/stream-info?duration=180")
    assert response.status_code == 200
    res_json = response.json()
    data = res_json.get("data", res_json)
    assert data["songId"] == "test_song_456"
    assert data["duration"] == 180

    # 180 seconds at 96 kbps -> 96 * 180 / 8 / 1000 = 2.16 MB
    variants = data["audio"]["variants"]
    var_96 = next(v for v in variants if v["bitrate"] == 96)
    assert var_96["estimatedMb"] == 2.16
