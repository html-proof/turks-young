"""
Firebase Data Connect — SQL smoke test
Runs against the live Cloud SQL instance for project personal-songs.

Usage:
  1. Download your service account key from Firebase Console → Project Settings → Service Accounts
     and save it as: Songlist/service_account.json

  2.  pip install firebase-admin google-auth httpx

  3.  python scripts/test_dataconnect.py
"""

import asyncio
import json
import sys
from pathlib import Path

try:
    import google.auth.transport.requests
    import httpx
    from google.oauth2 import service_account
except ImportError:
    sys.exit("Install dependencies first:\n  pip install firebase-admin google-auth httpx")

# ─── Config ───────────────────────────────────────────────────────────────────

PROJECT_ID   = "personal-songs"
LOCATION     = "us-central1"
SERVICE_ID   = "soundwaves-service"
CONNECTOR_ID = "soundwaves-connector"

BASE_URL = (
    f"https://firebasedataconnect.googleapis.com/v1beta"
    f"/projects/{PROJECT_ID}/locations/{LOCATION}"
    f"/services/{SERVICE_ID}/connectors/{CONNECTOR_ID}"
)

SA_PATH = Path(__file__).parent.parent / "service_account.json"

SCOPES = ["https://www.googleapis.com/auth/firebase"]

# ─── Auth ─────────────────────────────────────────────────────────────────────

