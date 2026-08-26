-- Backend-owned Pulse catalogue, ranking snapshots, read state and notification deduplication.
CREATE TABLE IF NOT EXISTS music_releases (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('song','album','single','ep')),
  title TEXT NOT NULL,
  artist_ids TEXT[] NOT NULL DEFAULT '{}',
  artist_names TEXT[] NOT NULL DEFAULT '{}',
  album_id TEXT,
  album_name TEXT,
  languages TEXT[] NOT NULL DEFAULT '{}',
  image_url TEXT,
  stream_url TEXT,
  release_date TIMESTAMPTZ,
  popularity DOUBLE PRECISION NOT NULL DEFAULT 0,
  trending_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_music_releases_release_date ON music_releases(release_date DESC);
CREATE INDEX IF NOT EXISTS idx_music_releases_trending ON music_releases(trending_score DESC);
CREATE INDEX IF NOT EXISTS idx_music_releases_languages ON music_releases USING GIN(languages);
CREATE INDEX IF NOT EXISTS idx_music_releases_artists ON music_releases USING GIN(artist_ids);

CREATE TABLE IF NOT EXISTS trending_metrics (
  content_id TEXT PRIMARY KEY REFERENCES music_releases(id) ON DELETE CASCADE,
  plays_1h BIGINT NOT NULL DEFAULT 0, plays_6h BIGINT NOT NULL DEFAULT 0,
  plays_24h BIGINT NOT NULL DEFAULT 0, likes_24h BIGINT NOT NULL DEFAULT 0,
  saves_24h BIGINT NOT NULL DEFAULT 0, playlist_adds_24h BIGINT NOT NULL DEFAULT 0,
  shares_24h BIGINT NOT NULL DEFAULT 0, searches_24h BIGINT NOT NULL DEFAULT 0,
  unique_listeners_24h BIGINT NOT NULL DEFAULT 0,
  calculated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_pulse_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  content_id TEXT NOT NULL REFERENCES music_releases(id) ON DELETE CASCADE,
  content_type TEXT NOT NULL, reason TEXT NOT NULL, score DOUBLE PRECISION NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','seen','opened','read')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ,
  UNIQUE(user_id, content_id, content_type)
);
CREATE INDEX IF NOT EXISTS idx_user_pulse_items_page ON user_pulse_items(user_id, score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_pulse_items_unread ON user_pulse_items(user_id, state) WHERE state <> 'read';

CREATE TABLE IF NOT EXISTS pulse_notification_dedup (
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  pulse_item_id UUID NOT NULL REFERENCES user_pulse_items(id) ON DELETE CASCADE,
  notification_type TEXT NOT NULL, sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  opened_at TIMESTAMPTZ, seen_at TIMESTAMPTZ,
  PRIMARY KEY(user_id, pulse_item_id, notification_type)
);

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS pulse_followed_releases_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS pulse_selected_releases_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS pulse_trending_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS pulse_recommendations_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Incremental, short-term momentum. Lifetime play counts are intentionally not used.
CREATE OR REPLACE FUNCTION refresh_pulse_trending_metrics() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO trending_metrics (content_id, plays_1h, plays_6h, plays_24h, unique_listeners_24h, calculated_at)
  SELECT song_id,
    COUNT(*) FILTER (WHERE created_at >= now() - interval '1 hour'),
    COUNT(*) FILTER (WHERE created_at >= now() - interval '6 hours'),
    COUNT(*), COUNT(DISTINCT user_id), now()
  FROM user_events
  WHERE song_id IS NOT NULL AND created_at >= now() - interval '24 hours'
  GROUP BY song_id
  ON CONFLICT (content_id) DO UPDATE SET
    plays_1h=EXCLUDED.plays_1h, plays_6h=EXCLUDED.plays_6h,
    plays_24h=EXCLUDED.plays_24h, unique_listeners_24h=EXCLUDED.unique_listeners_24h,
    calculated_at=now();
  UPDATE music_releases r SET trending_score = LEAST(100, COALESCE(t.plays_1h,0)*4 + COALESCE(t.plays_6h,0)*1.5 + COALESCE(t.unique_listeners_24h,0)*2), updated_at=now()
  FROM trending_metrics t WHERE t.content_id=r.id;
END $$;
