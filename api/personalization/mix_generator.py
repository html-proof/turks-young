"""
Personalized Mix & Generated Playlist Engine.

Automatically constructs personalized mixes for each user UUID:
  - Daily Mix 1, Daily Mix 2 (clustered around distinct top artist/genre sub-tastes)
  - On Repeat (tracks with high repetition and completion in the last 30 days)
  - Rediscover (formerly loved tracks not played in > 3 weeks)
  - Your [Language] Mix (e.g. Your Malayalam Mix, Your Tamil Mix)
  - New Releases For You
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any

from api.catalog.normalize import song
from api.catalog.artwork import artwork_candidates
from api.personalization.models import GeneratedPlaylistSnapshot, UserTasteProfile

logger = logging.getLogger(__name__)


def _mix_cover(tracks: list[dict[str, Any]]) -> str | None:
    # Choose the highest-ranked actual member with usable artwork, not merely
    # index zero's possibly missing image_url field.
    for track in tracks:
        urls = artwork_candidates(track)
        if urls:
            return urls[0]
    return None


class MixGenerator:
    """Generates virtual personalized mix snapshots per user UUID."""

    def __init__(self, repository: Any | None = None) -> None:
        self.repository = repository

    def generate_mixes(
        self,
        profile: UserTasteProfile,
        favorites: list[dict[str, Any]],
        history: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> list[GeneratedPlaylistSnapshot]:
        """Generate a complete set of personalized mixes for this user profile."""
        now = now or datetime.now(timezone.utc)
        user_id = profile.user_id
        mixes: list[GeneratedPlaylistSnapshot] = []

        # 1. "On Repeat" Mix (tracks played multiple times with high completion)
        track_counts: dict[str, int] = {}
        track_store: dict[str, dict[str, Any]] = {}
        for h in history:
            ev = h.get("event") if (isinstance(h, dict) and "event" in h and h.get("event")) else h
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = h
            if not isinstance(ev, dict):
                ev = h
            tid = str(ev.get("song_id") or ev.get("seokey") or ev.get("track_id") or ev.get("id") or h.get("song_id") or h.get("seokey") or "").lower().strip()
            if not tid:
                continue
            ratio = float(ev.get("completion_ratio") or (1.0 if ev.get("completed") else 0.5))
            if ratio >= 0.50:
                track_counts[tid] = track_counts.get(tid, 0) + 1
                if tid not in track_store:
                    track_store[tid] = song(ev)

        repeated_tracks = [
            track_store[tid]
            for tid, count in sorted(track_counts.items(), key=lambda x: x[1], reverse=True)
            if count >= 2
        ][:20]

        if repeated_tracks:
            mix_id = f"mix:on_repeat:{user_id}"
            mixes.append(GeneratedPlaylistSnapshot(
                id=mix_id,
                user_id=user_id,
                mix_type="on_repeat",
                title="On Repeat",
                description="Songs you've been playing non-stop lately.",
                tracks=repeated_tracks,
                cover_url=_mix_cover(repeated_tracks),
                algorithm_version="rec_v2",
                generated_at=now.isoformat(),
            ))

        # 2. "Rediscover" Mix (formerly favorited / high-engagement tracks not played in > 21 days)
        recent_3w_ids = set()
        for h in history:
            ev = h.get("event") if (isinstance(h, dict) and "event" in h and h.get("event")) else h
            if isinstance(ev, str):
                try:
                    ev = json.loads(ev)
                except Exception:
                    ev = h
            if not isinstance(ev, dict):
                ev = h
            ts_str = h.get("played_at") or h.get("started_at") or ev.get("played_at")
            if ts_str:
                try:
                    p_time = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    if not p_time.tzinfo:
                        p_time = p_time.replace(tzinfo=timezone.utc)
                    if (now - p_time) <= timedelta(days=21):
                        tid = str(ev.get("song_id") or ev.get("seokey") or ev.get("track_id") or ev.get("id") or h.get("song_id") or "").lower().strip()
                        if tid:
                            recent_3w_ids.add(tid)
                except (ValueError, TypeError):
                    pass

        rediscover_tracks = []
        for fav in favorites:
            fid = str(fav.get("id") or fav.get("seokey") or "").lower().strip()
            if fid and fid not in recent_3w_ids:
                rediscover_tracks.append(song(fav))

        if rediscover_tracks:
            mix_id = f"mix:rediscover:{user_id}"
            mixes.append(GeneratedPlaylistSnapshot(
                id=mix_id,
                user_id=user_id,
                mix_type="rediscover",
                title="Rediscover",
                description="Past favorites you haven't heard in a while.",
                tracks=rediscover_tracks[:20],
                cover_url=_mix_cover(rediscover_tracks),
                algorithm_version="rec_v2",
                generated_at=now.isoformat(),
            ))

        # 3. "Daily Mix 1" & "Daily Mix 2"
        # Group candidates by top artists
        sorted_artists = sorted(profile.artists.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_artists) >= 2:
            # Daily Mix 1 around top artist
            art1 = sorted_artists[0][0].lower()
            mix1_tracks = [
                c for c in candidates
                if art1 in str(c.get("artist") or "").lower() or any(
                    art1 in str(a.get("name") if isinstance(a, dict) else a).lower()
                    for a in (c.get("artists") or [])
                )
            ][:20]
            if not mix1_tracks:
                mix1_tracks = candidates[:15]

            mixes.append(GeneratedPlaylistSnapshot(
                id=f"mix:daily_1:{user_id}",
                user_id=user_id,
                mix_type="daily_mix_1",
                title="Daily Mix 1",
                description=f"Featuring {art1.title()} and similar artists.",
                tracks=mix1_tracks,
                cover_url=_mix_cover(mix1_tracks),
                algorithm_version="rec_v2",
                generated_at=now.isoformat(),
            ))

            # Daily Mix 2 around second top artist
            art2 = sorted_artists[1][0].lower()
            mix2_tracks = [
                c for c in candidates
                if art2 in str(c.get("artist") or "").lower() or any(
                    art2 in str(a.get("name") if isinstance(a, dict) else a).lower()
                    for a in (c.get("artists") or [])
                )
            ][:20]
            if mix2_tracks:
                mixes.append(GeneratedPlaylistSnapshot(
                    id=f"mix:daily_2:{user_id}",
                    user_id=user_id,
                    mix_type="daily_mix_2",
                    title="Daily Mix 2",
                    description=f"Featuring {art2.title()} and similar artists.",
                    tracks=mix2_tracks,
                    cover_url=_mix_cover(mix2_tracks),
                    algorithm_version="rec_v2",
                    generated_at=now.isoformat(),
                ))

        # 4. Language-specific Mixes (e.g. "Your Malayalam Mix", "Your Tamil Mix")
        for lang_name, affinity in sorted(profile.languages.items(), key=lambda x: x[1], reverse=True)[:2]:
            lang_tracks = [
                c for c in candidates
                if str(c.get("language") or "").lower() == lang_name.lower()
            ][:20]
            if lang_tracks:
                lang_clean = lang_name.title()
                mixes.append(GeneratedPlaylistSnapshot(
                    id=f"mix:{lang_name.lower()}:{user_id}",
                    user_id=user_id,
                    mix_type=f"language_{lang_name.lower()}",
                    title=f"Your {lang_clean} Mix",
                    description=f"The best {lang_clean} music tailored for you.",
                    tracks=lang_tracks,
                    cover_url=_mix_cover(lang_tracks),
                    algorithm_version="rec_v2",
                    generated_at=now.isoformat(),
                ))

        return mixes
