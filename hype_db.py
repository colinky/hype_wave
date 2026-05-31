from __future__ import annotations

"""
hype_db.py
----------
Database interface for the Hype Wave project.
Supports dual connection engines:
1. Supabase PostgreSQL (Production): Used if `SUPABASE_DB_URL` is set in the environment.
2. Local SQLite: Used as a fallback for offline development or local testing.

Standardizes SQLite operators (GLOB, INSTR) to standard SQL for seamless engine translation.
"""

import hashlib
import json
import logging
import re
import os
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hype_scoring import calculate_combined_genz_score, calculate_rank_score

LOG = logging.getLogger("hype_db")


KST = timezone(timedelta(hours=9))
MATCHED_STATUSES = {"matched", "cached_match", "proxy_matched", "manual_override"}
DEFAULT_HYPE_WEIGHTS = {"apple": 0.4, "melon_genz": 0.4, "ytmusic": 0.2}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def kst_today() -> str:
    return datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d")


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


class PostgresRow(dict):
    """PostgreSQL result row representation that mimics SQLite's Row object behavior.

    Allows accessing database columns both by string key (e.g., row['title'])
    and by integer index (e.g., row[0]).
    """
    def __init__(self, description, row_tuple):
        self._row_tuple = row_tuple
        super().__init__({desc[0]: val for desc, val in zip(description, row_tuple)})
        
    def __getitem__(self, item):
        if isinstance(item, int):
            return self._row_tuple[item]
        return super().__getitem__(item)


class PostgresCursorWrapper:
    """Cursor wrapper for PostgreSQL query execution.

    Translates psycopg2 cursor results into a list of PostgresRow objects
    to ensure full code compatibility with scripts expecting sqlite3.Row results.
    """
    def __init__(self, cursor):
        self.cursor = cursor
        
    def fetchall(self):
        try:
            rows = self.cursor.fetchall()
            desc = self.cursor.description
            return [PostgresRow(desc, r) for r in rows] if rows is not None else []
        except Exception:
            return []
            
    def fetchone(self):
        try:
            row = self.cursor.fetchone()
            desc = self.cursor.description
            return PostgresRow(desc, row) if row is not None else None
        except Exception:
            return None
            
    @property
    def lastrowid(self):
        # Placeholder property to emulate sqlite3.Cursor.lastrowid
        return None


class PostgresConnectionWrapper:
    """Connection wrapper for PostgreSQL that acts like a sqlite3.Connection.

    Translates SQLite query syntax into standard SQL accepted by PostgreSQL:
    1. Replaces '?' placeholders with '%s'.
    2. Translates SQLite 'GLOB' patterns to standard 'LIKE' constraints.
    3. Converts SQLite 'INSTR' functions to equivalent standard SQL 'LIKE' expressions.
    4. Escapes literal '%' symbols (e.g., standard LIKE operators) to '%%' for psycopg2 formatting.
    """
    def __init__(self, conn):
        self.conn = conn
        self.row_factory = None  # SQLite compatibility dummy
        
    def execute(self, sql: str, parameters=None):
        # Replaces '?' placeholders with '%s' only when NOT enclosed inside single quotes
        sql_pg = re.sub(r"\?(?=(?:[^']*'[^']*')*[^']*$)", "%s", sql)
        # Dynamic compatibility conversion for LIKE standard patterns
        sql_pg = sql_pg.replace("GLOB '????-??-??'", "LIKE '____-__-__'")
        sql_pg = sql_pg.replace("INSTR(reference_period, '-W') = 0", "reference_period NOT LIKE '%-W%'")
        sql_pg = sql_pg.replace("INSTR(reference_period, '-W') > 0", "reference_period LIKE '%-W%'")
        
        # Escape literal % characters for psycopg2 by protecting %s placeholders
        sql_pg = sql_pg.replace('%s', '__PARAM_PLACEHOLDER__')
        sql_pg = sql_pg.replace('%', '%%')
        sql_pg = sql_pg.replace('__PARAM_PLACEHOLDER__', '%s')
        
        cursor = self.conn.cursor()
        cursor.execute(sql_pg, parameters or ())
        return PostgresCursorWrapper(cursor)
        
    def executemany(self, sql: str, seq_of_parameters):
        # Replaces '?' placeholders with '%s' only when NOT enclosed inside single quotes
        sql_pg = re.sub(r"\?(?=(?:[^']*'[^']*')*[^']*$)", "%s", sql)
        # Dynamic compatibility conversion for LIKE standard patterns
        sql_pg = sql_pg.replace("GLOB '????-??-??'", "LIKE '____-__-__'")
        sql_pg = sql_pg.replace("INSTR(reference_period, '-W') = 0", "reference_period NOT LIKE '%-W%'")
        sql_pg = sql_pg.replace("INSTR(reference_period, '-W') > 0", "reference_period LIKE '%-W%'")
        
        # Escape literal % characters for psycopg2 by protecting %s placeholders
        sql_pg = sql_pg.replace('%s', '__PARAM_PLACEHOLDER__')
        sql_pg = sql_pg.replace('%', '%%')
        sql_pg = sql_pg.replace('__PARAM_PLACEHOLDER__', '%s')
        
        from psycopg2.extras import execute_batch
        cursor = self.conn.cursor()
        execute_batch(cursor, sql_pg, seq_of_parameters)
        return PostgresCursorWrapper(cursor)
        
    def commit(self):
        self.conn.commit()
        
    def rollback(self):
        self.conn.rollback()
        
    def close(self):
        self.conn.close()


@contextmanager
def connect(db_path: str | Path):
    """Database connection context manager supporting dual engines.

    Prioritizes Supabase PostgreSQL connection if `SUPABASE_DB_URL` environment
    variable is set, returning a wrapped connection mimicking sqlite3.
    Otherwise, falls back to a local SQLite database connection at `db_path`.
    """
    pg_url = os.environ.get("SUPABASE_DB_URL")
    if pg_url:
        import psycopg2
        import time
        retries = 3
        delay = 1.0
        raw_conn = None
        for i in range(retries):
            try:
                raw_conn = psycopg2.connect(pg_url)
                break
            except psycopg2.OperationalError as exc:
                if i == retries - 1:
                    LOG.error("Failed to connect to Supabase PostgreSQL after %d attempts: %s", retries, exc)
                    raise exc
                wait_time = delay * (2 ** i)
                LOG.warning("Supabase connection failed. Retrying in %.1fs... (%d/%d): %s", wait_time, i + 1, retries, exc)
                time.sleep(wait_time)
        
        conn = PostgresConnectionWrapper(raw_conn)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db(db_path: str | Path) -> None:
    if os.environ.get("SUPABASE_DB_URL"):
        return
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        init_schema(conn)


