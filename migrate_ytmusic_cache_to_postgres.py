#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from hype_db_common import postgres_connect_config


ARTIST_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ytmusic_artist_translations (
    artist_id TEXT PRIMARY KEY,
    names JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

SONG_TABLE_SQL = """
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate local ytmusic_cache.db SQLite cache into Supabase PostgreSQL."
    )
    parser.add_argument(
        "--cache-db",
        default=str(Path(__file__).parent / "ytmusic_cache.db"),
        help="Path to restored ytmusic_cache.db. Defaults to repo root ytmusic_cache.db.",
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("SUPABASE_DB_URL"),
        help="PostgreSQL connection URL. Defaults to SUPABASE_DB_URL.",
    )
    return parser.parse_args()


def parse_updated_at(value: str) -> datetime:
    if not value:
        raise ValueError("updated_at is empty")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_artist_rows(conn: sqlite3.Connection) -> list[tuple[str, list[str], datetime]]:
    rows: list[tuple[str, list[str], datetime]] = []
    for row in conn.execute("SELECT artist_id, names, updated_at FROM artist_translations"):
        artist_id = row["artist_id"]
        if not artist_id:
            raise ValueError("artist_translations contains an empty artist_id")
        names_raw = row["names"]
        names: Any = json.loads(names_raw)
        if not isinstance(names, list):
            raise ValueError(f"artist_translations.names must be a JSON array: {artist_id}")
        rows.append((artist_id, [str(item) for item in names if str(item)], parse_updated_at(row["updated_at"])))
    return rows


def load_song_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for row in conn.execute(
        """
        SELECT video_id, title_ko, title_en, artist_ko, artist_en, album_ko, album_en, updated_at
        FROM song_translations
        """
    ):
        video_id = row["video_id"]
        if not video_id:
            raise ValueError("song_translations contains an empty video_id")
        rows.append(
            (
                video_id,
                row["title_ko"] or "",
                row["title_en"] or "",
                row["artist_ko"] or "",
                row["artist_en"] or "",
                row["album_ko"] or "",
                row["album_en"] or "",
                parse_updated_at(row["updated_at"]),
            )
        )
    return rows


def ensure_postgres_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(ARTIST_TABLE_SQL)
        cursor.execute(SONG_TABLE_SQL)
    conn.commit()


def connect_postgres(postgres_url: str):
    import psycopg2

    pg_config = postgres_connect_config()
    retries = int(pg_config["retries"])
    retry_delay = float(pg_config["retry_delay"])
    connect_timeout = int(pg_config["connect_timeout"])
    for attempt in range(retries):
        try:
            return psycopg2.connect(postgres_url, connect_timeout=connect_timeout)
        except psycopg2.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(retry_delay * (2**attempt))
    raise RuntimeError("unreachable")


def migrate(cache_db: Path, postgres_url: str) -> tuple[int, int]:
    from psycopg2.extras import Json, execute_values

    if not cache_db.exists():
        raise FileNotFoundError(f"Cache DB not found: {cache_db}")

    sqlite_conn = sqlite3.connect(cache_db)
    sqlite_conn.row_factory = sqlite3.Row
    try:
        artist_rows = [
            (artist_id, Json(names), updated_at)
            for artist_id, names, updated_at in load_artist_rows(sqlite_conn)
        ]
        song_rows = load_song_rows(sqlite_conn)
    finally:
        sqlite_conn.close()

    pg_conn = connect_postgres(postgres_url)
    try:
        ensure_postgres_schema(pg_conn)
        with pg_conn.cursor() as cursor:
            if artist_rows:
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
            if song_rows:
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
        pg_conn.commit()
    except BaseException:
        pg_conn.rollback()
        raise
    finally:
        pg_conn.close()

    return len(artist_rows), len(song_rows)


def main() -> int:
    args = parse_args()
    if not args.postgres_url:
        print("SUPABASE_DB_URL or --postgres-url is required.", file=sys.stderr)
        return 2

    artist_count, song_count = migrate(Path(args.cache_db), args.postgres_url)
    print(f"Migrated ytmusic cache to PostgreSQL: {artist_count} artists, {song_count} songs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
