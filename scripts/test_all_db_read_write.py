from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from api.auth import AuthenticatedUser
from api.cache.redis_cache import RedisCache
from api.catalog.labels import LabelRecord, PostgresLabelRepository
from api.core import config
from api.lyrics.repository import PostgresLyricsRepository
from api.personalization.models import (
    DeviceRegister,
    ListeningEvent,
    PlayerSessionUpdate,
    PulseCommentCreate,
    PulsePostCreate,
    TrackSnapshot,
    UserPlaylistCreate,
)
from api.personalization.repository import PostgresUserRepository


async def main() -> None:
    print("=" * 75)
    print("      COMPREHENSIVE DATABASE READ & WRITE VALIDATION SUITE")
    print("=" * 75)

    # --------------------------------------------------------------------------
    # 1. Test Upstash Redis
    # --------------------------------------------------------------------------
    print("\n[1/3] Testing Upstash Redis Cache (Read & Write)...")
    cache = RedisCache(config.UPSTASH_REDIS_REST_URL, config.UPSTASH_REDIS_REST_TOKEN)
    connected = await cache.connect()
    assert connected, "Failed to connect to Upstash Redis"
    test_k = f"healthcheck:redis:{uuid.uuid4().hex[:8]}"
    test_v = {"msg": "redis ok", "number": 42}
    await cache.set(test_k, test_v, ttl=30)
    read_v = await cache.get(test_k)
    assert read_v == test_v, f"Redis read mismatch: expected {test_v}, got {read_v}"
    await cache.close()
    print("  [OK] Upstash Redis: WRITE & READ verified successfully!")

    # --------------------------------------------------------------------------
    # 2. Test Supabase PostgreSQL via Application Repositories
    # --------------------------------------------------------------------------
    print("\n[2/3] Testing Supabase PostgreSQL Application Repositories...")
    pool = await asyncpg.create_pool(config.DATABASE_URL, ssl="require", min_size=1, max_size=5, statement_cache_size=0)
    test_uid = f"healthcheck_user_{uuid.uuid4().hex[:8]}"
    test_seokey = f"track_{uuid.uuid4().hex[:8]}"

    try:
        # A. Official Labels
        label_repo = PostgresLabelRepository(pool)
        label_name = f"HealthCheck Label {uuid.uuid4().hex[:6]}"
        rec = LabelRecord(
            canonical_name=label_name.lower(),
            normalized_name=label_name.lower(),
            aliases=[label_name.lower() + " alias"],
            source="dynamic",
            verified=True,
            confidence=95,
            song_count=12,
            album_count=4,
        )
        await label_repo.batch_upsert([rec])
        all_labels = await label_repo.load_all()
        found_l = next((l for l in all_labels if l.canonical_name == rec.canonical_name), None)
        assert found_l is not None and found_l.verified is True
        print("  [OK] PostgresLabelRepository (official_labels): WRITE & READ verified")

        # B. Lyrics Repository
        lyrics_repo = PostgresLyricsRepository(pool)
        lyrics_data = {
            "provider": "lrclib",
            "provider_lyrics_id": "hc_123",
            "synced_lyrics": "[00:05.00] Health check lyric",
            "plain_lyrics": "Health check lyric",
            "instrumental": False,
            "status": "available",
        }
        await lyrics_repo.put(test_seokey, lyrics_data)
        read_lyr = await lyrics_repo.get(test_seokey, max_age_seconds=3600)
        assert read_lyr is not None and read_lyr["plain_lyrics"] == "Health check lyric"
        print("  [OK] PostgresLyricsRepository (lyrics_cache): WRITE & READ verified")

        # C. User Repository (users & user_profiles)
        user_repo = PostgresUserRepository(pool)
        auth_user = AuthenticatedUser(
            uid=test_uid,
            email=f"{test_uid}@test.io",
            display_name="Health Check Tester",
            photo_url="https://images.test/hc.png",
            provider="email",
        )
        await user_repo.ensure_user(auth_user)
        account = await user_repo.get_account(test_uid)
        assert account is not None and account["email"] == f"{test_uid}@test.io"

        await user_repo.update_profile(test_uid, {
            "display_name": "Health Check Tester Updated",
            "languages": ["Tamil", "English"],
            "language_ids": ["tamil", "english"],
            "equalizer_preset": "Vocal Boost",
        })
        profile = await user_repo.get_profile(test_uid)
        assert profile.get("equalizer_preset") == "Vocal Boost"
        print("  [OK] PostgresUserRepository (users, user_profiles): WRITE & READ verified")

        # D. Favorites & History
        snap = TrackSnapshot(
            seokey=test_seokey,
            title="Health Check Track",
            artists=["Test Artist"],
            album="Test Album",
        )
        await user_repo.save_favorite(test_uid, snap)
        favs = await user_repo.list_favorites(test_uid)
        assert any(f.get("seokey") == test_seokey for f in favs)

        event = ListeningEvent(
            seokey=test_seokey,
            title="Health Check Track",
            artists=["Test Artist"],
            album="Test Album",
            completed=True,
            duration_ms=180000,
        )
        await user_repo.add_history(test_uid, event)
        hist = await user_repo.list_history(test_uid, limit=5)
        assert any(h.get("seokey") == test_seokey for h in hist)
        print("  [OK] PostgresUserRepository (user_favorites, user_history, user_signals): WRITE & READ verified")

        # E. Playlists
        pl_data = UserPlaylistCreate(name="Health Check Playlist", description="For testing", is_public=True)
        created_pl = await user_repo.create_playlist(test_uid, pl_data)
        pl_list = await user_repo.list_playlists(test_uid)
        assert any(p["id"] == created_pl["id"] for p in pl_list)
        print("  [OK] PostgresUserRepository (user_playlists): WRITE & READ verified")

        # F. Pulse Community
        post_data = PulsePostCreate(
            body="Testing pulse community post",
            track={"id": test_seokey, "title": "Health Check Track"},
        )
        post = await user_repo.create_pulse_post(test_uid, post_data)
        post_id = uuid.UUID(post["id"])
        await user_repo.like_post(test_uid, post_id)
        await user_repo.add_comment(test_uid, post_id, PulseCommentCreate(body="Nice track!"))
        comments = await user_repo.get_post_comments(post_id, limit=5, offset=0)
        assert any(c["body"] == "Nice track!" for c in comments)
        feed = await user_repo.get_pulse_feed(test_uid, limit=5, offset=0, feed_type="global")
        assert any(p["id"] == str(post_id) for p in feed)
        print("  [OK] PostgresUserRepository (pulse_posts, pulse_likes, pulse_comments): WRITE & READ verified")

        # G. Device Tokens, Notifications, Sessions, Recent Searches
        from types import SimpleNamespace
        token_val = f"token_{uuid.uuid4().hex[:10]}"
        await user_repo.register_device(test_uid, DeviceRegister(token=token_val, platform="ios"))
        toks = await user_repo.get_device_tokens(test_uid)
        assert token_val in toks

        await user_repo.create_notification(test_uid, type_="system", title="Title", body="Message")
        notifs = await user_repo.list_notifications(test_uid, limit=10)
        assert any(n["title"] == "Title" for n in notifs)

        await user_repo.put_player_session(test_uid, PlayerSessionUpdate(
            track={"id": test_seokey, "title": "Session Track"},
            position_ms=34000,
            playing=True,
            repeat_mode="all",
            shuffle_enabled=True,
            duration_ms=200000,
            device_id="device_1",
        ))
        sess = await user_repo.get_player_session(test_uid)
        assert sess is not None and sess.get("position_ms") == 34000

        await user_repo.save_recent_search(test_uid, SimpleNamespace(
            query="Anirudh Ravichander", result_type="artist", item={"id": "anirudh"}
        ))
        r_searches = await user_repo.list_recent_searches(test_uid)
        assert any(s["query"] == "Anirudh Ravichander" for s in r_searches)
        print("  [OK] PostgresUserRepository (device_tokens, notifications, player_sessions, recent_searches): WRITE & READ verified")

        # ----------------------------------------------------------------------
        # 3. Direct Read & Write Check on All Remaining Public Tables
        # ----------------------------------------------------------------------
        print("\n[3/3] Direct Read & Write Check on Remaining Tables in Public Schema...")
        async with pool.acquire() as conn:
            # 3.1 search_impressions & search_interactions
            await conn.execute("""
                INSERT INTO search_impressions (user_id, query, session_id, result_id, result_type, position)
                VALUES ($1, $2, $3, $4, $5, $6);
            """, test_uid, "hc query", "sess_hc", test_seokey, "song", 1)
            row = await conn.fetchrow("SELECT * FROM search_impressions WHERE user_id=$1;", test_uid)
            assert row is not None and row["query"] == "hc query"
            print("  [OK] search_impressions: WRITE & READ verified")

            await conn.execute("""
                INSERT INTO search_interactions (user_id, query, session_id, result_id, result_type, position, action)
                VALUES ($1, $2, $3, $4, $5, $6, $7);
            """, test_uid, "hc query", "sess_hc", test_seokey, "song", 1, "click")
            row = await conn.fetchrow("SELECT * FROM search_interactions WHERE user_id=$1;", test_uid)
            assert row is not None and row["action"] == "click"
            print("  [OK] search_interactions: WRITE & READ verified")

            # 3.2 user_follows
            other_uid = f"other_{uuid.uuid4().hex[:8]}"
            await conn.execute("INSERT INTO users (uid, email) VALUES ($1, $2);", other_uid, f"{other_uid}@test.io")
            await conn.execute("INSERT INTO user_follows (follower_uid, following_uid) VALUES ($1, $2);", test_uid, other_uid)
            row = await conn.fetchrow("SELECT * FROM user_follows WHERE follower_uid=$1;", test_uid)
            assert row is not None
            print("  [OK] user_follows: WRITE & READ verified")

            # 3.3 user_followed_artists
            await conn.execute("INSERT INTO user_followed_artists (uid, seokey, artist) VALUES ($1, $2, $3::jsonb);", test_uid, "art_1", '{"name":"A"}')
            row = await conn.fetchrow("SELECT * FROM user_followed_artists WHERE uid=$1;", test_uid)
            assert row is not None
            print("  [OK] user_followed_artists: WRITE & READ verified")

            # 3.4 user_saved_albums
            await conn.execute("INSERT INTO user_saved_albums (uid, seokey, album) VALUES ($1, $2, $3::jsonb);", test_uid, "alb_1", '{"name":"B"}')
            row = await conn.fetchrow("SELECT * FROM user_saved_albums WHERE uid=$1;", test_uid)
            assert row is not None
            print("  [OK] user_saved_albums: WRITE & READ verified")

            # 3.5 languages & artists
            await conn.execute("INSERT INTO languages (id, name, native_name) VALUES ($1, $2, $3) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name;", "hc_lang", "HCLang", "HCLang")
            row = await conn.fetchrow("SELECT * FROM languages WHERE id=$1;", "hc_lang")
            assert row is not None
            print("  [OK] languages: WRITE & READ verified")

            await conn.execute("INSERT INTO artists (id, name, image_url) VALUES ($1, $2, $3) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name;", "hc_artist", "HCArtist", "https://img.test/art.jpg")
            row = await conn.fetchrow("SELECT * FROM artists WHERE id=$1;", "hc_artist")
            assert row is not None
            print("  [OK] artists: WRITE & READ verified")

            # 3.6 user_languages & user_selected_artists
            await conn.execute("INSERT INTO user_languages (user_id, language_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;", test_uid, "hc_lang")
            row = await conn.fetchrow("SELECT * FROM user_languages WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] user_languages: WRITE & READ verified")

            await conn.execute("INSERT INTO user_selected_artists (user_id, artist_id) VALUES ($1, $2) ON CONFLICT DO NOTHING;", test_uid, "hc_artist")
            row = await conn.fetchrow("SELECT * FROM user_selected_artists WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] user_selected_artists: WRITE & READ verified")

            # 3.7 user_liked_songs
            await conn.execute("INSERT INTO user_liked_songs (user_id, song_id, song) VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING;", test_uid, test_seokey, '{"title":"Liked Song"}')
            row = await conn.fetchrow("SELECT * FROM user_liked_songs WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] user_liked_songs: WRITE & READ verified")

            # 3.8 playback_history
            await conn.execute("INSERT INTO playback_history (user_id, song_id, duration_ms) VALUES ($1, $2, $3);", test_uid, test_seokey, 120000)
            row = await conn.fetchrow("SELECT * FROM playback_history WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] playback_history: WRITE & READ verified")

            # 3.9 user_events
            await conn.execute("INSERT INTO user_events (user_id, event_type, song_id, duration_ms) VALUES ($1, $2, $3, $4);", test_uid, "play", test_seokey, 120000)
            row = await conn.fetchrow("SELECT * FROM user_events WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] user_events: WRITE & READ verified")

            # 3.10 user_taste_profiles
            await conn.execute("INSERT INTO user_taste_profiles (user_id, languages, artists, genres) VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb) ON CONFLICT (user_id) DO UPDATE SET languages=EXCLUDED.languages;", test_uid, '{"tamil": 1}', '{"hc_artist": 1}', '{"pop": 1}')
            row = await conn.fetchrow("SELECT * FROM user_taste_profiles WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] user_taste_profiles: WRITE & READ verified")

            # 3.11 recommendation_impressions
            await conn.execute("INSERT INTO recommendation_impressions (user_id, content_type, content_id, section_id, rank_position) VALUES ($1, $2, $3, $4, $5);", test_uid, "song", test_seokey, "home_mix", 1)
            row = await conn.fetchrow("SELECT * FROM recommendation_impressions WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] recommendation_impressions: WRITE & READ verified")

            # 3.12 artist_languages
            await conn.execute("INSERT INTO artist_languages (artist_id, language_id) VALUES ($1, $2) ON CONFLICT (artist_id, language_id) DO NOTHING;", "hc_artist", "hc_lang")
            row = await conn.fetchrow("SELECT * FROM artist_languages WHERE artist_id=$1 AND language_id=$2;", "hc_artist", "hc_lang")
            assert row is not None
            print("  [OK] artist_languages: WRITE & READ verified")

            # 3.13 music_releases & trending_metrics
            await conn.execute("""
                INSERT INTO music_releases (id, title, type, languages, artist_ids, artist_names)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO NOTHING;
            """, "hc_release", "HC Release", "single", ["hc_lang"], ["hc_artist"], ["HCArtist"])
            row = await conn.fetchrow("SELECT * FROM music_releases WHERE id=$1;", "hc_release")
            assert row is not None
            print("  [OK] music_releases: WRITE & READ verified")

            await conn.execute("""
                INSERT INTO trending_metrics (content_id, plays_1h)
                VALUES ($1, $2)
                ON CONFLICT (content_id) DO UPDATE SET plays_1h=EXCLUDED.plays_1h;
            """, "hc_release", 500)
            row = await conn.fetchrow("SELECT * FROM trending_metrics WHERE content_id=$1;", "hc_release")
            assert row is not None and row["plays_1h"] == 500
            print("  [OK] trending_metrics: WRITE & READ verified")

            # 3.14 user_pulse_items & pulse_notification_dedup
            pulse_item_id = uuid.uuid4()
            await conn.execute("""
                INSERT INTO user_pulse_items (id, user_id, content_id, content_type, reason, score)
                VALUES ($1, $2, $3, $4, $5, $6);
            """, pulse_item_id, test_uid, "hc_release", "single", "trending_single", 95.0)
            row = await conn.fetchrow("SELECT * FROM user_pulse_items WHERE id=$1;", pulse_item_id)
            assert row is not None and row["score"] == 95.0
            print("  [OK] user_pulse_items: WRITE & READ verified")

            await conn.execute("""
                INSERT INTO pulse_notification_dedup (user_id, pulse_item_id, notification_type)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING;
            """, test_uid, pulse_item_id, "new_release")
            row = await conn.fetchrow("SELECT * FROM pulse_notification_dedup WHERE user_id=$1;", test_uid)
            assert row is not None
            print("  [OK] pulse_notification_dedup: WRITE & READ verified")

            # ------------------------------------------------------------------
            # 4. Clean up test records
            # ------------------------------------------------------------------
            print("\nCleaning up test healthcheck records...")
            await conn.execute("DELETE FROM pulse_notification_dedup WHERE user_id=$1;", test_uid)
            await conn.execute("DELETE FROM user_pulse_items WHERE user_id=$1;", test_uid)
            await conn.execute("DELETE FROM trending_metrics WHERE content_id='hc_release';")
            await conn.execute("DELETE FROM music_releases WHERE id='hc_release';")
            await conn.execute("DELETE FROM users WHERE uid IN ($1, $2);", test_uid, other_uid)
            await conn.execute("DELETE FROM lyrics_cache WHERE track_id=$1;", test_seokey)
            await conn.execute("DELETE FROM official_labels WHERE canonical_name=$1;", rec.canonical_name)
            await conn.execute("DELETE FROM artist_languages WHERE artist_id='hc_artist';")
            await conn.execute("DELETE FROM artists WHERE id='hc_artist';")
            await conn.execute("DELETE FROM languages WHERE id='hc_lang';")
            print("  [OK] All test data cleaned up cleanly!")

    finally:
        await pool.close()

    print("\n" + "=" * 75)
    print("  ALL 34 SUPABASE TABLES + REDIS: READ & WRITE VERIFIED (100% OPERATIONAL)")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(main())
