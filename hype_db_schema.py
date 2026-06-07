from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hype_db_common import (
    _source_variant_from_legacy,
    job_frequency,
    legacy_to_job_name,
    normalized_service,
    postgres_connect_config,
)
from hype_db_common import (
    normalize_source_variant,
    reference_period_for_date,
    utc_now_iso,
)

LOG = logging.getLogger("hype_db")
_POSTGRES_INDEXES_CHECKED = False
__all__ = [
    "PostgresRow",
    "PostgresCursorWrapper",
    "PostgresConnectionWrapper",
    "is_postgres_connection",
    "ensure_postgres_indexes",
    "connect",
    "init_db",
    "run_schema_migrations",
    "init_schema",
    "table_columns",
    "table_exists",
]

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


def is_postgres_connection(conn: Any) -> bool:
    return type(conn).__name__ == "PostgresConnectionWrapper"


def ensure_postgres_indexes(raw_conn: Any) -> None:
    """Create missing Supabase/PostgreSQL indexes used by the hot read/write paths."""
    global _POSTGRES_INDEXES_CHECKED
    if os.environ.get("HYPE_SKIP_POSTGRES_INDEX_CHECK") in {"1", "true", "TRUE"}:
        _POSTGRES_INDEXES_CHECKED = True
        return
    if _POSTGRES_INDEXES_CHECKED:
        return
    indexes = {
        "idx_tracks_canonical_yt": ("tracks", "CREATE INDEX IF NOT EXISTS idx_tracks_canonical_yt ON tracks(canonical_yt_video_id)"),
        "idx_yt_video_ids_track": ("yt_video_ids", "CREATE INDEX IF NOT EXISTS idx_yt_video_ids_track ON yt_video_ids(track_uid)"),
        "idx_platform_song_ids_track": ("platform_song_ids", "CREATE INDEX IF NOT EXISTS idx_platform_song_ids_track ON platform_song_ids(track_uid)"),
        "idx_metadata_lookup_track": ("metadata_lookup_index", "CREATE INDEX IF NOT EXISTS idx_metadata_lookup_track ON metadata_lookup_index(track_uid)"),
        "idx_playlist_order_job_period": ("playlist_order", "CREATE INDEX IF NOT EXISTS idx_playlist_order_job_period ON playlist_order(job_name, reference_period)"),
        "idx_playlist_order_effective": ("playlist_order", "CREATE INDEX IF NOT EXISTS idx_playlist_order_effective ON playlist_order(service, job_name, source_variant, reference_period)"),
        "idx_match_attempts_video": ("match_attempts", "CREATE INDEX IF NOT EXISTS idx_match_attempts_video ON match_attempts(video_id)"),
        "idx_match_attempts_created_at": ("match_attempts", "CREATE INDEX IF NOT EXISTS idx_match_attempts_created_at ON match_attempts(created_at)"),
        "idx_match_candidates_created_at": ("match_candidates", "CREATE INDEX IF NOT EXISTS idx_match_candidates_created_at ON match_candidates(created_at)"),
        "idx_playlist_update_items_run": ("playlist_update_items", "CREATE INDEX IF NOT EXISTS idx_playlist_update_items_run ON playlist_update_items(update_run_id)"),
    }
    with raw_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = ANY (current_schemas(false))
            """
        )
        existing = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = ANY (current_schemas(false))
            """
        )
        existing_tables = {row[0] for row in cursor.fetchall()}
        missing = [name for name, (table, _) in indexes.items() if name not in existing and table in existing_tables]
        for name in missing:
            LOG.info("Creating missing PostgreSQL index: %s", name)
            cursor.execute(indexes[name][1])
    raw_conn.commit()
    _POSTGRES_INDEXES_CHECKED = True


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
        pg_config = postgres_connect_config()
        retries = int(pg_config["retries"])
        delay = float(pg_config["retry_delay"])
        connect_timeout = int(pg_config["connect_timeout"])
        raw_conn = None
        for i in range(retries):
            try:
                raw_conn = psycopg2.connect(pg_url, connect_timeout=connect_timeout)
                with raw_conn.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '30s'")
                    cursor.execute("SET statement_timeout = '180s'")
                    cursor.execute("SET idle_in_transaction_session_timeout = '180s'")
                raw_conn.commit()
                ensure_postgres_indexes(raw_conn)
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
        if not row and table_exists(conn, "playlist_order"):
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
        elif not row:
            from datetime import datetime, timezone
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                ("spotify_weekly_date_recovery", datetime.now(timezone.utc).isoformat(), "Skip Spotify weekly recovery until playlist_order exists")
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

        CREATE TABLE IF NOT EXISTS chart_source_audit (
            audit_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            job_name TEXT NOT NULL,
            expected_chart_period_start TEXT,
            expected_chart_period_end TEXT,
            expected_reference_period TEXT,
            fetched_chart_period_start TEXT,
            fetched_chart_period_end TEXT,
            status TEXT NOT NULL,
            entry_count INTEGER DEFAULT 0,
            source TEXT,
            message TEXT,
            created_at TEXT NOT NULL
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
    from hype_db_store import repair_failed_source_bindings

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
        INSERT INTO schema_migrations(version, applied_at, description)
        VALUES ('db_source_v2_lean', ?, 'Lean DB source of truth schema')
        ON CONFLICT(version) DO NOTHING
        """,
        (utc_now_iso(),),
    )
    conn.execute("PRAGMA foreign_keys = ON")



def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone() is not None



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
