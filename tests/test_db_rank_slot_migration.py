from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hype_db_schema import PLAYLIST_ORDER_PK, init_db, init_schema, table_primary_key
from hype_db_store import persist_crawl_run, persist_crawled_tracks, track_list_metadata_params


def raw_track(rank: int, song_id: str, *, locale: str = "en") -> dict[str, object]:
    return {
        "rank": rank,
        "song_id": song_id,
        "title": f"Title {rank}",
        "artist": "Artist",
        "album": "Album",
        "locale": locale,
    }


def matched(track: dict[str, object]) -> dict[str, object]:
    return {
        **track,
        "video_id": f"video-{track['song_id']}",
        "yt_title": track["title"],
        "yt_artist": track["artist"],
        "yt_album": track["album"],
        "status": "matched",
        "score": 1.0,
    }


class RankSlotMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    @staticmethod
    def legacy_connection(path: Path, *, duplicate_slot: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE playlist_order (
                service TEXT NOT NULL,
                job_name TEXT NOT NULL,
                source_variant TEXT NOT NULL DEFAULT 'default',
                reference_period TEXT NOT NULL,
                song_id TEXT NOT NULL,
                rank_order INTEGER NOT NULL,
                PRIMARY KEY (service, job_name, source_variant, reference_period, song_id)
            );
            INSERT INTO playlist_order VALUES
                ('apple', 'Fixture-Daily', 'default', '2026-08-30', 'song-a', 1),
                ('apple', 'Fixture-Daily', 'default', '2026-08-30', 'song-b', 2);
            """
        )
        if duplicate_slot:
            conn.execute(
                "INSERT INTO playlist_order VALUES ('apple', 'Fixture-Daily', 'default', '2026-08-30', 'song-c', 2)"
            )
        conn.commit()
        return conn

    def test_legacy_song_pk_becomes_rank_pk_and_seeds_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "legacy.db"
            conn = self.legacy_connection(path)
            init_schema(conn)

            self.assertEqual(table_primary_key(conn, "playlist_order"), PLAYLIST_ORDER_PK)
            snapshot = conn.execute(
                "SELECT status, completed_at, total_tracks FROM match_runs WHERE source = 'migration_snapshot'"
            ).fetchone()
            self.assertEqual((snapshot["status"], snapshot["total_tracks"]), ("completed", 2))
            self.assertTrue(snapshot["completed_at"])

            conn.execute(
                "INSERT INTO playlist_order VALUES ('apple', 'Fixture-Daily', 'default', '2026-08-30', 'song-a', 3)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO playlist_order VALUES ('apple', 'Fixture-Daily', 'default', '2026-08-30', 'song-c', 2)"
                )
            conn.rollback()

            conn.execute(
                "INSERT INTO playlist_order VALUES ('apple', 'Other-Daily', 'default', '2026-08-30', 'new-song', 1)"
            )
            conn.commit()
            init_schema(conn)
            newly_seeded = conn.execute(
                "SELECT COUNT(*) FROM match_runs WHERE source = 'migration_snapshot' AND job_name = 'Other-Daily'"
            ).fetchone()[0]
            self.assertEqual(newly_seeded, 0)
            conn.close()

    def test_duplicate_legacy_rank_aborts_without_replacing_table(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "collision.db"
            conn = self.legacy_connection(path, duplicate_slot=True)
            with self.assertRaisesRegex(ValueError, "Duplicate playlist_order rank slot"):
                init_schema(conn)

            self.assertEqual(
                table_primary_key(conn, "playlist_order"),
                ("service", "job_name", "source_variant", "reference_period", "song_id"),
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_order").fetchone()[0], 3)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE version = 'playlist_order_rank_pk_v1'").fetchone()
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'playlist_order_rebuild_tmp'"
                ).fetchone()
            )
            conn.close()

    def test_late_marker_failure_rolls_back_pk_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "late-failure.db"
            conn = self.legacy_connection(path)
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT
                );
                CREATE TRIGGER reject_rank_marker
                BEFORE INSERT ON schema_migrations
                WHEN NEW.version = 'playlist_order_rank_pk_v1'
                BEGIN
                    SELECT RAISE(ABORT, 'forced late failure');
                END;
                """
            )
            conn.commit()

            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced late failure"):
                init_schema(conn)

            self.assertEqual(
                table_primary_key(conn, "playlist_order"),
                ("service", "job_name", "source_variant", "reference_period", "song_id"),
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_order").fetchone()[0], 2)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'playlist_order_rebuild_tmp'"
                ).fetchone()
            )
            conn.close()


class RawEffectiveLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_duplicate_raw_slots_readiness_and_same_period_refresh(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "lifecycle.db"
            raw = [raw_track(1, "a"), raw_track(2, "duplicate"), raw_track(3, "b"), raw_track(4, "duplicate")]
            effective = raw[:3]

            persist_crawled_tracks(
                path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=raw,
            )
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            counts = conn.execute(
                "SELECT COUNT(*) AS slots, COUNT(DISTINCT song_id) AS songs FROM playlist_order"
            ).fetchone()
            self.assertEqual((counts["slots"], counts["songs"]), (4, 3))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM frontend_history_source").fetchone()[0], 0)
            conn.close()

            persist_crawl_run(
                path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                started_at="2026-08-30T01:00:00Z",
                tracks=effective,
                matches=[matched(row) for row in effective],
                skip_playlist_order=True,
            )
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            run = conn.execute(
                "SELECT status, completed_at, total_tracks FROM match_runs WHERE source = 'crawler'"
            ).fetchone()
            self.assertEqual((run["status"], run["total_tracks"]), ("completed", 3))
            self.assertTrue(run["completed_at"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM frontend_history_source").fetchone()[0], 3)
            self.assertTrue(
                all(
                    row[0] == "matched"
                    for row in conn.execute(
                        "SELECT t.match_status FROM tracks t JOIN platform_song_ids ps ON ps.track_uid = t.track_uid"
                    )
                )
            )
            conn.close()

            persist_crawled_tracks(
                path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=raw,
            )
            init_db(path)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            run = conn.execute(
                "SELECT status, completed_at FROM match_runs WHERE source = 'crawler'"
            ).fetchone()
            self.assertEqual((run["status"], run["completed_at"]), ("stale", None))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM frontend_history_source").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM match_runs WHERE source = 'migration_snapshot' AND job_name = 'Fixture-Daily'"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(
                all(
                    row[0] == "matched"
                    for row in conn.execute(
                        "SELECT t.match_status FROM tracks t JOIN platform_song_ids ps ON ps.track_uid = t.track_uid"
                    )
                )
            )
            conn.close()

    def test_invalid_raw_structure_never_replaces_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "invalid-raw.db"
            valid = [raw_track(1, "a"), raw_track(2, "b")]
            persist_crawled_tracks(
                path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=valid,
            )
            with patch.dict(os.environ, {"BYPASS_TRACK_COUNT_VAL": "true"}), self.assertRaisesRegex(
                ValueError, "duplicate rank slot"
            ):
                persist_crawled_tracks(
                    path,
                    service="apple",
                    job_name="Fixture-Daily",
                    chart_date="2026-08-30",
                    reference_period="2026-08-30",
                    tracks=[raw_track(1, "replacement-a"), raw_track(1, "replacement-b")],
                )
            conn = sqlite3.connect(path)
            self.assertEqual(
                conn.execute("SELECT song_id, rank_order FROM playlist_order ORDER BY rank_order").fetchall(),
                [("a", 1), ("b", 2)],
            )
            conn.close()

    def test_effective_subset_cannot_mark_snapshot_completed(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "subset.db"
            raw = [raw_track(1, "a"), raw_track(2, "b")]
            persist_crawled_tracks(
                path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=raw,
            )
            with self.assertRaisesRegex(ValueError, "does not match raw snapshot"):
                persist_crawl_run(
                    path,
                    service="apple",
                    job_name="Fixture-Daily",
                    chart_date="2026-08-30",
                    reference_period="2026-08-30",
                    started_at="2026-08-30T01:00:00Z",
                    tracks=raw[:1],
                    matches=[matched(raw[0])],
                    skip_playlist_order=True,
                )
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM match_runs WHERE status = 'completed'").fetchone()[0], 0)
            conn.close()

    def test_generic_metadata_uses_only_source_locale(self) -> None:
        korean = track_list_metadata_params(
            service="apple", song_id="ko", row=raw_track(1, "ko", locale="ko-KR")
        )
        english = track_list_metadata_params(
            service="apple", song_id="en", row=raw_track(1, "en", locale="en-US")
        )
        self.assertEqual(korean[3:9], ("Title 1", "Artist", "Album", "", "", ""))
        self.assertEqual(english[3:9], ("", "", "", "Title 1", "Artist", "Album"))


if __name__ == "__main__":
    unittest.main()
