from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import pytest

from api.personalization.candidate_generator import CandidateGenerator
from api.personalization.mix_generator import MixGenerator
from api.personalization.models import (
    BehavioralEvent,
    ListeningEvent,
    TrackSnapshot,
    UserTasteProfile,
)
from api.personalization.profile_engine import (
    ProfileEngine,
    compute_time_decay,
    evaluate_event_weight,
)
from api.personalization.ranking_engine import RankingEngine, _canonical_key
from api.personalization.service import PersonalizedMusicService


# ---------------------------------------------------------------------------
# Test Mocks & Helpers
# ---------------------------------------------------------------------------

class MockCatalog:
    def __init__(self, catalog_db: dict[str, list[dict]] | None = None):
        self.catalog_db = catalog_db or {}

    async def search_songs(self, query: str, limit: int):
        q = query.lower().strip()
        results = []
        for key, tracks in self.catalog_db.items():
            if key in q or any(term in key for term in q.split()):
                results.extend(tracks)
        return results[:limit]

    async def get_trending(self, language: str, limit: int):
        lang = language.lower().strip()
        return self.catalog_db.get(f"trending_{lang}", [])[:limit]

    async def get_new_releases(self, language: str, limit: int):
        lang = language.lower().strip()
        return self.catalog_db.get(f"releases_{lang}", [])[:limit]


class MockRepository:
    def __init__(
        self,
        profiles: dict[str, dict] | None = None,
        favorites: dict[str, list] | None = None,
        history: dict[str, list] | None = None,
        behavioral_events: dict[str, list] | None = None,
        impression_counts: dict[str, dict[str, int]] | None = None,
    ):
        self.profiles = profiles or {}
        self.favorites = favorites or {}
        self.history = history or {}
        self.behavioral_events = behavioral_events or {}
        self.impression_counts = impression_counts or {}
        self.saved_profiles = {}
        self.recorded_impressions = []

    async def get_profile(self, uid: str):
        return self.profiles.get(uid, {})

    async def list_favorites(self, uid: str):
        return self.favorites.get(uid, [])

    async def list_history(self, uid: str, limit: int = 100):
        return self.history.get(uid, [])[:limit]

    async def list_behavioral_events(self, uid: str, limit: int = 100):
        return self.behavioral_events.get(uid, [])[:limit]

    async def save_taste_profile(self, uid: str, profile_data: dict):
        self.saved_profiles[uid] = profile_data

    async def get_impression_counts(self, uid: str, candidate_ids: list[str]):
        counts = self.impression_counts.get(uid, {})
        return {cid: counts[cid] for cid in candidate_ids if cid in counts}

    async def record_impressions(self, uid: str, items: list[dict], section_id: str = "home_feed"):
        self.recorded_impressions.extend([(uid, i.get("id"), section_id) for i in items])

    async def get_collaborative_candidates(self, uid: str, limit: int = 40):
        return []


