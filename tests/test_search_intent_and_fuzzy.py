from unittest.mock import AsyncMock

import pytest

from api.catalog.search.ranking import RankingTier, album_core_key, rank, score_item
from api.catalog.search.vocabulary import SearchVocabulary
from api.catalog.service import CatalogService, LanguageCatalog


TAMIL_ALBUM = {
    "seokey": "sarkar-tamil", "album_id": "2236606",
    "title": "Sarkar (Tamil) (Original Motion Picture Soundtrack)",
    "artists": "A. R. Rahman", "language": "Tamil", "song_count": 5,
}
HINDI_ALBUM = {
    "seokey": "sarkar-hindi-0", "album_id": "137627", "title": "Sarkar",
    "artists": "Amitabh Bachchan", "language": "Hindi", "song_count": 10,
}
PUNJABI_SINGLE = {
    "seokey": "sarkar-punjabi", "album_id": "9", "title": "Sarkar",
    "artists": "Jaura Phagwara", "language": "Punjabi", "song_count": 1,
}
PUNJABI_SONG = {
    "seokey": "sarkar-song", "track_id": "p1", "title": "Sarkar",
    "artists": "Jaura Phagwara", "album": "Sarkar", "album_seokey": "sarkar-punjabi",
    "language": "Punjabi", "duration": "210",
}
TAMIL_TRACKS = [
    {"track_id": "t1", "seokey": "simtaangaran", "title": "Simtaangaran", "artist": "A. R. Rahman",
     "album": "Sarkar (Tamil) (Original Motion Picture Soundtrack)", "album_seokey": "sarkar-tamil", "language": "Tamil"},
    {"track_id": "t2", "seokey": "oruviral-puratchi", "title": "Oruviral Puratchi", "artist": "A. R. Rahman",
     "album": "Sarkar (Tamil) (Original Motion Picture Soundtrack)", "album_seokey": "sarkar-tamil", "language": "Tamil"},
]


def test_album_core_key_collapses_language_and_soundtrack_qualifiers():
    assert album_core_key("Sarkar (Tamil) (Original Motion Picture Soundtrack)") == "sarkar"
    assert album_core_key("Sarkar - Original Soundtrack") == "sarkar"
    assert album_core_key({"title": "Sarkar [OST]"}) == "sarkar"
    assert album_core_key("Sarkar 3 (Original Motion Picture Soundtrack)") == "sarkar 3"
    assert album_core_key("Sarkar Raj") == "sarkar raj"
    assert album_core_key("") == ""


def test_soundtrack_songs_survive_exact_title_gate():
    songs = [PUNJABI_SONG, *TAMIL_TRACKS]
    ranked = rank("Sarkar", songs, "song", 10)
    titles = {item["title"] for item in ranked}
    assert {"Sarkar", "Simtaangaran", "Oruviral Puratchi"} <= titles


def test_preferred_language_soundtrack_scores_like_exact_title():
    tier, score, reasons = score_item("Sarkar", TAMIL_TRACKS[0], "song", user_languages=["tamil"])
    assert tier == RankingTier.TIER_A
    assert any("preferred_language" in r for r in reasons)
    _, single_score, _ = score_item("Sarkar", PUNJABI_SONG, "song", user_languages=["tamil"])
    assert score >= single_score


def test_fuzzy_word_order_and_typos_rank_as_relevant():
    item = {"id": "1", "title": "Simtaangaran", "artists": [{"name": "A. R. Rahman"}], "album": "Sarkar (Tamil)"}
    tier, score, _ = score_item("simtangaran rahman", item, "song")
    assert tier <= RankingTier.TIER_B
    assert score >= 300
    short_tier, short_score, _ = score_item("AR", {"id": "2", "name": "Anirudh Ravichander"}, "artist")
    assert short_tier > RankingTier.TIER_B or short_score < 300


def test_movie_album_prefers_listener_language():
    albums = [dict(HINDI_ALBUM, id="sarkar-hindi-0"), dict(TAMIL_ALBUM, id="sarkar-tamil"), dict(PUNJABI_SINGLE, id="sarkar-punjabi")]
    chosen = CatalogService._movie_album_for_query("sarkar", albums, ["tamil"], None)
    assert chosen["id"] == "sarkar-tamil"
    chosen = CatalogService._movie_album_for_query("sarkar", albums, ["punjabi"], None)
    assert chosen["id"] == "sarkar-punjabi"
    assert CatalogService._movie_album_for_query("sarkar 3", albums, ["tamil"], None) is None


