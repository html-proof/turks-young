from api.catalog.search.ranking import merge_song_duplicates, rank


def _track(url, *, track_id="123", title="Appadi Podu", duration=270, quality="high_quality"):
    return {
        "id": track_id,
        "provider": "gaana",
        "title": title,
        "artists": [{"name": "KK, Anuradha Sriram"}],
        "album": "Ghilli",
        "duration": duration,
        "stream_url": url,
        "stream_urls": {"urls": {quality: url}},
    }


def test_duplicate_track_urls_merge_before_ranking():
    merged = merge_song_duplicates([
        _track("https://cdn.example/96?expired=1", quality="low_quality"),
        _track("https://cdn.example/160", quality="high_quality"),
        _track("https://cdn.example/320", quality="very_high_quality"),
    ])
    assert len(merged) == 1
    assert {"low_quality", "high_quality", "very_high_quality"}.issubset(merged[0]["stream_urls"]["urls"])
    assert len(rank("appadi podu", merged, "song", 10)) == 1


def test_live_version_is_not_merged_with_original():
    merged = merge_song_duplicates([
        _track("https://cdn.example/original"),
        _track("https://cdn.example/live", track_id="124", title="Appadi Podu - Live"),
    ])
    assert len(merged) == 2


def test_compilation_album_duplicates_merge_into_original_album():
    tracks = [
        {"id": "c1", "title": "Kesariya", "artists": [{"name": "Pritam, Arijit Singh"}], "album": "Pritam (All Time Hits)", "duration": 268, "stream_url": "https://cdn.example/c1"},
        {"id": "orig", "title": "Kesariya", "artists": [{"name": "Pritam, Arijit Singh"}], "album": "Brahmastra", "duration": 268, "stream_url": "https://cdn.example/orig"},
        {"id": "c2", "title": "Kesariya", "artists": [{"name": "Pritam, Arijit Singh"}], "album": "Dance Blast", "duration": 268, "stream_url": "https://cdn.example/c2"},
    ]
    merged = merge_song_duplicates(tracks)
    assert len(merged) == 1
    assert merged[0]["album"] == "Brahmastra"
    ranked = rank("Kesariya", tracks, "song", 10)
    assert len(ranked) == 1
    assert ranked[0]["album"] == "Brahmastra"

