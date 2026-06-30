#!/usr/bin/env python3
"""
heal_split_tracks.py
--------------------
Finds YTMusic chart entries in playlist_order whose song_id has no binding
in platform_song_ids, attempts to find the canonical track_uid for the same
song (e.g. a Live/MV version of a track that's already matched via Apple/Melon),
and re-binds the ytmusic song_id to that canonical track_uid.

This repairs the "split track UID" problem where:
  - `소문의 낙원 (Live)` (song_id D54StAZFUrc) has no platform_song_ids row
  - But `소문의 낙원` is already in tracks via Apple → canonical_yt_video_id 6Xa1VDLACPo
  - After healing, the ytmusic rank is properly aggregated in hype_report_for_date.

If SUPABASE_DB_URL is set in the environment, it automatically connects to the hosted
PostgreSQL database instead of the local SQLite database.

Usage:
    python heal_split_tracks.py [--db-path hype_wave_data.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from hype_db_common import clean_track_title, compact_metadata_key, strip_parens_from_title

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("heal_split_tracks")


# ---------------------------------------------------------------------------
# Core healing logic
# ---------------------------------------------------------------------------

def find_canonical_uid(conn: Any, title: str, artist: str) -> str | None:
    """Search metadata_lookup_index for a canonical track using cleaned title."""
    cleaned = clean_track_title(title)
    # Try original then cleaned title
    candidates = [
        compact_metadata_key(title, artist),
        compact_metadata_key(cleaned, artist),
    ]
    # Fallback: strip ALL parens from query title — matches the stripped-key
    # entries that upsert_metadata_lookup stores at score*0.6.
    stripped = strip_parens_from_title(title)
    if stripped != title and stripped != cleaned:
        candidates.append(compact_metadata_key(stripped, artist))
    for key in candidates:
        row = conn.execute(
            "SELECT mi.track_uid FROM metadata_lookup_index mi "
            "JOIN tracks t ON t.track_uid = mi.track_uid "
            "WHERE mi.lookup_key = ? AND t.canonical_yt_video_id IS NOT NULL AND t.canonical_yt_video_id != ''",
            (key,),
        ).fetchone()
        if row:
            return row[0]
    # Fallback 3: strip parens from artist
    # e.g. 'LE SSERAFIM (르세라핌)' → 'LE SSERAFIM' → matches Apple's 'boompala|le sserafim'
    stripped_artist = strip_parens_from_title(artist)
    if stripped_artist != artist:
        for t in (title, cleaned):
            row = conn.execute(
                "SELECT mi.track_uid FROM metadata_lookup_index mi "
                "JOIN tracks t ON t.track_uid = mi.track_uid "
                "WHERE mi.lookup_key = ? AND t.canonical_yt_video_id IS NOT NULL AND t.canonical_yt_video_id != ''",
                (compact_metadata_key(t, stripped_artist),),
            ).fetchone()
            if row:
                return row[0]
    return None


def _merge_into(conn: Any, loser_uid: str, winner_uid: str, dry_run: bool) -> None:
    """Rebind all loser's platform_song_ids to winner, migrate index, delete loser."""
    if not dry_run:
        conn.execute(
            "UPDATE platform_song_ids SET track_uid = ? WHERE track_uid = ?",
            (winner_uid, loser_uid),
        )
        conn.execute(
            "UPDATE metadata_lookup_index SET track_uid = ? WHERE track_uid = ?",
            (winner_uid, loser_uid),
        )
        # Add loser's video as non-canonical on winner
        loser_video = conn.execute(
            "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?", (loser_uid,)
        ).fetchone()
        if loser_video and loser_video[0]:
            conn.execute(
                """
                INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
                VALUES (?, ?, 0)
                ON CONFLICT(video_id) DO NOTHING
                """,
                (loser_video[0], winner_uid),
            )
        conn.execute("DELETE FROM tracks WHERE track_uid = ?", (loser_uid,))


