import pytest

from api.catalog.artwork import normalize_url
from api.catalog.normalize import song
from api.errors import Errors
from api.functions import Functions
from api.songs.songs import Songs

ALBUM_SMALL = "https://a10.gaanacdn.com/gn_img/albums/abc/size_s.jpg"
ALBUM_LARGE_GUESS = "https://a10.gaanacdn.com/gn_img/albums/abc/size_l.jpg"
ARTIST = "https://a10.gaanacdn.com/gn_img/artists/xyz/size_m.jpg"


def test_artist_image_is_not_a_track_artwork_candidate():
    track = song({
        "seokey": "kesariya",
        "title": "Kesariya",
        "artists": "Arijit Singh",
        "artist_image": ARTIST,
        "artist_detail": [{"name": "Arijit Singh", "seokey": "arijit-singh", "atw": ARTIST}],
        "images": {"urls": {"small_artwork": ALBUM_SMALL}},
    })
    candidates = track["artwork_candidates"]
    assert candidates
    assert all("/artists/" not in url for url in candidates)
    assert track["artist_image"]


def test_artist_image_alone_leaves_track_without_artwork_candidates():
    track = song({"seokey": "kesariya", "title": "Kesariya", "artists": "Arijit Singh",
                  "artist_image": ARTIST})
    assert track["artwork_candidates"] == []


def test_original_url_preserved_after_upgraded_candidate():
    track = song({"seokey": "kesariya", "title": "Kesariya", "artwork": ALBUM_SMALL})
    candidates = track["artwork_candidates"]
    assert candidates.index(ALBUM_LARGE_GUESS) < candidates.index(ALBUM_SMALL)
    assert track["artworkUrl"] == ALBUM_SMALL


@pytest.mark.asyncio
async def test_legacy_song_images_keep_provider_url_in_size_slots():
    class Legacy(Songs):
        def __init__(self):
            self.functions = Functions()
            self.errors = Errors()

    data = await Legacy().format_json_songs({"seokey": "kesariya", "artwork": ALBUM_SMALL})
    assert data["images"]["urls"]["large_artwork"] == ALBUM_SMALL
    assert data["artwork_candidates"] == [ALBUM_LARGE_GUESS, ALBUM_SMALL]


def test_url_normalization_whitespace_protocol_relative_and_https():
    assert normalize_url("  //cdn.test/a.jpg \n") == "https://cdn.test/a.jpg"
    assert normalize_url("http://cdn.test/a.jpg") == "https://cdn.test/a.jpg"
    assert normalize_url(" https://cdn.test/a b.jpg ") == "https://cdn.test/a%20b.jpg"
