-- Migration: 20260910000013_user_audio_settings.sql
-- Production-grade Spotify-style per-user audio settings keyed by user_uuid (Section 25)

CREATE TABLE IF NOT EXISTS user_audio_settings (
    user_uuid TEXT PRIMARY KEY,
    wifi_stream_quality INT NOT NULL DEFAULT 160,
    mobile_stream_quality INT NOT NULL DEFAULT 96,
    download_quality INT NOT NULL DEFAULT 160,
    automatic_quality_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    data_saver_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    wifi_only_downloads BOOLEAN NOT NULL DEFAULT TRUE,
    very_high_mobile_warning_acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_audio_settings_updated_at ON user_audio_settings(updated_at);
