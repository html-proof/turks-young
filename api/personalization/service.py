import asyncio
import re
from collections import defaultdict
from typing import Any, Protocol

from api.personalization.repository import FirebaseUserRepository


class MusicCatalog(Protocol):
    async def search_songs(self, search_query: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...

    async def get_trending(self, language: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _safe_search_seed(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s\-'.]", " ", value).strip()[:100]


class PersonalizedMusicService:
    MAX_SEARCH_SEEDS = 4
    MAX_CANDIDATES_PER_SEED = 8

    def __init__(self, repository: FirebaseUserRepository) -> None:
        self.repository = repository

    async def recommendations(
        self,
        uid: str,
        catalog: MusicCatalog,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        profile, favorites, history, signals = await asyncio.gather(
            self.repository.get_profile(uid),
            self.repository.list_favorites(uid),
            self.repository.list_history(uid, 50),
            self.repository.get_signals(uid),
        )

        preferences = self._preference_scores(profile, favorites, signals)
        seeds = self._search_seeds(preferences)
        preferred_language = self._preferred_language(profile, preferences)
        per_source = min(self.MAX_CANDIDATES_PER_SEED, max(4, limit))

        jobs: list[tuple[str, Any]] = [
            (f"Because you like {seed}", catalog.search_songs(seed, per_source))
            for seed in seeds
        ]
        jobs.append(
            (
                f"Trending in {preferred_language}",
                catalog.get_trending(preferred_language, per_source),
            )
        )

        results = await asyncio.gather(
            *[job for _, job in jobs],
            return_exceptions=True,
        )
        candidate_sources: list[tuple[str, dict[str, Any]]] = []
        for (source, _), result in zip(jobs, results):
            if isinstance(result, Exception) or not isinstance(result, list):
                continue
            candidate_sources.extend(
                (source, track) for track in result if isinstance(track, dict) and track.get("seokey")
            )

        favorite_keys = {item.get("seokey") for item in favorites}
        recent_keys = {item.get("seokey") for item in history[:20]}
        ranked: dict[str, dict[str, Any]] = {}
        for source, track in candidate_sources:
            seokey = track.get("seokey")
            if not seokey or seokey in favorite_keys:
                continue
            score, reasons = self._score_track(track, preferences, source)
            if seokey in recent_keys:
                score *= 0.35
                reasons.append("Played recently")

            recommendation = {
                **track,
                "recommendation": {
                    "score": round(score, 2),
                    "reasons": reasons[:3],
                },
            }
            previous = ranked.get(seokey)
            if not previous or previous["recommendation"]["score"] < score:
                ranked[seokey] = recommendation

        return sorted(
            ranked.values(),
            key=lambda item: item["recommendation"]["score"],
            reverse=True,
        )[:limit]

    @staticmethod
    def _preference_scores(
        profile: dict[str, Any],
        favorites: list[dict[str, Any]],
        signals: dict[str, Any],
    ) -> dict[str, dict[str, float]]:
        scores: dict[str, dict[str, float]] = {
            "artists": defaultdict(float),
            "genres": defaultdict(float),
            "languages": defaultdict(float),
        }

        for artist in _items(profile.get("favorite_artists")):
            scores["artists"][artist.casefold()] += 6.0
        for genre in _items(profile.get("favorite_genres")):
            scores["genres"][genre.casefold()] += 5.0
        for language in _items(profile.get("languages")):
            scores["languages"][language.casefold()] += 3.0

        for track in favorites:
            for artist in _items(track.get("artists")):
                scores["artists"][artist.casefold()] += 5.0
            for genre in _items(track.get("genres")):
                scores["genres"][genre.casefold()] += 4.0
            language = str(track.get("language") or "").strip()
            if language:
                scores["languages"][language.casefold()] += 2.0

        for bucket in ("artists", "genres", "languages"):
            bucket_signals = signals.get(bucket) or {}
            if not isinstance(bucket_signals, dict):
                continue
            for item in bucket_signals.values():
                if not isinstance(item, dict) or not item.get("value"):
                    continue
                scores[bucket][str(item["value"]).casefold()] += float(item.get("score", 0))

        return {bucket: dict(values) for bucket, values in scores.items()}

    def _search_seeds(self, preferences: dict[str, dict[str, float]]) -> list[str]:
        ranked: list[tuple[str, float]] = []
        ranked.extend(preferences["artists"].items())
        ranked.extend(preferences["genres"].items())
        ranked.sort(key=lambda item: item[1], reverse=True)

        seeds: list[str] = []
        for value, _ in ranked:
            seed = _safe_search_seed(value)
            if seed and seed.casefold() not in {item.casefold() for item in seeds}:
                seeds.append(seed)
            if len(seeds) >= self.MAX_SEARCH_SEEDS:
                break
        return seeds

    @staticmethod
    def _preferred_language(
        profile: dict[str, Any],
        preferences: dict[str, dict[str, float]],
    ) -> str:
        languages = preferences["languages"]
        if languages:
            return max(languages, key=languages.get).title()
        profile_languages = _items(profile.get("languages"))
        return profile_languages[0] if profile_languages else "English"

    @staticmethod
    def _score_track(
        track: dict[str, Any],
        preferences: dict[str, dict[str, float]],
        source: str,
    ) -> tuple[float, list[str]]:
        score = 1.0
        reasons = [source]

        artist_matches = [
            artist for artist in _items(track.get("artists"))
            if artist.casefold() in preferences["artists"]
        ]
        for artist in artist_matches:
            score += preferences["artists"][artist.casefold()]
        if artist_matches:
            reasons.append(f"Matches artist {artist_matches[0]}")

        genre_matches = [
            genre for genre in _items(track.get("genres"))
            if genre.casefold() in preferences["genres"]
        ]
        for genre in genre_matches:
            score += preferences["genres"][genre.casefold()] * 0.8
        if genre_matches:
            reasons.append(f"Matches genre {genre_matches[0]}")

        language = str(track.get("language") or "").casefold()
        if language in preferences["languages"]:
            score += preferences["languages"][language] * 0.5
            reasons.append(f"Matches language {track.get('language')}")

        return score, reasons
