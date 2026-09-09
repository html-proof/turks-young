-- Training and evaluation labels for catalog-search relevance.
-- Rows are written only for results the client actually rendered, avoiding
-- false impressions from prefetched or abandoned requests.
CREATE TABLE IF NOT EXISTS search_impressions (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  query TEXT NOT NULL,
  session_id TEXT NOT NULL,
  result_id TEXT NOT NULL,
  result_type TEXT NOT NULL CHECK (result_type IN ('song', 'artist', 'album', 'playlist')),
  position INTEGER NOT NULL CHECK (position >= 0 AND position <= 100),
  algorithm_version TEXT NOT NULL DEFAULT 'search_v1',
  shown_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_impressions_training
  ON search_impressions (query, result_id, shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_impressions_user_session
  ON search_impressions (user_id, session_id, shown_at DESC);

CREATE TABLE IF NOT EXISTS search_interactions (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  query TEXT NOT NULL,
  session_id TEXT NOT NULL,
  result_id TEXT NOT NULL,
  result_type TEXT NOT NULL CHECK (result_type IN ('song', 'artist', 'album', 'playlist')),
  position INTEGER NOT NULL CHECK (position >= 0 AND position <= 100),
  action TEXT NOT NULL CHECK (action IN ('click', 'play', 'complete', 'skip', 'like', 'save')),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_search_interactions_training
  ON search_interactions (query, result_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_interactions_user_session
  ON search_interactions (user_id, session_id, occurred_at DESC);