def run_schema_migrations(conn: sqlite3.Connection) -> None:
    # 1. Rename column in playlist_order
    if table_exists(conn, "playlist_order"):
        cols = table_columns(conn, "playlist_order")
        if "chart_period" in cols and "reference_period" not in cols:
            LOG.info("Migrating table playlist_order: renaming chart_period to reference_period")
            conn.execute("ALTER TABLE playlist_order RENAME COLUMN chart_period TO reference_period")
            conn.execute("DROP INDEX IF EXISTS idx_playlist_order_job_period")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_playlist_order_job_period ON playlist_order(job_name, reference_period)")

    # 2. Rename column in manual_overrides
    if table_exists(conn, "manual_overrides"):
        cols = table_columns(conn, "manual_overrides")
        if "chart_period" in cols and "reference_period" not in cols:
            LOG.info("Migrating table manual_overrides: renaming chart_period to reference_period")
            conn.execute("ALTER TABLE manual_overrides RENAME COLUMN chart_period TO reference_period")

    # 3. Rename column in review_conflicts
    if table_exists(conn, "review_conflicts"):
        cols = table_columns(conn, "review_conflicts")
        if "chart_period" in cols and "reference_period" not in cols:
            LOG.info("Migrating table review_conflicts: renaming chart_period to reference_period")
            conn.execute("ALTER TABLE review_conflicts RENAME COLUMN chart_period TO reference_period")

    # 4. Apply one-time migration: daily chart date shift (-1 day)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT
            )
            """
        )
        row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = 'reference_period_daily_shift'").fetchone()
        if not row:
            LOG.info("Skipping one-time migration: daily chart date shift (-1 day) (Disabled to prevent duplicate shifts)")
            from datetime import datetime, timezone
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                ("reference_period_daily_shift", datetime.now(timezone.utc).isoformat(), "Shift Apple/Melon daily chart dates by -1 day (Disabled)")
            )
            conn.commit()
    except Exception as exc:
        LOG.warning("Failed to run daily chart date shift migration: %s", exc)

    # 5. Spotify Weekly W## format recovery to Friday date
    try:
        row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = 'spotify_weekly_date_recovery'").fetchone()
        if not row:
            LOG.info("Applying one-time migration: Spotify weekly ISO week recovery to scheduled Friday date")
            rows = conn.execute("SELECT DISTINCT reference_period FROM playlist_order WHERE service = 'spotify' AND INSTR(reference_period, '-W') > 0").fetchall()
            for r in rows:
                iso_week = r[0]
                match = re.match(r"(\d{4})-W(\d{2})", iso_week)
                if match:
                    year = int(match.group(1))
                    week = int(match.group(2))
                    from datetime import datetime, timedelta
                    jan4 = datetime(year, 1, 4)
                    iso_year, iso_wk, iso_wd = jan4.isocalendar()
                    monday_wk1 = jan4 - timedelta(days=iso_wd - 1)
                    friday_of_target_wk = monday_wk1 + timedelta(weeks=week - 1, days=4)
                    friday_date_str = friday_of_target_wk.strftime("%Y-%m-%d")
                    
                    conn.execute(
                        "UPDATE playlist_order SET reference_period = ? WHERE service = 'spotify' AND reference_period = ?",
                        (friday_date_str, iso_week)
                    )
                    LOG.info("Converted Spotify week %s to Friday date %s", iso_week, friday_date_str)
            from datetime import datetime, timezone
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                ("spotify_weekly_date_recovery", datetime.now(timezone.utc).isoformat(), "Recover Spotify weekly ISO week to scheduled Friday date")
            )
            conn.commit()
    except Exception as exc:
        LOG.warning("Failed to run Spotify weekly date recovery migration: %s", exc)


def init_schema(conn: Any) -> None:
    if type(conn).__name__ == "PostgresConnectionWrapper":
        return
    run_schema_migrations(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        DROP VIEW IF EXISTS frontend_history_source;
        DROP VIEW IF EXISTS latest_failed_matches;
        DROP VIEW IF EXISTS latest_match_attempts;
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS tracks (
            track_uid TEXT PRIMARY KEY,
            canonical_yt_video_id TEXT,
            yt_title TEXT,
            yt_artist TEXT,
            yt_album TEXT,
            match_status TEXT DEFAULT 'unmatched',
            best_score REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yt_video_ids (
            video_id TEXT PRIMARY KEY,
            track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE,
            is_canonical INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS platform_song_ids (
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE,
            PRIMARY KEY (service, song_id)
        );

        CREATE TABLE IF NOT EXISTS track_list (
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            album_id TEXT,
            title_ko TEXT,
            artist_ko TEXT,
            album_ko TEXT,
            title_en TEXT,
            artist_en TEXT,
            album_en TEXT,
            artwork_url TEXT,
            PRIMARY KEY (service, song_id)
        );

        CREATE TABLE IF NOT EXISTS metadata_lookup_index (
            lookup_key TEXT PRIMARY KEY,
            track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE,
            source TEXT NOT NULL,
            score REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS playlist_order (
            service TEXT NOT NULL,
            job_name TEXT NOT NULL,
            source_variant TEXT NOT NULL DEFAULT 'default',
            reference_period TEXT NOT NULL,
            song_id TEXT NOT NULL,
            rank_order INTEGER NOT NULL,
            PRIMARY KEY (service, job_name, source_variant, reference_period, song_id)
        );

        CREATE TABLE IF NOT EXISTS match_runs (
            run_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            job_name TEXT NOT NULL,
            source_variant TEXT NOT NULL DEFAULT 'default',
            started_at TEXT NOT NULL,
            source TEXT,
            total_tracks INTEGER DEFAULT 0,
            matched_tracks INTEGER DEFAULT 0,
            failed_tracks INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            proxy_hits INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS match_attempts (
            run_id TEXT NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            track_uid TEXT,
            rank_order INTEGER,
            video_id TEXT,
            score REAL DEFAULT 0,
            title_score REAL DEFAULT 0,
            artist_score REAL DEFAULT 0,
            album_score REAL DEFAULT 0,
            yt_result_type TEXT,
            query TEXT,
            status TEXT,
            match_method TEXT,
            origin_method TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, service, song_id, rank_order)
        );

        CREATE TABLE IF NOT EXISTS match_candidates (
            run_id TEXT NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            rank_order INTEGER NOT NULL,
            candidate_order INTEGER NOT NULL,
            video_id TEXT,
            yt_title TEXT,
            yt_artist TEXT,
            yt_album TEXT,
            score REAL DEFAULT 0,
            title_score REAL DEFAULT 0,
            artist_score REAL DEFAULT 0,
            album_score REAL DEFAULT 0,
            yt_result_type TEXT,
            query TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, service, song_id, rank_order, candidate_order)
        );

        CREATE TABLE IF NOT EXISTS manual_overrides (
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'set_canonical',
            target_track_uid TEXT,
            canonical_yt_video_id TEXT,
            reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (service, song_id)
        );

        CREATE TABLE IF NOT EXISTS review_conflicts (
            conflict_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            job_name TEXT,
            source_variant TEXT,
            reference_period TEXT,
            title TEXT,
            artist TEXT,
            album TEXT,
            query TEXT,
            score REAL DEFAULT 0,
            source_file TEXT,
            existing_track_uid TEXT,
            incoming_track_uid TEXT,
            existing_video_id TEXT,
            incoming_video_id TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS migration_reports (
            report_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            rows_read INTEGER DEFAULT 0,
            tracks_seen INTEGER DEFAULT 0,
            conflicts_seen INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            payload_json TEXT
        );

        CREATE TABLE IF NOT EXISTS album_metadata (
            service TEXT NOT NULL,
            album_id TEXT NOT NULL,
            album_name TEXT,
            created_at TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            PRIMARY KEY (service, album_id)
        );

        CREATE TABLE IF NOT EXISTS playlist_update_runs (
            update_run_id TEXT PRIMARY KEY,
            playlist_id TEXT NOT NULL,
            service TEXT,
            job_name TEXT,
            started_at TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            requested_count INTEGER DEFAULT 0,
            existing_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS playlist_update_items (
            update_run_id TEXT NOT NULL REFERENCES playlist_update_runs(update_run_id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            video_id TEXT NOT NULL,
            item_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (update_run_id, action, video_id, item_order)
        );

        """
    )
    _rebuild_lean_schema(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_canonical_yt ON tracks(canonical_yt_video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_song_ids_track ON platform_song_ids(track_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_attempts_video ON match_attempts(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_playlist_order_job_period ON playlist_order(job_name, reference_period)")
    repair_stats = repair_failed_source_bindings(conn)
    if repair_stats.get("updated_bindings") or repair_stats.get("merged_tracks"):
        conn.execute(
            """
            INSERT OR REPLACE INTO migration_reports(
                report_id, source, rows_read, tracks_seen, conflicts_seen, created_at, payload_json
            )
            VALUES (?, 'db_repair', 0, ?, 0, ?, ?)
            """,
            (
                hashlib.sha1(json.dumps(repair_stats, sort_keys=True).encode("utf-8")).hexdigest(),
                int(repair_stats.get("updated_bindings", 0)) + int(repair_stats.get("merged_tracks", 0)),
                utc_now_iso(),
                json.dumps(repair_stats, ensure_ascii=False, sort_keys=True),
            ),
        )
    _create_views(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, applied_at, description)
        VALUES ('db_source_v2_lean', ?, 'Lean DB source of truth schema')
        """,
        (utc_now_iso(),),
    )
    conn.execute("PRAGMA foreign_keys = ON")


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


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _first_existing(cols: set[str], *names: str, default: str = "''") -> str:
    for name in names:
        if name in cols:
            return name
    return default


def _source_variant_from_legacy(value: str, current: str = "") -> str:
    if value == "Gen-10s-Top-100-Daily" or value.endswith(":gen10"):
        return "gen10"
    if value == "Gen-20s-Top-100-Daily" or value.endswith(":gen20"):
        return "gen20"
    return normalize_source_variant(current)


def _rebuild_lean_schema(conn: sqlite3.Connection) -> None:
    _rebuild_index_tables(conn)
    _rebuild_playlist_order(conn)
    _rebuild_match_runs(conn)
    _rebuild_match_attempts(conn)
    _rebuild_match_candidates(conn)
    _rebuild_review_conflicts(conn)
    _rebuild_album_metadata(conn)
    _rebuild_playlist_updates(conn)
    conn.execute("DROP TABLE IF EXISTS raw_crawl_rows")


def _rebuild_index_tables(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "platform_song_ids") and table_columns(conn, "platform_song_ids") != {"service", "song_id", "track_uid"}:
        conn.execute("ALTER TABLE platform_song_ids RENAME TO platform_song_ids_old")
        conn.execute("CREATE TABLE platform_song_ids(service TEXT NOT NULL, song_id TEXT NOT NULL, track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE, PRIMARY KEY(service, song_id))")
        conn.execute("INSERT OR REPLACE INTO platform_song_ids(service, song_id, track_uid) SELECT service, song_id, track_uid FROM platform_song_ids_old")
        conn.execute("DROP TABLE platform_song_ids_old")
    if table_exists(conn, "yt_video_ids") and table_columns(conn, "yt_video_ids") != {"video_id", "track_uid", "is_canonical"}:
        conn.execute("ALTER TABLE yt_video_ids RENAME TO yt_video_ids_old")
        conn.execute("CREATE TABLE yt_video_ids(video_id TEXT PRIMARY KEY, track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE, is_canonical INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT OR REPLACE INTO yt_video_ids(video_id, track_uid, is_canonical) SELECT video_id, track_uid, COALESCE(is_canonical, 0) FROM yt_video_ids_old")
        conn.execute("DROP TABLE yt_video_ids_old")
    if table_exists(conn, "metadata_lookup_index") and table_columns(conn, "metadata_lookup_index") != {"lookup_key", "track_uid", "source", "score"}:
        conn.execute("ALTER TABLE metadata_lookup_index RENAME TO metadata_lookup_index_old")
        conn.execute("CREATE TABLE metadata_lookup_index(lookup_key TEXT PRIMARY KEY, track_uid TEXT NOT NULL REFERENCES tracks(track_uid) ON DELETE CASCADE, source TEXT NOT NULL, score REAL DEFAULT 0)")
        conn.execute("INSERT OR REPLACE INTO metadata_lookup_index(lookup_key, track_uid, source, score) SELECT lookup_key, track_uid, source, COALESCE(score, 0) FROM metadata_lookup_index_old")
        conn.execute("DROP TABLE metadata_lookup_index_old")


def _rebuild_playlist_order(conn: sqlite3.Connection) -> None:
    target = {"service", "job_name", "source_variant", "reference_period", "song_id", "rank_order"}
    if not table_exists(conn, "playlist_order"):
        return
    if table_columns(conn, "playlist_order") == target:
        date_period_jobs = conn.execute(
            """
            SELECT DISTINCT job_name
            FROM playlist_order
            WHERE reference_period GLOB '????-??-??'
            """
        ).fetchall()
        needs_period_normalize = any(job_frequency(row["job_name"]) == "weekly" for row in date_period_jobs)
        if not needs_period_normalize:
            return
    rows = conn.execute("SELECT * FROM playlist_order").fetchall()
    temp_table = "playlist_order_rebuild_tmp"
    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
    conn.execute(
        f"""
        CREATE TABLE {temp_table}(
            service TEXT NOT NULL,
            job_name TEXT NOT NULL,
            source_variant TEXT NOT NULL DEFAULT 'default',
            reference_period TEXT NOT NULL,
            song_id TEXT NOT NULL,
            rank_order INTEGER NOT NULL,
            PRIMARY KEY(service, job_name, source_variant, reference_period, song_id)
        )
        """
    )
    for row in rows:
        data = dict(row)
        legacy_name = str(data.get("playlist_name") or "")
        job_name = legacy_to_job_name(data.get("job_name") or legacy_name)
        variant = _source_variant_from_legacy(legacy_name, str(data.get("source_variant") or ""))
        date_value = data.get("chart_period_end") or data.get("chart_period_start") or data.get("crawl_time") or data.get("chart_period") or data.get("reference_period")
        reference_period = reference_period_for_date(job_name, str(date_value or ""), str(data.get("reference_period") or data.get("chart_period") or ""))
        service = normalized_service(data.get("service"))
        song_id = str(data.get("song_id") or "").strip()
        rank_order = int(data.get("rank_order") or 0)
        if not service or not job_name or not song_id or not reference_period or not rank_order:
            continue
        conn.execute(
            f"""
            INSERT INTO {temp_table}(service, job_name, source_variant, reference_period, song_id, rank_order)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
                rank_order = excluded.rank_order
            """,
            (service, job_name, variant, reference_period, song_id, rank_order),
        )
    conn.execute("DROP TABLE playlist_order")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO playlist_order")


def _rebuild_match_runs(conn: sqlite3.Connection) -> None:
    target = {
        "run_id", "service", "job_name", "source_variant", "started_at", "source",
        "total_tracks", "matched_tracks", "failed_tracks", "cache_hits", "proxy_hits", "created_at",
    }
    if not table_exists(conn, "match_runs") or table_columns(conn, "match_runs") == target:
        return
    rows = conn.execute("SELECT * FROM match_runs").fetchall()
    conn.execute("ALTER TABLE match_runs RENAME TO match_runs_old")
    conn.execute(
        """
        CREATE TABLE match_runs(
            run_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            job_name TEXT NOT NULL,
            source_variant TEXT NOT NULL DEFAULT 'default',
            started_at TEXT NOT NULL,
            source TEXT,
            total_tracks INTEGER DEFAULT 0,
            matched_tracks INTEGER DEFAULT 0,
            failed_tracks INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            proxy_hits INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    for row in rows:
        data = dict(row)
        legacy_name = str(data.get("playlist_name") or "")
        job_name = legacy_to_job_name(data.get("job_name") or legacy_name)
        variant = _source_variant_from_legacy(legacy_name, str(data.get("source_variant") or ""))
        conn.execute(
            """
            INSERT OR REPLACE INTO match_runs(
                run_id, service, job_name, source_variant, started_at, source, total_tracks,
                matched_tracks, failed_tracks, cache_hits, proxy_hits, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("run_id"),
                normalized_service(data.get("service")),
                job_name,
                variant,
                data.get("started_at") or data.get("created_at") or utc_now_iso(),
                data.get("source") or "",
                int(data.get("total_tracks") or 0),
                int(data.get("matched_tracks") or 0),
                int(data.get("failed_tracks") or 0),
                int(data.get("cache_hits") or 0),
                int(data.get("proxy_hits") or 0),
                data.get("created_at") or utc_now_iso(),
            ),
        )
    conn.execute("DROP TABLE match_runs_old")


def _rebuild_match_attempts(conn: sqlite3.Connection) -> None:
    target = {
        "run_id", "service", "song_id", "track_uid", "rank_order", "video_id", "score",
        "title_score", "artist_score", "album_score", "yt_result_type", "query", "status",
        "match_method", "origin_method", "created_at",
    }
    if not table_exists(conn, "match_attempts") or table_columns(conn, "match_attempts") == target:
        return
    cols = table_columns(conn, "match_attempts")
    conn.execute("ALTER TABLE match_attempts RENAME TO match_attempts_old")
    conn.execute(
        """
        CREATE TABLE match_attempts(
            run_id TEXT NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            track_uid TEXT,
            rank_order INTEGER,
            video_id TEXT,
            score REAL DEFAULT 0,
            title_score REAL DEFAULT 0,
            artist_score REAL DEFAULT 0,
            album_score REAL DEFAULT 0,
            yt_result_type TEXT,
            query TEXT,
            status TEXT,
            match_method TEXT,
            origin_method TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, service, song_id, rank_order)
        )
        """
    )
    select_cols = [
        "run_id", "service", "song_id", "track_uid", "rank_order", "video_id", "score",
        "title_score", "artist_score", "album_score", "yt_result_type", "query", "status",
        "match_method", "origin_method", "created_at",
    ]
    expressions = [name if name in cols else ("0" if name in {"score", "title_score", "artist_score", "album_score"} else "''") for name in select_cols]
    conn.execute(
        f"INSERT OR REPLACE INTO match_attempts({', '.join(select_cols)}) SELECT {', '.join(expressions)} FROM match_attempts_old"
    )
    conn.execute("DROP TABLE match_attempts_old")


def _rebuild_match_candidates(conn: sqlite3.Connection) -> None:
    target = {
        "run_id", "service", "song_id", "rank_order", "candidate_order", "video_id",
        "yt_title", "yt_artist", "yt_album", "score", "title_score", "artist_score",
        "album_score", "yt_result_type", "query", "created_at",
    }
    if not table_exists(conn, "match_candidates") or table_columns(conn, "match_candidates") == target:
        return
    cols = table_columns(conn, "match_candidates")
    conn.execute("ALTER TABLE match_candidates RENAME TO match_candidates_old")
    conn.execute(
        """
        CREATE TABLE match_candidates(
            run_id TEXT NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            rank_order INTEGER NOT NULL,
            candidate_order INTEGER NOT NULL,
            video_id TEXT,
            yt_title TEXT,
            yt_artist TEXT,
            yt_album TEXT,
            score REAL DEFAULT 0,
            title_score REAL DEFAULT 0,
            artist_score REAL DEFAULT 0,
            album_score REAL DEFAULT 0,
            yt_result_type TEXT,
            query TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(run_id, service, song_id, rank_order, candidate_order)
        )
        """
    )
    select_cols = [
        "run_id", "service", "song_id", "rank_order", "candidate_order", "video_id",
        "yt_title", "yt_artist", "yt_album", "score", "title_score", "artist_score",
        "album_score", "yt_result_type", "query", "created_at",
    ]
    expressions = [name if name in cols else ("0" if name in {"score", "title_score", "artist_score", "album_score"} else "''") for name in select_cols]
    conn.execute(
        f"INSERT OR REPLACE INTO match_candidates({', '.join(select_cols)}) SELECT {', '.join(expressions)} FROM match_candidates_old"
    )
    conn.execute("DROP TABLE match_candidates_old")


def _rebuild_review_conflicts(conn: sqlite3.Connection) -> None:
    target = {
        "conflict_id", "service", "song_id", "job_name", "source_variant", "reference_period",
        "title", "artist", "album", "query", "score", "source_file", "existing_track_uid",
        "incoming_track_uid", "existing_video_id", "incoming_video_id", "reason", "status", "created_at",
    }
    if not table_exists(conn, "review_conflicts") or table_columns(conn, "review_conflicts") == target:
        return
    rows = conn.execute("SELECT * FROM review_conflicts").fetchall()
    conn.execute("ALTER TABLE review_conflicts RENAME TO review_conflicts_old")
    conn.execute(
        """
        CREATE TABLE review_conflicts(
            conflict_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            song_id TEXT NOT NULL,
            job_name TEXT,
            source_variant TEXT,
            reference_period TEXT,
            title TEXT,
            artist TEXT,
            album TEXT,
            query TEXT,
            score REAL DEFAULT 0,
            source_file TEXT,
            existing_track_uid TEXT,
            incoming_track_uid TEXT,
            existing_video_id TEXT,
            incoming_video_id TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )
        """
    )
    for row in rows:
        data = dict(row)
        legacy_name = str(data.get("playlist_name") or "")
        job_name = legacy_to_job_name(data.get("job_name") or legacy_name)
        period = reference_period_for_date(job_name, str(data.get("reference_period") or data.get("chart_period") or data.get("crawl_time") or data.get("created_at") or ""))
        conn.execute(
            """
            INSERT OR REPLACE INTO review_conflicts(
                conflict_id, service, song_id, job_name, source_variant, reference_period, title,
                artist, album, query, score, source_file, existing_track_uid, incoming_track_uid,
                existing_video_id, incoming_video_id, reason, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("conflict_id"),
                normalized_service(data.get("service")),
                data.get("song_id") or "",
                job_name,
                _source_variant_from_legacy(legacy_name, str(data.get("source_variant") or "")),
                period,
                data.get("title") or "",
                data.get("artist") or "",
                data.get("album") or "",
                data.get("query") or "",
                float(data.get("score") or 0),
                data.get("source_file") or "",
                data.get("existing_track_uid"),
                data.get("incoming_track_uid"),
                data.get("existing_video_id"),
                data.get("incoming_video_id"),
                data.get("reason") or "unknown",
                data.get("status") or "open",
                data.get("created_at") or utc_now_iso(),
            ),
        )
    conn.execute("DROP TABLE review_conflicts_old")


def _rebuild_album_metadata(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "album_metadata") and table_columns(conn, "album_metadata") != {"service", "album_id", "album_name", "created_at", "last_checked"}:
        conn.execute("ALTER TABLE album_metadata RENAME TO album_metadata_old")
        conn.execute("CREATE TABLE album_metadata(service TEXT NOT NULL, album_id TEXT NOT NULL, album_name TEXT, created_at TEXT NOT NULL, last_checked TEXT NOT NULL, PRIMARY KEY(service, album_id))")
        conn.execute("INSERT OR REPLACE INTO album_metadata(service, album_id, album_name, created_at, last_checked) SELECT service, album_id, album_name, created_at, last_checked FROM album_metadata_old")
        conn.execute("DROP TABLE album_metadata_old")


def _rebuild_playlist_updates(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "playlist_update_runs") and table_columns(conn, "playlist_update_runs") != {"update_run_id", "playlist_id", "service", "job_name", "started_at", "dry_run", "requested_count", "existing_count", "created_at"}:
        rows = conn.execute("SELECT * FROM playlist_update_runs").fetchall()
        conn.execute("ALTER TABLE playlist_update_runs RENAME TO playlist_update_runs_old")
        conn.execute(
            """
            CREATE TABLE playlist_update_runs(
                update_run_id TEXT PRIMARY KEY,
                playlist_id TEXT NOT NULL,
                service TEXT,
                job_name TEXT,
                started_at TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                requested_count INTEGER DEFAULT 0,
                existing_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        for row in rows:
            data = dict(row)
            conn.execute(
                """
                INSERT OR REPLACE INTO playlist_update_runs(
                    update_run_id, playlist_id, service, job_name, started_at,
                    dry_run, requested_count, existing_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("update_run_id"),
                    data.get("playlist_id") or "",
                    normalized_service(data.get("service")) if data.get("service") else "",
                    legacy_to_job_name(data.get("job_name") or data.get("playlist_name") or ""),
                    data.get("started_at") or data.get("created_at") or utc_now_iso(),
                    int(data.get("dry_run") or 0),
                    int(data.get("requested_count") or 0),
                    int(data.get("existing_count") or 0),
                    data.get("created_at") or utc_now_iso(),
                ),
            )
        conn.execute("DROP TABLE playlist_update_runs_old")
    if table_exists(conn, "playlist_update_items") and table_columns(conn, "playlist_update_items") != {"update_run_id", "action", "video_id", "item_order", "created_at"}:
        conn.execute("ALTER TABLE playlist_update_items RENAME TO playlist_update_items_old")
        conn.execute("CREATE TABLE playlist_update_items(update_run_id TEXT NOT NULL REFERENCES playlist_update_runs(update_run_id) ON DELETE CASCADE, action TEXT NOT NULL, video_id TEXT NOT NULL, item_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, PRIMARY KEY(update_run_id, action, video_id, item_order))")
        conn.execute("INSERT OR REPLACE INTO playlist_update_items(update_run_id, action, video_id, item_order, created_at) SELECT update_run_id, action, video_id, item_order, created_at FROM playlist_update_items_old")
        conn.execute("DROP TABLE playlist_update_items_old")

def _create_views(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS frontend_history_source;

        CREATE VIEW IF NOT EXISTS latest_match_attempts AS
        SELECT a.*
        FROM match_attempts a
        JOIN (
            SELECT service, song_id, MAX(created_at) AS max_created_at
            FROM match_attempts
            GROUP BY service, song_id
        ) latest
          ON latest.service = a.service
         AND latest.song_id = a.song_id
         AND latest.max_created_at = a.created_at;

        CREATE VIEW IF NOT EXISTS latest_failed_matches AS
        SELECT *
        FROM latest_match_attempts
        WHERE COALESCE(video_id, '') = ''
           OR status IN ('failed', 'duplicate_skipped', 'manual_blocked');

        CREATE VIEW IF NOT EXISTS frontend_history_source AS
        SELECT
            p.service,
            p.job_name,
            p.source_variant,
            p.reference_period,
            p.reference_period AS chart_period,
            p.reference_period AS chart_date,
            p.song_id,
            ps.track_uid,
            p.rank_order,
            t.canonical_yt_video_id AS video_id,
            t.yt_title,
            t.yt_artist,
            t.yt_album,
            COALESCE(NULLIF(tl.title_ko, ''), tl.title_en, t.yt_title) AS title,
            COALESCE(NULLIF(tl.artist_ko, ''), tl.artist_en, t.yt_artist) AS artist,
            COALESCE(NULLIF(tl.album_ko, ''), tl.album_en, t.yt_album) AS album,
            '' AS url,
            tl.artwork_url
        FROM playlist_order p
        JOIN platform_song_ids ps
          ON ps.service = p.service
         AND ps.song_id = p.song_id
        JOIN tracks t ON t.track_uid = ps.track_uid
        LEFT JOIN track_list tl
          ON LOWER(tl.service) = LOWER(p.service)
         AND tl.song_id = p.song_id;
        """
    )


def find_track_by_service_song(conn: sqlite3.Connection, service: str, song_id: str) -> str | None:
    if not service or not song_id:
        return None
    row = conn.execute(
        "SELECT track_uid FROM platform_song_ids WHERE service = ? AND song_id = ?",
        (normalized_service(service), song_id),
    ).fetchone()
    return row["track_uid"] if row else None


def find_track_by_video(conn: sqlite3.Connection, video_id: str | None) -> str | None:
    if not video_id:
        return None
    row = conn.execute("SELECT track_uid FROM yt_video_ids WHERE video_id = ?", (video_id,)).fetchone()
    return row["track_uid"] if row else None


def find_track_by_metadata(conn: sqlite3.Connection, title: str, artist: str, album: str = "") -> str | None:
    keys = [metadata_key(title, artist, album), compact_metadata_key(title, artist)]
    # Fallback 1: strip performance variant suffixes (Live, MV, Acoustic …)
    # e.g. '소문의 낙원 (Live)' → '소문의 낙원'
    cleaned = clean_track_title(title)
    if cleaned != title:
        keys.append(compact_metadata_key(cleaned, artist))
        if album:
            keys.append(metadata_key(cleaned, artist, album))
    # Fallback 2: strip ALL parenthetical content from the *query* title
    # e.g. 'KISS KISS KISS' matches index key for 'KISS KISS KISS (Prod. by Hukky Shibaseki)'
    stripped_title = strip_parens_from_title(title)
    if stripped_title != title and stripped_title != cleaned:
        keys.append(compact_metadata_key(stripped_title, artist))
        if album:
            keys.append(metadata_key(stripped_title, artist, album))
    # Fallback 3: strip parenthetical content from the *artist*
    # e.g. Melon stores 'LE SSERAFIM (르세라핌)' — strip → 'LE SSERAFIM'
    # which matches Apple's existing 'boompala|le sserafim' index key.
    stripped_artist = strip_parens_from_title(artist)
    if stripped_artist != artist:
        keys.append(compact_metadata_key(title, stripped_artist))
        keys.append(compact_metadata_key(cleaned, stripped_artist))
        if album:
            keys.append(metadata_key(title, stripped_artist, album))
    for key in keys:
        row = conn.execute(
            "SELECT track_uid FROM metadata_lookup_index WHERE lookup_key = ?",
            (key,),
        ).fetchone()
        if row:
            return row["track_uid"]
    return None



def manual_override(conn: sqlite3.Connection, service: str, song_id: str) -> sqlite3.Row | None:
    if not service or not song_id:
        return None
    return conn.execute(
        "SELECT * FROM manual_overrides WHERE service = ? AND song_id = ?",
        (normalized_service(service), song_id),
    ).fetchone()


def ensure_track(
    conn: sqlite3.Connection,
    *,
    track_uid: str,
    video_id: str | None = None,
    yt_title: str = "",
    yt_artist: str = "",
    yt_album: str = "",
    status: str = "unmatched",
    score: float = 0.0,
) -> str:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO tracks (
            track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
            match_status, best_score, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(track_uid) DO UPDATE SET
            canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
            yt_title = COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title),
            yt_artist = COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist),
            yt_album = COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album),
            match_status = CASE
                WHEN excluded.match_status != 'failed' THEN excluded.match_status
                ELSE tracks.match_status
            END,
            best_score = CASE WHEN COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0) THEN COALESCE(excluded.best_score, 0) ELSE COALESCE(tracks.best_score, 0) END,
            updated_at = excluded.updated_at
        """,
        (track_uid, video_id, yt_title, yt_artist, yt_album, status, score, now, now),
    )
    if video_id:
        conn.execute(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                is_canonical = CASE WHEN excluded.is_canonical > yt_video_ids.is_canonical THEN excluded.is_canonical ELSE yt_video_ids.is_canonical END
            """,
            (video_id, track_uid),
        )
    return track_uid


def record_conflict(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    existing_track_uid: str | None,
    incoming_track_uid: str | None,
    existing_video_id: str | None,
    incoming_video_id: str | None,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> None:
    seed = "|".join(
        [
            normalized_service(service),
            song_id,
            existing_video_id or "",
            incoming_video_id or "",
            reason,
        ]
    )
    now = utc_now_iso()
    payload = payload or {}
    conn.execute(
        """
        INSERT INTO review_conflicts(
            conflict_id, service, song_id, job_name, source_variant, reference_period, title, artist,
            album, query, score, source_file, existing_track_uid, incoming_track_uid,
            existing_video_id, incoming_video_id, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (conflict_id) DO NOTHING
        """,
        (
            hashlib.sha1(seed.encode("utf-8")).hexdigest(),
            normalized_service(service),
            song_id,
            payload.get("job_name") or legacy_to_job_name(payload.get("playlist_name") or payload.get("chart_type") or ""),
            normalize_source_variant(payload.get("source_variant")),
            reference_period_for_date(
                payload.get("job_name") or legacy_to_job_name(payload.get("playlist_name") or payload.get("chart_type") or ""),
                payload.get("reference_period") or payload.get("chart_period") or payload.get("extracted_at") or payload.get("crawl_time") or "",
            ),
            payload.get("title") or payload.get("title_ko") or payload.get("title_en") or "",
            payload.get("artist") or payload.get("artist_ko") or payload.get("artist_en") or "",
            payload.get("album") or payload.get("album_ko") or payload.get("album_en") or "",
            payload.get("query") or "",
            float(payload.get("score") or 0),
            payload.get("source_file") or "",
            existing_track_uid,
            incoming_track_uid,
            existing_video_id,
            incoming_video_id,
            reason,
            now,
        ),
    )


def upsert_track_list_metadata(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    track_uid: str,
    row: dict[str, Any],
    locale: str = "",
    bind_source_id: bool = True,
) -> None:
    if bind_source_id:
        conn.execute(
            """
            INSERT INTO platform_song_ids(service, song_id, track_uid)
            VALUES (?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                track_uid = excluded.track_uid
            """,
            (normalized_service(service), song_id, track_uid),
        )
    conn.execute(
        """
        INSERT INTO track_list(
            service, song_id, album_id, title_ko, artist_ko, album_ko,
            title_en, artist_en, album_en, artwork_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service, song_id) DO UPDATE SET
            album_id = COALESCE(NULLIF(excluded.album_id, ''), track_list.album_id),
            title_ko = COALESCE(NULLIF(excluded.title_ko, ''), track_list.title_ko),
            artist_ko = COALESCE(NULLIF(excluded.artist_ko, ''), track_list.artist_ko),
            album_ko = COALESCE(NULLIF(excluded.album_ko, ''), track_list.album_ko),
            title_en = COALESCE(NULLIF(excluded.title_en, ''), track_list.title_en),
            artist_en = COALESCE(NULLIF(excluded.artist_en, ''), track_list.artist_en),
            album_en = COALESCE(NULLIF(excluded.album_en, ''), track_list.album_en),
            artwork_url = COALESCE(NULLIF(excluded.artwork_url, ''), track_list.artwork_url)
        """,
        (
            normalized_service(service),
            song_id,
            infer_album_id(service, row),
            row.get("title_ko") or row.get("title", ""),
            row.get("artist_ko") or row.get("artist", ""),
            row.get("album_ko") or row.get("album", ""),
            "" if normalized_service(service) == "melon" else (row.get("title_en") or row.get("title", "")),
            "" if normalized_service(service) == "melon" else (row.get("artist_en") or row.get("artist", "")),
            "" if normalized_service(service) == "melon" else (row.get("album_en") or row.get("album", "")),
            row.get("artwork_url", ""),
        ),
    )


def upsert_metadata_lookup(conn: sqlite3.Connection, *, track_uid: str, row: dict[str, Any], source: str, score: float) -> None:
    candidates = [
        (row.get("title"), row.get("artist"), row.get("album")),
        (row.get("title_en"), row.get("artist_en"), row.get("album_en")),
        (row.get("title_ko"), row.get("artist_ko"), row.get("album_ko")),
    ]
    for title, artist, album in candidates:
        if not title or not artist:
            continue
        full_key = metadata_key(title, artist, album)
        compact_key = compact_metadata_key(title, artist)
        # compact key (album 없음)는 full key보다 낮은 신뢰도로 저장
        # → 나중에 정확한 album 정보가 있는 full key가 들어오면 우선됨
        key_score_pairs = [(full_key, score)]
        if compact_key != full_key:
            key_score_pairs.append((compact_key, score * 0.8))
        # Fallback: parens-stripped title key (score*0.6) so that short chart titles
        # (e.g. 'KISS KISS KISS') can match a canonical title with extra credits.
        # Only added when stripping actually changes the title.
        stripped_title = strip_parens_from_title(title)
        if stripped_title != title and stripped_title:
            stripped_full_key = metadata_key(stripped_title, artist, album)
            stripped_compact_key = compact_metadata_key(stripped_title, artist)
            key_score_pairs.append((stripped_compact_key, score * 0.6))
            if stripped_full_key != stripped_compact_key:
                key_score_pairs.append((stripped_full_key, score * 0.6))
        # Fallback: parens-stripped artist key (score*0.6)
        # Handles 'LE SSERAFIM (르세라핌)' → 'LE SSERAFIM' so future Melon entries
        # find the canonical track already created by Apple/Spotify/YTMusic.
        stripped_artist = strip_parens_from_title(artist)
        if stripped_artist != artist and stripped_artist:
            key_score_pairs.append((compact_metadata_key(title, stripped_artist), score * 0.6))
            if stripped_title != title:
                key_score_pairs.append((compact_metadata_key(stripped_title, stripped_artist), score * 0.6))
        for key, effective_score in key_score_pairs:
            if not key.strip("|"):
                continue
            conn.execute(
                """
                INSERT INTO metadata_lookup_index(lookup_key, track_uid, source, score)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                    track_uid = CASE
                        WHEN excluded.score >= metadata_lookup_index.score THEN excluded.track_uid
                        ELSE metadata_lookup_index.track_uid
                    END,
                    source = CASE
                        WHEN excluded.score >= metadata_lookup_index.score THEN excluded.source
                        ELSE metadata_lookup_index.source
                    END,
                    score = CASE WHEN excluded.score > metadata_lookup_index.score THEN excluded.score ELSE metadata_lookup_index.score END
                """,
                (key, track_uid, source, effective_score),
            )


def _verify_metadata_merge(
    conn: sqlite3.Connection,
    track_uid: str,
    row: dict[str, Any],
    threshold: float = 0.5,
) -> bool:
    """기존 트랙의 소스 메타데이터와 입력 메타데이터의 유사도를 검증.
    
    metadata_lookup_index 키 충돌로 인한 오병합을 방지합니다.
    임계값(0.5)은 "거리에서" vs "Trip" (유사도≈0.0)은 거부하되,
    "뛰어(JUMP)" vs "뛰어" (유사도≈0.9)은 허용하는 수준입니다.
    """
    from ytmusic_playlist_sync import similarity

    existing_meta = conn.execute(
        """
        SELECT tl.title_ko, tl.title_en, tl.artist_ko, tl.artist_en
        FROM platform_song_ids ps
        JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
        WHERE ps.track_uid = ?
        ORDER BY
            CASE tl.service WHEN 'melon' THEN 0 WHEN 'apple' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (track_uid,),
    ).fetchone()
    if not existing_meta:
        return True  # 메타데이터가 없으면 기존 동작 유지

    src_title = row.get("title") or row.get("title_ko") or row.get("title_en") or ""
    if not src_title:
        return True

    # 기존 트랙의 한/영 제목 모두와 비교하여 최대 유사도를 사용
    existing_titles = [
        existing_meta["title_ko"] or "",
        existing_meta["title_en"] or "",
    ]
    best_sim = max(
        (similarity(src_title, t) for t in existing_titles if t),
        default=0.0,
    )
    if best_sim < threshold:
        LOG.warning(
            "Metadata merge rejected: input='%s' vs existing='%s'/'%s' (sim=%.2f < %.2f)",
            src_title,
            existing_meta["title_ko"],
            existing_meta["title_en"],
            best_sim,
            threshold,
        )
        return False
    return True


def resolve_track_uid(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    row: dict[str, Any],
    video_id: str | None,
) -> str:
    service = normalized_service(service)
    override = manual_override(conn, service, song_id)
    if override and override["action"] == "block":
        return stable_uid(f"blocked:{service}:{song_id}")
    if override and override["target_track_uid"]:
        return override["target_track_uid"]
    if override and override["canonical_yt_video_id"]:
        existing = find_track_by_video(conn, override["canonical_yt_video_id"])
        if existing:
            return existing
        return stable_uid(f"yt:{override['canonical_yt_video_id']}")

    existing_by_song = find_track_by_service_song(conn, service, song_id)
    existing_by_video = find_track_by_video(conn, video_id)
    if existing_by_song:
        song_track = conn.execute(
            "SELECT canonical_yt_video_id, match_status FROM tracks WHERE track_uid = ?",
            (existing_by_song,),
        ).fetchone()
        song_video = song_track["canonical_yt_video_id"] if song_track else ""
        song_status = song_track["match_status"] if song_track else ""
        if existing_by_video and existing_by_video != existing_by_song:
            if not song_video or song_video == video_id or song_status in {"failed", "duplicate_skipped", "manual_blocked", "unmatched"}:
                return existing_by_video
            return existing_by_song
        if song_video:
            return existing_by_song
        if existing_by_video:
            return existing_by_video
    if existing_by_video:
        return existing_by_video
    existing_by_meta = find_track_by_metadata(
        conn,
        row.get("title") or row.get("title_en") or row.get("title_ko") or "",
        row.get("artist") or row.get("artist_en") or row.get("artist_ko") or "",
        row.get("album") or row.get("album_en") or row.get("album_ko") or "",
    )
    if existing_by_meta:
        if _verify_metadata_merge(conn, existing_by_meta, row):
            return existing_by_meta
    if video_id:
        return stable_uid(f"yt:{video_id}")
    if service and song_id:
        return stable_uid(f"{service}:{song_id}")
    return stable_uid(
        metadata_key(
            row.get("title") or row.get("title_en") or row.get("title_ko") or "",
            row.get("artist") or row.get("artist_en") or row.get("artist_ko") or "",
            row.get("album") or row.get("album_en") or row.get("album_ko") or "",
        )
    )


def upsert_track_match(
    conn: sqlite3.Connection,
    *,
    service: str,
    source_row: dict[str, Any],
    match_row: dict[str, Any] | None = None,
) -> str:
    service = normalized_service(service)
    merged = dict(source_row)
    if match_row:
        merged.update({k: v for k, v in match_row.items() if v not in (None, "")})
    song_id = normalize_song_id(service, merged)
    video_id = merged.get("video_id") or merged.get("canonical_yt_video_id")
    status = str(merged.get("status") or ("matched" if video_id else "failed"))
    score = float(merged.get("score") or 0)
    if status in {"failed", "duplicate_skipped"} or (service == "spotify" and str(song_id).startswith("fallback:")):
        track_uid = stable_uid(f"unmatched:{service}:{song_id or metadata_key(merged.get('title'), merged.get('artist'), merged.get('album'))}")
        
        is_ytmusic_video = (service == "ytmusic" and song_id and not str(song_id).startswith("fallback:"))
        
        ensure_track(
            conn,
            track_uid=track_uid,
            status=status,
            score=score,
            video_id=song_id if is_ytmusic_video else None,
        )
        if song_id and not (service == "spotify" and str(song_id).startswith("fallback:")):
            upsert_track_list_metadata(
                conn,
                service=service,
                song_id=song_id,
                track_uid=track_uid,
                row=merged,
                bind_source_id=is_ytmusic_video,
            )
        return track_uid
    track_uid = resolve_track_uid(conn, service=service, song_id=song_id, row=merged, video_id=video_id)

    existing = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?", (track_uid,)).fetchone()
    existing_video = existing["canonical_yt_video_id"] if existing else None
    override = manual_override(conn, service, song_id)
    override_video = override["canonical_yt_video_id"] if override else None
    canonical_video = override_video or video_id

    if existing_video and video_id and existing_video != video_id and not override_video:
        record_conflict(
            conn,
            service=service,
            song_id=song_id,
            existing_track_uid=track_uid,
            incoming_track_uid=find_track_by_video(conn, video_id),
            existing_video_id=existing_video,
            incoming_video_id=video_id,
            reason="same_track_uid_different_video",
            payload=merged,
        )
        canonical_video = existing_video

    ensure_track(
        conn,
        track_uid=track_uid,
        video_id=canonical_video,
        yt_title=merged.get("yt_title", ""),
        yt_artist=merged.get("yt_artist", ""),
        yt_album=merged.get("yt_album", ""),
        status=status,
        score=score,
    )
    if song_id:
        bound_uid = find_track_by_service_song(conn, service, song_id)
        if bound_uid and bound_uid != track_uid:
            bound_video = conn.execute(
                "SELECT canonical_yt_video_id, match_status FROM tracks WHERE track_uid = ?",
                (bound_uid,),
            ).fetchone()
            bound_canonical = bound_video["canonical_yt_video_id"] if bound_video else None
            bound_status = bound_video["match_status"] if bound_video else ""
            can_rebind = (
                not bound_canonical
                or bound_canonical == canonical_video
                or bound_status in {"failed", "duplicate_skipped", "manual_blocked", "unmatched"}
            )
            if not can_rebind:
                record_conflict(
                    conn,
                    service=service,
                    song_id=song_id,
                    existing_track_uid=bound_uid,
                    incoming_track_uid=track_uid,
                    existing_video_id=bound_canonical,
                    incoming_video_id=video_id,
                    reason="service_song_id_already_bound",
                    payload=merged,
                )
                track_uid = bound_uid
        upsert_track_list_metadata(conn, service=service, song_id=song_id, track_uid=track_uid, row=merged)
    if canonical_video:
        conn.execute(
            "UPDATE tracks SET canonical_yt_video_id = COALESCE(canonical_yt_video_id, ?) WHERE track_uid = ?",
            (canonical_video, track_uid),
        )
    upsert_metadata_lookup(conn, track_uid=track_uid, row=merged, source=status, score=score)
    return track_uid


def upsert_chart_rank(
    conn: sqlite3.Connection,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    song_id: str,
    track_uid: str,
    rank_order: int,
    album_id: str = "",
) -> None:
    if not song_id or not rank_order:
        return
    service = normalized_service(service)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    ref_p = reference_period or chart_period
    period = reference_period_for_date(job_name, chart_date, ref_p)
    if not period:
        return
    conn.execute(
        """
        INSERT INTO playlist_order(
            service, job_name, source_variant, reference_period, song_id, rank_order
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
            rank_order = excluded.rank_order
        """,
        (
            service,
            job_name,
            source_variant,
            period,
            song_id,
            int(rank_order),
        ),
    )


def start_match_run(
    conn: sqlite3.Connection,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    started_at: str,
    source: str = "",
    total_tracks: int = 0,
) -> str:
    service = normalized_service(service)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    run_id = hashlib.sha1(f"{service}|{job_name}|{started_at}".encode("utf-8")).hexdigest()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO match_runs(run_id, service, job_name, source_variant, started_at, source, total_tracks, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            job_name = excluded.job_name,
            source_variant = excluded.source_variant,
            total_tracks = excluded.total_tracks,
            source = excluded.source
        """,
        (run_id, service, job_name, source_variant, started_at, source, total_tracks, now),
    )
    return run_id


def record_match_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    service: str,
    song_id: str,
    track_uid: str,
    row: dict[str, Any],
) -> None:
    now = utc_now_iso()
    match_method, origin_method, _ = match_method_for_status(row.get("status"), row.get("query"))
    conn.execute(
        """
        INSERT INTO match_attempts(
            run_id, service, song_id, track_uid, rank_order,
            video_id, score, title_score, artist_score,
            album_score, yt_result_type, query, status, match_method, origin_method,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, service, song_id, rank_order) DO UPDATE SET
            track_uid = EXCLUDED.track_uid,
            rank_order = EXCLUDED.rank_order,
            video_id = EXCLUDED.video_id,
            score = EXCLUDED.score,
            title_score = EXCLUDED.title_score,
            artist_score = EXCLUDED.artist_score,
            album_score = EXCLUDED.album_score,
            yt_result_type = EXCLUDED.yt_result_type,
            query = EXCLUDED.query,
            status = EXCLUDED.status,
            match_method = EXCLUDED.match_method,
            origin_method = EXCLUDED.origin_method,
            created_at = EXCLUDED.created_at
        """,
        (
            run_id,
            normalized_service(service),
            song_id,
            track_uid,
            int(row.get("rank") or 0),
            row.get("video_id", ""),
            float(row.get("score") or 0),
            float(row.get("title_score") or 0),
            float(row.get("artist_score") or 0),
            float(row.get("album_score") or 0),
            row.get("yt_result_type", ""),
            row.get("query", ""),
            row.get("status", ""),
            row.get("match_method", match_method),
            row.get("origin_method", origin_method),
            now,
        ),
    )


def record_match_candidates(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    service: str,
    song_id: str,
    rank_order: int,
    candidates: Iterable[dict[str, Any]],
) -> None:
    now = utc_now_iso()
    for index, candidate in enumerate(candidates, 1):
        conn.execute(
            """
            INSERT INTO match_candidates(
                run_id, service, song_id, rank_order, candidate_order, video_id,
                yt_title, yt_artist, yt_album, score, title_score, artist_score,
                album_score, yt_result_type, query, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, service, song_id, rank_order, candidate_order) DO UPDATE SET
                video_id = EXCLUDED.video_id,
                yt_title = EXCLUDED.yt_title,
                yt_artist = EXCLUDED.yt_artist,
                yt_album = EXCLUDED.yt_album,
                score = EXCLUDED.score,
                title_score = EXCLUDED.title_score,
                artist_score = EXCLUDED.artist_score,
                album_score = EXCLUDED.album_score,
                yt_result_type = EXCLUDED.yt_result_type,
                query = EXCLUDED.query,
                created_at = EXCLUDED.created_at
            """,
            (
                run_id,
                normalized_service(service),
                song_id,
                int(rank_order or 0),
                index,
                candidate.get("video_id") or candidate.get("videoId", ""),
                candidate.get("yt_title") or candidate.get("title", ""),
                candidate.get("yt_artist") or candidate.get("artist", ""),
                candidate.get("yt_album") or candidate.get("album", ""),
                float(candidate.get("score") or 0),
                float(candidate.get("title_score") or 0),
                float(candidate.get("artist_score") or 0),
                float(candidate.get("album_score") or 0),
                candidate.get("yt_result_type") or candidate.get("resultType", ""),
                candidate.get("query", ""),
                now,
            ),
        )


def cleanup_old_attempts_and_candidates(conn: sqlite3.Connection, days: int = 15) -> None:
    """15일이 지난 매칭 시도 이력 및 검색 후보 데이터를 삭제하여 용량을 최적화합니다."""
    conn.execute("DELETE FROM match_attempts WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')", (days,))
    conn.execute("DELETE FROM match_candidates WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')", (days,))


def get_expected_track_count(job_name: str) -> int | None:
    job = str(job_name).lower().strip()
    if "top-songs" in job:
        return 200
    if "top-100" in job or "hot-100" in job or "gen-z" in job:
        return 100
    if "top-25" in job:
        return 25
    return None


def _persist_crawled_tracks_impl(
    conn: Any,
    service: str,
    job_name: str,
    source_variant: str,
    chart_date: str,
    reference_period: str | None,
    chart_period: str | None,
    tracks: Iterable[Any],
) -> None:
    track_rows = [row_dict(t) for t in tracks]
    ref_p = reference_period or chart_period
    resolved_period = reference_period_for_date(job_name, chart_date, ref_p)
    if resolved_period and not (normalized_service(service) == "melon" and job_name == "Gen-Z-Daily" and source_variant == "combined"):
        LOG.info("Cleaning up existing playlist_order records for %s / %s / %s (period: %s)", service, job_name, source_variant, resolved_period)
        conn.execute(
            """
            DELETE FROM playlist_order
            WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
            """,
            (normalized_service(service), job_name, source_variant, resolved_period)
        )

    # 1. Fetch all existing UIDs in one select query
    song_ids = [normalize_song_id(service, t) for t in track_rows if normalize_song_id(service, t)]
    existing_uids = {}
    if song_ids:
        placeholders = ",".join("?" for _ in song_ids)
        rows = conn.execute(
            f"SELECT song_id, track_uid FROM platform_song_ids WHERE service = ? AND song_id IN ({placeholders})",
            (normalized_service(service), *song_ids)
        ).fetchall()
        existing_uids = {row["song_id"]: row["track_uid"] for row in rows}

    now = utc_now_iso()
    tracks_params = []
    platform_song_ids_params = []
    track_list_params = []
    playlist_order_params = []

    for track in track_rows:
        song_id = normalize_song_id(service, track)
        if not song_id:
            continue
        
        existing_uid = existing_uids.get(song_id)
        if existing_uid:
            track_uid = existing_uid
        else:
            track_uid = stable_uid(f"unmatched:{service}:{song_id}")
        
        # Collect tracks params
        tracks_params.append((track_uid, None, "", "", "", "unmatched", 0.0, now, now))
        
        # Collect platform_song_ids params
        platform_song_ids_params.append((normalized_service(service), song_id, track_uid))
        
        # Collect track_list metadata params
        album_id = infer_album_id(service, track)
        title_ko = str(track.get("title_ko") or track.get("title") or "").strip()
        artist_ko = str(track.get("artist_ko") or track.get("artist") or "").strip()
        album_ko = str(track.get("album_ko") or track.get("album") or "").strip()
        title_en = str(track.get("title_en") or "").strip()
        artist_en = str(track.get("artist_en") or "").strip()
        album_en = str(track.get("album_en") or "").strip()
        artwork_url = str(track.get("artwork_url") or "").strip()
        
        track_list_params.append((
            normalized_service(service),
            song_id,
            album_id,
            title_ko,
            artist_ko,
            album_ko,
            title_en,
            artist_en,
            album_en,
            artwork_url
        ))
        
        # Collect playlist_order params
        playlist_order_params.append((
            normalized_service(service),
            job_name,
            source_variant,
            resolved_period,
            song_id,
            int(track.get("rank") or 0),
        ))

    if tracks_params:
        conn.executemany(
            """
            INSERT INTO tracks (
                track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
                match_status, best_score, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_uid) DO UPDATE SET
                canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
                yt_title = COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title),
                yt_artist = COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist),
                yt_album = COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album),
                match_status = CASE
                    WHEN excluded.match_status != 'failed' THEN excluded.match_status
                    ELSE tracks.match_status
                END,
                best_score = CASE WHEN COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0) THEN COALESCE(excluded.best_score, 0) ELSE COALESCE(tracks.best_score, 0) END,
                updated_at = excluded.updated_at
            """,
            tracks_params
        )

    if platform_song_ids_params:
        conn.executemany(
            """
            INSERT INTO platform_song_ids(service, song_id, track_uid)
            VALUES (?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                track_uid = excluded.track_uid
            """,
            platform_song_ids_params
        )

    if track_list_params:
        conn.executemany(
            """
            INSERT INTO track_list(
                service, song_id, album_id, title_ko, artist_ko, album_ko,
                title_en, artist_en, album_en, artwork_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                album_id = COALESCE(NULLIF(excluded.album_id, ''), track_list.album_id),
                title_ko = COALESCE(NULLIF(excluded.title_ko, ''), track_list.title_ko),
                artist_ko = COALESCE(NULLIF(excluded.artist_ko, ''), track_list.artist_ko),
                album_ko = COALESCE(NULLIF(excluded.album_ko, ''), track_list.album_ko),
                title_en = COALESCE(NULLIF(excluded.title_en, ''), track_list.title_en),
                artist_en = COALESCE(NULLIF(excluded.artist_en, ''), track_list.artist_en),
                album_en = COALESCE(NULLIF(excluded.album_en, ''), track_list.album_en),
                artwork_url = COALESCE(NULLIF(excluded.artwork_url, ''), track_list.artwork_url)
            """,
            track_list_params
        )

    if playlist_order_params:
        conn.executemany(
            """
            INSERT INTO playlist_order(
                service, job_name, source_variant, reference_period,
                song_id, rank_order
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
                rank_order = excluded.rank_order
            """,
            playlist_order_params
        )


def persist_crawled_tracks(
    db_path: str | Path,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    tracks: Iterable[Any],
    conn: Any = None,
) -> None:
    init_db(db_path)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    
    # Validation check: Ensure the track count matches the expected count
    expected = get_expected_track_count(job_name)
    if expected is not None and len(track_rows) != expected:
        if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
            LOG.warning(
                "Track count validation bypassed. Job '%s' has %d tracks, expected %d.",
                job_name, len(track_rows), expected
            )
        else:
            raise ValueError(
                f"Validation Error: Job '{job_name}' has {len(track_rows)} tracks, "
                f"but expected exactly {expected} tracks. Aborting database persistence to prevent corruption. "
                f"Set BYPASS_TRACK_COUNT_VAL=true to bypass."
            )

    if conn is not None:
        _persist_crawled_tracks_impl(conn, service, job_name, source_variant, chart_date, reference_period, chart_period, tracks)
        conn.commit()
    else:
        with connect(db_path) as new_conn:
            _persist_crawled_tracks_impl(new_conn, service, job_name, source_variant, chart_date, reference_period, chart_period, tracks)


def _persist_crawl_run_impl(
    conn: Any,
    service: str,
    job_name: str,
    source_variant: str,
    chart_date: str,
    reference_period: str | None,
    chart_period: str | None,
    started_at: str,
    tracks: Iterable[Any],
    matches: Iterable[Any],
) -> None:
    track_rows = [row_dict(t) for t in tracks]
    match_rows = [row_dict(m) for m in matches]
    ref_p = reference_period or chart_period
    resolved_period = reference_period_for_date(job_name, chart_date, ref_p)
    if resolved_period and not (normalized_service(service) == "melon" and job_name == "Gen-Z-Daily" and source_variant == "combined"):
        LOG.info("Cleaning up existing playlist_order records for %s / %s / %s (period: %s)", service, job_name, source_variant, resolved_period)
        conn.execute(
            """
            DELETE FROM playlist_order
            WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
            """,
            (normalized_service(service), job_name, source_variant, resolved_period)
        )

    run_id = start_match_run(
        conn,
        service=service,
        job_name=job_name,
        source_variant=source_variant,
        started_at=started_at,
        source="crawler",
        total_tracks=len(track_rows),
    )
    track_by_song = {}
    for track in track_rows:
        song_id = normalize_song_id(service, track)
        if song_id:
            track_by_song[song_id] = track

    matched_count = 0
    failed_count = 0
    cache_hits = 0
    proxy_hits = 0
    for match in match_rows:
        song_id = normalize_song_id(service, match)
        source_row = track_by_song.get(song_id, match)
        track_uid = upsert_track_match(conn, service=service, source_row=source_row, match_row=match)
        if match.get("video_id") and match.get("status") != "duplicate_skipped":
            matched_count += 1
        else:
            failed_count += 1
        if match.get("status") == "cached_match" or match.get("query") == "db_cache":
            cache_hits += 1
        if match.get("status") == "proxy_matched":
            proxy_hits += 1
        if song_id:
            if not (normalized_service(service) == "melon" and job_name == "Gen-Z-Daily" and source_variant == "combined"):
                upsert_chart_rank(
                    conn,
                    service=service,
                    job_name=job_name,
                    source_variant=source_variant,
                    chart_date=chart_date,
                    reference_period=reference_period or chart_period,
                    song_id=song_id,
                    track_uid=track_uid,
                    rank_order=int(match.get("rank") or source_row.get("rank") or 0),
                    album_id=infer_album_id(service, source_row),
                )
            record_match_attempt(
                conn,
                run_id=run_id,
                service=service,
                song_id=song_id,
                track_uid=track_uid,
                row=match,
            )
            if match.get("candidates"):
                record_match_candidates(
                    conn,
                    run_id=run_id,
                    service=service,
                    song_id=song_id,
                    rank_order=int(match.get("rank") or source_row.get("rank") or 0),
                    candidates=match.get("candidates") or [],
                )
    conn.execute(
        """
        UPDATE match_runs
        SET matched_tracks = ?, failed_tracks = ?, cache_hits = ?, proxy_hits = ?
        WHERE run_id = ?
        """,
        (matched_count, failed_count, cache_hits, proxy_hits, run_id),
    )
    cleanup_old_attempts_and_candidates(conn, days=15)


def persist_crawl_run(
    db_path: str | Path,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    started_at: str,
    tracks: Iterable[Any],
    matches: Iterable[Any],
    conn: Any = None,
) -> None:
    init_db(db_path)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    match_rows = [row_dict(m) for m in matches]
    
    # Validation check: Ensure the track count matches the expected count
    expected = get_expected_track_count(job_name)
    if expected is not None and len(track_rows) != expected:
        if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
            LOG.warning(
                "Track count validation bypassed. Job '%s' has %d tracks, expected %d.",
                job_name, len(track_rows), expected
            )
        else:
            raise ValueError(
                f"Validation Error: Job '{job_name}' has {len(track_rows)} tracks, "
                f"but expected exactly {expected} tracks. Aborting database persistence to prevent corruption. "
                f"Set BYPASS_TRACK_COUNT_VAL=true to bypass."
            )

    if conn is not None:
        _persist_crawl_run_impl(conn, service, job_name, source_variant, chart_date, reference_period, chart_period, started_at, tracks, matches)
        conn.commit()
    else:
        with connect(db_path) as new_conn:
            _persist_crawl_run_impl(new_conn, service, job_name, source_variant, chart_date, reference_period, chart_period, started_at, tracks, matches)


def repair_failed_source_bindings(conn: sqlite3.Connection) -> dict[str, int]:
    """Move failed exact source-id bindings back to known canonical tracks."""
    updated_bindings = 0
    merged_tracks = 0

    failed_bindings = conn.execute(
        """
        SELECT ps.service, ps.song_id, ps.track_uid, t.canonical_yt_video_id, t.match_status
        FROM platform_song_ids ps
        LEFT JOIN tracks t ON t.track_uid = ps.track_uid
        WHERE COALESCE(t.canonical_yt_video_id, '') = ''
           OR COALESCE(t.match_status, '') IN ('failed', 'duplicate_skipped', 'manual_blocked', 'unmatched')
        """
    ).fetchall()
    for row in failed_bindings:
        override = manual_override(conn, row["service"], row["song_id"])
        if override and override["action"] in {"block", "split"}:
            continue
        candidate = conn.execute(
            """
            SELECT video_id, track_uid, score, created_at
            FROM match_attempts
            WHERE service = ?
              AND song_id = ?
              AND COALESCE(video_id, '') != ''
              AND status IN ('matched', 'cached_match', 'proxy_matched', 'manual_override')
            ORDER BY score DESC, created_at DESC
            LIMIT 1
            """,
            (row["service"], row["song_id"]),
        ).fetchone()
        if not candidate:
            meta = conn.execute(
                """
                SELECT tl.title_ko, tl.artist_ko, tl.album_ko, tl.title_en, tl.artist_en, tl.album_en
                FROM track_list tl
                WHERE tl.service = ? AND tl.song_id = ?
                """,
                (row["service"], row["song_id"]),
            ).fetchone()
            if meta:
                target_uid = find_track_by_metadata(
                    conn,
                    meta["title_ko"] or meta["title_en"] or "",
                    meta["artist_ko"] or meta["artist_en"] or "",
                    meta["album_ko"] or meta["album_en"] or "",
                )
                if target_uid:
                    target = conn.execute(
                        "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
                        (target_uid,),
                    ).fetchone()
                    if target and target["canonical_yt_video_id"]:
                        conn.execute(
                            "UPDATE platform_song_ids SET track_uid = ? WHERE service = ? AND song_id = ?",
                            (target_uid, row["service"], row["song_id"]),
                        )
                        updated_bindings += 1
            continue

        target_uid = find_track_by_video(conn, candidate["video_id"])
        if not target_uid:
            target_uid = candidate["track_uid"] or stable_uid(f"yt:{candidate['video_id']}")
            ensure_track(conn, track_uid=target_uid, video_id=candidate["video_id"], status="matched", score=float(candidate["score"] or 1.0))
        if target_uid and target_uid != row["track_uid"]:
            conn.execute(
                "UPDATE platform_song_ids SET track_uid = ? WHERE service = ? AND song_id = ?",
                (target_uid, row["service"], row["song_id"]),
            )
            updated_bindings += 1

    duplicate_videos = conn.execute(
        """
        SELECT canonical_yt_video_id
        FROM tracks
        WHERE COALESCE(canonical_yt_video_id, '') != ''
        GROUP BY canonical_yt_video_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicate_videos:
        video_id = row["canonical_yt_video_id"]
        manual_split = conn.execute(
            """
            SELECT 1
            FROM manual_overrides
            WHERE action = 'split'
              AND (
                  canonical_yt_video_id = ?
                  OR target_track_uid IN (
                      SELECT track_uid FROM tracks WHERE canonical_yt_video_id = ?
                  )
              )
            LIMIT 1
            """,
            (video_id, video_id),
        ).fetchone()
        if manual_split:
            continue
        indexed_uid = find_track_by_video(conn, video_id)
        candidates = conn.execute(
            """
            SELECT track_uid, best_score, created_at
            FROM tracks
            WHERE canonical_yt_video_id = ?
            ORDER BY
                CASE WHEN track_uid = ? THEN 0 ELSE 1 END,
                best_score DESC,
                created_at ASC
            """,
            (video_id, indexed_uid or ""),
        ).fetchall()
        if len(candidates) < 2:
            continue
        target_uid = candidates[0]["track_uid"]
        conn.execute(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                track_uid = excluded.track_uid,
                is_canonical = 1
            """,
            (video_id, target_uid),
        )
        for candidate in candidates[1:]:
            old_uid = candidate["track_uid"]
            conn.execute("UPDATE platform_song_ids SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE metadata_lookup_index SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE match_attempts SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE yt_video_ids SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE manual_overrides SET target_track_uid = ? WHERE target_track_uid = ?", (target_uid, old_uid))
            conn.execute("DELETE FROM tracks WHERE track_uid = ?", (old_uid,))
            merged_tracks += 1

    return {"updated_bindings": updated_bindings, "merged_tracks": merged_tracks}


def _verify_cached_title(
    conn: sqlite3.Connection,
    track_uid: str,
    title: str,
    artist: str,
    threshold: float = 0.4,
) -> bool:
    """캐시 반환 전, 요청된 곡과 캐시된 트랙의 소스 제목 유사도를 검증.
    
    threshold가 _verify_metadata_merge(0.5)보다 낮은 이유:
    - 이 함수는 기존 바인딩의 안전망이므로, 명확한 오류만 거부하면 됨
    - 한/영 표기 차이가 큰 정상 곡을 거짓 거부하지 않기 위함
    """
    from ytmusic_playlist_sync import similarity

    rows = conn.execute(
        """
        SELECT tl.title_ko, tl.title_en
        FROM platform_song_ids ps
        JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
        WHERE ps.track_uid = ?
        """,
        (track_uid,),
    ).fetchall()
    if not rows:
        return True
    
    existing_titles = []
    for r in rows:
        if r["title_ko"]:
            existing_titles.append(r["title_ko"])
        if r["title_en"]:
            existing_titles.append(r["title_en"])
            
    existing_titles = list(set(existing_titles))
    if not existing_titles:
        return True
    
    best_sim = max(similarity(title, t) for t in existing_titles)
    return best_sim >= threshold


def _get_cached_match_impl(
    conn: Any,
    service: str,
    song_id: str,
    title: str,
    artist: str,
    album: str,
) -> dict[str, Any] | None:
    service = normalized_service(service)
    override = manual_override(conn, service, song_id)
    if override and override["action"] == "block":
        return {"status": "manual_blocked"}
    if override and override["canonical_yt_video_id"]:
        return _cache_row_for_video(conn, override["canonical_yt_video_id"], status="manual_override")
    track_uid = find_track_by_service_song(conn, service, song_id)
    if track_uid:
        cached = _cache_row_for_track(conn, track_uid)
        if cached:
            # 소스 메타데이터 검증: 잘못된 song_id→track_uid 바인딩 감지
            if title and not _verify_cached_title(conn, track_uid, title, artist):
                LOG.warning(
                    "Cache rejected for %s:%s — title mismatch with '%s'",
                    service, song_id, title,
                )
                return None  # fallback to search
            return cached
    track_uid = find_track_by_metadata(conn, title, artist, album)
    if track_uid:
        cached = _cache_row_for_track(conn, track_uid)
        if cached:
            if title and not _verify_cached_title(conn, track_uid, title, artist):
                return None
            return cached
    return None


def get_cached_match(
    db_path: str | Path,
    *,
    service: str,
    song_id: str = "",
    title: str = "",
    artist: str = "",
    album: str = "",
    conn: Any = None,
) -> dict[str, Any] | None:
    path = Path(db_path)
    if not path.exists() and not os.environ.get("SUPABASE_DB_URL"):
        return None
        
    if conn is not None:
        return _get_cached_match_impl(conn, service, song_id, title, artist, album)
        
    with connect(path) as new_conn:
        init_schema(new_conn)
        return _get_cached_match_impl(new_conn, service, song_id, title, artist, album)


def _cache_row_for_video(conn: sqlite3.Connection, video_id: str, *, status: str) -> dict[str, Any] | None:
    track_uid = find_track_by_video(conn, video_id)
    if track_uid:
        return _cache_row_for_track(conn, track_uid, status=status)
    return {"video_id": video_id, "score": 1.0, "status": status, "query": status}


def _cache_row_for_track(conn: sqlite3.Connection, track_uid: str, *, status: str = "cached_match") -> dict[str, Any] | None:
    track = conn.execute("SELECT * FROM tracks WHERE track_uid = ?", (track_uid,)).fetchone()
    if not track or not track["canonical_yt_video_id"]:
        return None
    meta = conn.execute(
        """
        SELECT tl.*
        FROM platform_song_ids ps
        JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
        WHERE ps.track_uid = ?
        ORDER BY
            CASE tl.service WHEN 'apple' THEN 0 WHEN 'melon' THEN 1 WHEN 'spotify' THEN 2 ELSE 3 END
        LIMIT 1
        """,
        (track_uid,),
    ).fetchone()
    out = {
        "video_id": track["canonical_yt_video_id"],
        "yt_title": track["yt_title"] or "",
        "yt_artist": track["yt_artist"] or "",
        "yt_album": track["yt_album"] or "",
        "score": float(track["best_score"] or 1.0),
        "title_score": 1.0,
        "artist_score": 1.0,
        "album_score": 1.0,
        "yt_result_type": "song",
        "query": "db_cache",
        "status": status,
    }
    if meta:
        out.update(
            {
                "title": meta["title_ko"] or meta["title_en"] or "",
                "artist": meta["artist_ko"] or meta["artist_en"] or "",
                "album": meta["album_ko"] or meta["album_en"] or "",
                "title_en": meta["title_en"] or "",
                "artist_en": meta["artist_en"] or "",
                "album_en": meta["album_en"] or "",
                "title_ko": meta["title_ko"] or "",
                "artist_ko": meta["artist_ko"] or "",
                "album_ko": meta["album_ko"] or "",
                "artwork_url": meta["artwork_url"] or "",
                "url": build_track_url(meta["service"], meta["song_id"], meta["album_id"]),
            }
        )
    return out


def build_match_cache(db_path: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(db_path)
    if not path.exists() and not os.environ.get("SUPABASE_DB_URL"):
        return {}
    cache: dict[str, dict[str, Any]] = {}
    with connect(path) as conn:
        init_schema(conn)
        rows = conn.execute(
            """
            SELECT m.lookup_key, t.track_uid
            FROM metadata_lookup_index m
            JOIN tracks t ON t.track_uid = m.track_uid
            WHERE t.canonical_yt_video_id IS NOT NULL AND t.canonical_yt_video_id != ''
            """
        ).fetchall()
        for row in rows:
            cached = _cache_row_for_track(conn, row["track_uid"])
            if cached:
                cache[row["lookup_key"]] = cached
    return cache


def record_playlist_update(
    db_path: str | Path,
    *,
    playlist_id: str,
    service: str = "",
    job_name: str = "",
    requested_video_ids: Iterable[str] = (),
    existing_video_ids: Iterable[str] = (),
    dry_run: bool = False,
) -> str:
    init_db(db_path)
    requested = [v for v in requested_video_ids if v]
    existing = [v for v in existing_video_ids if v]
    job_name = require_job_name(job_name)
    now = utc_now_iso()
    run_id = hashlib.sha1(
        f"{playlist_id}|{service}|{job_name}|{now}|{len(requested)}".encode("utf-8")
    ).hexdigest()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO playlist_update_runs(
                update_run_id, playlist_id, service, job_name, started_at,
                dry_run, requested_count, existing_count, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                playlist_id,
                normalized_service(service) if service else "",
                job_name,
                now,
                1 if dry_run else 0,
                len(requested),
                len(existing),
                now,
            ),
        )
        for index, video_id in enumerate(existing, 1):
            conn.execute(
                """
                INSERT INTO playlist_update_items(
                    update_run_id, action, video_id, item_order, created_at
                )
                VALUES (?, 'existing', ?, ?, ?)
                ON CONFLICT (update_run_id, action, video_id) DO UPDATE SET
                    item_order = EXCLUDED.item_order,
                    created_at = EXCLUDED.created_at
                """,
                (run_id, video_id, index, now),
            )
        for index, video_id in enumerate(requested, 1):
            conn.execute(
                """
                INSERT INTO playlist_update_items(
                    update_run_id, action, video_id, item_order, created_at
                )
                VALUES (?, 'requested', ?, ?, ?)
                ON CONFLICT (update_run_id, action, video_id) DO UPDATE SET
                    item_order = EXCLUDED.item_order,
                    created_at = EXCLUDED.created_at
                """,
                (run_id, video_id, index, now),
            )
        conn.commit()
    return run_id


def export_frontend_history(db_path: str | Path, output_path: str | Path, *, days: int = 31) -> dict[str, list[dict[str, Any]]]:
    path = Path(db_path)
    if not path.exists() and not os.environ.get("SUPABASE_DB_URL"):
        return {}
    with connect(path) as conn:
        apple_playlists = [
            name for name, item in hype_inputs().items()
            if item.get("hype_group") == "apple"
        ] or ["KR-Top-100"]
        placeholders = ",".join("?" for _ in apple_playlists)
        date_rows = conn.execute(
            f"""
            SELECT DISTINCT reference_period AS chart_date
            FROM playlist_order
            WHERE job_name IN ({placeholders})
              AND reference_period LIKE '____-__-__'
            ORDER BY chart_date DESC
            LIMIT ?
            """,
            (*apple_playlists, days),
        ).fetchall()
        dates = []
        for row in date_rows:
            try:
                from datetime import datetime, timedelta
                dt = datetime.strptime(row["chart_date"], "%Y-%m-%d")
                dates.append((dt + timedelta(days=1)).strftime("%Y-%m-%d"))
            except Exception:
                dates.append(row["chart_date"])
        history: dict[str, list[dict[str, Any]]] = {}
        previous_apple_videos: set[str] = set()
        for date in sorted(dates):
            report = hype_report_for_date(conn, date, previous_apple_videos=previous_apple_videos)
            history[date] = report
            previous_apple_videos = {row["video_id"] for row in report if row.get("apple_rank")}
        history = {date: history[date] for date in sorted(history.keys(), reverse=True)}
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return history


def hype_report_for_date(
    conn: sqlite3.Connection,
    chart_date: str,
    *,
    previous_apple_videos: set[str] | None = None,
) -> list[dict[str, Any]]:
    previous_apple_videos = previous_apple_videos or set()
    input_config = hype_inputs()
    ytmusic_jobs = [name for name, item in input_config.items() if item.get("hype_group") == "ytmusic"]
    # target_week: 주별 차트(ytmusic 등)의 carry-forward 기준 ISO 주 (예: '2026-W18')
    target_week = reference_period_for_date(ytmusic_jobs[0], chart_date) if ytmusic_jobs else chart_date
    rows = conn.execute(
        """
        WITH effective AS (
            -- 각 (service, job_name, source_variant) 조합에 대해
            -- 해당 날짜 이하의 가장 최신 reference_period를 선택합니다 (전체 소스 carry-forward).
            --   일별 차트 (Apple, Melon) 및 Spotify 주간 playlist: reference_period = 날짜 형식 (예: '2026-05-27')
            --     → INSTR(reference_period, '-W') = 0 이므로 chart_date 기준 비교
            --   주별 차트 (ytmusic): reference_period = ISO 주 형식 (예: '2026-W18')
            --     → INSTR(reference_period, '-W') > 0 이므로 target_week 기준 비교
            SELECT service, job_name, source_variant,
                   MAX(reference_period) AS eff_period
            FROM playlist_order
            WHERE (reference_period NOT LIKE '%-W%' AND reference_period < ?)
               OR (reference_period LIKE '%-W%' AND reference_period <= ?)
            GROUP BY service, job_name, source_variant
        )
        SELECT
            ps.track_uid,
            p.service,
            p.job_name,
            p.source_variant,
            p.song_id,
            tl.album_id,
            p.rank_order,
            t.canonical_yt_video_id AS video_id,
            t.yt_title,
            t.yt_artist,
            t.yt_album,
            COALESCE(NULLIF(tl.title_ko, ''), tl.title_en) AS title,
            COALESCE(NULLIF(tl.artist_ko, ''), tl.artist_en) AS artist,
            COALESCE(NULLIF(tl.album_ko, ''), tl.album_en) AS album,
            tl.artwork_url
        FROM playlist_order p
        JOIN effective e
          ON e.service = p.service
         AND e.job_name = p.job_name
         AND e.source_variant = p.source_variant
         AND p.reference_period = e.eff_period
        JOIN platform_song_ids ps
          ON ps.service = p.service
         AND ps.song_id = p.song_id
        JOIN tracks t ON t.track_uid = ps.track_uid
        LEFT JOIN track_list tl
            ON LOWER(tl.service) = LOWER(p.service)
           AND tl.song_id = p.song_id
        WHERE t.canonical_yt_video_id IS NOT NULL
          AND t.canonical_yt_video_id != ''
        """,
        (chart_date, target_week),
    ).fetchall()
    
    # Load Generation-wise weights dynamically from sync_config
    gen_z_task = input_config.get("Gen-Z-Daily") or {}
    gen1_weight = 0.60
    gen2_weight = 0.40
    source_urls = gen_z_task.get("source_urls", [])
    if isinstance(source_urls, list):
        for src in source_urls:
            if isinstance(src, dict):
                if str(src.get("gen")) == "1":
                    gen1_weight = float(src.get("weight") or 0.60)
                elif str(src.get("gen")) == "2":
                    gen2_weight = float(src.get("weight") or 0.40)

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row["video_id"]
        item = grouped.setdefault(
            uid,
            {
                "video_id": row["video_id"],
                "title": row["title"] or row["yt_title"] or "",
                "artist": row["artist"] or row["yt_artist"] or "",
                "album": row["album"] or row["yt_album"] or "",
                "yt_title": row["yt_title"] or "",
                "yt_artist": row["yt_artist"] or "",
                "yt_album": row["yt_album"] or "",
                "artwork_url": row["artwork_url"] or "",
                "apple_url": "",
                "melon_url": "",
                "spotify_url": "",
                "apple_rank": None,
                "melon_rank": None,
                "melon_genz_rank": None,
                "ytmusic_rank": None,
                "_score_parts": {},
                "_gen1_rank": None,
                "_gen2_rank": None,
            },
        )
        service = normalized_service(row["service"])
        job_name = str(row["job_name"] or "").strip()
        source_variant = str(row["source_variant"] or "default").strip()
        track_url = build_track_url(service, row["song_id"], row["album_id"])
        group = (input_config.get(job_name) or {}).get("hype_group", "")
        weight = float((input_config.get(job_name) or {}).get("hype_weight") or 0.0)
        if not group and service == "melon" and job_name == "Gen-Z-Daily":
            group = "melon_genz"
            weight = DEFAULT_HYPE_WEIGHTS["melon_genz"]
        if group == "apple":
            item["apple_rank"] = min(item["apple_rank"] or 9999, row["rank_order"])
            item["apple_url"] = track_url or item["apple_url"]
            item["_score_parts"]["apple"] = max(item["_score_parts"].get("apple", 0.0), calculate_rank_score(row["rank_order"]) * weight)
        elif group == "melon_genz":
            item["melon_genz_rank"] = min(item["melon_genz_rank"] or 9999, row["rank_order"])
            item["melon_url"] = track_url or item["melon_url"]
            if source_variant == "gen10":
                item["_gen1_rank"] = row["rank_order"]
            elif source_variant == "gen20":
                item["_gen2_rank"] = row["rank_order"]
            else:
                item["_score_parts"]["melon_genz"] = max(
                    item["_score_parts"].get("melon_genz", 0.0),
                    calculate_rank_score(row["rank_order"]) * weight,
                )
        elif group == "ytmusic":
            item["ytmusic_rank"] = min(item["ytmusic_rank"] or 9999, row["rank_order"])
            item["_score_parts"]["ytmusic"] = max(item["_score_parts"].get("ytmusic", 0.0), calculate_rank_score(row["rank_order"]) * weight)
        elif service == "melon" and job_name == "Top-100-Daily":
            item["melon_rank"] = min(item["melon_rank"] or 9999, row["rank_order"])
            item["melon_url"] = track_url or item["melon_url"]
        elif service == "spotify":
            item["spotify_url"] = track_url or item["spotify_url"]
        if row["rank_order"] and (not item["title"] or row["rank_order"] < min(item.get("_best_rank", 9999), 9999)):
            item["_best_rank"] = row["rank_order"]
            item["title"] = row["title"] or item["title"]
            item["artist"] = row["artist"] or item["artist"]
            item["album"] = row["album"] or item["album"]
            item["artwork_url"] = row["artwork_url"] or item["artwork_url"]

    report = []
    for item in grouped.values():
        item.pop("_gen1_rank", None)
        item.pop("_gen2_rank", None)
        score_parts = item.pop("_score_parts", {})
        hype_index = sum(score_parts.values())
        apple_rank = item["apple_rank"] or 101
        melon_rank = item["melon_rank"] or 101
        is_wave = apple_rank <= 100 and melon_rank > 100
        is_new_wave = is_wave and item["video_id"] not in previous_apple_videos
        item.pop("_best_rank", None)
        if item["apple_rank"] in (9999, None):
            item["apple_rank"] = None
        if item["melon_rank"] in (9999, None):
            item["melon_rank"] = None
        if item["melon_genz_rank"] in (9999, None):
            item["melon_genz_rank"] = None
        if item["ytmusic_rank"] in (9999, None):
            item["ytmusic_rank"] = None
        item.update(
            {
                "hype_index": round(hype_index, 2),
                "is_wave": is_wave,
                "is_new_wave": is_new_wave,
            }
        )
        if hype_index > 0:
            report.append(item)

    report.sort(key=lambda row: row["hype_index"], reverse=True)
    for index, row in enumerate(report, 1):
        row["hype_rank"] = index
    return report[:200]
