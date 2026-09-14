# Account validity and deletion implementation

Implemented in the active Songlist FastAPI backend and music_app Flutter client. Existing screens and design are retained.

## Session authority

Firebase Admin verifies tokens with revocation checking. The common protected dependency then queries the backend by Firebase UID, without trusting a client UUID or cached identity mapping. Missing, disabled, deleted and deletion-pending accounts return ACCOUNT_INVALID. Expired tokens have a separate refreshable response; service outages are not account deletion.

GET /session/validate and /api/v1/session/validate return only validity, internal user ID and ACTIVE status. Flutter validates before restoring protected startup state and on resume, with a 15-second resume threshold. Authenticated API calls bypass response caching; stale in-flight responses cannot cross a session generation. The HTTP layer dispatches positive invalidity to the shared single-flight forceLocalLogout function.

Explicit Google login may exchange identity at /api/v1/auth/google. Session restoration cannot provision users. A database identity fingerprint ledger prevents resurrection after deletion. Missing legacy identities created before migration installation are rejected; new Firebase identities can register. Existing active accounts retain their UUID and preferences.

## Deletion and recovery

DELETE /account, /me/account and /api/v1/me share AccountDeletionService. Self-deletion derives identity exclusively from the token and requires authentication within five minutes; Flutter uses Google reauthentication when required. DELETE /admin/accounts/{firebase_uid} requires a verified Firebase admin custom claim.

Deletion commits DELETION_PENDING and a durable job before external actions. It disables/revokes Firebase credentials, purges Redis keys for the Firebase UID and internal UUID, removes the root database row and cascading children, deletes Firebase, and removes the job. External failures leave the account blocked and the job retryable. A worker retries every 30 seconds and reconciles bounded pages of Firebase identities. Direct database deletion also enqueues cleanup through a trigger.

## Ownership audit

The migration audits and guards 30 foreign-key ownership relationships. Covered tables include user_profiles, user_favorites, user_history, user_signals, user_playlists, user_followed_artists, user_saved_albums, user_follows (both identities), pulse_posts, pulse_likes, pulse_comments, device_tokens, notifications, player_sessions, recent_searches, user_languages, user_selected_artists, user_liked_songs, playback_history, user_events, user_taste_profiles, recommendation_impressions, user_pulse_items, pulse_notification_dedup, search_impressions, search_interactions, generated_playlists, user_taste_vectors and user_audio_settings. Playlist contents are stored with their playlist records. Audio settings previously lacked an ownership foreign key; the migration repairs it.

Database triggers lock the owner row and reject inserts/updates after the deletion barrier. Home and Pulse cache publication use the same locking boundary. Shared music, artist, language, lyrics and catalog assets are preserved.

Client cleanup stops playback, clears the queue/media item and notification session, cancels downloads and pending synchronization, removes account SQLite rows and private search cache, clears preferences and retry outboxes, resets personalization state, and signs out Firebase/Google. Download cleanup removes the app-generated account directory including temporary files. A persisted cleanup marker prevents another login after an incomplete cleanup.

No active Supabase Storage upload integration or separately managed secure-token store was found in this app. Firebase owns its credential persistence. The unused music_app/services/recommendation_service.dart and dataconnect schema are legacy Data Connect artifacts, not imported by the running client. The sibling sample-song-list-activity backend is a separate implementation and was not changed. These facts do not establish that external services have no historic user data; deployment owners must reconcile any previously deployed legacy stores.

## Validation and rollout

Backend tests: 171 passed. The lifecycle migration and all preceding migrations executed in isolated PGlite PostgreSQL, including vector support. Checks verified all 30 ownership constraints, cascade cleanup, blocked late writes, durable direct-delete jobs, tombstones, preservation of another account, and shared catalog preservation. This is not a multi-process production PostgreSQL lock-contention test.

Final clean Flutter run: 233 passed, one failed. All account-validity tests passed. The remaining failure is the search ranking assertion in search_engine_test.dart (expected 150, actual 10), outside the account lifecycle changes. The analyzer reports no errors and 52 warnings/informational findings. Results are in music_app/account-validity-test-results.txt and music_app/account-validity-analysis.txt. Native phone notification removal and two-device Firebase/Supabase behavior still need deployed integration acceptance.

Apply supabase/migrations/20260912000017_account_validity.sql after existing migrations, then deploy the Songlist backend with Firebase Admin credentials, then distribute the updated Flutter build. The migration intentionally deletes already-orphaned audio settings and queues cleanup of previously soft-deleted accounts. Backend startup schema.sql alone does not install this migration.

No production migration, deployment, account deletion, device installation or release APK distribution was performed during this implementation. The retained SHA-256 identity fingerprints are an intentional minimal anti-resurrection record; profiles, email and tokens are not retained in that ledger. An account's inability to access APIs is enforced on each protected request; idle/offline devices cannot learn a remote deletion until they communicate again.
