# Artwork repair — 11 September 2026

## Verified causes

- Flutter and backend parsing missed image arrays and several nested artwork aliases.
- Listening snapshots flattened album objects and ignored unrecognized artwork fields before preserving their covers.
- The shared widget picked one URL and skipped its supplied fallback after failure; retries created separate cache aliases.
- Speculative dimension rewriting could change valid provider paths or signed query strings.
- Generated backend mixes read only the first member's image_url.
- A detail-request timeout prevented the independent home metadata search fallback.
- Public search returned a provider default-album image for Kanmaniye (saavn-vHO9WcR1). A 200 image response alone does not prove a real album cover.

## Changes

Centralized artwork parsing on both sides; preserve original URLs, normalize protocol-relative paths, support explicit provider origins, reject known placeholders, retain alternate candidates across track copies/cache serialization, and select a real mix-member cover. Shared cards try primary, alternatives and artist artwork sequentially, use a quiet loading surface, retain a successful alternative, retry once after 30 seconds, and retry following a network change/app resume. Failed entries and artist metadata are invalidated individually. The existing shared image cache is reused without per-retry keys; no image/audio cache or user preferences were globally cleared.

Home/search backend cache namespaces are versioned so updated code does not reuse the incompatible response format. Local cached metadata is normalized on read; background repair also runs for guest refresh and pagination. UI layout and audio playback logic were not changed.

## Validation

- 76 backend tests passed: artwork, catalog, personalization and personalization engine.
- 46 Flutter tests passed: artwork parsing, sequential widget fallback, card replacement and existing model tests.
- Targeted Flutter analysis: no issues.
- Broader Flutter suite initially: 171 passed, three failed. The same three failures reproduced in a separate unchanged HEAD checkout: cross-provider search deduplication, bitrate expectation (64 vs 16), and a pending native audio-capability timer in mini-player navigation. These are not artwork regressions.

Read-only live probe: python -m scripts.validate_artwork. Public search was used for the screenshot titles, not the user's authenticated Home response.

| Live sample | Result |
| --- | --- |
| Theekkali Aarambham | One WebP, HTTP 200 |
| Kanmaniye | Two WebPs, HTTP 200; one default-album placeholder, HTTP 200 |
| Varnajaalam | One WebP, HTTP 200 |
| Murivukal | One WebP and one JPEG, HTTP 200 |
| Thalapathy Kacheri | Two WebPs, HTTP 200 |
| Album and artist search | Probe requests failed; inconclusive |
| Playlist search | No sample items returned |

Nine sampled URLs: eight image responses and one known placeholder, all from c.saavncdn.com, no redirects. HEAD timings were 326–489 ms. These validate headers, not image decoding or exact recording identity. The probe does not log signed URLs or audio links.

## Remaining release verification

Backend code has not been deployed by this task. Live responses above are from the existing deployment. The updated APK alone cannot repair server records whose provider offers no real image.

Real-device authenticated Home, notification/lock-screen artwork, offline restart, reconnection and scrolling performance still need verification. No device session was available. Stable canonical caching across rotating signed URLs, global success/placeholder/cache-hit metrics, atomic decode-validated disk writes, and proactive signed-URL refresh are not implemented here; the existing cache manager remains in use. Do not describe this as every acceptance criterion being verified or as zero bugs guaranteed.

Unrelated concurrent vector/repository changes were preserved.
