import json
import pytest
from unittest.mock import AsyncMock

from api.catalog.search.parser import AdvancedQueryParser, ParsedSearchQuery
from api.catalog.search.ranking import (
    RankingTier,
    canonical_song_key,
    confidence,
    normalize_query,
    rank,
    score_item,
)
from api.catalog.service import CatalogService, LanguageCatalog


def test_advanced_query_parser_explicit_filters():
    # 1. artist:AR Rahman
    q1 = AdvancedQueryParser.parse("artist:AR Rahman")
    assert q1.artist == "AR Rahman"
    assert q1.entity_intent == "ARTIST"

    # 2. Quoted artist: "A R Rahman"
    q2 = AdvancedQueryParser.parse('artist:"A R Rahman"')
    assert q2.artist == "A R Rahman"

    # 3. album:Premam
    q3 = AdvancedQueryParser.parse("album:Premam")
    assert q3.album == "Premam"
    assert q3.entity_intent == "ALBUM"

    # 4. Single year
    q4 = AdvancedQueryParser.parse("year:2015")
    assert q4.year == 2015
    assert q4.year_range is None

    # 5. Year range
    q5 = AdvancedQueryParser.parse("year:1990-1999")
    assert q5.year_range == (1990, 1999)

    # 6. Combined filter: artist:Vidyasagar language:tamil
    q6 = AdvancedQueryParser.parse("artist:Vidyasagar language:tamil")
    assert q6.artist == "Vidyasagar"
    assert q6.language == "tamil"

    # 7. Normal query + filter: Ghilli artist:Vidyasagar
    q7 = AdvancedQueryParser.parse("Ghilli artist:Vidyasagar")
    assert q7.free_text == "Ghilli"
    assert q7.artist == "Vidyasagar"


def test_natural_language_eras_and_genres():
    # "90s Tamil melody songs"
    q = AdvancedQueryParser.parse("90s Tamil melody songs")
    assert q.year_range == (1990, 1999)
    assert q.language == "tamil"
    assert q.genre == "melody"

    # "2010s Malayalam songs"
    q2 = AdvancedQueryParser.parse("2010s Malayalam songs")
    assert q2.year_range == (2010, 2019)
    assert q2.language == "malayalam"


def test_quoted_exact_phrase():
    q = AdvancedQueryParser.parse('"nee kavithaigala"')
    assert q.is_exact_phrase is True
    assert q.quoted_phrase == "nee kavithaigala"


def test_lyric_candidate_detection():
    # >= 3 meaningful lyric words without filters
    q1 = AdvancedQueryParser.parse("enthinu veroru suryodayam")
    assert q1.lyric_candidate is True

    # Short query (< 3 words) must NOT be lyric candidate
    q2 = AdvancedQueryParser.parse("ka")
    assert q2.lyric_candidate is False

    # Title with explicit filter must NOT be lyric candidate
    q3 = AdvancedQueryParser.parse("artist:Anirudh")
    assert q3.lyric_candidate is False


def test_exact_match_beats_personalization_section_83_84():
    """Rule 83 & 84: User with 100% Malayalam preference searches 'Ghilli'.
    The exact Tamil album/movie must rank #1 over unrelated Malayalam songs.
    Exact query relevance ALWAYS beats personalization.
    """
    tamil_album = {
        "id": "ghilli-tamil",
        "title": "Ghilli",
        "artists": [{"name": "Vidyasagar"}],
        "language": "tamil",
    }
    malayalam_song = {
        "id": "malayalam-popular",
        "title": "Premam Malare",
        "artists": [{"name": "Vijay Yesudas"}],
        "language": "malayalam",
        "popularity": 0.95,
    }

    tier_tamil, score_tamil, _ = score_item(
        "Ghilli",
        tamil_album,
        "album",
        user_languages=["malayalam"],
        user_artists=["Vijay Yesudas"],
    )
    tier_malayalam, score_malayalam, _ = score_item(
        "Ghilli",
        malayalam_song,
        "song",
        user_languages=["malayalam"],
        user_artists=["Vijay Yesudas"],
    )

    # Tamil exact album must be Tier A and beat Malayalam song
    assert tier_tamil == RankingTier.TIER_A
    assert tier_malayalam >= RankingTier.TIER_C
    assert score_tamil > score_malayalam


