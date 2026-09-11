from __future__ import annotations

import atexit
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from ytmusicapi import YTMusic

from hype_db_common import (
    has_version_mismatch,
    postgres_connect_config,
    strip_content_rating_version_markers,
    version_signature,
)

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
    value = re.sub(
        r"[^0-9a-z가-힣\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\ud7b0-\ud7ff"
        r"\u3040-\u30ff\u4e00-\u9fff]+",
        " ",
        value,
    )
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
    locale: str = ""


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


def localized_source_fields(
    track: SourceTrack,
    track_ko: SourceTrack | None = None,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Map source metadata to language-specific fields without guessing a locale."""
    previous = previous or {}
    if track.locale.lower().startswith("ko"):
        korean = track_ko or track
        return {
            "title_en": str(previous.get("title_en") or ""),
            "artist_en": str(previous.get("artist_en") or ""),
            "album_en": str(previous.get("album_en") or ""),
            "title_ko": korean.title,
            "artist_ko": korean.artist,
            "album_ko": korean.album,
        }

    return {
        "title_en": track.title,
        "artist_en": track.artist,
        "album_en": track.album,
        "title_ko": track_ko.title if track_ko else str(previous.get("title_ko") or ""),
        "artist_ko": track_ko.artist if track_ko else str(previous.get("artist_ko") or ""),
        "album_ko": track_ko.album if track_ko else str(previous.get("album_ko") or ""),
    }


def match_from_prev(
    track: SourceTrack,
    prev: dict[str, Any],
    *,
    track_ko: SourceTrack | None = None,
    status: str = "cached_match",
) -> MatchResult:
    """Build a MatchResult reusing data from a previous match (cache or proxy)."""
    localized = localized_source_fields(track, track_ko, previous=prev)
    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        service=track.service,
        **localized,
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


class SQLiteBilingualCache:
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


def _normalize_cached_names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item)]
        except json.JSONDecodeError:
            pass
    return []


def _normalize_cached_song(row: dict[str, Any]) -> dict[str, str]:
    return {
        "title_ko": row.get("title_ko") or "",
        "title_en": row.get("title_en") or "",
        "artist_ko": row.get("artist_ko") or "",
        "artist_en": row.get("artist_en") or "",
        "album_ko": row.get("album_ko") or "",
        "album_en": row.get("album_en") or "",
    }


class PostgresBilingualCache:
    """Buffered Supabase PostgreSQL-backed cache with SQLite fallback."""

    def __init__(self, pg_url: str, fallback_path: Path):
        self.pg_url = pg_url
        self.fallback_path = fallback_path
        self.fallback: SQLiteBilingualCache | None = None
        self.loaded = False
        self.fallback_active = False
        self.artists: dict[str, list[str]] = {}
        self.songs: dict[str, dict[str, str]] = {}
        self.dirty_artists: dict[str, tuple[list[str], str]] = {}
        self.dirty_songs: dict[str, tuple[dict[str, str], str]] = {}

    def _fallback(self) -> SQLiteBilingualCache:
        if self.fallback is None:
            self.fallback = SQLiteBilingualCache(self.fallback_path)
        return self.fallback

    def _connect(self):
        import psycopg2

        pg_config = postgres_connect_config()
        conn = psycopg2.connect(self.pg_url, connect_timeout=int(pg_config["connect_timeout"]))
        with conn.cursor() as cursor:
            cursor.execute("SET statement_timeout = '180s'")
            cursor.execute("SET idle_in_transaction_session_timeout = '180s'")
        conn.commit()
        return conn

    def _init_db(self, conn):
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ytmusic_artist_translations (
                    artist_id TEXT PRIMARY KEY,
                    names JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ytmusic_song_translations (
                    video_id TEXT PRIMARY KEY,
                    title_ko TEXT,
                    title_en TEXT,
                    artist_ko TEXT,
                    artist_en TEXT,
                    album_ko TEXT,
                    album_en TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()

    def _ensure_loaded(self):
        if self.loaded:
            return
        conn = None
        try:
            conn = self._connect()
            self._init_db(conn)
            with conn.cursor() as cursor:
                cursor.execute("SELECT artist_id, names FROM ytmusic_artist_translations")
                self.artists = {
                    artist_id: names
                    for artist_id, raw_names in cursor.fetchall()
                    if (names := _normalize_cached_names(raw_names))
                }
                cursor.execute(
                    """
                    SELECT video_id, title_ko, title_en, artist_ko, artist_en, album_ko, album_en
                    FROM ytmusic_song_translations
                    """
                )
                self.songs = {
                    video_id: _normalize_cached_song(
                        {
                            "title_ko": title_ko,
                            "title_en": title_en,
                            "artist_ko": artist_ko,
                            "artist_en": artist_en,
                            "album_ko": album_ko,
                            "album_en": album_en,
                        }
                    )
                    for video_id, title_ko, title_en, artist_ko, artist_en, album_ko, album_en in cursor.fetchall()
                }
            LOG.info(
                "Loaded YTMusic bilingual cache from PostgreSQL: %d artists, %d songs",
                len(self.artists),
                len(self.songs),
            )
        except Exception as exc:
            LOG.warning("Failed to load PostgreSQL bilingual cache; using SQLite fallback: %s", exc)
            self.fallback_active = True
        finally:
            if conn is not None:
                conn.close()
            self.loaded = True

    def get_artist(self, artist_id: str) -> list[str] | None:
        self._ensure_loaded()
        if self.fallback_active:
            return self._fallback().get_artist(artist_id)
        return self.artists.get(artist_id)

    def set_artist(self, artist_id: str, names: list[str]):
        self._ensure_loaded()
        if self.fallback_active:
            self._fallback().set_artist(artist_id, names)
            return
        now_str = datetime.now(timezone.utc).isoformat()
        self.artists[artist_id] = names
        self.dirty_artists[artist_id] = (names, now_str)

    def get_song(self, video_id: str) -> dict[str, str] | None:
        self._ensure_loaded()
        if self.fallback_active:
            return self._fallback().get_song(video_id)
        return self.songs.get(video_id)

    def set_song(self, video_id: str, details: dict[str, str]):
        self._ensure_loaded()
        if self.fallback_active:
            self._fallback().set_song(video_id, details)
            return
        now_str = datetime.now(timezone.utc).isoformat()
        normalized = _normalize_cached_song(details)
        self.songs[video_id] = normalized
        self.dirty_songs[video_id] = (normalized, now_str)

    def flush(self):
        if self.fallback_active or not (self.dirty_artists or self.dirty_songs):
            return
        conn = None
        try:
            from psycopg2.extras import Json, execute_values

            conn = self._connect()
            self._init_db(conn)
            with conn.cursor() as cursor:
                if self.dirty_artists:
                    artist_rows = [
                        (artist_id, Json(names), updated_at)
                        for artist_id, (names, updated_at) in self.dirty_artists.items()
                    ]
                    execute_values(
                        cursor,
                        """
                        INSERT INTO ytmusic_artist_translations (artist_id, names, updated_at)
                        VALUES %s
                        ON CONFLICT (artist_id) DO UPDATE SET
                            names = EXCLUDED.names,
                            updated_at = EXCLUDED.updated_at
                        """,
                        artist_rows,
                    )
                if self.dirty_songs:
                    song_rows = [
                        (
                            video_id,
                            details.get("title_ko", ""),
                            details.get("title_en", ""),
                            details.get("artist_ko", ""),
                            details.get("artist_en", ""),
                            details.get("album_ko", ""),
                            details.get("album_en", ""),
                            updated_at,
                        )
                        for video_id, (details, updated_at) in self.dirty_songs.items()
                    ]
                    execute_values(
                        cursor,
                        """
                        INSERT INTO ytmusic_song_translations (
                            video_id, title_ko, title_en, artist_ko, artist_en, album_ko, album_en, updated_at
                        )
                        VALUES %s
                        ON CONFLICT (video_id) DO UPDATE SET
                            title_ko = EXCLUDED.title_ko,
                            title_en = EXCLUDED.title_en,
                            artist_ko = EXCLUDED.artist_ko,
                            artist_en = EXCLUDED.artist_en,
                            album_ko = EXCLUDED.album_ko,
                            album_en = EXCLUDED.album_en,
                            updated_at = EXCLUDED.updated_at
                        """,
                        song_rows,
                    )
            conn.commit()
            LOG.info(
                "Flushed YTMusic bilingual cache to PostgreSQL: %d artists, %d songs",
                len(self.dirty_artists),
                len(self.dirty_songs),
            )
            self.dirty_artists.clear()
            self.dirty_songs.clear()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            LOG.warning("Failed to flush PostgreSQL bilingual cache: %s", exc)
        finally:
            if conn is not None:
                conn.close()


class BilingualCache:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.backend: SQLiteBilingualCache | PostgresBilingualCache | None = None
        self.read_only = False

    def _backend(self) -> SQLiteBilingualCache | PostgresBilingualCache:
        if self.backend is not None:
            return self.backend
        pg_url = os.environ.get("SUPABASE_DB_URL")
        if pg_url:
            self.backend = PostgresBilingualCache(pg_url, self.db_path)
        else:
            self.backend = SQLiteBilingualCache(self.db_path)
        return self.backend

    def get_artist(self, artist_id: str) -> list[str] | None:
        if self.read_only:
            if (
                isinstance(self.backend, PostgresBilingualCache)
                and self.backend.loaded
                and not self.backend.fallback_active
            ):
                return self.backend.artists.get(artist_id)
            return None
        return self._backend().get_artist(artist_id)

    def set_artist(self, artist_id: str, names: list[str]):
        if self.read_only:
            return
        self._backend().set_artist(artist_id, names)

    def get_song(self, video_id: str) -> dict[str, str] | None:
        if self.read_only:
            if (
                isinstance(self.backend, PostgresBilingualCache)
                and self.backend.loaded
                and not self.backend.fallback_active
            ):
                return self.backend.songs.get(video_id)
            return None
        return self._backend().get_song(video_id)

    def set_song(self, video_id: str, details: dict[str, str]):
        if self.read_only:
            return
        self._backend().set_song(video_id, details)

    def flush(self):
        if self.read_only:
            return
        if self.backend is None:
            return
        flush = getattr(self.backend, "flush", None)
        if flush:
            flush()


BILINGUAL_CACHE = BilingualCache(Path(__file__).parent / "ytmusic_cache.db")
atexit.register(BILINGUAL_CACHE.flush)


@contextmanager
def bilingual_cache_read_only():
    """Prevent a dry run from opening or writing the persistent bilingual cache."""
    previous = BILINGUAL_CACHE.read_only
    BILINGUAL_CACHE.read_only = True
    try:
        yield
    finally:
        BILINGUAL_CACHE.read_only = previous


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


def _watch_playlist_for_metadata(client: YTMusic, video_id: str) -> dict[str, Any]:
    try:
        return client.get_watch_playlist(videoId=video_id)
    except KeyError as exc:
        if exc.args != ("endpoint",):
            raise
        # Some responses omit the optional related/lyrics tab endpoint. Reuse
        # the installed track parser without requiring those unrelated tabs.
        from ytmusicapi.parsers.watch import parse_watch_playlist

        payload = client._send_request("next", {
            "videoId": video_id, "playlistId": "RDAMVM" + video_id,
            "enablePersistentPlaylistPanel": True, "isAudioOnly": True,
            "tunerSettingValue": "AUTOMIX_SETTING_NORMAL",
            "watchEndpointMusicSupportedConfigs": {"watchEndpointMusicConfig": {
                "hasPersistentPlaylistPanel": True, "musicVideoType": "MUSIC_VIDEO_TYPE_ATV",
            }},
        })
        tabs = payload["contents"]["singleColumnMusicWatchNextResultsRenderer"]["tabbedRenderer"]["watchNextTabbedResultsRenderer"]["tabs"]
        queue = tabs[0]["tabRenderer"]["content"]["musicQueueRenderer"]["content"]["playlistPanelRenderer"]
        return {"tracks": parse_watch_playlist(queue["contents"])}


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

    def exact_watch_track(payload: Any) -> dict[str, Any] | None:
        tracks = payload.get("tracks") if isinstance(payload, dict) else None
        if not isinstance(tracks, list):
            return None
        return next(
            (
                track
                for track in tracks
                if isinstance(track, dict) and track.get("videoId") == video_id
            ),
            None,
        )
    
    # 1. KO locale
    try:
        playlist_ko = _watch_playlist_for_metadata(yt_ko, video_id)
        track = exact_watch_track(playlist_ko)
        if track:
            details["title_ko"] = track.get("title", "")
            artists = track.get("artists", [])
            details["artist_ko"] = ", ".join(a.get("name", "") for a in artists if a.get("name") and a.get("id") is not None)
            album_obj = track.get("album")
            if album_obj:
                details["album_ko"] = album_obj.get("name", "")
        else:
            LOG.debug("KO watch playlist did not contain requested video %s", video_id)
    except Exception as e:
        LOG.debug("Failed to get KO song details for %s: %s", video_id, e)

    # 2. EN locale
    try:
        yt_en = get_ytmusic_en(yt_ko)
        playlist_en = _watch_playlist_for_metadata(yt_en, video_id)
        track = exact_watch_track(playlist_en)
        if track:
            details["title_en"] = track.get("title", "")
            artists = track.get("artists", [])
            details["artist_en"] = ", ".join(a.get("name", "") for a in artists if a.get("name") and a.get("id") is not None)
            album_obj = track.get("album")
            if album_obj:
                details["album_en"] = album_obj.get("name", "")
        else:
            LOG.debug("EN watch playlist did not contain requested video %s", video_id)
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
    if is_title:
        left = strip_content_rating_version_markers(left)
        right = strip_content_rating_version_markers(right)
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
        candidate_titles_for_version = [cand_title]
        
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
                    candidate_titles_for_version = titles_to_check
                    
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

        if _has_recording_version_mismatch([title, normalized_title], candidate_titles_for_version):
            LOG.debug(
                "Rejected video-to-song version mismatch: source=%s candidate=%s",
                [title, normalized_title],
                candidate_titles_for_version,
            )
            continue

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


def _has_recording_version_mismatch(
    target_titles: list[str],
    candidate_titles: list[str],
) -> bool:
    target_titles = [title for title in target_titles if title]
    candidate_titles = [title for title in candidate_titles if title]
    if not target_titles or not candidate_titles:
        return False

    target_has_version = any(version_signature(title) for title in target_titles)
    candidate_has_version = any(version_signature(title) for title in candidate_titles)
    if not target_has_version and not candidate_has_version:
        return False

    return all(
        has_version_mismatch(target_title, candidate_title)
        for target_title in target_titles
        for candidate_title in candidate_titles
    )


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

    source_album_is_title = False
    if raw_title_score >= 0.95 and raw_artist_score >= 0.95:
        source_album_variants = []
        if track_en.album:
            source_album_variants.extend(album_variants(track_en.album))
        if track_ko and track_ko.album:
            source_album_variants.extend(album_variants(track_ko.album))
        source_album_is_title = any(
            normalize_text(album_v) == normalize_text(title_v)
            for album_v in source_album_variants
            for title_v in target_title_variants
        )

    album_score = max(
        album_score_en,
        album_score_ko,
        0.9 if title_is_album else 0.0,
        0.9 if source_album_is_title else 0.0,
    )
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

    target_titles_for_version = [track_en.title]
    if track_ko and track_ko.title:
        target_titles_for_version.append(track_ko.title)
    candidate_titles_for_version = [yt_title]
    if resolved_details:
        if resolved_details.get("title_ko"):
            candidate_titles_for_version.append(resolved_details["title_ko"])
        if resolved_details.get("title_en"):
            candidate_titles_for_version.append(resolved_details["title_en"])
    if _has_recording_version_mismatch(target_titles_for_version, candidate_titles_for_version):
        LOG.debug(
            "Rejected version mismatch: target=%s candidate=%s",
            target_titles_for_version,
            candidate_titles_for_version,
        )
        return 0.0, 0.0, 0.0, album_score

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
        "english ver", "english version", "eng ver", "eng version"
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
                "english ver", "english version", "eng ver", "eng version"
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
    # 7.1. Target artist views (EN/KO are alternate views, not cumulative requirements)
    def build_groups(artist_text: str) -> list[set[str]]:
        groups = []
        for part in unique_values(split_artist_names(artist_text)):
            variants = ALIASES.get_variants(part, "artist")
            expanded = []
            for v in variants:
                expanded.extend(_extract_parentheses_variants(v))
                expanded.append(v)
            group = {normalize_text(v) for v in unique_values(expanded) if v}
            if group:
                groups.append(group)
        return groups

    target_group_views = []
    if track_en.artist:
        target_group_views.append(build_groups(track_en.artist))
    if track_ko and track_ko.artist:
        target_group_views.append(build_groups(track_ko.artist))


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

    # Compare against the best source language view.
    if target_group_views and cand_groups:
        def group_matches(left_group: set[str], right_group: set[str]) -> bool:
            return any(similarity(left, right, is_title=False) >= 0.85 for left in left_group for right in right_group)

        def matched_ratio(target_groups: list[set[str]]) -> float:
            if not target_groups:
                return 1.0
            matched_targets = sum(
                1 for t_group in target_groups
                if any(group_matches(t_group, c_group) for c_group in cand_groups)
            )
            return matched_targets / len(target_groups)

        valid_views = [groups for groups in target_group_views if groups]
        if valid_views:
            best_matched_ratio = max(matched_ratio(groups) for groups in valid_views)

            # Case A: Target has required artists missing in candidate (e.g. duet target vs solo candidate)
            if best_matched_ratio < 1.0:
                score *= (1.0 - 0.5 * (1.0 - best_matched_ratio))

            # Case B: Candidate has extra artists not in target (e.g. solo target vs duet candidate)
            # EN/KO source views are alternate labels, so a candidate is extra only
            # when none of the source language views can explain it.
            all_target_groups = [
                target_group
                for view in valid_views
                for target_group in view
            ]
            unmatched_cands = sum(
                1 for c_group in cand_groups
                if not any(group_matches(t_group, c_group) for t_group in all_target_groups)
            )
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
    min_score: float = 0.6,
    min_title_score: float = 0.65,
    min_artist_score: float = 0.55,
) -> list[dict[str, Any]]:
    # Build dummy source tracks to score candidates inside search_ytmusic_songs
    track_en = SourceTrack(rank=1, title=track_title, artist=track_artist, album=track_album)
    track_ko = SourceTrack(rank=1, title=track_title_ko, artist=track_artist_ko, album=track_album_ko) if (track_title_ko or track_artist_ko) else None

    # A failed API request is not evidence that no matching song exists.
    max_retries = 3
    delay = 3.0
    for attempt in range(max_retries):
        try:
            stage1_candidates: list[dict[str, Any]] = []
            stage1_error: Exception | None = None

            # 1. Stage 1: Search with filter="songs"
            try:
                stage1_results = ytmusic.search(query, filter="songs", limit=limit)
            except Exception as e:
                LOG.warning("Stage 1 search failed for query '%s': %s", query, e)
                stage1_error = e
                stage1_results = []

            for r in stage1_results[:limit]:
                r_type = ytmusic_result_type(r.get("resultType"))
                v_id = r.get("videoId")
                if r_type == "song" and v_id:
                    stage1_candidates.append(r)

            # Evaluate if Stage 1 results are satisfactory
            need_stage2 = True
            if stage1_candidates:
                for r in stage1_candidates[:3]:
                    try:
                        # Use force_resolve=False inside search_ytmusic_songs to prevent redundant API calls
                        score, title_score, artist_score, _ = score_result(
                            track_en, r, track_ko, ytmusic=ytmusic, force_resolve=False
                        )
                        if passes_match_gates(
                            track_en, score=score, title_score=title_score,
                            artist_score=artist_score, min_score=max(0.75, min_score),
                            min_title_score=min_title_score, min_artist_score=min_artist_score,
                        ):
                            need_stage2 = False
                            break
                    except Exception as e:
                        LOG.debug("Error scoring in search_ytmusic_songs: %s", e)

            # 2. Stage 2: Fallback to mixed search
            stage2_candidates: list[dict[str, Any]] = []
            if need_stage2:
                try:
                    stage2_results = ytmusic.search(query, limit=limit)
                except Exception as e:
                    LOG.warning("Stage 2 search failed for query '%s': %s", query, e)
                    if stage1_error is not None:
                        raise RuntimeError("Both YouTube Music search stages failed") from e
                    stage2_results = []

                direct_stage2_results: list[dict[str, Any]] = []
                resolved_album_songs: list[dict[str, Any]] = []
                for r in stage2_results[:limit]:
                    r_type = ytmusic_result_type(r.get("resultType"))
                    v_id = r.get("videoId")

                    if r_type in ["song", "video"] and v_id:
                        direct_stage2_results.append(r)
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

                stage2_candidates = resolved_album_songs + direct_stage2_results

            # Stage 2 only runs when Stage 1 is weak, so reserve a full stage-sized
            # slice for it instead of letting an oversized Stage 1 response erase it.
            # Keep one complete record per ID; never splice fields from records
            # whose titles/artists may disagree.
            candidate_pool: dict[str, dict[str, Any]] = {}
            candidate_order: list[str] = []

            def add_candidates(candidates: list[dict[str, Any]], cap: int) -> None:
                added = 0
                for candidate in candidates:
                    video_id = candidate.get("videoId")
                    if not video_id:
                        continue
                    existing = candidate_pool.get(video_id)
                    if existing is None:
                        if added >= cap:
                            continue
                        candidate_pool[video_id] = dict(candidate)
                        candidate_order.append(video_id)
                        added += 1
                        continue
                    if _candidate_metadata_quality(candidate) > _candidate_metadata_quality(existing):
                        candidate_pool[video_id] = dict(candidate)

            if need_stage2:
                add_candidates(stage2_candidates, limit)
            add_candidates(stage1_candidates, limit)
            return [candidate_pool[video_id] for video_id in candidate_order[: 2 * limit]]

        except Exception as exc:
            if attempt < max_retries - 1:
                LOG.warning("Search failed for query '%s' on attempt %d: %s. Retrying in %.1fs...", query, attempt + 1, exc, delay)
                time.sleep(delay)
                delay *= 2.0
                continue
            LOG.error("All search attempts failed for query '%s': %s", query, exc)
            raise RuntimeError(f"YouTube Music search unavailable for {query!r}") from exc
    return []


def _candidate_metadata_quality(result: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Prefer complete records for the same video without merging conflicts."""
    has_title = bool(result.get("title"))
    has_artists = bool(result_artists(result))
    has_album = bool(result_album(result))
    has_duration = bool(result.get("duration") or result.get("duration_seconds"))
    is_song = ytmusic_result_type(result.get("resultType")) == "song"
    return (
        sum((has_title, has_artists, has_album, has_duration)),
        int(has_album),
        int(has_artists),
        int(is_song),
        int(has_duration),
    )


def _candidate_metadata_signature(result: dict[str, Any]) -> tuple[str, ...]:
    """Identify an exact metadata view while allowing localized views to be rescored."""
    return (
        str(result.get("videoId") or ""),
        normalize_text(str(result.get("title") or "")),
        normalize_text(result_artists(result)),
        normalize_text(result_album(result)),
        ytmusic_result_type(result.get("resultType")),
        str(result.get("duration_seconds") or result.get("duration") or ""),
    )


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
        localized = localized_source_fields(track, track_ko)
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            service=track.service,
            **localized,
            song_id=track.song_id,
            album_id=track.album_id,
            video_id=manual_video_id,
            status="manual_override",
            score=1.0,
            query="manual_override"
        )

    # 2. Proceed with search if no override
    best_passing: dict[str, Any] | None = None
    best_diagnostic: dict[str, Any] | None = None
    last_search_error: Exception | None = None
    # `ignore_video_ids` is retained for caller compatibility, but source tracks must
    # independently select their best canonical match before playlist-level deduplication.
    evaluated_candidates: dict[tuple[str, ...], dict[str, Any]] = {}

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
                min_score=min_score,
                min_title_score=min_title_score,
                min_artist_score=min_artist_score,
            )
        except Exception as exc:
            LOG.warning("Search failed: %s (%s)", query, exc)
            last_search_error = exc
            continue

        for index, result in enumerate(results):
            video_id = result.get("videoId")
            if not is_song_result(result) or not video_id:
                continue

            signature = _candidate_metadata_signature(result)
            if signature in evaluated_candidates:
                continue
            quality = _candidate_metadata_quality(result)

            # Force resolve for the top 3 search results of each query
            force_resolve = (index < 3)
            candidate_score, title_score, artist_score, album_score = score_result(
                track,
                result,
                track_ko,
                ytmusic=ytmusic,
                force_resolve=force_resolve
            )
            evaluated_candidates[signature] = {
                "result": result,
                "score": candidate_score,
                "title_score": title_score,
                "artist_score": artist_score,
                "album_score": album_score,
                "query": query,
                "quality": quality,
            }

        if evaluated_candidates:
            best_diagnostic = max(
                evaluated_candidates.values(), key=lambda item: item["score"]
            )
            passing = [
                item
                for item in evaluated_candidates.values()
                if passes_match_gates(
                    track,
                    score=item["score"],
                    title_score=item["title_score"],
                    artist_score=item["artist_score"],
                    min_score=min_score,
                    min_title_score=min_title_score,
                    min_artist_score=min_artist_score,
                )
            ]
            best_passing = max(passing, key=lambda item: item["score"], default=None)

        if best_passing:
            break

    if not best_passing:
        if last_search_error is not None:
            raise RuntimeError(f"YouTube Music search incomplete for {track.title!r}") from last_search_error
        diagnostic = best_diagnostic or {}
        diagnostic_result = diagnostic.get("result") or {}
        localized = localized_source_fields(track, track_ko)
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            service=track.service,
            **localized,
            song_id=track.song_id,
            album_id=track.album_id,
            video_id=None,
            yt_title=diagnostic_result.get("title", ""),
            yt_artist=result_artists(diagnostic_result),
            yt_album=result_album(diagnostic_result),
            score=round(diagnostic.get("score", 0.0), 3),
            title_score=round(diagnostic.get("title_score", 0.0), 3),
            artist_score=round(diagnostic.get("artist_score", 0.0), 3),
            album_score=round(diagnostic.get("album_score", 0.0), 3),
            yt_result_type=diagnostic_result.get("resultType", ""),
            query=diagnostic.get("query", ""),
        )

    best_result = best_passing["result"]
    localized = localized_source_fields(track, track_ko)
    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        service=track.service,
        **localized,
        song_id=track.song_id,
        album_id=track.album_id,
        artwork_url=track.artwork_url,
        video_id=best_result["videoId"],
        yt_title=best_result.get("title", ""),
        yt_artist=result_artists(best_result),
        yt_album=result_album(best_result),
        score=round(best_passing["score"], 3),
        title_score=round(best_passing["title_score"], 3),
        artist_score=round(best_passing["artist_score"], 3),
        album_score=round(best_passing["album_score"], 3),
        yt_result_type=best_result.get("resultType", ""),
        query=best_passing["query"],
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
        raise RuntimeError(f"Failed to fetch YouTube Music playlist {playlist_id}") from exc

    if not isinstance(playlist, dict) or not isinstance(playlist.get("tracks"), list):
        raise RuntimeError(
            f"Playlist {playlist_id} response is missing a tracks list"
        )
    tracks = playlist["tracks"]
    reported_count = playlist.get("trackCount")
    if (
        isinstance(reported_count, int)
        or isinstance(reported_count, str) and reported_count.isdigit()
    ) and int(reported_count) != len(tracks):
        raise RuntimeError(
            f"Playlist {playlist_id} returned {len(tracks)} tracks but reports "
            f"trackCount={reported_count}"
        )

    items: list[dict[str, str]] = []
    for index, track in enumerate(tracks, 1):
        video_id = track.get("videoId")
        set_video_id = track.get("setVideoId")
        if not video_id or not set_video_id:
            raise RuntimeError(
                f"Playlist {playlist_id} item {index} is missing videoId or setVideoId"
            )
        items.append({"videoId": video_id, "setVideoId": set_video_id})
    return items


