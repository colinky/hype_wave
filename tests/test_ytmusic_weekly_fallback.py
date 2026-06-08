from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_all
import ytmusic_to_ytmusic_crawl as ytmusic_crawl
from hype_db import init_db


def chart_entry(
    *,
    video_id: str = "aaaaaaaaaaa",
    rank: int = 1,
    title: str = "Song A",
    artist: str = "Artist A",
    period_start: str = "2026-05-29",
    period_end: str = "2026-06-04",
) -> dict[str, object]:
    return {
        "rank": rank,
        "video_id": video_id,
        "title": title,
        "artist": artist,
        "source": "youtube_charts_weekly_browse_api",
        "chart_period_start": period_start,
        "chart_period_end": period_end,
    }


def seed_playlist_order(db_path: Path, reference_period: str, *, song_id: str = "seedvideo01") -> None:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO playlist_order(service, job_name, source_variant, reference_period, song_id, rank_order)
            VALUES ('ytmusic', 'Weekly-Hot-100', 'default', ?, ?, 1)
            """,
            (reference_period, song_id),
        )
        conn.commit()
    finally:
        conn.close()


class WeeklyHot100FallbackTests(unittest.TestCase):
    def test_weekly_hot_100_retry_window_is_sunday_to_tuesday(self) -> None:
        task = {"service": "ytmusic", "job_name": "Weekly-Hot-100", "schedule": "Sunday"}
        sunday = datetime(2026, 6, 7, 9, 0)
        monday = datetime(2026, 6, 8, 9, 0)
        tuesday = datetime(2026, 6, 9, 9, 0)
        wednesday = datetime(2026, 6, 10, 9, 0)

        self.assertTrue(sync_all.task_enabled(task, sunday))
        self.assertTrue(sync_all.task_enabled(task, monday))
        self.assertTrue(sync_all.task_enabled(task, tuesday))
        self.assertFalse(sync_all.task_enabled(task, wednesday))
        self.assertFalse(hasattr(sync_all, "chart_period_end_for_task"))

    def test_existing_schedule_without_retry_still_requires_exact_day(self) -> None:
        task = {"service": "spotify", "schedule": "Friday"}
        thursday = datetime(2026, 6, 4, 9, 0)
        friday = datetime(2026, 6, 5, 9, 0)

        self.assertFalse(sync_all.schedule_window(task, thursday)[0])
        self.assertTrue(sync_all.schedule_window(task, friday)[0])

    def test_new_fetched_period_persists_when_db_latest_is_older(self) -> None:
        entries = [
            chart_entry(video_id="aaaaaaaaaaa", rank=1),
            chart_entry(video_id="bbbbbbbbbbb", rank=2, title="Song B", artist="Artist B"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            seed_playlist_order(db_path, "2026-W22")
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--track-limit",
                "2",
            ]
            old_bypass = os.environ.get("BYPASS_TRACK_COUNT_VAL")
            os.environ["BYPASS_TRACK_COUNT_VAL"] = "true"
            try:
                with patch.object(sys, "argv", argv), patch.object(
                    ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=entries
                ), patch.object(
                    ytmusic_crawl,
                    "discover_youtube_charts_source_info",
                    return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
                ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    self.assertEqual(ytmusic_crawl.main(), 0)
            finally:
                if old_bypass is None:
                    os.environ.pop("BYPASS_TRACK_COUNT_VAL", None)
                else:
                    os.environ["BYPASS_TRACK_COUNT_VAL"] = old_bypass

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM playlist_order
                        WHERE service='ytmusic' AND job_name='Weekly-Hot-100' AND reference_period='2026-W23'
                        """
                    ).fetchone()[0],
                    2,
                )
                audit = conn.execute(
                    "SELECT status, expected_chart_period_end, expected_reference_period, entry_count FROM chart_source_audit"
                ).fetchall()
                self.assertEqual(audit, [("published", "2026-06-04", "2026-W23", 2)])
            finally:
                conn.close()

    def test_already_current_soft_skips_without_playlist_rewrite(self) -> None:
        entries = [chart_entry()]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            seed_playlist_order(db_path, "2026-W23")
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--track-limit",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=entries
            ), patch.object(
                ytmusic_crawl,
                "discover_youtube_charts_source_info",
                return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
            ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                self.assertEqual(ytmusic_crawl.main(), 0)

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM playlist_order WHERE reference_period='2026-W23'").fetchone()[0],
                    1,
                )
                audit = conn.execute("SELECT status, entry_count FROM chart_source_audit").fetchall()
                self.assertEqual(audit, [("already_current", 1)])
            finally:
                conn.close()

    def test_stale_source_soft_skips_without_playlist_rewrite(self) -> None:
        entries = [chart_entry()]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            seed_playlist_order(db_path, "2026-W24")
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--track-limit",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=entries
            ), patch.object(
                ytmusic_crawl,
                "discover_youtube_charts_source_info",
                return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
            ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                self.assertEqual(ytmusic_crawl.main(), 0)

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM playlist_order WHERE reference_period='2026-W23'").fetchone()[0],
                    0,
                )
                audit = conn.execute("SELECT status, entry_count FROM chart_source_audit").fetchall()
                self.assertEqual(audit, [("stale_source", 1)])
            finally:
                conn.close()

    def test_empty_chart_rows_are_source_error_soft_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=[]
            ), patch.object(
                ytmusic_crawl,
                "discover_youtube_charts_source_info",
                return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
            ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                self.assertEqual(ytmusic_crawl.main(), 0)

            conn = sqlite3.connect(db_path)
            try:
                audit = conn.execute("SELECT status, entry_count FROM chart_source_audit").fetchall()
                self.assertEqual(audit, [("source_error", 0)])
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_order'"
                ).fetchone()
                self.assertIsNone(table)
            finally:
                conn.close()

    def test_periodless_scraped_chart_rows_are_validation_failed(self) -> None:
        entries = [
            {
                "rank": 1,
                "video_id": "aaaaaaaaaaa",
                "title": "Song A",
                "artist": "Artist A",
                "source": "youtube_charts_weekly_embedded_json",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--track-limit",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=entries
            ), patch.object(
                ytmusic_crawl,
                "discover_youtube_charts_source_info",
                return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
            ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                self.assertEqual(ytmusic_crawl.main(), 0)

            conn = sqlite3.connect(db_path)
            try:
                audit = conn.execute(
                    "SELECT status, expected_chart_period_end, fetched_chart_period_end, entry_count FROM chart_source_audit"
                ).fetchall()
                self.assertEqual(audit, [("validation_failed", "", "", 1)])
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_order'"
                ).fetchone()
                self.assertIsNone(table)
            finally:
                conn.close()

    def test_published_audit_is_not_written_when_raw_persistence_fails(self) -> None:
        entries = [chart_entry()]

        def fail_persist(*args, **kwargs):
            raise RuntimeError("forced persist failure")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--track-limit",
                "1",
            ]
            old_bypass = os.environ.get("BYPASS_TRACK_COUNT_VAL")
            os.environ["BYPASS_TRACK_COUNT_VAL"] = "true"
            try:
                with patch.object(sys, "argv", argv), patch.object(
                    ytmusic_crawl, "extract_chart_entries_from_youtube_charts", return_value=entries
                ), patch.object(
                    ytmusic_crawl,
                    "discover_youtube_charts_source_info",
                    return_value={"page_url": "", "csv_url": "", "csv_url_candidates": []},
                ), patch("hype_db.persist_crawled_tracks", fail_persist), patch.dict(
                    os.environ, {"SUPABASE_DB_URL": ""}
                ):
                    with self.assertRaises(RuntimeError):
                        ytmusic_crawl.main()
            finally:
                if old_bypass is None:
                    os.environ.pop("BYPASS_TRACK_COUNT_VAL", None)
                else:
                    os.environ["BYPASS_TRACK_COUNT_VAL"] = old_bypass

            conn = sqlite3.connect(db_path)
            try:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chart_source_audit'"
                ).fetchone()
                self.assertIsNone(table)
            finally:
                conn.close()

    def test_csv_input_derives_period_and_skips_network_discovery(self) -> None:
        csv_text = (
            "Rank,Track Name,Artist Names,YouTube URL\n"
            "1,Song A,Artist A,https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "2,Song B,Artist B,https://www.youtube.com/watch?v=bbbbbbbbbbb\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            csv_path = Path(tmp) / "ytmusic_20260604.csv"
            csv_path.write_text(csv_text, encoding="utf-8")
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--youtube-charts-csv",
                str(csv_path),
                "--track-limit",
                "2",
            ]
            old_bypass = os.environ.get("BYPASS_TRACK_COUNT_VAL")
            os.environ["BYPASS_TRACK_COUNT_VAL"] = "true"
            try:
                with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    init_db(db_path)
                with patch.object(sys, "argv", argv), patch.object(
                    ytmusic_crawl, "discover_youtube_charts_source_info"
                ) as discover_mock, patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    self.assertEqual(ytmusic_crawl.main(), 0)
                    discover_mock.assert_not_called()
            finally:
                if old_bypass is None:
                    os.environ.pop("BYPASS_TRACK_COUNT_VAL", None)
                else:
                    os.environ["BYPASS_TRACK_COUNT_VAL"] = old_bypass

            conn = sqlite3.connect(db_path)
            try:
                audit = conn.execute(
                    "SELECT status, expected_chart_period_end, expected_reference_period, entry_count FROM chart_source_audit"
                ).fetchall()
                self.assertEqual(audit, [("published", "2026-06-04", "2026-W23", 2)])
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM playlist_order WHERE reference_period='2026-W23'").fetchone()[0],
                    2,
                )
            finally:
                conn.close()

    def test_duplicate_audit_events_do_not_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            conn = sqlite3.connect(db_path)
            try:
                expected = ytmusic_crawl.chart_audit_expected(
                    chart_period_start="2026-05-29",
                    chart_period_end="2026-06-04",
                    reference_period="2026-W23",
                )
                for _ in range(2):
                    ytmusic_crawl.record_chart_source_audit(
                        conn,
                        job_name="Weekly-Hot-100",
                        expected=expected,
                        fetched_chart_period_start="2026-05-29",
                        fetched_chart_period_end="2026-06-04",
                        status="already_current",
                        entry_count=100,
                        source="youtube_charts_weekly_browse_api",
                        message="duplicate test",
                    )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM chart_source_audit").fetchone()[0], 2)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