def test_bounded_personalization_tie_breaker_section_85():
    """Rule 85: User A (Malayalam heavy) and User B (Tamil heavy) search 'Love'.
    Similar text relevance items can be tie-broken by personalization, but
    personalization is strictly bounded (<= 30 points).
    """
    song_malayalam = {
        "id": "love-mal",
        "title": "Love Theme",
        "artists": [{"name": "Gopi Sundar"}],
        "language": "malayalam",
    }
    song_tamil = {
        "id": "love-tam",
        "title": "Love Theme",
        "artists": [{"name": "Anirudh"}],
        "language": "tamil",
    }

    # User A prefers Malayalam
    _, score_a_mal, _ = score_item("Love", song_malayalam, "song", user_languages=["malayalam"])
    _, score_a_tam, _ = score_item("Love", song_tamil, "song", user_languages=["malayalam"])
    assert score_a_mal > score_a_tam

    # User B prefers Tamil
    _, score_b_mal, _ = score_item("Love", song_malayalam, "song", user_languages=["tamil"])
    _, score_b_tam, _ = score_item("Love", song_tamil, "song", user_languages=["tamil"])
    assert score_b_tam > score_b_mal

    # Ensure personalization did not exceed 30 points
    diff_a = score_a_mal - score_a_tam
    assert diff_a <= 30


def test_typo_tolerance_strictness_section_86():
    """Rule 86:
    - 'anirud' matches 'Anirudh'
    - 'vidyasagr' matches 'Vidyasagar'
    - Short query 'AR' must NOT fuzzy-match 'Anirudh'
    """
    artist_anirudh = {"id": "1", "name": "Anirudh Ravichander"}
    artist_vidyasagar = {"id": "2", "name": "Vidyasagar"}

    tier_ani, score_ani, _ = score_item("anirud", artist_anirudh, "artist")
    assert tier_ani == RankingTier.TIER_B
    assert score_ani >= 500

    tier_vid, score_vid, _ = score_item("vidyasagr", artist_vidyasagar, "artist")
    assert tier_vid == RankingTier.TIER_B
    assert score_vid >= 500

    # Strict for short queries: "AR" should not match Anirudh via typo
    tier_short, score_short, _ = score_item("AR", artist_anirudh, "artist")
    assert tier_short > RankingTier.TIER_B or score_short < 300


def test_prefix_relevance_section_87():
    """Rule 87: 'ghi' -> Entities beginning with 'Ghi...' rank before entities
    where token appears deep inside unrelated metadata.
    """
    item_prefix = {"id": "1", "title": "Ghilli Theme", "artists": [{"name": "Vidyasagar"}]}
    item_internal = {"id": "2", "title": "Great Hits of Vidyasagar feat. Ghibli", "artists": [{"name": "Others"}]}

    ranked = rank("ghi", [item_internal, item_prefix], "song", 10)
    assert ranked[0]["id"] == "1"


def test_canonical_deduplication_section_91():
    """Rule 91: If providers return the same recording 4 times across singles,
    compilations, etc., collapse to one primary canonical result.
    """
    dup1 = {"id": "d1", "title": "Appadi Podu", "artists": "Vidyasagar, KK", "album": "Ghilli", "duration": "240"}
    dup2 = {"id": "d2", "title": "Appadi Podu", "artists": "Vidyasagar, KK", "album": "Ghilli", "duration": "242"}
    dup3 = {"id": "d3", "title": "Appadi Podu", "artists": "Vidyasagar, KK", "album": "Ghilli", "duration": "239"}
    dup4 = {"id": "d4", "title": "Appadi Podu", "artists": "Vidyasagar, KK", "album": "Ghilli", "duration": "241"}
    diff_song = {"id": "d5", "title": "Arjunaru Villu", "artists": "Vidyasagar", "album": "Ghilli", "duration": "260"}

    # All 4 Appadi Podu share the same canonical key
    k1 = canonical_song_key(dup1, "song")
    k2 = canonical_song_key(dup2, "song")
    assert k1 == k2

    ranked = rank("Appadi Podu", [dup1, dup2, dup3, dup4, diff_song], "song", 10)
    # Exactly one copy of Appadi Podu in results
    appadi_hits = [item for item in ranked if "Appadi Podu" in item["title"]]
    assert len(appadi_hits) == 1


