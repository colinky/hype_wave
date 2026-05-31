from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from ytmusicapi import YTMusic

try:
    from ytmusicapi.auth.oauth import OAuthCredentials
except ImportError:  # pragma: no cover - compatibility with older ytmusicapi.
    OAuthCredentials = None


LOG = logging.getLogger("ytmusic_playlist_sync")


def get_resilient_session(retries: int = 3, backoff_factor: float = 0.3) -> requests.Session:
    """
    자동 재시도와 백오프가 탑재된 HTTP Session 객체 생성.
    """
    session = requests.Session()
    retry_policy = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@lru_cache(maxsize=8192)
def normalize_text(value: str) -> str:
    """
    텍스트 정규화: 다국어 환경에서 안정적인 매칭을 위해 노이즈를 제거합니다.
    1. NFKC 정규화 및 소문자 변환
    2. 피처링(feat, ft) 정보 제거 (곡 제목 본연의 의미 유지)
    3. 일반적인 접미사(EP, Single) 및 특수문자 제거
    4. CJK(한글, 일어, 한자) 및 영문/숫자만 남김
    """
    value = unicodedata.normalize("NFKC", value).lower()
    # Remove "- Topic" and "- 주제" suffixes (often added to official artist channel names)
    value = re.sub(r"\s*-\s*(topic|주제)\b.*$", " ", value, flags=re.IGNORECASE)
    # 괄호 안의 피처링 정보 제거
    value = re.sub(r"\([^)]*(feat\.?|ft\.?)[^)]*\)", " ", value)
    value = re.sub(r"\[[^\]]*(feat\.?|ft\.?)[^\]]*\]", " ", value)
    value = re.sub(r"\b(feat\.?|ft\.?)\b.*$", " ", value)
    value = re.sub(r"\s*-\s*(ep|single)\b.*$", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^0-9a-z가-힣\u3040-\u30ff\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class AliasManager:
    """
    플랫폼별로 다른 아티스트/곡명/앨범명 표기법을 관리합니다.
    matching_alias.json의 설정을 기반으로 하며, overrides를 통해 특정 곡을 강제 매칭할 수 있습니다.
    """
    def __init__(self, path: str | Path | None = None):
        self.artist_map: dict[str, list[str]] = {}
        self.title_map: dict[str, list[str]] = {}
        self.album_map: dict[str, list[str]] = {}
        self.overrides: dict[str, str] = {}
        
        if path is None:
            path = Path(__file__).parent / "matching_alias.json"
        self.load(path)

    def load(self, path: str | Path):
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._build_map(data.get("artists", []), self.artist_map)
            self._build_map(data.get("titles", []), self.title_map)
            self._build_map(data.get("albums", []), self.album_map)

            # Overrides 조회 시 정확한 비교를 위해 키를 정규화하여 저장
            self.overrides = {}
            for raw_key, video_id in data.get("overrides", {}).items():
                if "|" in raw_key:
                    parts = raw_key.split("|")
                    norm_key = "|".join(normalize_text(p) for p in parts)
                    self.overrides[norm_key] = video_id
                else:
                    self.overrides[normalize_text(raw_key)] = video_id
        except Exception as e:
            LOG.warning("Failed to load alias file %s: %s", path, e)

    def _build_map(self, clusters: list[list[str]], target_map: dict[str, list[str]]):
        for cluster in clusters:
            cleaned_cluster = [c.strip() for c in cluster if c.strip()]
            for item in cleaned_cluster:
                key = normalize_text(item)
                if key:
                    target_map[key] = list(set(target_map.get(key, []) + cleaned_cluster))

    def get_variants(self, value: str, category: str) -> list[str]:
        if not value:
            return []
        key = normalize_text(value)
        mapping = getattr(self, f"{category}_map", {})
        # Return a copy to prevent mutation of the cached alias mapping
        return list(mapping.get(key, [value]))


ALIASES = AliasManager()

# Global cache for dynamically resolved artist names (bilingual matching)
ARTIST_ID_CACHE: dict[str, list[str]] = {}
_YTMUSIC_EN_INSTANCE: YTMusic | None = None

def get_ytmusic_en(yt_ko: YTMusic) -> YTMusic:
    global _YTMUSIC_EN_INSTANCE
    if _YTMUSIC_EN_INSTANCE is None:
        try:
            yt_en = YTMusic(language="en")
            yt_en.headers.update(yt_ko.headers)
            yt_en.headers.update({"Accept-Language": "en-US,en;q=0.9"})
            _YTMUSIC_EN_INSTANCE = yt_en
        except Exception as e:
            LOG.warning("Failed to clone English YTMusic instance: %s", e)
            _YTMUSIC_EN_INSTANCE = yt_ko
    return _YTMUSIC_EN_INSTANCE

@dataclass
class SourceTrack:
    """
    동기화 소스(Apple, Spotify, Melon 등)로부터 수집된 원본 곡 정보를 저장하는 클래스입니다.
    유튜브 뮤직 검색 및 매칭의 기준 데이터로 활용됩니다.
    """
    rank: int
    title: str
    artist: str
    service: str = ""
    album: str = ""
    source: str = "track_lockup"
    artwork_url: str = ""
    song_id: str = ""
    album_id: str = ""


@dataclass
class MatchResult:
    """
    유튜브 뮤직 검색 및 매칭 결과를 담는 데이터 클래스입니다.
    소스 서비스의 메타데이터와 유튜브에서 검색된 실제 정보, 
    그리고 매칭의 신뢰도를 나타내는 각종 점수들을 포함합니다.
    """
    rank: int
    title: str
    artist: str
    album: str
    service: str = ""
    title_en: str = ""      # 영문 곡 제목 (매칭 보조용)
    artist_en: str = ""     # 영문 아티스트 명 (매칭 보조용)
    album_en: str = ""      # 영문 앨범 명 (매칭 보조용)
    title_ko: str = ""      # 국문 곡 제목 (매칭 보조용)
    artist_ko: str = ""     # 국문 아티스트 명 (매칭 보조용)
    album_ko: str = ""      # 국문 앨범 명 (매칭 보조용)
    song_id: str = ""       # 내부 표준 소스 곡 ID
    album_id: str = ""
    artwork_url: str = ""   # 앨범 아트워크 이미지 URL
    video_id: str | None = None # 매칭된 유튜브 뮤직 비디오 ID
    yt_title: str = ""      # 유튜브 검색 결과 제목
    yt_artist: str = ""     # 유튜브 검색 결과 아티스트
    yt_album: str = ""      # 유튜브 검색 결과 앨범
    score: float = 0.0      # 최종 합산 매칭 점수
    title_score: float = 0.0 # 제목 유사도 점수
    artist_score: float = 0.0 # 아티스트 유사도 점수
    album_score: float = 0.0 # 앨범 유사도 점수
    yt_result_type: str = "" # 결과 타입 (song, video 등)
    query: str = ""         # 매칭에 사용된 검색 쿼리
    status: str = "failed"  # 매칭 상태 (matched, failed, proxy_matched 등)


def match_from_prev(
    track: SourceTrack,
    prev: dict[str, Any],
    *,
    track_ko: SourceTrack | None = None,
    status: str = "cached_match",
) -> MatchResult:
    """Build a MatchResult reusing data from a previous match (cache or proxy)."""
    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        service=track.service,
        title_en=prev.get("title_en", track.title),
        artist_en=prev.get("artist_en", track.artist),
        album_en=prev.get("album_en", track.album),
        title_ko=track_ko.title if track_ko else prev.get("title_ko", ""),
        artist_ko=track_ko.artist if track_ko else prev.get("artist_ko", ""),
        album_ko=track_ko.album if track_ko else prev.get("album_ko", ""),
        song_id=track.song_id,
        album_id=track.album_id,
        artwork_url=track.artwork_url,
        video_id=prev["video_id"],
        yt_title=prev.get("yt_title", ""),
        yt_artist=prev.get("yt_artist", ""),
        yt_album=prev.get("yt_album", ""),
        score=prev.get("score", 0.0),
        title_score=prev.get("title_score", 0.0),
        artist_score=prev.get("artist_score", 0.0),
        album_score=prev.get("album_score", 0.0),
        yt_result_type=prev.get("yt_result_type", "song"),
        query=prev.get("query", status),
        status=status,
    )


