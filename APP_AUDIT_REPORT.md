# Music Hub audit — 12 September 2026

## Result

The available automated app and backend suites pass: **212 Flutter tests and 142 backend tests**. This is a code-level audit and release-build check, not a certification that every feature works on every phone.

## Corrections made in this audit

- AppState now removes the actual callback registered with the player. Previously a disposed screen state could remain subscribed and be notified later.
- Repeated media-handler initialization shares one operation rather than registering duplicate listeners.
- Reinitializing authentication cancels the old subscription first.
- Player disposal releases its owned fallback player and detaches callbacks from the shared background handler.
- Signing out after an asynchronous dialog checks whether its screen is still mounted before navigation.
- App initialization failures within SoundwavesApp show a retry screen instead of leaving an unhandled error and a loading screen. Connectivity-monitor startup failures no longer escape the background initialization task.
- Lyrics loading checks screen/request validity after local-cache access. Plain-text lyrics are reused and saved for offline use, and cache-write errors do not escape as unhandled futures.
- Removed unused declarations and redundant null assertions reported by analysis. No user data or audio cache was cleared.
- Connected-device crash records exposed an additional native teardown crash: removal of an unregistered Spatializer listener. Cleanup now retains the exact Spatializer instance used for registration, clears ownership before removal, and tolerates duplicate cancellation. The release APK was rebuilt and installed using an in-place update.

## Coverage

| Area | Verification performed |
| --- | --- |
| Account, onboarding, likes | Account restoration/isolation, failed request handling, atomic onboarding and like-sync suites |
| Home and artwork | Initial feed, recommendation, metadata, language, image parsing and fallback tests |
| Search | Ranking, deduplication, provider search and backend catalog suites |
| Playback | Tap startup, stale taps, album progression, shuffle, quality, spatial selection and buffering tests |
| Library and downloads | Model, playlist, account-cache, audio-cache and offline-first suites; lifecycle review |
| Lyrics | Backend lyrics suite and screen lifecycle/cache review |
| Background controls | Media configuration, notification resource retention and handler lifecycle tests |
| Static analysis | No analyzer errors or warnings; 51 informational style/deprecation notices remain |

The full test commands were flutter test --no-pub and python -m pytest tests -q. Static analysis used flutter analyze --no-pub --no-fatal-infos. Native playback APIs are mocked or unavailable in host tests, so their passing assertions do not demonstrate audible playback on Android.

## Release checks and limitations

The release APK is rebuilt from the current workspace. Verify its signature and confirm drawable/ic_stat_music exists in its packaged resource table before delivery; the source-only icon test is not sufficient. Existing edits from other work were preserved. No production deployment was performed.

A phone connected late in the audit. Its crash buffer confirmed both the invalid-small-icon crash and the Spatializer listener teardown crash. The corrected APK passed signature verification, includes the notification drawable, and installed successfully without clearing data. The phone remained locked; the user was asked to unlock it for interactive verification. Actual sign-in, live provider playback, headset/call interruption, lock-screen controls, background track changes, airplane-mode downloads, network reconnection, long scrolling, and battery-management behavior remain unverified. Live provider availability and production databases were not covered by this run.

Build-tool upgrade notices and informational lints remain maintenance work; they were not hidden by disabling rules. Avoid calling this a zero-bug guarantee or a completed physical-device acceptance test.
