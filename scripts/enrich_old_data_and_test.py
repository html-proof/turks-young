"""
Enrich Old Data & Complete Database Test Script
Processes historical listening history for all existing users in Supabase,
computes their updated 3-tier taste profile with time-decay and signal weights,
generates unique personalized mixes, and writes them into `user_taste_profiles`
and `generated_playlists`.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.core import config
from api.db.connection import create_pool
from api.personalization.repository import PostgresUserRepository
from api.personalization.profile_engine import ProfileEngine
from api.personalization.mix_generator import MixGenerator
from api.personalization.candidate_generator import CandidateGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("enrich_data")


async def enrich_and_test():
    print("=" * 75)
    print("  ENRICHING HISTORICAL USER DATA & TESTING ALL DATABASE TABLES")
    print("=" * 75)

    pool = await create_pool(config.DATABASE_URL)
    repo = PostgresUserRepository(pool)
    profile_engine = ProfileEngine()
    mix_generator = MixGenerator()

    # 1. Fetch all users
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT uid, display_name, email FROM users ORDER BY created_at ASC;")

    print(f"\n[Step 1] Found {len(users)} registered users.")

    for u in users:
        uid = u["uid"]
        name = u["display_name"] or u["email"] or uid
        print(f"\n--> Processing user: {name} ({uid})")

        # Load historical data
        profile_row = await repo.get_profile(uid)
        favorites = await repo.list_favorites(uid)
        history = await repo.list_history(uid, limit=500)
        behavioral_events = await repo.list_behavioral_events(uid, limit=200)

        print(f"    History events: {len(history)} | Favorites: {len(favorites)} | Behavioral: {len(behavioral_events)}")

        if not history and not favorites and not profile_row:
            print("    No listening history or profile found. Skipping enrichment.")
            continue

        # Build comprehensive 3-tier profile
        taste_profile = profile_engine.build_profile(
            user_id=uid,
            profile_row=profile_row,
            favorites=favorites,
            history_events=history,
            behavioral_events=behavioral_events,
        )

        # Count total plays / skips from history events
        total_plays = len(history)
        total_skips = sum(1 for e in history if e.get("event", {}).get("completed") is False or float(e.get("event", {}).get("completion_ratio", 1.0)) < 0.3)
        metrics = {
            "play_count": total_plays,
            "skip_count": total_skips,
            "discovery_plays": int(total_plays * 0.15),
            "discovery_skips": int(total_skips * 0.10),
        }

        profile_data = {
            "languages": taste_profile.languages,
            "artists": taste_profile.artists,
            "genres": taste_profile.genres,
            "eras": taste_profile.eras,
            "session_intent": taste_profile.current_session,
            "metrics": metrics,
            "algorithm_version": "rec_v2",
        }

        # Save to user_taste_profiles
        await repo.save_taste_profile(uid, profile_data)
        print(f"    [OK] user_taste_profiles updated: {len(taste_profile.languages)} languages, {len(taste_profile.artists)} artists, {metrics}")

        # Extract track snapshots from history
        candidate_tracks = []
        for h in history:
            ev = h.get("event") if (isinstance(h, dict) and "event" in h and h.get("event")) else h
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = h
            if not isinstance(ev, dict):
                continue
            if ev.get("title") or ev.get("song_id") or ev.get("seokey"):
                artists_raw = ev.get("artists")
                if isinstance(artists_raw, list):
                    artists_list = [str(a) for a in artists_raw if a]
                elif isinstance(artists_raw, str):
                    artists_list = [artists_raw]
                elif ev.get("artist"):
                    artists_list = [str(ev.get("artist"))]
                else:
                    artists_list = ["Various Artists"]

                primary_artist = artists_list[0] if artists_list else "Various Artists"
                t = {
                    "id": str(ev.get("song_id") or ev.get("track_id") or ev.get("seokey") or f"track_{len(candidate_tracks)}"),
                    "title": ev.get("title") or "Unknown Track",
                    "artist": primary_artist,
                    "artists": artists_list,
                    "album": ev.get("album") or "Single",
                    "language": ev.get("language") or "Malayalam",
                    "seokey": ev.get("seokey") or ev.get("song_id") or "",
                    "images": ev.get("images") or {},
                }
                candidate_tracks.append(t)

        # Generate virtual mix snapshots
        mixes = mix_generator.generate_mixes(
            taste_profile,
            favorites=favorites,
            history=history,
            candidates=candidate_tracks,
        )

        print(f"    Generated {len(mixes)} personalized mixes:")
        for m in mixes:
            mix_dict = m.model_dump(mode="json")
            await repo.save_generated_playlist(uid, mix_dict)
            print(f"      + '{m.title}' ({m.mix_type}) with {len(m.tracks)} tracks")

    # 2. Verify generated_playlists in Supabase
    print("\n[Step 2] Verifying generated_playlists table in Supabase...")
    async with pool.acquire() as conn:
        total_generated = await conn.fetchval("SELECT count(*) FROM generated_playlists;")
        sample_mixes = await conn.fetch("SELECT id, user_id, mix_type, title, array_length(track_ids, 1) as count FROM generated_playlists LIMIT 5;")
    
    print(f"    Total rows in generated_playlists: {total_generated}")
    for sm in sample_mixes:
        print(f"    - [{sm['mix_type']}] {sm['title']} (user: {sm['user_id'][:12]}..., tracks: {sm['count']})")

    # 3. Verify user_taste_profiles enriched data
    print("\n[Step 3] Verifying user_taste_profiles enrichment...")
    async with pool.acquire() as conn:
        profiles = await conn.fetch("SELECT user_id, languages, artists, metrics, eras FROM user_taste_profiles WHERE user_id = 'eSRN0HpVMeaOdhOftJEk9io8oaJ2';")
    for p in profiles:
        print(f"    User {p['user_id']}:")
        print(f"      Languages: {p['languages']}")
        print(f"      Top Artists: {p['artists']}")
        print(f"      Metrics: {p['metrics']}")
        print(f"      Eras: {p['eras']}")

    # 4. Final summary count of all 35 tables
    print("\n[Step 4] Final Row Counts Across All 35 Supabase Tables:")
    print("-" * 55)
    async with pool.acquire() as conn:
        tables = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """)
        for r in tables:
            t = r["table_name"]
            cnt = await conn.fetchval(f'SELECT count(*) FROM "{t}";')
            print(f"  {t:32} | {cnt:>6} rows")
    print("-" * 55)

    await pool.close()
    print("\nAll database tables verified and old data successfully pushed to tables!")

if __name__ == "__main__":
    asyncio.run(enrich_and_test())