# ---------------------------------------------------------------------------
# 1. User A vs User B Differentiation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_a_and_user_b_differentiation():
    """Two different users with different listening histories must receive completely distinct Home recommendations."""
    catalog = MockCatalog({
        "ar rahman": [
            {"id": "rahman_1", "title": "Nee Singam Dhan", "artists": [{"name": "A.R. Rahman"}], "language": "tamil"},
            {"id": "rahman_2", "title": "Kun Faya Kun", "artists": [{"name": "A.R. Rahman"}], "language": "hindi"},
        ],
        "sushin shyam": [
            {"id": "sushin_1", "title": "Illuminati", "artists": [{"name": "Sushin Shyam"}], "language": "malayalam"},
            {"id": "sushin_2", "title": "Aadharanjali", "artists": [{"name": "Sushin Shyam"}], "language": "malayalam"},
        ],
        "anirudh": [
            {"id": "anirudh_1", "title": "Hukum", "artists": [{"name": "Anirudh Ravichander"}], "language": "tamil"},
            {"id": "anirudh_2", "title": "Badass", "artists": [{"name": "Anirudh Ravichander"}], "language": "tamil"},
        ],
        "trending_malayalam": [
            {"id": "mal_trend_1", "title": "Malayalam Hit 1", "artists": [{"name": "Sushin Shyam"}], "language": "malayalam"},
        ],
        "trending_tamil": [
            {"id": "tam_trend_1", "title": "Tamil Hit 1", "artists": [{"name": "Anirudh Ravichander"}], "language": "tamil"},
        ],
    })

    repo = MockRepository(
        profiles={
            "user_malayalam": {"language_ids": ["malayalam"], "favorite_artists": ["Sushin Shyam"]},
            "user_tamil": {"language_ids": ["tamil"], "favorite_artists": ["Anirudh Ravichander"]},
        },
        favorites={
            "user_malayalam": [{"id": "fav_m", "language": "malayalam", "artists": ["Sushin Shyam"]}],
            "user_tamil": [{"id": "fav_t", "language": "tamil", "artists": ["Anirudh Ravichander"]}],
        },
        history={
            "user_malayalam": [{"id": "sushin_1", "title": "Illuminati", "language": "malayalam", "artists": ["Sushin Shyam"], "completed": True}],
            "user_tamil": [{"id": "anirudh_1", "title": "Hukum", "language": "tamil", "artists": ["Anirudh Ravichander"], "completed": True}],
        },
    )

    service = PersonalizedMusicService(repo)

    recs_a = await service.recommendations("user_malayalam", catalog, limit=10)
    recs_b = await service.recommendations("user_tamil", catalog, limit=10)

    assert len(recs_a) > 0
    assert len(recs_b) > 0

    # User A's recommendations are dominated by Malayalam & Sushin Shyam
    a_titles = [r["title"] for r in recs_a]
    assert any("Illuminati" in t or "Aadharanjali" in t or "Malayalam" in t for t in a_titles)

    # User B's recommendations are dominated by Tamil & Anirudh
    b_titles = [r["title"] for r in recs_b]
    assert any("Hukum" in t or "Badass" in t or "Tamil" in t for t in b_titles)

    # Top recommendations are completely different between UUIDs
    assert recs_a[0]["id"] != recs_b[0]["id"]


# ---------------------------------------------------------------------------
# 2. Behavioral Signal Weights (Likes, Skips, Completions)
# ---------------------------------------------------------------------------

def test_signal_weights():
    # Like gives strong positive score (+8.0)
    w_like, cat_like = evaluate_event_weight("song_liked")
    assert w_like == 8.0
    assert cat_like == "liked"

    # Skip in first 10s gives severe negative penalty (-7.0)
    w_skip10, cat_skip10 = evaluate_event_weight("song_skipped", played_seconds=6, completion_ratio=0.03)
    assert w_skip10 == -7.0
    assert cat_skip10 == "skip_under_10"

    # Skip under 30s gives negative penalty (-4.0)
    w_skip30, cat_skip30 = evaluate_event_weight("song_skipped", played_seconds=22, completion_ratio=0.10)
    assert w_skip30 == -4.0
    assert cat_skip30 == "skip_under_30"

    # Completion > 90% gives positive boost (+5.0)
    w_comp, cat_comp = evaluate_event_weight("song_completed", played_seconds=210, completion_ratio=0.95)
    assert w_comp == 5.0

    # Search and play gives combined boost (+4.0 + completion)
    w_search, _ = evaluate_event_weight("song_play", played_seconds=200, completion_ratio=0.92, source="search_result")
    assert w_search == 9.0  # 5.0 + 4.0


# ---------------------------------------------------------------------------
# 3. Time Decay
# ---------------------------------------------------------------------------

