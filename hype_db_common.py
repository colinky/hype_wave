from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("hype_db")

KST = timezone(timedelta(hours=9))
MATCHED_STATUSES = {"matched", "cached_match", "proxy_matched", "manual_override"}
DEFAULT_HYPE_WEIGHTS = {"apple": 0.4, "melon_genz": 0.4, "ytmusic": 0.2}
__all__ = [
    "KST",
    "MATCHED_STATUSES",
    "DEFAULT_HYPE_WEIGHTS",
    "utc_now_iso",
    "kst_today",
    "normalize_text",
    "metadata_key",
    "compact_metadata_key",
    "strip_parens_from_title",
    "feature_signature",
    "has_feature_mismatch",
    "version_signature",
    "has_version_mismatch",
    "clean_track_title",
    "stable_uid",
    "row_dict",
    "normalized_service",
    "normalize_song_id",
    "infer_album_id",
    "job_frequency",
    "job_list_type",
    "job_service",
    "reference_period_for_date",
    "parse_crawl_date",
    "normalize_source_variant",
    "build_album_url",
    "build_track_url",
    "hype_identity_key",
    "load_sync_config",
    "hype_inputs",
    "postgres_connect_config",
    "match_method_for_status",
    "playlist_job_mappings",
    "normalize_job_name",
    "legacy_to_job_name",
    "require_job_name",
]

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def kst_today() -> str:
    return datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        LOG.warning("Ignoring invalid integer env %s=%r; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        LOG.warning("Ignoring too-small integer env %s=%r; using %s", name, raw, default)
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("Ignoring invalid float env %s=%r; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        LOG.warning("Ignoring too-small float env %s=%r; using %s", name, raw, default)
        return default
    return value


def postgres_connect_config() -> dict[str, int | float]:
    """Return shared PostgreSQL connection retry settings.

    Defaults are intentionally conservative for GitHub Actions, where Supabase
    pooler connections can occasionally take longer than local runs.
    """
    return {
        "retries": _env_int("HYPE_PG_CONNECT_RETRIES", 5, minimum=1),
        "retry_delay": _env_float("HYPE_PG_CONNECT_RETRY_DELAY", 2.0, minimum=0.0),
        "connect_timeout": _env_int("HYPE_PG_CONNECT_TIMEOUT", 20, minimum=1),
    }


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\([^)]*(feat\.?|ft\.?)[^)]*\)", " ", value)
    value = re.sub(r"\[[^\]]*(feat\.?|ft\.?)[^\]]*\]", " ", value)
    value = re.sub(r"\b(feat\.?|ft\.?)\b.*$", " ", value)
    value = re.sub(r"\s*-\s*(ep|single)\b.*$", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^0-9a-z가-힣\u3040-\u30ff\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def metadata_key(title: str | None, artist: str | None, album: str | None = "") -> str:
    return f"{normalize_text(title)}|{normalize_text(artist)}|{normalize_text(album)}"


def compact_metadata_key(title: str | None, artist: str | None) -> str:
    return f"{normalize_text(title)}|{normalize_text(artist)}"


def strip_parens_from_title(title: str | None) -> str:
    """Remove ALL parenthetical/bracketed groups from a title.

    Used as a last-resort fallback key so that chart entries with short titles
    can match canonical tracks whose full title includes production credits etc.

    Example:
      'KISS KISS KISS (Prod. by Hukky Shibaseki)' → 'KISS KISS KISS'
      '소문의 낙원 (Live)'                          → '소문의 낙원'   (also handled by clean_track_title)
    """
    stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", (title or "")).strip()
    return stripped or (title or "")


def feature_signature(title: str | None) -> str:
    """Return normalized featuring-artist text from a title, if present."""
    text = title or ""
    patterns = [
        r"\((?:feat\.?|ft\.?|featuring)\s*([^)]+)\)",
        r"\[(?:feat\.?|ft\.?|featuring)\s*([^\]]+)\]",
        r"\b(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = re.split(r"[\)\]\-_:|]", match.group(1))[0].strip()
            return normalize_text(value)
    return ""


def has_feature_mismatch(left: str | None, right: str | None) -> bool:
    return feature_signature(left) != feature_signature(right)


