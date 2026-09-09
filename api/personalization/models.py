import ast
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SEO_KEY_PATTERN = r"^[a-zA-Z0-9\-_./%\[\]()+@]+$"


def _is_artist_noise(s: str) -> bool:
    if not s:
        return True
    s_clean = str(s).lower().strip().strip("'\"{}[]")
    if not s_clean:
        return True
    if s_clean.startswith("type:") or s_clean.startswith("type :"):
        return True
    if "artist}" in s_clean or "{artist" in s_clean or "'type'" in s_clean or '"type"' in s_clean:
        return True
    if s_clean in ("artist", "artists", "singer", "singers", "song", "track", "album", "true", "false", "none", "null"):
        return True
    return False


def _clean_str_item(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        name = str(val.get("name") or val.get("title") or val.get("id") or val.get("seokey") or "").strip()
        return "" if _is_artist_noise(name) else name
    s = str(val).strip()
    if _is_artist_noise(s):
        return ""
    if (s.startswith("{") or s.startswith("'") or s.startswith('"') or s.startswith("[")) and ("name" in s or "id" in s):
        match = re.search(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match and not _is_artist_noise(match.group(1)):
            return match.group(1).strip()
        match_id = re.search(r"['\"](?:id|seokey|artist_id)['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match_id and not _is_artist_noise(match_id.group(1)):
            return match_id.group(1).strip()
    s = re.sub(r"^[{}\[\]\'\"\s]+|[{}\[\]\'\"\s]+$", "", s).strip()
    return "" if _is_artist_noise(s) else s


def clean_album_or_title(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, dict):
        t = val.get("title") or val.get("name") or val.get("album") or ""
        if t:
            return clean_album_or_title(t)
        slug = val.get("id") or val.get("seokey") or val.get("album_seokey") or ""
        if slug:
            return str(slug).replace("-", " ").title()
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if (s.startswith("{") and s.endswith("}")) or (
        "'id':" in s or '"id":' in s or "'title':" in s or '"title":' in s or "'name':" in s or '"name":' in s
    ):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                t = parsed.get("title") or parsed.get("name") or parsed.get("album")
                if t:
                    return clean_album_or_title(t)
                slug = parsed.get("id") or parsed.get("seokey") or parsed.get("album_seokey") or ""
                if slug:
                    return str(slug).replace("-", " ").title()
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict):
                t = parsed.get("title") or parsed.get("name") or parsed.get("album")
                if t:
                    return clean_album_or_title(t)
                slug = parsed.get("id") or parsed.get("seokey") or parsed.get("album_seokey") or ""
                if slug:
                    return str(slug).replace("-", " ").title()
        except Exception:
            pass
        m = re.search(r"""['"](?:title|name)['"]\s*:\s*['"]([^'"]+)['"]""", s)
        if m and m.group(1).strip():
            return clean_album_or_title(m.group(1).strip())
        m_id = re.search(r"""['"](?:id|seokey|album_seokey)['"]\s*:\s*['"]([^'"]+)['"]""", s)
        if m_id and m_id.group(1).strip():
            return m_id.group(1).strip().replace("-", " ").title()
    s = re.sub(r"^[{}\[\]\'\"\s]+|[{}\[\]\'\"\s]+$", "", s).strip()
    return s


def _clean_id_slug(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, dict):
        return str(val.get("id") or val.get("seokey") or val.get("album_seokey") or val.get("album_id") or "").strip()
    s = str(val).strip()
    if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("id" in s or "seokey" in s):
        m = re.search(r"""['"](?:id|seokey|album_seokey|album_id)['"]\s*:\s*['"]([^'"]+)['"]""", s)
        if m:
            return m.group(1).strip()
    return s.strip("'\"{}[]")


def _list_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                parsed = json.loads(s.replace("'", '"'))
                return _list_from_value(parsed)
            except Exception:
                pass
            try:
                parsed = ast.literal_eval(s)
                return _list_from_value(parsed)
            except Exception:
                pass
        cleaned_str = re.sub(r"""['"]?type['"]?\s*:\s*['"]?[a-zA-Z0-9_\-]+['"]?\}?""", "", s, flags=re.IGNORECASE)
        cleaned_str = re.sub(r"""['"]?(?:id|seokey|artist_id)['"]\s*:\s*['"]([^'"]+)['"]""", r"\1", cleaned_str)
        cleaned_str = re.sub(r"""['"]?name['"]\s*:\s*['"]([^'"]+)['"]""", r"\1", cleaned_str)
        cleaned_str = re.sub(r"""[\{\}\[\]]""", "", cleaned_str)
        values = cleaned_str.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = []
        for x in value:
            if isinstance(x, str) and (x.strip().startswith("[") or x.strip().startswith("{") or "," in x):
                values.extend(_list_from_value(x))
            else:
                values.append(x)
    else:
        values = [value]

    unique: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = _clean_str_item(item)
        if not normalized or _is_artist_noise(normalized):
            continue
        key = normalized.casefold()
        if key not in seen:
            unique.append(normalized)
            seen.add(key)

    # Subsume slugs if full display name exists
    filtered: list[str] = []
    for item in unique:
        is_slug = bool(re.match(r'^[a-z0-9\-]+$', item) and '-' in item)
        is_subsumed = False
        for other in unique:
            if other != item and ' ' in other:
                other_words = [w.lower() for w in re.split(r'[\s\-]+', other) if w]
                item_words = [w.lower() for w in re.split(r'[\s\-]+', item) if w]
                if is_slug and any(w1 in w2 or w2 in w1 for w1 in item_words for w2 in other_words):
                    is_subsumed = True
                    break
                slug = re.sub(r'[^a-zA-Z0-9]+', '', item).lower()
                other_slug = re.sub(r'[^a-zA-Z0-9]+', '', other).lower()
                if slug == other_slug or slug in other_slug or other_slug in slug:
                    is_subsumed = True
                    break
        if not is_subsumed:
            filtered.append(item)
    return filtered


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    languages: list[str] | None = Field(default=None, max_length=10)
    favorite_genres: list[str] | None = Field(default=None, max_length=20)
    favorite_artists: list[str] | None = Field(default=None, max_length=30)
    onboarding_completed: bool | None = None
    streaming_quality_wifi: str | None = Field(default=None, max_length=20)
    streaming_quality_mobile: str | None = Field(default=None, max_length=20)
    download_quality: str | None = Field(default=None, max_length=20)
    data_saver_enabled: bool | None = None
    autoplay_enabled: bool | None = None
    push_notifications_enabled: bool | None = None
    pulse_followed_releases_enabled: bool | None = None
    pulse_selected_releases_enabled: bool | None = None
    pulse_trending_enabled: bool | None = None
    pulse_recommendations_enabled: bool | None = None
    explicit_content_enabled: bool | None = None
    equalizer_preset: str | None = Field(default=None, max_length=30)

    @field_validator("languages", "favorite_genres", "favorite_artists", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str] | None:
        return None if value is None else _list_from_value(value)


class TrackSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    track_id: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=300)
    artists: list[str] = Field(default_factory=list, max_length=20)
    artist_ids: list[str] = Field(default_factory=list, max_length=20)
    genres: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=50)
    album: str = Field(default="", max_length=300)
    album_id: str = Field(default="", max_length=100)
    album_seokey: str = Field(default="", max_length=200)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_./%\[\]()+@]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("title", "album", mode="before")
    @classmethod
    def normalize_title_album(cls, value: Any) -> str:
        return clean_album_or_title(value)

    @field_validator("album_id", "album_seokey", mode="before")
    @classmethod
    def normalize_album_identifiers(cls, value: Any) -> str:
        return _clean_id_slug(value)

    @field_validator("track_id", "language", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}

    @field_validator("artists", "artist_ids", "genres", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        return _list_from_value(value)


class ListeningEvent(TrackSnapshot):
    played_seconds: int = Field(default=0, ge=0, le=86400)
    completed: bool = False
    source: str = Field(default="playback", min_length=1, max_length=50)
    event_type: str = Field(default="song_play", max_length=50)
    duration_ms: int = Field(default=0, ge=0)
    completion_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    session_id: str | None = Field(default=None, max_length=100)

    @field_validator("played_seconds", mode="before")
    @classmethod
    def normalize_played_seconds(cls, value: Any) -> int:
        if value is None:
            return 0
        try:
            return max(0, min(86400, int(float(value))))
        except (ValueError, TypeError):
            return 0

    @field_validator("completed", mode="before")
    @classmethod
    def normalize_completed(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: Any) -> str:
        if not value:
            return "playback"
        return str(value).strip()[:50] or "playback"


class ArtistSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    artist_id: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=300)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_.%]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("artist_id", "name", mode="before")
    @classmethod
    def normalize_strings(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}


class AlbumSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    seokey: str = Field(min_length=1, max_length=200, pattern=SEO_KEY_PATTERN)
    album_id: str = Field(default="", max_length=100)
    title: str = Field(min_length=1, max_length=300)
    artists: list[str] = Field(default_factory=list, max_length=20)
    images: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seokey", mode="before")
    @classmethod
    def normalize_seokey(cls, value: Any) -> str:
        if not value:
            return "unknown"
        normalized = str(value).strip()
        cleaned = re.sub(r"[^a-zA-Z0-9\-_.%]", "-", normalized)
        return cleaned.strip("-") or "unknown"

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> str:
        return clean_album_or_title(value)

    @field_validator("album_id", mode="before")
    @classmethod
    def normalize_album_id(cls, value: Any) -> str:
        return _clean_id_slug(value)

    @field_validator("images", mode="before")
    @classmethod
    def normalize_images(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            return {"urls": {"large_artwork": value.strip()}}
        return {}

    @field_validator("artists", mode="before")
    @classmethod
    def normalize_artists(cls, value: Any) -> list[str]:
        return _list_from_value(value)


class UserPlaylistCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    is_public: bool = False


class UserPlaylistUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_public: bool | None = None


class TrackOrderUpdate(BaseModel):
    seokeys: list[str] = Field(min_length=1, max_length=500)


class OnboardingUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    languages: list[str] | None = Field(default=None, max_length=10)
    favorite_artists: list[str] | None = Field(default=None, max_length=30)
    completed: bool | None = None
    step: str | None = Field(default=None, max_length=50)

    @field_validator("languages", "favorite_artists", mode="before")
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str] | None:
        return None if value is None else _list_from_value(value)


class PulsePostCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(default="", max_length=1000)
    track: dict[str, Any] | None = None
    album: dict[str, Any] | None = None
    playlist_id: str | None = Field(default=None, max_length=36)


class PulseCommentCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1, max_length=1000)


class DeviceRegister(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=1, max_length=500)
    platform: str = Field(min_length=1, max_length=20)
    device_name: str | None = Field(default=None, max_length=150)


class PlayerSessionUpdate(BaseModel):
    track: dict[str, Any] | None = None
    queue: list[Any] = Field(default_factory=list)
    position_ms: int = Field(default=0, ge=0)
    playing: bool = False
    repeat_mode: str = Field(default="off", pattern="^(off|one|all)$")
    shuffle_enabled: bool = False
    duration_ms: int = Field(default=0, ge=0)
    device_id: str | None = Field(default=None, max_length=200)


class PreferenceIds(BaseModel):
    """IDs submitted by onboarding/settings; identity comes from the token."""
    language_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    artist_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("language_ids", "artist_ids")
    @classmethod
    def unique_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not result:
            raise ValueError("At least one ID is required")
        return result


class RecommendationEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=50)
    song_id: str | None = Field(default=None, max_length=200)
    artist_id: str | None = Field(default=None, max_length=200)
    album_id: str | None = Field(default=None, max_length=200)
    playlist_id: str | None = Field(default=None, max_length=200)
    position_ms: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    source: str | None = Field(default=None, max_length=50)
    context_id: str | None = Field(default=None, max_length=100)
    query: str | None = Field(default=None, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class SearchImpression(BaseModel):
    """One ranked result that was actually rendered for a catalog search."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=100)
    result_id: str = Field(min_length=1, max_length=200)
    result_type: Literal["song", "artist", "album", "playlist"]
    position: int = Field(ge=0, le=100)
    algorithm_version: str = Field(default="search_v1", min_length=1, max_length=50)


class SearchInteraction(BaseModel):
    """A user action tied to a previously shown search result."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=100)
    result_id: str = Field(min_length=1, max_length=200)
    result_type: Literal["song", "artist", "album", "playlist"]
    position: int = Field(ge=0, le=100)
    action: Literal["click", "play", "complete", "skip", "like", "save"]


class BehavioralEvent(BaseModel):
    """User behavioral signal across playback, library, social, and search."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=50)
    song_id: str | None = Field(default=None, max_length=200)
    artist_id: str | None = Field(default=None, max_length=200)
    album_id: str | None = Field(default=None, max_length=200)
    playlist_id: str | None = Field(default=None, max_length=200)
    played_seconds: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    completion_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = Field(default="app", max_length=50)
    session_id: str | None = Field(default=None, max_length=100)
    query: str | None = Field(default=None, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None


class UserTasteProfile(BaseModel):
    """Continuously updated 3-tier preference vector for a user UUID."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    user_id: str
    languages: dict[str, float] = Field(default_factory=dict)
    artists: dict[str, float] = Field(default_factory=dict)
    genres: dict[str, float] = Field(default_factory=dict)
    eras: dict[str, float] = Field(default_factory=dict)
    long_term: dict[str, dict[str, float]] = Field(default_factory=dict)
    short_term: dict[str, dict[str, float]] = Field(default_factory=dict)
    current_session: dict[str, dict[str, float]] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    interaction_count: int = 0
    discovery_receptivity: float = 0.5
    algorithm_version: str = "rec_v2"
    updated_at: str | None = None


class RecommendationCandidate(BaseModel):
    """Internal candidate representation with ranking score and explanation."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    title: str
    artists: list[str] = Field(default_factory=list)
    artist_ids: list[str] = Field(default_factory=list)
    language: str = ""
    album: str = ""
    image_url: str | None = None
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    source: str = "recommendation"


class GeneratedPlaylistSnapshot(BaseModel):
    """Personalized generated mix snapshot (Daily Mix, On Repeat, etc.)."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    user_id: str
    mix_type: str
    title: str
    description: str = ""
    tracks: list[dict[str, Any]] = Field(default_factory=list)
    cover_url: str | None = None
    algorithm_version: str = "rec_v2"
    generated_at: str | None = None
