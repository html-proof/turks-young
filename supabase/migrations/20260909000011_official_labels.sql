-- Migration: Create official_labels table for dynamic label discovery and verification
CREATE TABLE IF NOT EXISTS official_labels (
    id SERIAL PRIMARY KEY,
    canonical_name VARCHAR(255) NOT NULL UNIQUE,
    normalized_name VARCHAR(255) NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    source VARCHAR(100) NOT NULL DEFAULT 'dynamic',
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    confidence INTEGER NOT NULL DEFAULT 0,
    song_count INTEGER NOT NULL DEFAULT 1,
    album_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_official_labels_canonical ON official_labels(canonical_name);
CREATE INDEX IF NOT EXISTS idx_official_labels_verified ON official_labels(verified);