def version_signature(title: str | None) -> str:
    """Return a normalized recording/version marker such as remix, live, or acoustic."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = re.sub(r"\b(?:feat\.?|ft\.?|featuring)\b.*", " ", text)
    suffix_chunks = re.findall(r"[\(\[]([^\)\]]+)[\)\]]", text)
    suffix_chunks.extend(re.findall(r"\s[-–—]\s(.+)$", text))
    chunks = suffix_chunks or [text]
    signatures: list[str] = []

    def add(value: str) -> None:
        if value and value not in signatures:
            signatures.append(value)

    for chunk in chunks:
        normalized = normalize_text(chunk)
        if not normalized:
            continue
        remaster_marker = bool(
            re.search(r"\b(?:re)?master(?:ed|ing)?\b|\bremaster\b|\banniversary\b|\bbonus track\b", normalized)
        )
        year_version_marker = bool(
            re.fullmatch(r"(?:19|20)\d{2}\s+(?:ver|version|edition|edit)", normalized)
        )
        if "remix" in normalized.split():
            remix_prefix = re.sub(r"\bremix\b.*$", "", normalized).strip()
            add(f"remix:{remix_prefix}" if remix_prefix else "remix")
        if re.search(r"\bacoustic\b", normalized):
            add("acoustic")
        if re.search(r"\blive\b|\blive ver\b|\blive version\b", normalized):
            add("live")
        if re.search(r"\binstrumental\b|\binst\b|\bkaraoke\b", normalized):
            add("instrumental")
        if re.search(r"\bsped up\b|\bslowed\b", normalized):
            add("speed")
        if re.search(r"\bcover\b|\barrange\b", normalized):
            add("cover")
        version_match = re.search(
            r"\b(japanese|jp|chinese|cn|english|eng|korean|kr)?\s*(?:ver|version|edition|edit)\b",
            normalized,
        )
        if version_match and not remaster_marker and not year_version_marker:
            locale = (version_match.group(1) or "").strip()
            add(f"version:{locale}" if locale else "version")
    return "|".join(signatures)


def has_version_mismatch(left: str | None, right: str | None) -> bool:
    return version_signature(left) != version_signature(right)


# Patterns that denote a performance/video variant of a track rather than the studio recording.
# These are stripped from the title when looking up a canonical counterpart.
_VARIANT_SUFFIX_RE = re.compile(
    r"[\(\[\s]*"
    r"(?:live|live\s+ver(?:sion)?|live\s+performance|acoustic|acoustic\s+ver(?:sion)?"
    r"|mv|m/v|music\s+video|official\s+video|official\s+mv|performance\s+video"
    r"|stage|stage\s+ver(?:sion)?|dance\s+ver(?:sion)?|visualizer)"
    r"[\)\]\s]*$",
    re.IGNORECASE,
)


def clean_track_title(title: str | None) -> str:
    """Strip performance/video variant suffixes to get the canonical studio title.

    Examples:
      '소문의 낙원 (Live)'  → '소문의 낙원'
      '갑자기 (MV)'        → '갑자기'
      'Song (Live Ver.)'   → 'Song'
    """
    cleaned = _VARIANT_SUFFIX_RE.sub("", (title or "")).strip()
    return cleaned or (title or "")


def stable_uid(seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"trk_{digest}"


def row_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return dict(vars(value))


def normalized_service(service: str | None) -> str:
    service = (service or "").strip().lower()
    if service.startswith("apple"):
        return "apple"
    if service.startswith("melon"):
        return "melon"
    if service.startswith("spotify"):
        return "spotify"
    if service in {"ytmusic", "youtube", "youtube_music"}:
        return "ytmusic"
    return service or "unknown"


def normalize_song_id(service: str, row: dict[str, Any]) -> str:
    service = normalized_service(service)
    song_id = str(row.get("song_id") or "").strip()
    if song_id:
        return song_id

    legacy_source_id = str(row.get("apple" + "_id") or "").strip()
    if service == "melon":
        if legacy_source_id.startswith("melon_"):
            return legacy_source_id.split("_", 1)[1]
        return legacy_source_id
    if service == "spotify":
        return "" if legacy_source_id.startswith("fallback:") else legacy_source_id
    if service == "apple":
        if legacy_source_id:
            return legacy_source_id
        track_url = str(row.get("url") or "").strip()
        match = re.search(r"[?&]i=(\d+)", track_url)
        return match.group(1) if match else ""
    return legacy_source_id


def infer_album_id(service: str, row: dict[str, Any]) -> str:
    service = normalized_service(service)
    album_id = str(row.get("album_id") or "").strip()
    if album_id:
        return album_id
    track_url = str(row.get("url") or "").strip()
    if service == "apple" and "/album/" in track_url:
        before_query = track_url.split("?", 1)[0]
        return before_query.rstrip("/").split("/")[-1]
    return ""


def job_frequency(job_name: str, config_path: str | Path | None = None) -> str:
    for task in load_sync_config(config_path):
        if str(task.get("job_name") or "").strip() == str(job_name or "").strip():
            return str(task.get("frequency") or "daily").strip().lower() or "daily"
    name = (job_name or "").lower()
    if "weekly" in name or "week" in name:
        return "weekly"
    return "daily"


def job_list_type(job_name: str, config_path: str | Path | None = None) -> str:
    for task in load_sync_config(config_path):
        if str(task.get("job_name") or "").strip() == str(job_name or "").strip():
            return str(task.get("list_type") or "chart").strip().lower() or "chart"
    return "chart"


def job_service(job_name: str, config_path: str | Path | None = None) -> str:
    for task in load_sync_config(config_path):
        if str(task.get("job_name") or "").strip() == str(job_name or "").strip():
            return str(task.get("service") or "").strip().lower()
    return ""


def reference_period_for_date(job_name: str, chart_date: str, reference_period: str | None = None) -> str:
    frequency = job_frequency(job_name)
    list_type = job_list_type(job_name)
    service = job_service(job_name)
    if reference_period:
        value = str(reference_period).strip()
        if re.match(r"\d{4}-W\d{2}$", value):
            return value
        if re.match(r"\d{4}-\d{2}-\d{2}$", value):
            return value
    date_value = parse_crawl_date(chart_date) or kst_today()
    if list_type == "playlist":
        schedule_day = None
        for task in load_sync_config():
            if str(task.get("job_name") or "").strip() == str(job_name or "").strip():
                schedule_day = task.get("schedule")
                break
        if schedule_day:
            try:
                from datetime import datetime, timedelta
                dt = datetime.strptime(date_value, "%Y-%m-%d")
                day_map = {
                    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                    "friday": 4, "saturday": 5, "sunday": 6
                }
                target_wd = day_map.get(schedule_day.lower())
                if target_wd is not None:
                    current_wd = dt.weekday()
                    diff = (current_wd - target_wd) % 7
                    aligned_dt = dt - timedelta(days=diff)
                    return aligned_dt.strftime("%Y-%m-%d")
            except Exception as e:
                LOG.warning("Failed to align playlist schedule date: %s", e)
        return date_value

    if frequency == "weekly" and list_type == "chart":
        try:
            from datetime import datetime
            dt = datetime.strptime(date_value, "%Y-%m-%d")
            iso_year, iso_week, _ = dt.isocalendar()
            return f"{iso_year}-W{iso_week:02d}"
        except ValueError:
            return date_value

    if frequency == "daily" and list_type == "chart":
        if not reference_period and job_service(job_name) == "apple":
            try:
                from datetime import datetime, timedelta
                dt = datetime.strptime(date_value, "%Y-%m-%d")
                return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                pass
        return date_value

    return date_value


def parse_crawl_date(value: str | None) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    match = re.search(r"(\d{8})T(\d{6})Z", value)
    if match:
        dt = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d")
    return value[:10] if len(value) >= 10 else ""


def normalize_source_variant(value: str | None = "") -> str:
    value = str(value or "").strip()
    return value or "default"


def build_album_url(service: str, album_id: str | None) -> str:
    service = normalized_service(service)
    album_id = str(album_id or "").strip()
    if not album_id:
        return ""
    if service == "melon":
        return f"https://www.melon.com/album/detail.htm?albumId={album_id}"
    if service == "apple":
        return f"https://music.apple.com/album/{album_id}"
    if service == "spotify":
        return f"https://open.spotify.com/album/{album_id}"
    return ""


def build_track_url(service: str, song_id: str | None, album_id: str | None = "") -> str:
    service = normalized_service(service)
    song_id = str(song_id or "").strip()
    album_id = str(album_id or "").strip()
    if not song_id:
        return ""
    if service == "melon":
        return f"https://www.melon.com/song/detail.htm?songId={song_id}"
    if service == "spotify":
        return f"https://open.spotify.com/track/{song_id}"
    if service == "apple" and album_id:
        return f"https://music.apple.com/album/{album_id}?i={song_id}"
    return ""


def hype_identity_key(row: Any) -> str:
    """Return the aggregation key used by Hype reports.

    YouTube Music can resolve the same chart song to different video IDs across
    services. Hype output should still contain one entry per source song, while
    keeping true feature/version variants separate.
    """
    title = row["title"] or row["yt_title"] or ""
    artist = row["artist"] or row["yt_artist"] or ""
    title_key = normalize_text(clean_track_title(title))
    artist_key = normalize_text(artist)
    if not title_key or not artist_key:
        return f"video:{row['video_id']}"
    return "|".join(
        (
            "meta",
            title_key,
            artist_key,
            feature_signature(title),
            version_signature(title),
        )
    )


def _sync_config_path() -> Path:
    return Path(__file__).resolve().parent / "sync_config.json"


def load_sync_config(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else _sync_config_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))

def hype_inputs(config_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    config_list = load_sync_config(config_path)
    if not config_list:
        raise FileNotFoundError(
            "Configuration file 'sync_config.json' not found or empty. "
            "Please ensure sync_config.json exists in the root directory."
        )
    for task in config_list:
        if not task.get("include_in_hype"):
            continue
        job_name = str(task.get("job_name") or "").strip()
        if not job_name:
            continue
        group = str(task.get("hype_group") or "").strip()
        weight = float(task.get("hype_weight") or DEFAULT_HYPE_WEIGHTS.get(group, 1.0))
        out[job_name] = {**task, "hype_group": group, "hype_weight": weight}
    if not out:
        raise ValueError(
            "No jobs found with 'include_in_hype': true in the configuration file!"
        )
    return out


def match_method_for_status(status: str | None, query: str | None = "") -> tuple[str, str, bool]:
    status = (status or "").strip()
    query = (query or "").strip()
    if status == "manual_override":
        return "manual", "manual", False
    if status == "proxy_matched":
        return "proxy", "proxy", False
    if status == "cached_match":
        return "cache", "unknown", True
    if query == "db_cache":
        return "cache", "db", True
    if status in {"failed", "duplicate_skipped", "manual_blocked"}:
        return "failed", "none", False
    return "search", "search", False


def playlist_job_mappings(
    config_path: str | Path | None = None,
    *,
    include_config_playlist_names: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_job: dict[str, dict[str, Any]] = {}
    legacy_to_job: dict[str, str] = {}
    builtins = {
        "Apple-KR-Top-100": "KR-Top-100",
        "Apple-KR-Top-Songs": "KR-Top-Songs",
        "Apple-Seoul-Top-25": "Seoul-Top-25",
        "Apple-Busan-Top-25": "Busan-Top-25",
        "Melon-KR-Top-100-Daily": "Top-100-Daily",
        "Melon-KR-Top-100-Weekly": "Top-100-Weekly",
        "Melon-Gen-Z-Top-100-Daily": "Gen-Z-Daily",
        "Spotify-Hot-Hits-Korea": "Hot-Hits-Korea",
        "Spotify-Fresh-Indie-Korea": "Fresh-Indie-Korea",
        "Gen-10s-Top-100-Daily": "Gen-Z-Daily",
        "Gen-20s-Top-100-Daily": "Gen-Z-Daily",
    }
    for task in load_sync_config(config_path):
        job = str(task.get("job_name") or "").strip()
        if not job:
            continue
        by_job[job] = dict(task)
        legacy_to_job[job] = job
        legacy_playlist_name = str(task.get("playlist_name") or "").strip()
        if include_config_playlist_names and legacy_playlist_name:
            legacy_to_job[legacy_playlist_name] = job
    legacy_to_job.update(builtins)
    return by_job, legacy_to_job


def normalize_job_name(value: str | None, *, config_path: str | Path | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    _, legacy_to_job = playlist_job_mappings(config_path)
    return legacy_to_job.get(raw, raw)


def legacy_to_job_name(value: str | None, *, config_path: str | Path | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    _, legacy_to_job = playlist_job_mappings(config_path, include_config_playlist_names=True)
    return legacy_to_job.get(raw, raw)


def require_job_name(value: str | None) -> str:
    job_name = normalize_job_name(value)
    if not job_name:
        raise ValueError("job_name is required for DB persistence")
    return job_name


def _source_variant_from_legacy(value: str, current: str = "") -> str:
    if value == "Gen-10s-Top-100-Daily" or value.endswith(":gen10"):
        return "gen10"
    if value == "Gen-20s-Top-100-Daily" or value.endswith(":gen20"):
        return "gen20"
    return normalize_source_variant(current)