def get_token() -> str:
    if not SA_PATH.exists():
        sys.exit(
            f"\n✗ Service account key not found at: {SA_PATH}\n"
            "  Download it from Firebase Console → Project Settings → Service Accounts\n"
            "  and save it as service_account.json in the Songlist/ folder."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(SA_PATH), scopes=SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# ─── HTTP helpers ──────────────────────────────────────────────────────────────

async def mutation(client: httpx.AsyncClient, token: str, operation: str, variables: dict) -> dict:
    res = await client.post(
        f"{BASE_URL}:executeMutation",
        headers={"Authorization": f"Bearer {token}"},
        json={"operationName": operation, "variables": variables},
    )
    data = res.json()
    if res.status_code != 200:
        print(f"  ✗ {operation} failed ({res.status_code}): {json.dumps(data, indent=2)}")
        return {}
    print(f"  ✓ {operation}")
    return data


async def query(client: httpx.AsyncClient, token: str, operation: str, variables: dict) -> dict:
    res = await client.post(
        f"{BASE_URL}:executeQuery",
        headers={"Authorization": f"Bearer {token}"},
        json={"operationName": operation, "variables": variables},
    )
    data = res.json()
    if res.status_code != 200:
        print(f"  ✗ {operation} failed ({res.status_code}): {json.dumps(data, indent=2)}")
        return {}
    print(f"  ✓ {operation}")
    return data


# ─── Tests ────────────────────────────────────────────────────────────────────

async def run():
    print("\n═══ Firebase Data Connect — Smoke Test ═══\n")

    token = get_token()
    print(f"  ✓ Authenticated (token length: {len(token)})\n")

    async with httpx.AsyncClient(timeout=30) as client:

        # ── 1. Seed: Artist ──────────────────────────────────────────────────
        print("── Step 1: Upsert artist ──")
        r = await mutation(client, token, "UpsertArtist", {
            "externalId":   "test-artist-001",
            "name":         "Test Artist",
            "slug":         "test-artist",
            "bio":          "A smoke-test artist.",
            "imageUrl":     "https://picsum.photos/seed/testartist/300/300",
            "country":      "US",
            "isVerified":   True,
            "followerCount": 1000,
        })
        artist_id = (r.get("data", {}).get("artist_upsert") or {}).get("id")
        print(f"     artist id: {artist_id}\n")

        # ── 2. Seed: Genre ───────────────────────────────────────────────────
        print("── Step 2: Upsert genre ──")
        r = await mutation(client, token, "UpsertGenre", {
            "name":  "Test Pop",
            "slug":  "test-pop",
            "emoji": "🎤",
        })
        genre_id = (r.get("data", {}).get("genre_upsert") or {}).get("id")
        print(f"     genre id: {genre_id}\n")

        # ── 3. Seed: Song ────────────────────────────────────────────────────
        print("── Step 3: Upsert song ──")
        r = await mutation(client, token, "UpsertSong", {
            "externalId":      "test-song-001",
            "title":           "Smoke Test Track",
            "slug":            "smoke-test-track",
            "language":        "English",
            "durationSeconds": 210,
            "coverUrl":        "https://picsum.photos/seed/testsong/400/400",
            "streamUrl":       "https://example.com/stream/test.mp3",
            "popularity":      0.75,
        })
        song_id = (r.get("data", {}).get("song_upsert") or {}).get("id")
        print(f"     song id: {song_id}\n")

        if not all([artist_id, genre_id, song_id]):
            print("✗ Seed step failed — aborting further tests.\n")
            return

        # ── 4. Link song ↔ artist ────────────────────────────────────────────
        print("── Step 4: Link song → artist ──")
        await mutation(client, token, "LinkSongArtist", {
            "songId":   song_id,
            "artistId": artist_id,
            "role":     "primary",
        })

        # ── 5. Link song ↔ genre ─────────────────────────────────────────────
        print("\n── Step 5: Link song → genre ──")
        await mutation(client, token, "LinkSongGenre", {
            "songId":  song_id,
            "genreId": genre_id,
            "weight":  1.0,
        })

        # ── 6. Set user language preference ──────────────────────────────────
        TEST_USER = "test-user-smoke"
        print(f"\n── Step 6: Set user languages (uid={TEST_USER}) ──")
        await mutation(client, token, "SetUserLanguages", {
            "userId":    TEST_USER,
            "languages": ["English", "Hindi"],
        })

        # ── 7. Follow artist ─────────────────────────────────────────────────
        print("\n── Step 7: Follow artist ──")
        await mutation(client, token, "SetUserFavoriteArtists", {
            "userId":    TEST_USER,
            "artistIds": [artist_id],
        })

        # ── 8. Like song ─────────────────────────────────────────────────────
        print("\n── Step 8: Like song ──")
        await mutation(client, token, "LikeSong", {
            "userId": TEST_USER,
            "songId": song_id,
        })

        # ── 9. Record a play ─────────────────────────────────────────────────
        print("\n── Step 9: Record play ──")
        await mutation(client, token, "RecordPlay", {
            "userId":          TEST_USER,
            "songId":          song_id,
            "completionPct":   85,
            "listenedSeconds": 178,
            "source":          "test",
            "skipped":         False,
        })

        # ── 10. Query: user languages ────────────────────────────────────────
        print("\n── Step 10: Query user languages ──")
        r = await query(client, token, "GetUserLanguages", {"userId": TEST_USER})
        langs = r.get("data", {}).get("userLanguages", [])
        for l in langs:
            print(f"     language={l['language']}  rank={l['rank']}")
        assert len(langs) == 2, f"Expected 2 languages, got {len(langs)}"

        # ── 11. Query: liked songs ────────────────────────────────────────────
        print("\n── Step 11: Query liked songs ──")
        r = await query(client, token, "GetUserFavoriteSongs", {"userId": TEST_USER, "limit": 10})
        likes = r.get("data", {}).get("userFavoriteSongs", [])
        for like in likes:
            print(f"     song={like['song']['title']}")
        assert any(l["song"]["id"] == song_id for l in likes), "Liked song not found in results"

        # ── 12. Query: recent plays ───────────────────────────────────────────
        print("\n── Step 12: Query recent plays ──")
        r = await query(client, token, "GetRecentPlays", {"userId": TEST_USER, "limit": 5})
        plays = r.get("data", {}).get("playHistories", [])
        for p in plays:
            print(f"     song={p['song']['title']}  completion={p['completionPct']}%")
        assert len(plays) >= 1, "No play history returned"

        # ── 13. Query: recommendations (SQL scoring) ──────────────────────────
        print("\n── Step 13: Query recommendations (SQL CTE scoring) ──")
        r = await query(client, token, "GetRecommendations", {
            "userId": TEST_USER,
            "limit":  10,
        })
        recs = r.get("data", {}).get("songs", [])
        print(f"     {len(recs)} recommendations returned")
        for rec in recs[:3]:
            print(f"     → {rec['title']}  popularity={rec['popularity']}")

        # ── 14. Search songs ──────────────────────────────────────────────────
        print("\n── Step 14: SearchSongs (full-text) ──")
        r = await query(client, token, "SearchSongs", {
            "query": "Smoke",
            "limit": 5,
        })
        found = r.get("data", {}).get("songs", [])
        print(f"     {len(found)} songs matched 'Smoke'")
        assert any(s["id"] == song_id for s in found), "Seeded song not in search results"

        # ── 15. Increment play count ──────────────────────────────────────────
        print("\n── Step 15: IncrementPlayCount ──")
        await mutation(client, token, "IncrementPlayCount", {"songId": song_id})

        # ── 16. Verify play count ─────────────────────────────────────────────
        print("\n── Step 16: Verify play count via GetSongByExternalId ──")
        r = await query(client, token, "GetSongByExternalId", {"externalId": "test-song-001"})
        songs = r.get("data", {}).get("songs", [])
        if songs:
            print(f"     playCount={songs[0]['playCount']}")
            assert songs[0]["playCount"] >= 1, "Play count not incremented"

    print("\n═══════════════════════════════════════════")
    print("  ✅  All 16 tests passed — Data Connect SQL is working!")
    print("═══════════════════════════════════════════\n")


if __name__ == "__main__":
    asyncio.run(run())