def _catalog_with_sarkar():
    catalog = AsyncMock()
    catalog.search_songs = AsyncMock(return_value=[PUNJABI_SONG])
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.search_albums = AsyncMock(return_value=[HINDI_ALBUM, TAMIL_ALBUM, PUNJABI_SINGLE])
    catalog.search_playlists = AsyncMock(return_value=[])
    catalog.get_album_info = AsyncMock(return_value=[{"id": "sarkar-tamil", "seokey": "sarkar-tamil",
                                                      "title": TAMIL_ALBUM["title"], "language": "Tamil",
                                                      "tracks": TAMIL_TRACKS}])
    return catalog


@pytest.mark.asyncio
async def test_movie_search_leads_with_soundtrack_for_tamil_listener():
    catalog = _catalog_with_sarkar()
    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))

    res = await service.search("Sarkar tamil", None, 1, 20)

    assert res["top_result"]["type"] == "album"
    assert res["top_result"]["item"]["id"] == "sarkar-tamil"
    titles = [s["title"] for s in res["songs"]]
    assert titles[:2] == ["Simtaangaran", "Oruviral Puratchi"]
    assert "Sarkar" in titles
    catalog.get_album_info.assert_called_with(["sarkar-tamil"], True, fetch_missing_tracks=False)


@pytest.mark.asyncio
async def test_song_tab_expands_movie_album_and_leads_with_its_tracks():
    catalog = _catalog_with_sarkar()
    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))

    res = await service.search("Sarkar tamil", "song", 1, 20)

    titles = [s["title"] for s in res["items"]]
    assert titles[:2] == ["Simtaangaran", "Oruviral Puratchi"]
    assert "Sarkar" in titles


def test_vocabulary_suggests_and_corrects():
    vocab = SearchVocabulary()
    vocab.observe([
        {"title": "Simtaangaran", "artists": [{"name": "A. R. Rahman"}], "album": "Sarkar (Tamil)"},
        {"title": "Sarkar", "artist": "Jaura Phagwara"},
    ], "song")
    assert "Sarkar" in vocab.suggest("sar")
    assert vocab.correct("sarkr") == "Sarkar"
    assert vocab.correct("simtangaran") == "Simtaangaran"
    assert vocab.correct("sarkar") is None
    assert vocab.correct("xyzzy") is None


@pytest.mark.asyncio
async def test_misspelled_query_is_corrected_and_retried():
    catalog = AsyncMock()

    async def songs_for(query, limit):
        return [PUNJABI_SONG] if query.lower().startswith("sarkar") else []

    catalog.search_songs = AsyncMock(side_effect=songs_for)
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.search_albums = AsyncMock(return_value=[])
    catalog.search_playlists = AsyncMock(return_value=[])
    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))
    service.vocabulary.add("Sarkar")

    res = await service.search("sarkr", "song", 1, 10)

    assert res["corrected_query"] == "Sarkar"
    assert res["original_query"] == "sarkr"
    assert res["items"][0]["title"] == "Sarkar"

    exact = await service.search("sarkr", "song", 1, 10, _allow_correction=False)
    assert exact["items"] == []
    assert "corrected_query" not in exact


@pytest.mark.asyncio
async def test_suggest_merges_recent_and_catalog_terms():
    catalog = AsyncMock()
    catalog.search_songs = AsyncMock(return_value=[{"seokey": "s", "track_id": "1", "title": "Sarkar", "artists": "Virat"}])
    service = CatalogService(catalog, LanguageCatalog.from_json("[]"))
    service.vocabulary.add("Sarkar (Tamil)")

    items = await service.suggest("sar", 8, ["sarkar songs", "premam"])

    texts = [i["text"] for i in items]
    assert texts[0] == "sarkar songs"
    assert items[0]["source"] == "recent"
    assert "Sarkar (Tamil)" in texts
    assert "premam" not in texts