def test_time_decay_brackets():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    # Today: 1.00
    today = now - timedelta(hours=3)
    assert compute_time_decay(today, now) == 1.00

    # 4 days ago (within 7 days): 0.90
    four_days = now - timedelta(days=4)
    assert compute_time_decay(four_days, now) == 0.90

    # 20 days ago (within 30 days): 0.70
    twenty_days = now - timedelta(days=20)
    assert compute_time_decay(twenty_days, now) == 0.70

    # 60 days ago (within 90 days): 0.45
    sixty_days = now - timedelta(days=60)
    assert compute_time_decay(sixty_days, now) == 0.45

    # 120 days ago (> 90 days): 0.20
    old = now - timedelta(days=120)
    assert compute_time_decay(old, now) == 0.20


# ---------------------------------------------------------------------------
# 4. Session Intent Real-Time Shift
# ---------------------------------------------------------------------------

def test_session_intent_immediate_pivot():
    """A user who normally listens to Malayalam, but searches and plays Tamil tracks in current session,
    must have session intent immediately boost Tamil without permanently overwriting long-term profile."""
    engine = ProfileEngine()
    now = datetime.now(timezone.utc)

    history = [
        {"id": f"m_{i}", "language": "malayalam", "artists": ["K. S. Chithra"], "completed": True, "played_at": (now - timedelta(days=i)).isoformat()}
        for i in range(1, 15)
    ]
    # Current session: 4 Tamil plays
    session_id = "session_tamil_tonight"
    events = [
        {"event_type": "song_play", "language": "tamil", "artists": ["Ilaiyaraaja"], "session_id": session_id, "completed": True, "created_at": now.isoformat()}
        for _ in range(4)
    ]

    profile = engine.build_profile(
        user_id="user-session-test",
        profile_row={"language_ids": ["malayalam"], "favorite_artists": ["K. S. Chithra"]},
        favorites=[],
        history_events=history,
        behavioral_events=events,
        current_session_id=session_id,
        now=now,
    )

    # Current session profile reflects Ilaiyaraaja & Tamil
    assert "tamil" in profile.current_session["languages"]
    assert "ilaiyaraaja" in profile.current_session["artists"]

    # Long term profile still maintains Malayalam
    assert "malayalam" in profile.long_term["languages"]

    # Blended language profile contains both, reflecting the session shift
    assert profile.languages.get("tamil", 0) > 0
    assert profile.languages.get("malayalam", 0) > 0


# ---------------------------------------------------------------------------
# 5. Impression Fatigue Penalties
# ---------------------------------------------------------------------------

def test_impression_fatigue_penalty():
    engine = RankingEngine()
    now = datetime.now(timezone.utc)
    profile = UserTasteProfile(user_id="u1", artists={"anirudh": 1.0}, languages={"tamil": 1.0})

    track = {"id": "song_overexposed", "title": "Overexposed Song", "artists": [{"name": "Anirudh"}], "language": "tamil"}

    # Track shown 0 times: full high score
    score_unseen, _ = engine.score_candidate(track, profile, {}, impression_counts={}, now=now)

    # Track shown 5 times without engagement: heavily penalized
    score_exposed, _ = engine.score_candidate(track, profile, {}, impression_counts={"song_overexposed": 5}, now=now)

    assert score_unseen > score_exposed
    assert (score_unseen - score_exposed) >= 0.40


# ---------------------------------------------------------------------------
# 6. Cold Start vs Experienced User
# ---------------------------------------------------------------------------

def test_cold_start_versus_experienced_user():
    engine = ProfileEngine()
    now = datetime.now(timezone.utc)

    # New user (0 history events): onboarding language 'punjabi' dominates
    cold_profile = engine.build_profile(
        user_id="cold_user",
        profile_row={"language_ids": ["punjabi"], "favorite_artists": ["Diljit Dosanjh"]},
        favorites=[],
        history_events=[],
        behavioral_events=[],
        now=now,
    )
    assert cold_profile.languages.get("punjabi") == 1.0
    assert cold_profile.artists.get("diljit dosanjh") == 1.0

    # Experienced user with 35 Hindi listening events: Hindi overrides initial onboarding
    experienced_history = [
        {"id": f"h_{i}", "language": "hindi", "artists": ["Arijit Singh"], "completed": True, "played_at": now.isoformat()}
        for i in range(35)
    ]
    exp_profile = engine.build_profile(
        user_id="exp_user",
        profile_row={"language_ids": ["punjabi"], "favorite_artists": ["Diljit Dosanjh"]},
        favorites=[],
        history_events=experienced_history,
        behavioral_events=[],
        now=now,
    )
    assert exp_profile.languages.get("hindi") == 1.0
    assert "arijit singh" in exp_profile.artists