def load_dotenv(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_or_arg(value: str | None, env_name: str, *, required: bool = True) -> str:
    resolved = value or os.environ.get(env_name, "")
    if required and not resolved:
        raise SystemExit(f"Missing required value: --{env_name.lower().replace('_', '-')} or {env_name}")
    return resolved


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


@lru_cache(maxsize=1024)
def split_artist_names(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&| and | x | X | with |\+)\s*|\s+(?:및|와|과)\s+", value)
    return [part.strip() for part in parts if part.strip()]


@lru_cache(maxsize=1024)
def _extract_parentheses_variants(value: str) -> list[str]:
    if not value: return []
    variants = [value]
    # 괄호나 구분자(/, |)를 기준으로 원곡명과 부제목을 분리하여 변형 생성
    match = re.search(r"^(.*?)\s*[\(\[/\uFF08]\s*(.*?)\s*[\)\]/\uFF09]\s*$", value)
    if match:
        main, sub = match.groups()
        if main: variants.append(main.strip())
        if sub: variants.append(sub.strip())
    
    # Try splitting by common delimiters if no parentheses match
    if len(variants) == 1:
        for delim in [" / ", " | ", " - "]:
            if delim in value:
                parts = value.split(delim)
                variants.extend([p.strip() for p in parts if p.strip()])
                break
    return variants


class BilingualCache:
    """
    YouTube Music API 호출 횟수를 줄이기 위해 아티스트 채널 번역과 
    비디오 번역 정보를 SQLite 데이터베이스에 영구 캐싱합니다.
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS artist_translations (
                        artist_id TEXT PRIMARY KEY,
                        names TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS song_translations (
                        video_id TEXT PRIMARY KEY,
                        title_ko TEXT,
                        title_en TEXT,
                        artist_ko TEXT,
                        artist_en TEXT,
                        album_ko TEXT,
                        album_en TEXT,
                        updated_at TEXT NOT NULL
                    )
                """)
        except Exception as e:
            LOG.error("Failed to initialize BilingualCache database %s: %s", self.db_path, e)

    def get_artist(self, artist_id: str) -> list[str] | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT names FROM artist_translations WHERE artist_id = ?",
                    (artist_id,)
                ).fetchone()
                if row:
                    return json.loads(row[0])
        except Exception as e:
            LOG.warning("Failed to get artist from cache: %s", e)
        return None

    def set_artist(self, artist_id: str, names: list[str]):
        from datetime import datetime, timezone
        try:
            names_json = json.dumps(names)
            now_str = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO artist_translations (artist_id, names, updated_at) VALUES (?, ?, ?)",
                    (artist_id, names_json, now_str)
                )
        except Exception as e:
            LOG.warning("Failed to set artist in cache: %s", e)

    def get_song(self, video_id: str) -> dict[str, str] | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT title_ko, title_en, artist_ko, artist_en, album_ko, album_en 
                    FROM song_translations WHERE video_id = ?
                    """,
                    (video_id,)
                ).fetchone()
                if row:
                    return {
                        "title_ko": row[0] or "",
                        "title_en": row[1] or "",
                        "artist_ko": row[2] or "",
                        "artist_en": row[3] or "",
                        "album_ko": row[4] or "",
                        "album_en": row[5] or ""
                    }
        except Exception as e:
            LOG.warning("Failed to get song from cache: %s", e)
        return None


    def set_song(self, video_id: str, details: dict[str, str]):
        from datetime import datetime, timezone
        try:
            now_str = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO song_translations 
                    (video_id, title_ko, title_en, artist_ko, artist_en, album_ko, album_en, updated_at) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video_id,
                        details.get("title_ko", ""),
                        details.get("title_en", ""),
                        details.get("artist_ko", ""),
                        details.get("artist_en", ""),
                        details.get("album_ko", ""),
                        details.get("album_en", ""),
                        now_str
                    )
                )
        except Exception as e:
            LOG.warning("Failed to set song in cache: %s", e)


BILINGUAL_CACHE = BilingualCache(Path(__file__).parent / "ytmusic_cache.db")


def resolve_bilingual_artist(yt_ko: YTMusic, artist_id: str) -> list[str]:
    cached = BILINGUAL_CACHE.get_artist(artist_id)
    if cached is not None:
        return cached

    names = []
    try:
        a_ko = yt_ko.get_artist(artist_id)
        if a_ko.get("name"):
            names.append(a_ko["name"])
            names.extend(_extract_parentheses_variants(a_ko["name"]))
    except Exception as e:
        LOG.debug("Failed to get KO artist for %s: %s", artist_id, e)

    try:
        yt_en = get_ytmusic_en(yt_ko)
        a_en = yt_en.get_artist(artist_id)
        if a_en.get("name"):
            names.append(a_en["name"])
            names.extend(_extract_parentheses_variants(a_en["name"]))
    except Exception as e:
        LOG.debug("Failed to get EN artist for %s: %s", artist_id, e)

    unique_names = unique_values([n.strip() for n in names if n.strip()])
    if unique_names:
        BILINGUAL_CACHE.set_artist(artist_id, unique_names)
    return unique_names


def resolve_bilingual_song(yt_ko: YTMusic, video_id: str) -> dict[str, str]:
    cached = BILINGUAL_CACHE.get_song(video_id)
    if cached is not None:
        return cached

    details = {
        "title_ko": "",
        "title_en": "",
        "artist_ko": "",
        "artist_en": "",
        "album_ko": "",
        "album_en": ""
    }
    
    # 1. KO locale
    try:
        playlist_ko = yt_ko.get_watch_playlist(videoId=video_id)
        tracks_ko = playlist_ko.get("tracks", [])
        if tracks_ko:
            track = tracks_ko[0]
            details["title_ko"] = track.get("title", "")
            artists = track.get("artists", [])
            details["artist_ko"] = ", ".join(a.get("name", "") for a in artists if a.get("name") and a.get("id") is not None)
            album_obj = track.get("album")
            if album_obj:
                details["album_ko"] = album_obj.get("name", "")
    except Exception as e:
        LOG.debug("Failed to get KO song details for %s: %s", video_id, e)

    # 2. EN locale
    try:
        yt_en = get_ytmusic_en(yt_ko)
        playlist_en = yt_en.get_watch_playlist(videoId=video_id)
        tracks_en = playlist_en.get("tracks", [])
        if tracks_en:
            track = tracks_en[0]
            details["title_en"] = track.get("title", "")
            artists = track.get("artists", [])
            details["artist_en"] = ", ".join(a.get("name", "") for a in artists if a.get("name") and a.get("id") is not None)
            album_obj = track.get("album")
            if album_obj:
                details["album_en"] = album_obj.get("name", "")
    except Exception as e:
        LOG.debug("Failed to get EN song details for %s: %s", video_id, e)

    if details["title_ko"] or details["title_en"]:
        BILINGUAL_CACHE.set_song(video_id, details)
        
    return details


def artist_variants(artist: str) -> list[str]:
    # 1. Start with the direct name and its aliases
    base_variants = ALIASES.get_variants(artist, "artist")
    
    # 2. Add split artist parts (e.g. "A & B" -> ["A", "B"]) and their aliases
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
        parts = split_artist_names(v)
        for part in parts:
            expanded.extend(_extract_parentheses_variants(part))
            expanded.append(part)
            
    return unique_values(expanded)


def title_variants(title: str) -> list[str]:
    base_variants = ALIASES.get_variants(title, "title")
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
    return unique_values(expanded)


def album_variants(album: str) -> list[str]:
    if not album: return []
    base_variants = ALIASES.get_variants(album, "album")
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
    return unique_values(expanded)


def similarity(left: str, right: str, is_title: bool = False) -> float:
    """
    두 텍스트 간의 유사도를 측정합니다.
    - 부분 일치(Subset matching) 감지: "Song (English Ver.)"과 "Song" 매칭 시 0.95 부여
    - CJK 문자 특화: 한국어/일본어 등은 짧은 단어라도 정보량이 많으므로 더 낮은 길이 임계값 적용
    - 토큰 기반 및 서퀀스 기반 유사도 결합
    """
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        if is_title:
            def extract_feat(text: str) -> str:
                # Match (feat. X) or [feat. X]
                match = re.search(r"\((?:feat\.?|ft\.?)\s*([^)]+)\)", text, re.IGNORECASE)
                if not match:
                    match = re.search(r"\[(?:feat\.?|ft\.?)\s*([^\]]+)\]", text, re.IGNORECASE)
                if not match:
                    match = re.search(r"\b(?:feat\.?|ft\.?)\s*(.+)$", text, re.IGNORECASE)
                return normalize_text(match.group(1)) if match else ""

            feat_l = extract_feat(left)
            feat_r = extract_feat(right)
            if feat_l != feat_r:
                # Mismatch in featuring artist: apply penalty (0.85 instead of 1.0)
                return 0.85
        return 1.0

    # 1-2 digit number mismatch check (e.g. "Part 1" vs "Part 2", "Untitled 08" vs "Untitled 07")
    left_digits = {str(int(d)) for d in re.findall(r'(?<!\d)\d{1,2}(?!\d)', left_norm)}
    right_digits = {str(int(d)) for d in re.findall(r'(?<!\d)\d{1,2}(?!\d)', right_norm)}
    if left_digits and right_digits and not left_digits.intersection(right_digits):
        return 0.0

    if is_title and left_norm in right_norm:
        if right_norm.startswith(left_norm):
            remaining = right_norm[len(left_norm):].strip()
            # Don't treat as subset match if remaining contains a version/variant specifier.
            # e.g. source='BOOMPALA', candidate='BOOMPALA (KIM CHAEWON ver.)' → different recording.
            _is_variant_suffix = bool(re.search(
                r"\b(ver\.?|version|edition|remix|inst\.?|instrumental|cover|arrange|feat\.?|ft\.?)\b",
                remaining, re.IGNORECASE,
            ))
            if not remaining or (
                not _is_variant_suffix
                and all(ord(c) < 128 or c.isspace() for c in remaining)
            ):
                return 0.95


    from collections import Counter
    left_counts = Counter(left_norm.split())
    right_counts = Counter(right_norm.split())
    if left_counts and right_counts:
        intersection_counts = left_counts & right_counts
        intersection_len = sum(intersection_counts.values())
        left_len = sum(left_counts.values())
        right_len = sum(right_counts.values())
        containment = intersection_len / left_len
        coverage = intersection_len / right_len
        token_score = (containment * 0.8) + (coverage * 0.2)
    else:
        token_score = 0.0

    seq_score = SequenceMatcher(None, left_norm, right_norm).ratio()

    l_no_space, r_no_space = left_norm.replace(" ", ""), right_norm.replace(" ", "")
    no_space_score = 1.0 if l_no_space == r_no_space and l_no_space else 0.0

    if left_norm in right_norm or right_norm in left_norm:
        short, long = (left_norm, right_norm) if len(left_norm) < len(right_norm) else (right_norm, left_norm)
        len_ratio = len(short) / len(long)
        is_cjk = any('\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7af' for c in short)
        threshold = 2 if is_cjk else 4
        
        if len(short) >= threshold:
            if is_cjk:
                substr_score = max(0.85, 0.85 * (len_ratio ** 0.5))
            else:
                substr_score = 0.85 * (len_ratio ** 0.5)
        else:
            substr_score = 0.85 * (len_ratio ** 0.5)
    else:
        substr_score = 0.0

    return max(token_score, seq_score, substr_score, no_space_score)


def duration_to_seconds(value: str | None) -> int:
    if not value:
        return 0
    total = 0
    for part in str(value).split(":"):
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


def ytmusic_url(video_id: str | None) -> str:
    return f"https://music.youtube.com/watch?v={video_id}" if video_id else ""


def normalize_video_title(title: str | None) -> str:
    value = re.sub(
        r"\b(official|mv|m/v|music video|live|stage|performance|lyrics?)\b",
        " ",
        title or "",
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def ytmusic_result_type(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in {"song", "노래"}:
        return "song"
    if value in {"video", "동영상"}:
        return "video"
    return value


def resolve_video_to_song(
    ytmusic: YTMusic,
    *,
    video_id: str,
    title: str,
    artist: str = "",
    duration_seconds: int = 0,
    threshold: float = 0.86,
    search_limit: int = 10,
) -> dict[str, Any]:
    normalized_title = normalize_video_title(title)
    if not normalized_title:
        return {
            "resolved_video_id": video_id,
            "resolved_title": title,
            "resolved_artist": artist,
            "resolved_album": "",
            "mapping_status": "kept_original_video",
            "mapping_reason": "empty_normalized_title",
            "mapping_score": 1.0,
        }
    query = " ".join(part for part in [normalized_title, artist] if part).strip()
    best: dict[str, Any] | None = None
    best_score = 0.0
    best_threshold = threshold
    for idx, result in enumerate(
        search_ytmusic_songs(
            ytmusic,
            query,
            search_limit,
            track_title=normalized_title,
            track_artist=artist,
        )
    ):
        candidate_artist = result_artists(result)
        cand_title = result.get("title", "")
        artist_score = similarity(artist, candidate_artist, is_title=False)
        
        # 상위 2개 후보이거나 아티스트 일치도가 0.7 이상인 경우 다국어 교차 제목 비교로 보정
        if idx < 2 or artist_score >= 0.7:
            candidate_id = result.get("videoId")
            if candidate_id:
                try:
                    resolved_details = resolve_bilingual_song(ytmusic, candidate_id)
                    titles_to_check = [cand_title]
                    if resolved_details.get("title_ko"):
                        titles_to_check.append(resolved_details["title_ko"])
                    if resolved_details.get("title_en"):
                        titles_to_check.append(resolved_details["title_en"])
                    
                    title_score = max(
                        (similarity(normalized_title, t, is_title=True) for t in titles_to_check),
                        default=0.0
                    )
                    
                    # 아티스트 스코어도 다국어 정보로 보정
                    artists_to_check = [candidate_artist]
                    if resolved_details.get("artist_ko"):
                        artists_to_check.append(resolved_details["artist_ko"])
                    if resolved_details.get("artist_en"):
                        artists_to_check.append(resolved_details["artist_en"])
                    artist_score = max(
                        (similarity(artist, a, is_title=False) for a in artists_to_check),
                        default=artist_score
                    )
                except Exception:
                    title_score = similarity(normalized_title, cand_title, is_title=True)
            else:
                title_score = similarity(normalized_title, cand_title, is_title=True)
        else:
            title_score = similarity(normalized_title, cand_title, is_title=True)

        score = title_score * 0.7 + artist_score * 0.3 if artist else title_score * 0.7 + 0.3
        result_type = ytmusic_result_type(result.get("resultType"))
        if result_type == "song":
            score += 0.05
        elif result_type == "video":
            score -= 0.15
        result_text = " ".join(
            [
                str(result.get("title") or ""),
                str(result_album(result) or ""),
                str(candidate_artist or ""),
            ]
        ).lower()
        if re.search(r"\b(live|mv|m/v|music video|performance|stage|clip|shorts?|broadcast)\b", result_text):
            score -= 0.25
            
        current_threshold = threshold
        if result_type == "song" and similarity(artist, candidate_artist, is_title=False) >= 0.8:
            current_threshold = min(threshold, 0.75)

        if score > best_score:
            best = result
            best_score = score
            best_threshold = current_threshold
    if best and best.get("videoId") and best_score >= best_threshold:
        return {
            "resolved_video_id": best.get("videoId"),
            "resolved_title": best.get("title", ""),
            "resolved_artist": result_artists(best),
            "resolved_album": result_album(best),
            "mapping_status": "resolved_to_song" if best.get("videoId") != video_id else "already_song",
            "mapping_reason": query,
            "mapping_score": round(best_score, 3),
        }
    return {
        "resolved_video_id": video_id,
        "resolved_title": title,
        "resolved_artist": artist,
        "resolved_album": result_album(best) if best else "",
        "mapping_status": "kept_original_video" if best_score else "failed",
        "mapping_reason": query,
        "mapping_score": round(best_score, 3),
    }


def result_artists(result: dict[str, Any]) -> str:
    """유튜브 검색 결과에서 아티스트 명칭을 추출합니다."""
    artists = result.get("artists")
    if artists:
        return " ".join(a.get("name", "") for a in artists)
    
    # Fallback to 'author' or 'podcast' for videos/episodes
    author = result.get("author")
    if author:
        return author if isinstance(author, str) else author.get("name", "")
    
    podcast = result.get("podcast")
    if podcast:
        return podcast.get("name", "")
        
    return ""


def result_album(result: dict[str, Any]) -> str:
    album = result.get("album")
    if album:
        return album if isinstance(album, str) else album.get("name", "")
    
    # Check 'playlist' field (used for some result types)
    playlist = result.get("playlist")
    if playlist:
        return playlist.get("name", "")
        
    return ""


def is_song_result(result: dict[str, Any]) -> bool:
    # Strictly allow only songs and videos. Episodes (TV clips) are forbidden.
    allowed_types = ["song", "video", "노래", "동영상"]
    return result.get("resultType") in allowed_types and bool(result.get("videoId"))


def score_result(
    track_en: SourceTrack,
    result: dict[str, Any],
    track_ko: SourceTrack | None = None,
    ytmusic: YTMusic | None = None,
    *,
    force_resolve: bool = False,
) -> tuple[float, float, float, float]:
    yt_title = result.get("title", "")
    yt_album = result_album(result)
    yt_artists = result_artists(result)
    video_id = result.get("videoId")
    
    # 1. Target variants
    target_title_variants = []
    if track_en.title:
        target_title_variants.extend(title_variants(track_en.title))
    if track_ko and track_ko.title:
        target_title_variants.extend(title_variants(track_ko.title))
    target_title_variants = unique_values(target_title_variants)

    target_artist_variants = []
    if track_en.artist:
        target_artist_variants.extend(artist_variants(track_en.artist))
    if track_ko and track_ko.artist:
        target_artist_variants.extend(artist_variants(track_ko.artist))
    target_artist_variants = unique_values(target_artist_variants)

    # 2. Raw scoring (First-pass)
    cand_title_variants_raw = []
    if yt_title:
        cand_title_variants_raw.extend(title_variants(yt_title))
    cand_title_variants_raw = unique_values(cand_title_variants_raw)

    raw_title_score = max(
        (similarity(target_v, cand_v, is_title=True) for target_v in target_title_variants for cand_v in cand_title_variants_raw),
        default=0.0
    )

    cand_artist_names_raw = []
    if result.get("artists"):
        for a in result["artists"]:
            if a.get("name"):
                cand_artist_names_raw.append(a["name"])
    if not cand_artist_names_raw and yt_artists:
        cand_artist_names_raw.append(yt_artists)

    cand_artist_variants_raw = []
    for art in cand_artist_names_raw:
        cand_artist_variants_raw.extend(artist_variants(art))
    cand_artist_variants_raw = unique_values(cand_artist_variants_raw)

    raw_artist_score = max(
        (similarity(target_v, cand_v, is_title=False) for target_v in target_artist_variants for cand_v in cand_artist_variants_raw),
        default=0.0
    )

    # Album scoring (Common to raw and final)
    album_score_en = max(
        (similarity(av, yt_album, is_title=False) for av in album_variants(track_en.album)),
        default=0.0
    ) if track_en.album else 0.0

    album_score_ko = max(
        (similarity(av, yt_album, is_title=False) for av in album_variants(track_ko.album)),
        default=0.0
    ) if track_ko and track_ko.album else 0.0

    title_is_album = False
    if yt_album:
        yt_album_norm = normalize_text(yt_album)
        title_is_album = any(normalize_text(tv) == yt_album_norm for tv in target_title_variants)

    album_score = max(album_score_en, album_score_ko, 0.9 if title_is_album else 0.0)
    if track_en.album or (track_ko and track_ko.album):
        album_multiplier = 0.9 if not yt_album else (0.7 + (album_score * 0.3))
    else:
        album_multiplier = 1.0

    raw_score = raw_title_score * raw_artist_score * album_multiplier

    # 3. Bilingual resolution decision
    should_resolve = force_resolve or (raw_score >= 0.4) or (raw_title_score >= 0.7) or (raw_artist_score >= 0.7)
    resolved_details = {}
    
    if should_resolve and ytmusic and video_id:
        try:
            resolved_details = resolve_bilingual_song(ytmusic, video_id)
        except Exception as e:
            LOG.debug("Failed to resolve bilingual song details for %s: %s", video_id, e)

    # 4. Final title and artist scoring
    if resolved_details:
        cand_titles = [yt_title]
        if resolved_details.get("title_ko"):
            cand_titles.append(resolved_details["title_ko"])
        if resolved_details.get("title_en"):
            cand_titles.append(resolved_details["title_en"])
            
        cand_title_variants = []
        for t in cand_titles:
            cand_title_variants.extend(title_variants(t))
        cand_title_variants = unique_values(cand_title_variants)
        
        title_score = max(
            (similarity(target_v, cand_v, is_title=True) for target_v in target_title_variants for cand_v in cand_title_variants),
            default=0.0
        )
        
        cand_artists = list(cand_artist_names_raw)
        if resolved_details.get("artist_ko"):
            cand_artists.append(resolved_details["artist_ko"])
        if resolved_details.get("artist_en"):
            cand_artists.append(resolved_details["artist_en"])
            
        if result.get("artists"):
            for a in result["artists"]:
                a_id = a.get("id")
                if a_id:
                    try:
                        channel_names = resolve_bilingual_artist(ytmusic, a_id)
                        cand_artists.extend(channel_names)
                    except Exception as e:
                        LOG.debug("Failed to resolve bilingual artist %s: %s", a_id, e)
                        
        cand_artist_variants = []
        for art in cand_artists:
            cand_artist_variants.extend(artist_variants(art))
        cand_artist_variants = unique_values(cand_artist_variants)
        
        artist_score = max(
            (similarity(target_v, cand_v, is_title=False) for target_v in target_artist_variants for cand_v in cand_artist_variants),
            default=0.0
        )
        
        # Recalculate album score with resolved bilingual album names
        res_album_ko = resolved_details.get("album_ko")
        res_album_en = resolved_details.get("album_en")
        if res_album_ko or res_album_en:
            res_album_score_en = max(
                (similarity(av, res_album_en, is_title=False) for av in album_variants(track_en.album)),
                default=0.0
            ) if (track_en.album and res_album_en) else 0.0
            res_album_score_ko = max(
                (similarity(av, res_album_ko, is_title=False) for av in album_variants(track_ko.album)),
                default=0.0
            ) if (track_ko and track_ko.album and res_album_ko) else 0.0
            
            # Keep the highest album score found so far
            album_score = max(album_score, res_album_score_en, res_album_score_ko)
            
            # Recheck title_is_album
            for res_alb in [res_album_en, res_album_ko]:
                if res_alb:
                    res_alb_norm = normalize_text(res_alb)
                    if any(normalize_text(tv) == res_alb_norm for tv in target_title_variants):
                        album_score = max(album_score, 0.9)
                        
            has_resolved_album = bool(res_album_en or res_album_ko)
            if track_en.album or (track_ko and track_ko.album):
                album_multiplier = 0.9 if (not yt_album and not has_resolved_album) else (0.7 + (album_score * 0.3))
            else:
                album_multiplier = 1.0
    else:
        title_score = raw_title_score
        artist_score = raw_artist_score
        cand_artist_variants = cand_artist_variants_raw

    if not track_en.artist and (not track_ko or not track_ko.artist):
        score = title_score * album_multiplier
        return score, title_score, 0.0, album_score

    score = title_score * artist_score * album_multiplier

    # 5. Version mismatch penalty (Preview, Teaser, Instrumental, etc.)
    neg_markers = [
        "preview", "teaser", "instrumental", "inst", "karaoke", "performance",
        "acoustic", "live", "sped up", "slowed", "remix", "clip", "broadcast",
        "cover", "커버", "tribute", "fanmade", "mashup",
        "japanese", "japanese ver", "japanese version", "jp ver", "jp version",
        "chinese", "chinese ver", "chinese version", "cn ver", "cn version",
        "english ver", "english version", "eng ver", "eng version",
        "ver", "version"
    ]
    yt_combined = (yt_title + " " + yt_album).lower()
    if resolved_details:
        yt_combined += " " + (resolved_details.get("title_ko", "") + " " + resolved_details.get("title_en", "")).lower()

    apple_combined = (track_en.title + " " + track_en.artist + " " + track_en.album).lower()
    if track_ko:
        apple_combined += " " + (track_ko.title + " " + track_ko.artist + " " + track_ko.album).lower()

    for marker in neg_markers:
        # For ASCII markers, use word boundaries to avoid false positives (e.g. "very" containing "ver")
        is_ascii = bool(re.match(r"^[a-z0-9\s]+$", marker))
        if is_ascii:
            pattern = r"\b" + re.escape(marker) + r"\b"
            has_yt = bool(re.search(pattern, yt_combined))
            has_apple = bool(re.search(pattern, apple_combined))
        else:
            has_yt = marker in yt_combined
            has_apple = marker in apple_combined

        if has_yt and not has_apple:
            if marker in {
                "cover", "커버", "tribute", "fanmade", "mashup",
                "japanese", "japanese ver", "japanese version", "jp ver", "jp version",
                "chinese", "chinese ver", "chinese version", "cn ver", "cn version",
                "english ver", "english version", "eng ver", "eng version",
                "ver", "version"
            }:
                score *= 0.1
            else:
                score *= 0.5
            break

    # 6. Short artist name strict check to prevent SequenceMatcher false positives
    is_short_artist = (track_en.artist and len(track_en.artist.strip()) <= 5) or (track_ko and track_ko.artist and len(track_ko.artist.strip()) <= 5)
    if is_short_artist:
        source_artists_tokens = set()
        for sa in target_artist_variants:
            source_artists_tokens.update(normalize_text(sa).split())
            
        yt_artists_tokens = set()
        for art_v in cand_artist_variants:
            yt_artists_tokens.update(normalize_text(art_v).split())
            
        if source_artists_tokens and yt_artists_tokens:
            intersection = source_artists_tokens.intersection(yt_artists_tokens)
            if not intersection:
                score *= 0.1

    # 7. Artist completeness and featuring artist mismatch checks
    # 7.1. Target individual artists groups (including aliases and parentheses variants)
    target_groups = []
    target_individual = []
    if track_en.artist:
        target_individual.extend(split_artist_names(track_en.artist))
    if track_ko and track_ko.artist:
        target_individual.extend(split_artist_names(track_ko.artist))
        
    for part in unique_values(target_individual):
        variants = ALIASES.get_variants(part, "artist")
        expanded = []
        for v in variants:
            expanded.extend(_extract_parentheses_variants(v))
            expanded.append(v)
        target_groups.append({normalize_text(v) for v in unique_values(expanded) if v})


    # 7.2. Candidate individual artists groups (resolved via channel IDs where possible)
    cand_groups = []
    seen_artist_ids = set()
    if result.get("artists"):
        for a in result["artists"]:
            if a.get("name"):
                a_name = a["name"]
                a_name_norm = normalize_text(a_name)
                # Ignore noise items
                if a_name_norm and ("조회수" in a_name_norm or "views" in a_name_norm or "topic" in a_name_norm or "주제" in a_name_norm):
                    continue
                
                names = [a_name]
                a_id = a.get("id")
                if a_id and a_id not in seen_artist_ids:
                    seen_artist_ids.add(a_id)
                    if ytmusic:
                        try:
                            channel_names = resolve_bilingual_artist(ytmusic, a_id)
                            names.extend(channel_names)
                        except Exception:
                            pass
                cand_groups.append({normalize_text(n) for n in names if n})

    # Fallback to resolved bilingual artist names if candidate groups are empty
    if not cand_groups:
        cand_flat = []
        if resolved_details:
            if resolved_details.get("artist_ko"):
                cand_flat.extend(split_artist_names(resolved_details["artist_ko"]))
            if resolved_details.get("artist_en"):
                cand_flat.extend(split_artist_names(resolved_details["artist_en"]))
        if not cand_flat and yt_artists:
            cand_flat.extend(split_artist_names(yt_artists))
            
        for part in unique_values(cand_flat):
            part_norm = normalize_text(part)
            if part_norm and not ("조회수" in part_norm or "views" in part_norm or "topic" in part_norm or "주제" in part_norm):
                cand_groups.append({part_norm})

    # Compare artist groups if both lists are non-empty
    if target_groups and cand_groups:
        matched_targets = 0
        for t_group in target_groups:
            is_matched = False
            for c_group in cand_groups:
                if any(similarity(t_v, c_v, is_title=False) >= 0.85 for t_v in t_group for c_v in c_group):
                    is_matched = True
                    break
            if is_matched:
                matched_targets += 1
                
        matched_cands = 0
        unmatched_cands = 0
        for c_group in cand_groups:
            is_matched = False
            for t_group in target_groups:
                if any(similarity(t_v, c_v, is_title=False) >= 0.85 for t_v in t_group for c_v in c_group):
                    is_matched = True
                    break
            if is_matched:
                matched_cands += 1
            else:
                unmatched_cands += 1

        # Case A: Target has required artists missing in candidate (e.g. duet target vs solo candidate)
        if matched_targets < len(target_groups):
            missing_ratio = (len(target_groups) - matched_targets) / len(target_groups)
            score *= (1.0 - 0.5 * missing_ratio)
            
        # Case B: Candidate has extra artists not in target (e.g. solo target vs duet candidate)
        if unmatched_cands > 0:
            score *= 0.70

    # 7.3. Featuring artist mismatch in candidate title (duet/collaboration listed only in title)
    cand_titles_to_check = [yt_title]
    if resolved_details:
        if resolved_details.get("title_ko"):
            cand_titles_to_check.append(resolved_details["title_ko"])
        if resolved_details.get("title_en"):
            cand_titles_to_check.append(resolved_details["title_en"])

    has_unmatched_feat = False
    for title_text in cand_titles_to_check:
        feats = re.findall(r"\b(?:with|feat\.?|ft\.?|featuring)\b\s*([a-zA-Z0-9가-힣\s]+)", title_text, flags=re.IGNORECASE)
        for feat in feats:
            cleaned_feat = re.split(r"[)\]\-_:|]", feat)[0].strip()
            if len(cleaned_feat) >= 2:
                feat_norm = normalize_text(cleaned_feat)
                is_target_artist = False
                for sa in target_artist_variants:
                    if similarity(sa, cleaned_feat, is_title=False) >= 0.85:
                        is_target_artist = True
                        break
                if not is_target_artist:
                    target_combined = (track_en.title + " " + track_en.album).lower()
                    if track_ko:
                        target_combined += " " + (track_ko.title + " " + track_ko.album).lower()
                    if feat_norm not in normalize_text(target_combined):
                        has_unmatched_feat = True
                        break
        if has_unmatched_feat:
            break

    if has_unmatched_feat:
        score *= 0.5

    # 8. Result Type Priority (Prioritize 'song' over 'video')
    res_type = result.get("resultType")
    if res_type in ["video", "동영상"]:
        score *= 0.90

    return score, title_score, artist_score, album_score



def passes_match_gates(
    track: SourceTrack,
    *,
    score: float,
    title_score: float,
    artist_score: float,
    min_score: float,
    min_title_score: float,
    min_artist_score: float,
) -> bool:
    if score < min_score or title_score < min_title_score:
        return False
    if track.artist and artist_score < min_artist_score:
        return False
    return True


def search_queries_for_track(track: SourceTrack, track_ko: SourceTrack | None) -> list[str]:
    queries = []
    
    # 1. Base title & artist (English/primary)
    t_en = track.title.strip() if track.title else ""
    a_en = track.artist.strip() if track.artist else ""
    al_en = track.album.strip() if track.album else ""
    
    # Clean EP/Single tags from album name
    al_en_clean = re.sub(r"\s*-\s*(EP|Single)\b.*$", "", al_en, flags=re.IGNORECASE).strip() if al_en else ""
    
    # Primary queries
    if t_en and a_en:
        queries.append(f"{t_en} {a_en}")
        if al_en_clean:
            queries.append(f"{t_en} {a_en} {al_en_clean}")
            
    # 2. Korean title & artist (if different)
    if track_ko:
        t_ko = track_ko.title.strip() if track_ko.title else ""
        a_ko = track_ko.artist.strip() if track_ko.artist else ""
        al_ko = track_ko.album.strip() if track_ko.album else ""
        al_ko_clean = re.sub(r"\s*-\s*(EP|Single)\b.*$", "", al_ko, flags=re.IGNORECASE).strip() if al_ko else ""
        
        if t_ko and a_ko:
            queries.append(f"{t_ko} {a_ko}")
            if al_ko_clean:
                queries.append(f"{t_ko} {a_ko} {al_ko_clean}")
                
    # 3. Add alias variants for the artist
    artist_names = []
    if a_en:
        artist_names.extend(artist_variants(a_en))
    if track_ko and track_ko.artist and track_ko.artist != a_en:
        artist_names.extend(artist_variants(track_ko.artist))
        
    artist_names = unique_values([name for name in artist_names if name])
    
    title_names = []
    if t_en:
        title_names.extend(title_variants(t_en))
    if track_ko and track_ko.title and track_ko.title != t_en:
        title_names.extend(title_variants(track_ko.title))
    title_names = unique_values([name for name in title_names if name])
    
    # Add cross combinations of titles and artist variants
    for title in title_names[:2]:
        for artist in artist_names[:2]:
            queries.append(f"{title} {artist}")
            
    # Fallback to pure title search (if nothing else worked)
    if t_en:
        queries.append(t_en)
    if track_ko and track_ko.title and track_ko.title != t_en:
        queries.append(track_ko.title)
        
    return unique_values([q.strip() for q in queries if q.strip()])


def search_ytmusic_songs(
    ytmusic: YTMusic,
    query: str,
    limit: int,
    *,
    track_title: str = "",
    track_artist: str = "",
    track_album: str = "",
    track_title_ko: str = "",
    track_artist_ko: str = "",
    track_album_ko: str = "",
) -> list[dict[str, Any]]:
    # Build dummy source tracks to score candidates inside search_ytmusic_songs
    track_en = SourceTrack(rank=1, title=track_title, artist=track_artist, album=track_album)
    track_ko = SourceTrack(rank=1, title=track_title_ko, artist=track_artist_ko, album=track_album_ko) if (track_title_ko or track_artist_ko) else None

    # Implement retry with exponential backoff on failure (JSONDecodeError/429)
    max_retries = 3
    delay = 3.0
    for attempt in range(max_retries):
        try:
            filtered_results = []
            seen_video_ids = set()

            # 1. Stage 1: Search with filter="songs"
            try:
                stage1_results = ytmusic.search(query, filter="songs", limit=limit)
            except Exception as e:
                LOG.warning("Stage 1 search failed for query '%s': %s", query, e)
                stage1_results = []

            for r in stage1_results:
                r_type = ytmusic_result_type(r.get("resultType"))
                v_id = r.get("videoId")
                if r_type == "song" and v_id:
                    if v_id not in seen_video_ids:
                        seen_video_ids.add(v_id)
                        filtered_results.append(r)

            # Evaluate if Stage 1 results are satisfactory
            need_stage2 = True
            if filtered_results:
                best_raw_score = 0.0
                for r in filtered_results[:3]:
                    try:
                        # Use force_resolve=False inside search_ytmusic_songs to prevent redundant API calls
                        score, _, _, _ = score_result(
                            track_en, r, track_ko, ytmusic=ytmusic, force_resolve=False
                        )
                        if score > best_raw_score:
                            best_raw_score = score
                    except Exception as e:
                        LOG.debug("Error scoring in search_ytmusic_songs: %s", e)

                if best_raw_score >= 0.75:
                    need_stage2 = False

            # 2. Stage 2: Fallback to mixed search
            if need_stage2:
                try:
                    stage2_results = ytmusic.search(query, limit=limit)
                except Exception as e:
                    LOG.warning("Stage 2 search failed for query '%s': %s", query, e)
                    stage2_results = []

                resolved_album_songs = []
                for r in stage2_results:
                    r_type = ytmusic_result_type(r.get("resultType"))
                    v_id = r.get("videoId")

                    if r_type in ["song", "video"] and v_id:
                        if v_id not in seen_video_ids:
                            seen_video_ids.add(v_id)
                            filtered_results.append(r)
                    elif r_type == "album" and r.get("browseId"):
                        alb_title = r.get("title", "")
                        alb_artists = ""
                        artists_field = r.get("artists")
                        if isinstance(artists_field, list):
                            alb_artists = " ".join(a.get("name", "") for a in artists_field)
                        elif isinstance(artists_field, str):
                            alb_artists = artists_field
                        elif r.get("artist"):
                            artist_field = r.get("artist")
                            if isinstance(artist_field, str):
                                alb_artists = artist_field
                            elif isinstance(artist_field, dict):
                                alb_artists = artist_field.get("name", "")

                        # Match artist
                        artist_match_en = similarity(track_artist, alb_artists, is_title=False) >= 0.7 if track_artist else False
                        artist_match_ko = similarity(track_artist_ko, alb_artists, is_title=False) >= 0.7 if track_artist_ko else False

                        if artist_match_en or artist_match_ko:
                            alb_norm = normalize_text(alb_title)
                            is_target_album = False
                            if track_album and normalize_text(track_album) == alb_norm:
                                is_target_album = True
                            elif track_album_ko and normalize_text(track_album_ko) == alb_norm:
                                is_target_album = True
                            elif track_title and normalize_text(track_title) == alb_norm:
                                is_target_album = True
                            elif track_title_ko and normalize_text(track_title_ko) == alb_norm:
                                is_target_album = True

                            if is_target_album:
                                try:
                                    LOG.debug("Fetching album tracks for '%s' (ID: %s) to resolve song '%s'", alb_title, r.get("browseId"), track_title)
                                    alb_details = ytmusic.get_album(r.get("browseId"))
                                    for t in alb_details.get("tracks", []):
                                        t_title = t.get("title", "")
                                        t_video_id = t.get("videoId")
                                        if t_video_id:
                                            t_match_en = similarity(track_title, t_title, is_title=True) >= 0.85 if track_title else False
                                            t_match_ko = similarity(track_title_ko, t_title, is_title=True) >= 0.85 if track_title_ko else False
                                            if t_match_en or t_match_ko:
                                                song_res = {
                                                    "resultType": "song",
                                                    "videoId": t_video_id,
                                                    "title": t_title,
                                                    "artists": alb_details.get("artists"),
                                                    "album": {
                                                        "name": alb_details.get("title"),
                                                        "id": r.get("browseId")
                                                    },
                                                    "duration": t.get("duration"),
                                                    "duration_seconds": t.get("duration_seconds")
                                                }
                                                resolved_album_songs.append(song_res)
                                except Exception as e:
                                    LOG.warning("Failed to resolve tracks from album %s: %s", r.get("browseId"), e)

                for song_res in resolved_album_songs:
                    v_id = song_res.get("videoId")
                    if v_id and v_id not in seen_video_ids:
                        seen_video_ids.add(v_id)
                        filtered_results.append(song_res)

            return filtered_results[:limit]

        except Exception as exc:
            if "429" in str(exc) or "Expecting value" in str(exc):
                if attempt < max_retries - 1:
                    LOG.warning("Search failed for query '%s' on attempt %d: %s. Retrying in %.1fs...", query, attempt + 1, exc, delay)
                    time.sleep(delay)
                    delay *= 2.0
                    continue
            LOG.error("All search attempts failed for query '%s': %s", query, exc)
            return []
    return []

def search_youtube_music(
    ytmusic: YTMusic,
    track: SourceTrack,
    track_ko: SourceTrack | None,
    *,
    min_score: float,
    min_title_score: float,
    min_artist_score: float,
    limit: int,
    ignore_video_ids: set[str] | None = None,
) -> MatchResult:
    # 1. Check manual overrides first from matching_alias.json
    t_norm = normalize_text(track.title)
    a_norm = normalize_text(track.artist)
    override_key = f"{t_norm}|{a_norm}"
    
    manual_video_id = ALIASES.overrides.get(override_key)
    if not manual_video_id and track_ko:
        tk_norm = normalize_text(track_ko.title)
        ak_norm = normalize_text(track_ko.artist)
        manual_video_id = ALIASES.overrides.get(f"{tk_norm}|{ak_norm}")

    if manual_video_id:
        LOG.info("Manual override found for '%s | %s' -> video_id: %s", track.title, track.artist, manual_video_id)
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            service=track.service,
            title_en=track.title,
            artist_en=track.artist,
            album_en=track.album,
            title_ko=track_ko.title if track_ko else "",
            artist_ko=track_ko.artist if track_ko else "",
            album_ko=track_ko.album if track_ko else "",
            song_id=track.song_id,
            album_id=track.album_id,
            video_id=manual_video_id,
            status="manual_override",
            score=1.0,
            query="manual_override"
        )

    # 2. Proceed with search if no override
    best_result: dict[str, Any] | None = None
    best_score = 0.0
    best_title_score = 0.0
    best_artist_score = 0.0
    best_album_score = 0.0
    best_query = ""
    seen_video_ids: set[str] = set(ignore_video_ids or [])

    for query in search_queries_for_track(track, track_ko):
        time.sleep(0.5)  # Add sleep to prevent rate limiting (429 Too Many Requests)
        try:
            results = search_ytmusic_songs(
                ytmusic,
                query,
                limit,
                track_title=track.title,
                track_artist=track.artist,
                track_album=track.album,
                track_title_ko=track_ko.title if track_ko else "",
                track_artist_ko=track_ko.artist if track_ko else "",
                track_album_ko=track_ko.album if track_ko else "",
            )
        except Exception as exc:
            LOG.warning("Search failed: %s (%s)", query, exc)
            continue

        for index, result in enumerate(results):
            video_id = result.get("videoId")
            if not is_song_result(result) or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            
            # Force resolve for the top 3 search results of each query
            force_resolve = (index < 3)
            candidate_score, title_score, artist_score, album_score = score_result(
                track,
                result,
                track_ko,
                ytmusic=ytmusic,
                force_resolve=force_resolve
            )
            if candidate_score > best_score:
                best_score = candidate_score
                best_title_score = title_score
                best_artist_score = artist_score
                best_album_score = album_score
                best_result = result
                best_query = query

        if passes_match_gates(
            track,
            score=best_score,
            title_score=best_title_score,
            artist_score=best_artist_score,
            min_score=min_score,
            min_title_score=min_title_score,
            min_artist_score=min_artist_score,
        ):
            break

    if not best_result or not passes_match_gates(
        track,
        score=best_score,
        title_score=best_title_score,
        artist_score=best_artist_score,
        min_score=min_score,
        min_title_score=min_title_score,
        min_artist_score=min_artist_score,
    ):
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            service=track.service,
            title_en=track.title,
            artist_en=track.artist,
            album_en=track.album,
            title_ko=track_ko.title if track_ko else "",
            artist_ko=track_ko.artist if track_ko else "",
            album_ko=track_ko.album if track_ko else "",
            song_id=track.song_id,
            album_id=track.album_id,
            video_id=None,
            yt_title=best_result.get("title", "") if best_result else "",
            yt_artist=result_artists(best_result) if best_result else "",
            yt_album=result_album(best_result) if best_result else "",
            score=round(best_score, 3),
            title_score=round(best_title_score, 3),
            artist_score=round(best_artist_score, 3),
            album_score=round(best_album_score, 3),
            yt_result_type=best_result.get("resultType", "") if best_result else "",
            query=best_query,
        )

    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        service=track.service,
        title_en=track.title,
        artist_en=track.artist,
        album_en=track.album,
        title_ko=track_ko.title if track_ko else "",
        artist_ko=track_ko.artist if track_ko else "",
        album_ko=track_ko.album if track_ko else "",
        song_id=track.song_id,
        album_id=track.album_id,
        artwork_url=track.artwork_url,
        video_id=best_result["videoId"],
        yt_title=best_result.get("title", ""),
        yt_artist=result_artists(best_result),
        yt_album=result_album(best_result),
        score=round(best_score, 3),
        title_score=round(best_title_score, 3),
        artist_score=round(best_artist_score, 3),
        album_score=round(best_album_score, 3),
        yt_result_type=best_result.get("resultType", ""),
        query=best_query,
        status="matched",
    )


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def get_existing_playlist_items(ytmusic: YTMusic, playlist_id: str) -> list[dict[str, str]]:
    try:
        playlist = ytmusic.get_playlist(playlist_id, limit=None)
    except Exception as exc:
        if "404" in str(exc):
            LOG.error(f"Playlist {playlist_id} not found (404). Please verify the ID is correct and the playlist is Public or Unlisted.")
        else:
            LOG.error(f"Failed to fetch playlist items for {playlist_id}: {exc}")
        return []

    items: list[dict[str, str]] = []
    for track in playlist.get("tracks", []):
        video_id = track.get("videoId")
        set_video_id = track.get("setVideoId")
        if video_id and set_video_id:
            items.append({"videoId": video_id, "setVideoId": set_video_id})
    return items


def update_ytmusic_playlist(
    ytmusic: YTMusic,
    playlist_id: str,
    video_ids: list[str],
    *,
    description: str = "",
    dry_run: bool,
    db_path: str | Path | None = None,
    service: str = "",
    job_name: str = "",
    playlist_name: str = "",
) -> None:
    """Updates the target YouTube Music playlist and records the update run inside the database.

    If `SUPABASE_DB_URL` environment variable is set, it records the update audit inside the
    remote Supabase PostgreSQL database. Otherwise, it falls back to the SQLite DB at `db_path`.
    """
    if (description or playlist_name) and not dry_run:
        try:
            kwargs: dict[str, str] = {}
            if playlist_name:
                kwargs["title"] = playlist_name
            if description:
                kwargs["description"] = description
            ytmusic.edit_playlist(playlist_id, **kwargs)
            LOG.info("Updated playlist metadata")
        except Exception as exc:
            LOG.warning("Failed to update playlist metadata: %s", exc)

    existing_items = get_existing_playlist_items(ytmusic, playlist_id)
    LOG.info("Current YouTube Music playlist item count: %d", len(existing_items))
    if db_path and not dry_run:
        try:
            from hype_db import record_playlist_update
            record_playlist_update(
                db_path,
                playlist_id=playlist_id,
                service=service,
                job_name=job_name,
                requested_video_ids=video_ids,
                existing_video_ids=[item.get("videoId", "") for item in existing_items],
                dry_run=dry_run,
            )
        except Exception as exc:
            LOG.warning("Failed to record playlist update audit: %s", exc)

    if dry_run:
        LOG.info("Dry run enabled. Skipping playlist removal/addition.")
        return

    for chunk in chunked(existing_items, 50):
        for attempt in range(3):
            try:
                ytmusic.remove_playlist_items(playlist_id, chunk)
                LOG.info("Removed %d existing items", len(chunk))
                break
            except Exception as exc:
                LOG.warning("Failed to remove %d items (attempt %d): %s", len(chunk), attempt + 1, exc)
                time.sleep(2)
        time.sleep(1.0)

    for chunk in chunked(video_ids, 50):
        for attempt in range(3):
            try:
                ytmusic.add_playlist_items(playlist_id, chunk, duplicates=False)
                LOG.info("Added %d matched items", len(chunk))
                break
            except Exception as exc:
                LOG.warning("Failed to add %d items (attempt %d): %s", len(chunk), attempt + 1, exc)
                time.sleep(2)
        time.sleep(1.0)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_ytmusic(auth_file: str, client_id: str = "", client_secret: str = "", language: str = "en") -> YTMusic:
    yt = None
    if client_id or client_secret:
        if not client_id or not client_secret:
            raise SystemExit("Both YTMUSIC_OAUTH_CLIENT_ID and YTMUSIC_OAUTH_CLIENT_SECRET are required.")
        if OAuthCredentials is None:
            raise SystemExit("Installed ytmusicapi does not support OAuthCredentials.")
        yt = YTMusic(
            auth_file,
            language=language,
            oauth_credentials=OAuthCredentials(
                client_id=client_id,
                client_secret=client_secret,
            ),
        )
    else:
        yt = YTMusic(auth_file, language=language)

    # Force headers based on language to ensure metadata matches the source chart
    if language == "ko":
        yt.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en-US,en;q=0.8"})
    else:
        yt.headers.update({"Accept-Language": "en-US,en;q=0.9"})
    return yt

