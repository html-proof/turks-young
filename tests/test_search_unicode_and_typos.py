from api.catalog.search.ranking import normalize_query, _is_typo_match


def test_search_normalization_preserves_native_script_marks():
    assert normalize_query("  മലയാളം!!!  ") == "മലയാളം"
    assert normalize_query("Café") == "cafe"


def test_search_typo_matching_accepts_transposition_but_rejects_short_noise():
    assert _is_typo_match("ghlili", "ghilli")
    assert not _is_typo_match("gh", "ghilli")