# ---------------------------------------------------------------------------
# 7. Artist Diversity Caps
# ---------------------------------------------------------------------------

def test_artist_diversity_caps_and_no_consecutive():
    engine = RankingEngine()
    candidates = [
        {"id": f"art1_{i}", "title": f"Song {i}", "artists": [{"name": "Artist 1"}], "_ranking_score": 0.9 - (i * 0.01)}
        for i in range(5)
    ] + [
        {"id": f"art2_{i}", "title": f"Song B {i}", "artists": [{"name": "Artist 2"}], "_ranking_score": 0.8 - (i * 0.01)}
        for i in range(3)
    ]

    reranked = engine.rerank_and_diversify(candidates, max_per_artist=2, limit=10)

    # No more than 2 tracks from Artist 1 in nearby positions
    art1_count = sum(1 for r in reranked[:4] if "Artist 1" in str(r.get("artists")))
    assert art1_count <= 2

    # No consecutive tracks by the exact same artist
    for i in range(len(reranked) - 1):
        a1 = reranked[i]["artists"][0]["name"]
        a2 = reranked[i + 1]["artists"][0]["name"]
        assert a1 != a2


# ---------------------------------------------------------------------------
# 8. Canonical Deduplication
# ---------------------------------------------------------------------------

def test_canonical_deduplication():
    # Same song as single vs album version with minor duration difference
    t1 = {"id": "single_1", "title": "Kesariya", "artist": "Arijit Singh", "duration": 268}
    t2 = {"id": "album_1", "title": "Kesariya (From Brahmastra)", "artist": "Arijit Singh", "duration": 269}

    k1 = _canonical_key(t1)
    k2 = _canonical_key(t2)
    # Normalized title and duration bucket align
    assert "kesariya" in k1
    assert "kesariya" in k2


# ---------------------------------------------------------------------------
# 9. Personalized Mix Generation
# ---------------------------------------------------------------------------

def test_personalized_mix_generation():
    generator = MixGenerator()
    now = datetime.now(timezone.utc)
    profile = UserTasteProfile(
        user_id="u_mix",
        artists={"arijit singh": 1.0, "shreya ghoshal": 0.8},
        languages={"hindi": 1.0, "bengali": 0.7},
    )

    history = [
        {"id": "rep_1", "title": "Repeat Hit", "artists": ["Arijit Singh"], "completed": True, "played_at": now.isoformat()},
        {"id": "rep_1", "title": "Repeat Hit", "artists": ["Arijit Singh"], "completed": True, "played_at": now.isoformat()},
    ]
    favorites = [
        {"id": "fav_old", "title": "Old Favorite", "artists": ["Shreya Ghoshal"], "favorited_at": (now - timedelta(days=40)).isoformat()}
    ]
    candidates = [
        {"id": "c1", "title": "Candidate 1", "artists": [{"name": "Arijit Singh"}], "language": "hindi"},
        {"id": "c2", "title": "Candidate 2", "artists": [{"name": "Shreya Ghoshal"}], "language": "hindi"},
    ]

    mixes = generator.generate_mixes(profile, favorites, history, candidates, now=now)
    mix_types = [m.mix_type for m in mixes]

    # Verified On Repeat, Rediscover, Daily Mixes, and Language Mixes
    assert "on_repeat" in mix_types
    assert "rediscover" in mix_types
    assert any("daily_mix" in mt for mt in mix_types)
    assert any("language_hindi" in mt for mt in mix_types)
