-- Supporting indexes for growing personalization and typed catalogue queries.
CREATE INDEX IF NOT EXISTS idx_user_events_song_created
  ON user_events(user_id, song_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_artist_created
  ON user_events(user_id, artist_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_album_created
  ON user_events(user_id, album_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_events_playlist_created
  ON user_events(user_id, playlist_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendation_impressions_type
  ON recommendation_impressions(user_id, content_type, shown_at DESC);
ALTER TABLE player_sessions ADD COLUMN IF NOT EXISTS repeat_mode TEXT NOT NULL DEFAULT 'off';
ALTER TABLE player_sessions ADD COLUMN IF NOT EXISTS shuffle_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE player_sessions ADD COLUMN IF NOT EXISTS duration_ms BIGINT NOT NULL DEFAULT 0;
