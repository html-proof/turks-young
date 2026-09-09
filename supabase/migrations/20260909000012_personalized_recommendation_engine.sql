-- Migration: Personalized recommendation engine enhancements
-- Adds schema support for 3-tier taste profiles, behavioral events, generated mixes, and impression fatigue.

-- 1. Extend user_taste_profiles
ALTER TABLE user_taste_profiles
  ADD COLUMN IF NOT EXISTS eras JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS session_intent JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS metrics JSONB NOT NULL DEFAULT '{"skip_count": 0, "play_count": 0, "discovery_skips": 0, "discovery_plays": 0}'::jsonb;

-- 2. Enhanced indexes for user_events
CREATE INDEX IF NOT EXISTS idx_user_events_user_type_created
  ON user_events (user_id, event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_events_user_song
  ON user_events (user_id, song_id);

CREATE INDEX IF NOT EXISTS idx_user_events_user_artist
  ON user_events (user_id, artist_id);

-- 3. Enhanced index for recommendation_impressions
CREATE INDEX IF NOT EXISTS idx_rec_impressions_user_content_shown
  ON recommendation_impressions (user_id, content_id, shown_at DESC);

-- 4. Generated playlists table for virtual mix snapshots
CREATE TABLE IF NOT EXISTS generated_playlists (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  mix_type TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  track_ids TEXT[] NOT NULL DEFAULT '{}',
  tracks JSONB NOT NULL DEFAULT '[]'::jsonb,
  cover_url TEXT,
  algorithm_version TEXT NOT NULL DEFAULT 'rec_v2',
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_generated_playlists_user_type
  ON generated_playlists (user_id, mix_type, generated_at DESC);