def heal(db_path: Path, dry_run: bool) -> int:
    import os
    import sys
    # Add project root to sys.path if not present
    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    import hype_db

    # ── Pass 1: unbound YTMusic song_ids ────────────────────────────────────
    with hype_db.connect(db_path) as conn:
        if not os.environ.get("SUPABASE_DB_URL"):
            conn.execute("PRAGMA journal_mode=WAL")

        unbound = conn.execute(
            """
            SELECT DISTINCT po.song_id
            FROM playlist_order po
            WHERE po.service = 'ytmusic'
              AND NOT EXISTS (
                  SELECT 1 FROM platform_song_ids ps
                  WHERE ps.service = 'ytmusic' AND ps.song_id = po.song_id
              )
            """
        ).fetchall()

        LOG.info("Pass 1 — Found %d unbound YTMusic song_ids in playlist_order", len(unbound))

        healed = 0
        skipped = 0
        for row in unbound:
            song_id = row[0]
            tl = conn.execute(
                "SELECT title_ko, title_en, artist_ko, artist_en FROM track_list "
                "WHERE service = 'ytmusic' AND song_id = ?",
                (song_id,),
            ).fetchone()
            if not tl:
                skipped += 1
                continue

            title = tl["title_ko"] or tl["title_en"] or ""
            artist = tl["artist_ko"] or tl["artist_en"] or ""

            yt_vid_row = conn.execute(
                "SELECT track_uid FROM yt_video_ids WHERE video_id = ?", (song_id,)
            ).fetchone()
            if yt_vid_row:
                canonical_uid = yt_vid_row[0]
                strategy = "video_id"
            else:
                canonical_uid = find_canonical_uid(conn, title, artist)
                strategy = "metadata"

            if not canonical_uid:
                skipped += 1
                continue

            canon_row = conn.execute(
                "SELECT canonical_yt_video_id, match_status FROM tracks WHERE track_uid = ?",
                (canonical_uid,),
            ).fetchone()
            if not canon_row or not canon_row["canonical_yt_video_id"]:
                skipped += 1
                continue

            LOG.info(
                "[P1] [%s] '%s' / '%s' → %s (video: %s, strategy: %s)",
                song_id, title, artist, canonical_uid,
                canon_row["canonical_yt_video_id"], strategy,
            )

            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO platform_song_ids(service, song_id, track_uid)
                    VALUES ('ytmusic', ?, ?)
                    ON CONFLICT(service, song_id) DO UPDATE SET track_uid = excluded.track_uid
                    """,
                    (song_id, canonical_uid),
                )
                conn.execute(
                    """
                    INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
                    VALUES (?, ?, 0)
                    ON CONFLICT(video_id) DO NOTHING
                    """,
                    (song_id, canonical_uid),
                )

            healed += 1

        if not dry_run:
            conn.commit()
        LOG.info("Pass 1 done. healed=%d skipped=%d", healed, skipped)

        # ── Pass 2: cross-service wrong bindings (BOOMPALA pattern) ─────────────
        # Scan all bound tracks for cases where the artist field contains en+ko
        # (e.g. 'LE SSERAFIM (르세라핌)') and a better-scoring canonical exists
        # via the parens-stripped artist.
        LOG.info("Pass 2 — Scanning for cross-service wrong bindings...")

        bound_rows = conn.execute(
            """
            SELECT ps.service, ps.song_id, ps.track_uid,
                   COALESCE(tl.title_ko, tl.title_en, '') AS title,
                   COALESCE(tl.artist_ko, tl.artist_en, '') AS artist,
                   COALESCE(mi_max.best_score, 0) AS current_score
            FROM platform_song_ids ps
            LEFT JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
            LEFT JOIN (
                SELECT track_uid, MAX(score) AS best_score
                FROM metadata_lookup_index GROUP BY track_uid
            ) mi_max ON mi_max.track_uid = ps.track_uid
            JOIN tracks t ON t.track_uid = ps.track_uid
            WHERE t.canonical_yt_video_id IS NOT NULL AND t.canonical_yt_video_id != ''
              AND t.match_status NOT IN ('failed', 'duplicate_skipped', 'manual_blocked')
            """
        ).fetchall()

        merged = 0
        for row in bound_rows:
            title = row["title"]
            artist = row["artist"]
            if not title or not artist:
                continue

            stripped_artist = strip_parens_from_title(artist)
            if stripped_artist == artist:
                continue  # No parens in artist — not this pattern

            better_uid = find_canonical_uid(conn, title, stripped_artist)
            if not better_uid or better_uid == row["track_uid"]:
                continue

            better_score = conn.execute(
                "SELECT MAX(score) FROM metadata_lookup_index WHERE track_uid = ?", (better_uid,)
            ).fetchone()[0] or 0

            if better_score <= row["current_score"]:
                continue

            LOG.info(
                "[P2] '%s' / '%s' — %s/%s: %s → %s (score %.2f → %.2f)",
                title, artist,
                row["service"], row["song_id"],
                row["track_uid"][:18], better_uid[:18],
                row["current_score"], better_score,
            )

            if not dry_run:
                _merge_into(conn, row["track_uid"], better_uid, dry_run=False)

            merged += 1

        if not dry_run:
            conn.commit()
        LOG.info("Pass 2 done. merged=%d", merged)
        LOG.info("Total — healed=%d merged=%d dry_run=%s", healed, merged, dry_run)
        return healed + merged


def main() -> int:
    import os
    p = argparse.ArgumentParser(description="Heal split track UIDs in hype_wave_data.db")
    p.add_argument("--db-path", default="hype_wave_data.db")
    p.add_argument("--dry-run", action="store_true", help="Show what would be healed without writing")
    args = p.parse_args()

    db_path = Path(args.db_path).expanduser()
    if not os.environ.get("SUPABASE_DB_URL") and not db_path.exists():
        LOG.error("DB not found: %s", db_path)
        return 1

    healed = heal(db_path, dry_run=args.dry_run)
    return 0 if healed >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