@pytest.mark.asyncio
async def test_lyrics_search_integration_and_no_copyright_exposure_section_88():
    """Rule 88: When query has 3+ words, lyrics candidate search is supported,
    and results are marked with is_lyrics_match without exposing long lyrics text.
    """
    catalog = AsyncMock()
    catalog.search_songs = AsyncMock(return_value=[])
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.search_albums = AsyncMock(return_value=[])
    catalog.search_playlists = AsyncMock(return_value=[])

    lyrics_provider = AsyncMock()
    lyrics_provider._request = AsyncMock(return_value=[
        {
            "id": 12345,
            "trackName": "Enthinu Veroru Suryodayam",
            "artistName": "Sujatha Mohan",
            "albumName": "Mazhavillu",
            "duration": 280,
            "plainLyrics": "Enthinu veroru sooryodhayam... (full copyright text)",
        }
    ])

    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))
    service.lyrics_provider = lyrics_provider

    res = await service.search("enthinu veroru suryodayam", None, 1, 20)
    assert "songs" in res
    assert len(res["songs"]) >= 1
    top_song = res["songs"][0]
    assert top_song["title"] == "Enthinu Veroru Suryodayam"
    assert top_song.get("is_lyrics_match") is True
    # Verify large copyrighted lyric passages are not included in search results
    assert "plainLyrics" not in top_song
    assert "syncedLyrics" not in top_song


@pytest.mark.asyncio
async def test_service_with_authenticated_user_uuid_personalization():
    """Tests that passing user_uid to search uses bounded personalization
    without breaking exact matches.
    """
    catalog = AsyncMock()
    catalog.search_songs = AsyncMock(return_value=[
        {"id": "s1", "title": "Malare", "artists": "Vijay Yesudas", "language": "Malayalam"},
        {"id": "s2", "title": "Malare", "artists": "Other Artist", "language": "Tamil"},
    ])
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.search_albums = AsyncMock(return_value=[])
    catalog.search_playlists = AsyncMock(return_value=[])

    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))
    # Perform search with user_uid
    res = await service.search("Malare", None, 1, 20, user_uid="user-test-uuid")
    assert res["songs"][0]["title"] == "Malare"


@pytest.mark.asyncio
async def test_parallel_provider_deduplication_prefers_playable_stream():
    """Rule 18 & 20: When Gaana and JioSaavn return the same song, collapse to
    a single canonical entry and pick the one with the valid playable stream URL.
    """
    gaana_track = {
        "id": "100",
        "title": "Why This Kolaveri Di",
        "artists": [{"name": "Dhanush"}, {"name": "Anirudh"}],
        "album": "3",
        "duration": "250",
        "stream_url": None,
    }
    saavn_track = {
        "id": "saavn:200",
        "title": "Why This Kolaveri Di",
        "artists": [{"name": "Dhanush"}, {"name": "Anirudh"}],
        "album": "3",
        "duration": "252",
        "stream_url": "https://aac.saavncdn.com/stream_320.mp4",
    }

    # Both share the same canonical key
    assert canonical_song_key(gaana_track, "song") == canonical_song_key(saavn_track, "song")

    ranked = rank("Why This Kolaveri Di", [gaana_track, saavn_track], "song", 10)
    assert len(ranked) == 1
    # Entry with stream_url should be selected
    assert ranked[0]["stream_url"] == "https://aac.saavncdn.com/stream_320.mp4"


@pytest.mark.asyncio
async def test_soundtrack_expansion_lightweight_no_waterfall():
    """Rule 6 & 19: Soundtrack expansion must extract tracks from album payload
    with fetch_missing_tracks=False and not fail or throw TimeoutError.
    """
    catalog = AsyncMock()
    catalog.search_songs = AsyncMock(return_value=[])
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.search_albums = AsyncMock(return_value=[
        {"id": "ghilli-soundtrack", "title": "Ghilli", "language": "tamil"}
    ])
    catalog.search_playlists = AsyncMock(return_value=[])
    catalog.get_album_info = AsyncMock(return_value=[
        {
            "id": "ghilli-soundtrack",
            "title": "Ghilli",
            "tracks": [
                {"track_id": "t1", "seokey": "appadi-podu", "title": "Appadi Podu", "artist": "Vidyasagar"},
                {"track_id": "t2", "seokey": "arjunar-villu", "title": "Arjunar Villu", "artist": "Vidyasagar"},
            ],
        }
    ])

    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))
    res = await service.search("Ghilli", None, 1, 20)

    # Should expand soundtrack tracks into songs without error
    assert "songs" in res
    assert len(res["songs"]) >= 2
    titles = [s["title"] for s in res["songs"]]
    assert "Appadi Podu" in titles
    assert "Arjunar Villu" in titles
    # Confirm get_album_info was called with fetch_missing_tracks=False
    catalog.get_album_info.assert_called_with(["ghilli-soundtrack"], True, fetch_missing_tracks=False)
