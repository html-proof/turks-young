-- Normalized recommendation data. Legacy JSON snapshot tables remain for compatibility.
CREATE TABLE IF NOT EXISTS languages (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, native_name TEXT NOT NULL,
  image_url TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS artists (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, image_url TEXT, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_languages (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  language_id TEXT NOT NULL REFERENCES languages(id) ON DELETE CASCADE, weight DOUBLE PRECISION NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, language_id)
);
CREATE TABLE IF NOT EXISTS user_selected_artists (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  artist_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'onboarding', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, artist_id)
);
CREATE TABLE IF NOT EXISTS user_liked_songs (
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE, song_id TEXT NOT NULL,
  song JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(user_id, song_id)
);
CREATE TABLE IF NOT EXISTS playback_history (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  song_id TEXT NOT NULL, artist_id TEXT, album_id TEXT, started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ, duration_ms INTEGER NOT NULL DEFAULT 0, position_ms INTEGER NOT NULL DEFAULT 0,
  completion_percentage DOUBLE PRECISION NOT NULL DEFAULT 0, source TEXT, context_id TEXT, device_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS playback_history_user_started ON playback_history(user_id, started_at DESC);
CREATE TABLE IF NOT EXISTS user_events (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  event_type TEXT NOT NULL, song_id TEXT, artist_id TEXT, album_id TEXT, playlist_id TEXT,
  position_ms INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
  source TEXT, context_id TEXT, query TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_events_user_created ON user_events(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_taste_profiles (
  user_id TEXT PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
  languages JSONB NOT NULL DEFAULT '{}'::jsonb, artists JSONB NOT NULL DEFAULT '{}'::jsonb,
  genres JSONB NOT NULL DEFAULT '{}'::jsonb, algorithm_version TEXT NOT NULL DEFAULT 'rec_v1',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS recommendation_impressions (
  id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  content_type TEXT NOT NULL, content_id TEXT NOT NULL, section_id TEXT NOT NULL,
  rank_position INTEGER NOT NULL, recommendation_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  reason TEXT, algorithm_version TEXT NOT NULL DEFAULT 'rec_v1', shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  clicked_at TIMESTAMPTZ, played_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS recommendation_impressions_user_shown ON recommendation_impressions(user_id, shown_at DESC);