def _require_playlist_mutation_success(result: Any, operation: str) -> None:
    status = result.get("status", "") if isinstance(result, dict) else result
    if status in ("STATUS_SUCCEEDED", "SUCCEEDED"):
        return
    raise RuntimeError(f"YouTube Music playlist {operation} acknowledgement is missing or unsuccessful")


@lru_cache(maxsize=4096)
def _normalize_substitution_title(value: str) -> str:
    """Normalize punctuation only, retaining featured artists and version markers."""
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(
        r"[^0-9a-z가-힣\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\ud7b0-\ud7ff"
        r"\u3040-\u30ff\u4e00-\u9fff]+",
        " ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def _playlist_video_details(
    ytmusic: YTMusic, video_id: str, metadata_cache: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    cached = metadata_cache.get(video_id)
    if cached is not None:
        return cached
    response_received = False
    try:
        payload = ytmusic.get_song(video_id)
        response_received = True
        if not isinstance(payload, dict) or not isinstance(payload.get("videoDetails"), dict):
            raise ValueError("response is missing videoDetails")
        video = payload["videoDetails"]
        if video.get("videoId") != video_id:
            raise ValueError("videoDetails does not match the requested video ID")
        raw_title = video.get("title")
        raw_author = video.get("author")
        length_seconds = int(video.get("lengthSeconds") or 0)
        if (
            not isinstance(raw_title, str) or not raw_title.strip()
            or not isinstance(raw_author, str) or not raw_author.strip()
            or length_seconds <= 0
        ):
            raise ValueError("videoDetails is missing title, author, or duration")
        details = {
            "video_id": video_id,
            "title": raw_title,
            "normalized_title": _normalize_substitution_title(raw_title),
            "author": raw_author,
            "normalized_author": _normalize_substitution_title(raw_author),
            "length_seconds": length_seconds,
            "music_video_type": str(video.get("musicVideoType") or ""),
            "error": "",
        }
    except Exception as exc:
        details = {
            "video_id": video_id,
            "title": "", "normalized_title": "", "author": "", "normalized_author": "",
            "length_seconds": 0, "music_video_type": "",
            "error": f"{type(exc).__name__}: {exc}",
            "metadata_missing": response_received and isinstance(exc, (ValueError, TypeError)),
        }
    if not details["error"]:
        metadata_cache[video_id] = details
    return details


def get_verified_video_metadata(
    ytmusic: YTMusic,
    video_id: str,
    *,
    metadata_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Read current, exact-video bilingual metadata without persistent cache writes.

    Missing identity is inconclusive (None); API errors propagate so callers
    cannot turn an outage into a cache rejection and an unrelated fresh match.
    The supplied cache belongs to one execution, never to the persistent alias DB.
    """
    cache = metadata_cache if metadata_cache is not None else {}
    cache_key = f"verified:{video_id}"
    if cache_key in cache:
        return cache[cache_key]["metadata"]
    base = _playlist_video_details(ytmusic, video_id, cache)
    if base["error"]:
        if not base.get("metadata_missing"):
            raise RuntimeError(f"Unable to verify video {video_id}: {base['error']}")
        return None

    details = {
        "video_id": video_id,
        "title": base["title"], "artist": base["author"], "album": "",
        "length_seconds": base["length_seconds"],
        "music_video_type": base["music_video_type"],
    }
    artist_ids: set[str] | None = None
    names_by_id: dict[str, list[str]] = {}
    try:
        for language in ("ko", "en"):
            client_key = f"verified_client:{language}"
            if client_key not in cache:
                cache[client_key] = {"client": make_ytmusic(None, language=language)}
            client = cache[client_key]["client"]
            payload = _watch_playlist_for_metadata(client, video_id)
            tracks = payload.get("tracks") if isinstance(payload, dict) else None
            exact_tracks = [
                row for row in tracks if isinstance(row, dict) and row.get("videoId") == video_id
            ] if isinstance(tracks, list) else []
            if len(exact_tracks) != 1:
                return None
            track = exact_tracks[0]
            artists = track.get("artists")
            title = track.get("title")
            if isinstance(artists, list):
                # The watch parser includes unlinked view/like/year labels in
                # artists. Ignore only these numeric display tokens; an actual
                # artist name without an ID remains inconclusive.
                artists = [
                    artist for artist in artists
                    if not (
                        isinstance(artist, dict) and artist.get("id") is None
                        and isinstance(artist.get("name"), str)
                        and re.fullmatch(
                            r"(?:(?:조회수|좋아요)\s*\d[\d.,]*\s*[천만억]?\s*[회개]"
                            r"|(?:19|20)\d{2}년?"
                            r"|\d[\d.,]*\s*[KMB]?\s*(?:views?|likes?))",
                            artist["name"].strip(), re.IGNORECASE,
                        )
                    )
                ]
            if (
                not isinstance(title, str) or not title.strip()
                or not isinstance(artists, list) or not artists
                or any(
                    not isinstance(artist, dict)
                    or not isinstance(artist.get("id"), str) or not artist["id"].strip()
                    or not isinstance(artist.get("name"), str) or not artist["name"].strip()
                    for artist in artists
                )
            ):
                return None
            ids = {artist["id"] for artist in artists}
            if artist_ids is not None and ids != artist_ids:
                return None
            artist_ids = ids
            localized_names = []
            for artist in artists:
                artist_key = f"verified_artist:{language}:{artist['id']}"
                if artist_key not in cache:
                    artist_payload = client.get_artist(artist["id"])
                    name = artist_payload.get("name") if isinstance(artist_payload, dict) else None
                    if not isinstance(name, str) or not name.strip():
                        return None
                    cache[artist_key] = {"name": name}
                name = cache[artist_key].get("name")
                localized_names.append(name)
                names_by_id.setdefault(artist["id"], []).extend([artist["name"], name])
            album = track.get("album")
            album_name = album.get("name") if isinstance(album, dict) else ""
            details[f"title_{language}"] = title
            details[f"artist_{language}"] = ", ".join(localized_names)
            details[f"album_{language}"] = album_name if isinstance(album_name, str) else ""
    except Exception as exc:
        raise RuntimeError(f"Unable to retrieve verified metadata for {video_id}: {exc}") from exc

    details["artist_ids"] = sorted(artist_ids or ())
    details["artist_names_by_id"] = {
        artist_id: unique_values(names) for artist_id, names in names_by_id.items()
    }
    # Preserve the caller's locale when repairing canonical metadata, while
    # using the verified performer rather than get_song's uploader/author.
    language = getattr(ytmusic, "language", "ko")
    language = language if language in ("ko", "en") else "ko"
    details["title"] = details[f"title_{language}"]
    details["artist"] = details[f"artist_{language}"]
    details["album"] = details[f"album_{language}"] or details["album_ko"] or details["album_en"]
    cache[cache_key] = {"metadata": details}
    return details


def _compare_playlist_video_ids(
    ytmusic: YTMusic,
    expected: list[str],
    actual: list[str],
    *,
    metadata_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare every position and retain evidence for each ID substitution."""
    metadata_cache = metadata_cache if metadata_cache is not None else {}
    differences: list[dict[str, Any]] = []
    expected_positions = {video_id: index for index, video_id in enumerate(expected)}
    actual_positions = {video_id: index for index, video_id in enumerate(actual)}

    for index in range(max(len(expected), len(actual))):
        expected_id = expected[index] if index < len(expected) else ""
        actual_id = actual[index] if index < len(actual) else ""
        if expected_id == actual_id:
            continue

        if not expected_id or not actual_id:
            differences.append(
                {
                    "position": index + 1,
                    "expected_id": expected_id,
                    "actual_id": actual_id,
                    "accepted": False,
                    "reason": "missing_expected_id" if not expected_id else "missing_actual_id",
                    "expected": None,
                    "actual": None,
                    "checks": {"title": False, "duration": False, "author": False},
                }
            )
            continue

        # A known ID at another slot is an ordering error, not a provider-side
        # equivalent-ID substitution. It needs no metadata requests to reject.
        if (
            actual_positions.get(expected_id, index) != index
            or expected_positions.get(actual_id, index) != index
        ):
            differences.append(
                {
                    "position": index + 1,
                    "expected_id": expected_id,
                    "actual_id": actual_id,
                    "accepted": False,
                    "reason": "order_mismatch",
                    "expected": None,
                    "actual": None,
                    "checks": {
                        "title": False,
                        "duration": False,
                        "author": False,
                        "version": False,
                    },
                }
            )
            continue

        expected_details = _playlist_video_details(ytmusic, expected_id, metadata_cache)
        actual_details = _playlist_video_details(ytmusic, actual_id, metadata_cache)
        expected_title = expected_details["normalized_title"]
        actual_title = actual_details["normalized_title"]
        expected_seconds = expected_details["length_seconds"]
        actual_seconds = actual_details["length_seconds"]
        metadata_ok = not expected_details["error"] and not actual_details["error"]
        title_matches = bool(expected_title) and expected_title == actual_title
        duration_known = bool(expected_seconds and actual_seconds)
        tolerance = (
            max(10, round(max(expected_seconds, actual_seconds) * 0.05))
            if duration_known
            else 0
        )
        duration_matches = duration_known and abs(expected_seconds - actual_seconds) <= tolerance
        version_matches = not _has_recording_version_mismatch(
            [expected_details["title"]], [actual_details["title"]]
        )
        # A get_song author can be an uploader shared by several artists.
        # Every different-ID substitution needs complete watch artist identity.
        author_matches = False
        artist_identity = {"status": "not_checked", "reason": "title_duration_or_version_not_verified"}
        if metadata_ok and title_matches and duration_matches and version_matches:
            try:
                expected_identity = get_verified_video_metadata(
                    ytmusic, expected_id, metadata_cache=metadata_cache
                )
                actual_identity = get_verified_video_metadata(
                    ytmusic, actual_id, metadata_cache=metadata_cache
                )
                artist_identity = {
                    "expected": expected_identity,
                    "actual": actual_identity,
                }
                if not expected_identity or not actual_identity:
                    metadata_ok = False
                    artist_identity["error"] = "artist_identity_missing"
                author_matches = bool(
                    expected_identity and actual_identity
                    and expected_identity["artist_ids"]
                    and expected_identity["artist_ids"] == actual_identity["artist_ids"]
                )
            except RuntimeError as exc:
                metadata_ok = False
                artist_identity = {"error": str(exc)}
        accepted = bool(
            metadata_ok
            and title_matches
            and duration_matches
            and author_matches
            and version_matches
        )
        if not metadata_ok:
            reason = "metadata_error"
        elif not version_matches:
            reason = "version_mismatch"
        elif not title_matches:
            reason = "title_mismatch"
        elif not duration_matches:
            reason = "duration_mismatch"
        elif not author_matches:
            reason = "author_mismatch"
        else:
            reason = "metadata_equivalent"

        differences.append(
            {
                "position": index + 1,
                "expected_id": expected_id,
                "actual_id": actual_id,
                "accepted": accepted,
                "reason": reason,
                "expected": expected_details,
                "actual": actual_details,
                "artist_identity": artist_identity,
                "checks": {
                    "title": title_matches,
                    "duration": duration_matches,
                    "duration_tolerance_seconds": tolerance,
                    "author": author_matches,
                    "version": version_matches,
                },
            }
        )

    return {
        "matches": len(expected) == len(actual)
        and all(difference["accepted"] for difference in differences),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "differences": differences,
    }


def _playlist_video_ids_match(
    ytmusic: YTMusic,
    expected: list[str],
    actual: list[str],
) -> bool:
    """Compatibility wrapper for callers that only need the final boolean."""
    comparison = _compare_playlist_video_ids(ytmusic, expected, actual)
    for difference in comparison["differences"]:
        if difference["accepted"]:
            LOG.info(
                "Accepted YouTube Music equivalent video substitution: %s -> %s",
                difference["expected_id"],
                difference["actual_id"],
            )
    return bool(comparison["matches"])


class PlaylistMutationUncertain(RuntimeError):
    """An attempted mutation lacks durable, complete acknowledgement. Never replay it."""


def _playlist_slots(items: list[dict[str, str]]) -> list[str]:
    slots = [item["setVideoId"] for item in items]
    if len(set(slots)) != len(slots):
        raise RuntimeError("Playlist contains duplicate setVideoIds; ownership is ambiguous")
    return slots


def _same_owned_slots(actual: list[dict[str, str]], expected: list[dict[str, str]]) -> bool:
    # An owned slot may expose a provider-substituted video ID. This proves item
    # ownership only; _compare_playlist_video_ids still decides song identity.
    return _playlist_slots(actual) == _playlist_slots(expected)


def _addition_receipts(result: Any, requested: list[str]) -> list[dict[str, str]]:
    _require_playlist_mutation_success(result, "addition")
    rows = result.get("playlistEditResults") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != len(requested) or len(set(requested)) != len(requested):
        raise ValueError("Addition receipt does not cover each unique requested ID")
    mapped: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Addition receipt is incomplete")
        video_id, slot = row.get("videoId"), row.get("setVideoId")
        if not isinstance(video_id, str) or not isinstance(slot, str) or not slot.strip():
            raise ValueError("Addition receipt is missing videoId or setVideoId")
        if video_id not in requested or video_id in mapped:
            raise ValueError("Addition receipt has ambiguous request-to-item mapping")
        mapped[video_id] = {"videoId": video_id, "setVideoId": slot}
    items = [mapped[video_id] for video_id in requested]
    _playlist_slots(items)
    return items


def _replace_playlist_contents(
    ytmusic: YTMusic,
    playlist_id: str,
    current_items: list[dict[str, str]],
    target_video_ids: list[str],
    *,
    evidence: Any,
    phase: str,
    allow_duplicates: bool = False,
    remove_items: list[dict[str, str]] | None = None,
    require_exact_items: bool = False,
) -> list[dict[str, str]]:
    """Mutate once per durably recorded intent; retain every acknowledged slot."""
    if len(set(target_video_ids)) != len(target_video_ids):
        raise RuntimeError("Cannot prove receipt order for duplicate requested IDs")
    expected = list(current_items)
    _playlist_slots(expected)
    selected_items = list(current_items) if remove_items is None else list(remove_items)
    selected_slots = set(_playlist_slots(selected_items))
    if [item for item in current_items if item["setVideoId"] in selected_slots] != selected_items:
        raise RuntimeError("Selected removals do not belong to the exact current snapshot")
    for operation, chunks in (
        ("remove", chunked(selected_items, 50)),
        ("add", chunked(target_video_ids, 50)),
    ):
        for chunk_order, chunk in enumerate(chunks, 1):
            before = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(before, expected):
                raise RuntimeError("Playlist slots changed outside the audited mutation; refusing further mutation")
            if require_exact_items and before != expected:
                raise RuntimeError("Playlist video identity changed after fallback verification")
            items = chunk if operation == "remove" else [{"videoId": video_id} for video_id in chunk]
            intent = evidence({
                "phase": phase, "operation": operation, "state": "intent",
                "chunk_order": chunk_order, "attempt": 1,
                "items": items, "before_items": before,
            })
            try:
                if operation == "remove":
                    # Remove observed IDs for the exact audited slots, even when
                    # the provider has changed the video ID exposed by that slot.
                    selected = {item["setVideoId"] for item in chunk}
                    removed = [item for item in before if item["setVideoId"] in selected]
                    result = ytmusic.remove_playlist_items(playlist_id, removed)
                    _require_playlist_mutation_success(result, "removal")
                    acknowledged = removed
                    after = [item for item in before if item["setVideoId"] not in selected]
                else:
                    result = ytmusic.add_playlist_items(playlist_id, chunk, duplicates=allow_duplicates)
                    acknowledged = _addition_receipts(result, chunk)
                    after = before + acknowledged
                    _playlist_slots(after)
                # No later mutation is permitted before this acknowledgement commits.
                evidence({
                    "phase": phase, "operation": operation, "state": "ack",
                    "chunk_order": chunk_order, "attempt": 1,
                    "intent_seq": intent["seq"], "items": acknowledged, "after_items": after,
                })
            except Exception as exc:
                try:
                    evidence({
                        "phase": phase, "operation": operation, "state": "ambiguous",
                        "chunk_order": chunk_order, "attempt": 1,
                        "intent_seq": intent["seq"], "items": items,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                except Exception:
                    LOG.error("Unable to record ambiguous mutation; durable intent remains unresolved")
                raise PlaylistMutationUncertain(
                    f"{operation} acknowledgement is uncertain; automatic retry/restore is forbidden"
                ) from exc
            expected = after
            LOG.info("Acknowledged %s of %d playlist items", operation, len(chunk))
    return expected


def _publish_without_unverified_substitutions(
    ytmusic: YTMusic,
    playlist_id: str,
    requested: list[str],
    actual: list[dict[str, str]],
    comparison: dict[str, Any],
    *,
    evidence: Any,
) -> tuple[list[dict[str, str]], dict[str, Any]] | None:
    """Omit only observed bad substitutions; preserve every verified slot/order."""
    rejected = [row for row in comparison["differences"] if not row["accepted"]]
    allowed = {"title_mismatch", "author_mismatch", "version_mismatch", "duration_mismatch", "metadata_error"}
    if (not rejected or len(actual) != len(requested)
            or any(row["reason"] not in allowed for row in rejected)):
        return None
    positions = {row["position"] - 1 for row in rejected}
    effective = [video_id for index, video_id in enumerate(requested) if index not in positions]
    if not effective:
        return None  # A total failure is not a usable partial publication.
    excluded = [{"position": row["position"], "requested_video_id": row["expected_id"],
                 "actual_video_id": row["actual_id"], "reason": row["reason"]} for row in rejected]
    policy = {"publication_mode": "partial", "effective_video_ids": effective, "excluded_items": excluded}
    fresh = get_existing_playlist_items(ytmusic, playlist_id)
    if fresh != actual:
        raise RuntimeError("Playlist changed after fallback planning; refusing omission")
    # Preserve the approved reduced target before deleting anything, including
    # when the process terminates after a deletion ACK but before finalization.
    evidence({"phase": "publish", "operation": "observe", "state": "verified",
              "chunk_order": 0, "attempt": 1, "items": fresh,
              "verification_matches": False, "observation_complete": True,
              "differences": comparison["differences"], **policy})
    removed = [item for index, item in enumerate(fresh) if index in positions]
    expected = _replace_playlist_contents(
        ytmusic, playlist_id, fresh, [], evidence=evidence, phase="publish",
        remove_items=removed, require_exact_items=True,
    )
    remaining = get_existing_playlist_items(ytmusic, playlist_id)
    if remaining != expected:
        raise RuntimeError("Partial publication differs from the acknowledged retained items")
    verified = _compare_playlist_video_ids(ytmusic, effective, [item["videoId"] for item in remaining])
    if not verified["matches"]:
        raise RuntimeError("Retained playlist items failed partial-publication verification")
    evidence({"phase": "publish", "operation": "observe", "state": "verified",
              "chunk_order": 0, "attempt": 1, "items": remaining,
              "verification_matches": True, "observation_complete": True,
              "differences": comparison["differences"], **policy})
    return remaining, verified


def _identity_review_required(differences: list[dict[str, Any]]) -> bool:
    return any(
        not item.get("accepted") and item.get("reason") in {
            "title_mismatch", "author_mismatch", "version_mismatch", "duration_mismatch",
        }
        for item in differences
    )


def _audit_playlist_items(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise RuntimeError("Audit is missing a complete playlist item list")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid audit playlist item")
        video_id = item.get("videoId") or item.get("video_id")
        slot = item.get("setVideoId") or item.get("set_video_id")
        if not isinstance(video_id, str) or not video_id or not isinstance(slot, str) or not slot:
            raise RuntimeError("Audit playlist item is missing identity/ownership fields")
        normalized.append({"videoId": video_id, "setVideoId": slot})
    _playlist_slots(normalized)
    return normalized


def _recoverable_playlist_items(run: dict[str, Any]) -> list[dict[str, str]]:
    """Replay durable receipts, never infer ownership from matching raw IDs."""
    payload = run.get("recovery_payload") or {}
    snapshot = payload.get("snapshot") or {}
    baseline = snapshot.get("existing_items")
    if run.get("evidence_version") != 1 or not isinstance(baseline, list):
        raise RuntimeError("Legacy audit has no complete item ownership evidence")
    expected = _audit_playlist_items(baseline)
    seen_slots = set(_playlist_slots(expected))
    pending: dict[int, dict[str, Any]] = {}
    for event in payload.get("events", []):
        if event.get("operation") not in {"remove", "add"}:
            continue
        if event.get("state") == "intent":
            if pending or not _same_owned_slots(_audit_playlist_items(event.get("before_items")), expected):
                raise RuntimeError("Mutation evidence has an unresolved or inconsistent intent")
            pending[event["seq"]] = event
        elif event.get("state") == "ambiguous":
            raise RuntimeError("Mutation acknowledgement is ambiguous; manual review required")
        elif event.get("state") == "ack":
            intent = pending.pop(event.get("intent_seq"), None)
            if not intent or intent["operation"] != event["operation"] or intent["phase"] != event["phase"]:
                raise RuntimeError("Acknowledgement does not match its durable intent")
            items = _audit_playlist_items(event.get("items"))
            if event["operation"] == "remove":
                if _playlist_slots(items) != _playlist_slots(_audit_playlist_items(intent["items"])):
                    raise RuntimeError("Removal receipt does not match the requested slots")
                removed = set(_playlist_slots(items))
                expected = [item for item in expected if item["setVideoId"] not in removed]
            else:
                requested = [item.get("videoId") or item.get("video_id") for item in intent["items"]]
                validated = _addition_receipts(
                    {"status": "STATUS_SUCCEEDED", "playlistEditResults": items}, requested
                )
                if seen_slots.intersection(_playlist_slots(validated)):
                    raise RuntimeError("Addition receipt reuses a previously observed playlist slot")
                seen_slots.update(_playlist_slots(validated))
                expected += validated
            _playlist_slots(expected)
            after_items = _audit_playlist_items(event.get("after_items"))
            if expected != after_items:
                # Provider-exposed aliases can change before a later chunk;
                # tokens/order, not video identity, establish execution ownership.
                if not _same_owned_slots(expected, after_items):
                    raise RuntimeError("Receipt state is inconsistent")
                expected = after_items
    if pending:
        raise RuntimeError("Mutation was interrupted before a durable acknowledgement")
    return expected


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
    """Publish with durable item ownership; never equate recovery with song identity."""
    if not video_ids or any(not isinstance(value, str) or not value.strip() for value in video_ids):
        raise ValueError("Playlist video IDs must be non-empty strings")
    if len(set(video_ids)) != len(video_ids):
        raise ValueError("Playlist video IDs must be unique")
    if dry_run:
        LOG.info("Dry run enabled. Skipping playlist metadata, item changes and audit writes.")
        return
    if not db_path:
        raise RuntimeError("Durable playlist audit is required; refusing external mutation")

    from hype_db import (
        append_playlist_update_evidence, finish_playlist_update, get_pending_playlist_recovery,
        get_playlist_update_run, record_playlist_update,
    )
    # Source crawl/matching has already completed. Only publication is blocked.
    pending = get_pending_playlist_recovery(db_path, playlist_id)
    if pending:
        raise RuntimeError(
            f"Playlist {playlist_id} has pending recovery "
            f"(run_id={pending.get('update_run_id')}, status={pending.get('status')}, "
            f"started_at={pending.get('started_at')}); publication is blocked. "
            "Inspect this run with reconcile_playlist_update.py --run-id and --playlist-id; "
            "source collection and matching are not classified by this publication error."
        )
    existing = get_existing_playlist_items(ytmusic, playlist_id)
    _playlist_slots(existing)
    existing_ids = [item["videoId"] for item in existing]
    metadata_cache: dict[str, dict[str, Any]] = {}
    initial = _compare_playlist_video_ids(ytmusic, video_ids, existing_ids, metadata_cache=metadata_cache)
    claim_token = uuid.uuid4().hex
    run_id = record_playlist_update(
        db_path, playlist_id=playlist_id, service=service, job_name=job_name,
        requested_video_ids=video_ids, existing_video_ids=existing_ids,
        existing_items=existing, dry_run=False, claim_token=claim_token,
    )
    if not run_id:
        raise RuntimeError("Playlist audit returned no run ID; refusing external mutation")

    def evidence(event: dict[str, Any]) -> dict[str, Any]:
        return append_playlist_update_evidence(db_path, run_id, event, claim_token=claim_token)

    def finish(status: str, actual: list[dict[str, str]], comparison: dict[str, Any], error: str = "",
               *, observation_complete: bool = True) -> None:
        finish_playlist_update(
            db_path, run_id, status=status, actual_video_ids=[item["videoId"] for item in actual],
            actual_items=actual, observation_complete=observation_complete,
            error=error, differences=comparison.get("differences", []), claim_token=claim_token,
        )

    def observe() -> tuple[list[dict[str, str]], bool]:
        try:
            return get_existing_playlist_items(ytmusic, playlist_id), True
        except Exception as exc:
            LOG.error("Final playlist observation unavailable: %s", exc)
            return [], False

    # Different chart editions need not match the previous list semantically.
    # A complete audited baseline protects replacement; the new slots below
    # receive strict verification and, when possible, per-item omission.
    metadata_errors = [row for row in initial["differences"] if row["reason"] == "metadata_error"]
    if len(metadata_errors) == len(video_ids) == len(existing_ids):
        finish("verification_failed", existing, initial, "No requested item can be verified during metadata outage")
        raise RuntimeError("All requested substitutions lack metadata; preserving the existing playlist")
    if description or playlist_name:
        try:
            kwargs = {}
            if playlist_name:
                kwargs["title"] = playlist_name
            if description:
                kwargs["description"] = description
            ytmusic.edit_playlist(playlist_id, **kwargs)
        except Exception as exc:
            LOG.warning("Failed to update playlist metadata: %s", exc)
    if initial["matches"]:
        finish("skipped_current", existing, initial)
        LOG.info("Playlist already matches requested order; no item removal/addition.")
        return
    try:
        current = get_existing_playlist_items(ytmusic, playlist_id)
        if current != existing:
            raise RuntimeError("Playlist changed after its full audit snapshot")
    except Exception as exc:
        actual, complete = observe()
        finish("recovery_required", actual, initial, str(exc), observation_complete=complete)
        raise

    failure: Exception | None = None
    publication_verified = False
    comparison: dict[str, Any] = {"matches": False, "differences": []}
    try:
        expected_items = _replace_playlist_contents(
            ytmusic, playlist_id, current, video_ids, evidence=evidence, phase="publish",
        )
        for attempt in range(3):
            actual = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(actual, expected_items):
                raise RuntimeError("Playlist item ownership/order differs from acknowledged additions")
            comparison = _compare_playlist_video_ids(
                ytmusic, video_ids, [item["videoId"] for item in actual], metadata_cache=metadata_cache,
            )
            if comparison["matches"]:
                publication_verified = True
                finish("published", actual, comparison)
                return
            if attempt < 2:
                time.sleep(2)
        partial = _publish_without_unverified_substitutions(
            ytmusic, playlist_id, video_ids, actual, comparison, evidence=evidence,
        )
        if partial is not None:
            actual, verified = partial
            publication_verified = True
            finish("published", actual, verified)
            LOG.warning(
                "Partial publication completed: playlist=%s run_id=%s requested=%d published=%d omitted=%s",
                playlist_id, run_id, len(video_ids), len(actual),
                json.dumps([{key: row[key] for key in ("position", "expected_id", "actual_id", "reason")}
                            for row in comparison["differences"] if not row["accepted"]], ensure_ascii=False),
            )
            return
        failure = RuntimeError("Playlist verification failed: " + json.dumps(comparison["differences"], ensure_ascii=False))
    except Exception as exc:
        failure = exc

    if _identity_review_required(comparison["differences"]):
        # Persist the rejection BEFORE rollback: a crash after a restore ACK
        # must not turn the next run into an apparently harmless old snapshot.
        evidence({
            "phase": "publish", "operation": "observe", "state": "verified",
            "chunk_order": 0, "attempt": 1, "items": actual,
            "verification_matches": False, "identity_review_required": True,
            "differences": comparison["differences"],
        })
    actual, complete = observe()
    # A lost response/ACK cannot be made atomic with the provider. Preserve the
    # pending intent and stop, including when the uncertain operation was restore.
    if isinstance(failure, PlaylistMutationUncertain) or publication_verified:
        finish("recovery_required", actual, comparison, str(failure), observation_complete=complete)
        raise failure
    try:
        run = get_playlist_update_run(db_path, run_id, read_only=True)
        expected = _recoverable_playlist_items(run)
        current = get_existing_playlist_items(ytmusic, playlist_id)
        if not _same_owned_slots(current, expected):
            raise RuntimeError("Current slots are not owned by this execution; refusing destructive restore")
        # Even a same raw-ID list with new/unowned tokens must not enter this path.
        previous = _compare_playlist_video_ids(
            ytmusic, existing_ids, [item["videoId"] for item in current], metadata_cache=metadata_cache,
        )
        if not previous["matches"]:
            expected = _replace_playlist_contents(
                ytmusic, playlist_id, current, existing_ids, evidence=evidence,
                phase="restore", allow_duplicates=True,
            )
            current = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(current, expected):
                raise RuntimeError("Restored item ownership/order differs from durable receipts")
            previous = _compare_playlist_video_ids(
                ytmusic, existing_ids, [item["videoId"] for item in current], metadata_cache=metadata_cache,
            )
        if not previous["matches"]:
            raise RuntimeError("Restored playlist does not match the pre-update snapshot")
        needs_review = _identity_review_required(comparison["differences"])
        evidence({
            "phase": "restore", "operation": "observe", "state": "verified",
            "chunk_order": 0, "attempt": 1, "items": current,
            "restore_verified": True, "identity_review_required": needs_review,
        })
        finish("recovery_required" if needs_review else "restored", current, comparison, str(failure))
    except Exception as restore_exc:
        final, complete = observe()
        finish("recovery_required", final, comparison, f"{failure}; restore failed: {restore_exc}",
               observation_complete=complete)
        raise RuntimeError(f"{failure}; restore failed: {restore_exc}") from failure
    raise RuntimeError(f"{failure}; items restored" + ("; identity review required before republishing" if needs_review else ""))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_ytmusic(auth_file: str | None, client_id: str = "", client_secret: str = "", language: str = "en") -> YTMusic:
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
