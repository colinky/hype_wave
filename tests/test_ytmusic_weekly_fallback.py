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
from hype_db import init_db, reference_period_for_date


class WeeklyHot100FallbackTests(unittest.TestCase):
    def test_schedule_retry_window_anchors_to_sunday(self) -> None:
        task = {
            "schedule": "Sunday",
            "schedule_retry_days": 2,
            "chart_period_anchor": "schedule",
        }
        sunday = datetime(2026, 6, 7, 9, 0)
        monday = datetime(2026, 6, 8, 9, 0)
        tuesday = datetime(2026, 6, 9, 9, 0)
        wednesday = datetime(2026, 6, 10, 9, 0)

        self.assertTrue(sync_all.schedule_window(task, sunday)[0])
        self.assertEqual(sync_all.chart_period_end_for_task(task, sunday), "2026-06-07")
        self.assertTrue(sync_all.schedule_window(task, monday)[0])
        self.assertEqual(sync_all.chart_period_end_for_task(task, monday), "2026-06-07")
        self.assertTrue(sync_all.schedule_window(task, tuesday)[0])
        self.assertEqual(sync_all.chart_period_end_for_task(task, tuesday), "2026-06-07")
        self.assertFalse(sync_all.schedule_window(task, wednesday)[0])

    def test_task_enabled_and_anchor_use_same_now_at_boundary(self) -> None:
        task = {
            "schedule": "Sunday",
            "schedule_retry_days": 2,
            "chart_period_anchor": "schedule",
        }
        tuesday_late = datetime(2026, 6, 9, 23, 59)
        wednesday_early = datetime(2026, 6, 10, 0, 0)

        self.assertTrue(sync_all.task_enabled(task, tuesday_late))
        self.assertEqual(sync_all.chart_period_end_for_task(task, tuesday_late), "2026-06-07")
        self.assertFalse(sync_all.task_enabled(task, wednesday_early))
        self.assertEqual(sync_all.chart_period_end_for_task(task, wednesday_early), "")

    def test_existing_schedule_without_retry_still_requires_exact_day(self) -> None:
        task = {"schedule": "Friday"}
        thursday = datetime(2026, 6, 4, 9, 0)
        friday = datetime(2026, 6, 5, 9, 0)

        self.assertFalse(sync_all.schedule_window(task, thursday)[0])
        self.assertTrue(sync_all.schedule_window(task, friday)[0])

    def test_reference_period_uses_anchor_sunday_iso_week(self) -> None:
        self.assertEqual(reference_period_for_date("Weekly-Hot-100", "2026-06-07"), "2026-W23")
        self.assertEqual(reference_period_for_date("Weekly-Hot-100", "2026-06-08"), "2026-W24")

    def test_not_published_soft_skips_without_playlist_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            argv = [
                "ytmusic_to_ytmusic_crawl.py",
                "--db-path",
                str(db_path),
                "--db-only",
                "--no-resolve",
                "--chart-period-end",
                "2026-06-07",
                "--track-limit",
                "100",
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
                audit = conn.execute(
                    "SELECT status, expected_chart_period_end, expected_reference_period, entry_count FROM chart_source_audit"
                ).fetchall()
                self.assertEqual(audit, [("not_published", "2026-06-07", "2026-W23", 0)])
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_order'"
                ).fetchone()
                self.assertIsNone(table)
            finally:
                conn.close()

    def test_published_csv_writes_expected_week_playlist_order(self) -> None:
        csv_text = (
            "Rank,Track Name,Artist Names,YouTube URL\n"
            "1,Song A,Artist A,https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
            "2,Song B,Artist B,https://www.youtube.com/watch?v=bbbbbbbbbbb\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hype.db"
            csv_path = Path(tmp) / "ytmusic_20260607.csv"
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
                with patch.object(sys, "argv", argv), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    self.assertEqual(ytmusic_crawl.main(), 0)
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
                self.assertEqual(audit, [("published", "2026-06-07", "2026-W23", 2)])
                count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM playlist_order
                    WHERE service='ytmusic'
                      AND job_name='Weekly-Hot-100'
                      AND reference_period='2026-W23'
                    """
                ).fetchone()[0]
                self.assertEqual(count, 2)
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
                "--chart-period-end",
                "2026-06-07",
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
                self.assertEqual(audit, [("validation_failed", "2026-06-07", "", 1)])
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='playlist_order'"
                ).fetchone()
                self.assertIsNone(table)
            finally:
                conn.close()

    def test_published_audit_is_not_written_when_raw_persistence_fails(self) -> None:
        entries = [
            {
                "rank": 1,
                "video_id": "aaaaaaaaaaa",
                "title": "Song A",
                "artist": "Artist A",
                "source": "youtube_charts_weekly_browse_api",
                "chart_period_start": "2026-06-01",
                "chart_period_end": "2026-06-07",
            }
        ]

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
                "--chart-period-end",
                "2026-06-07",
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

    def test_source_playlist_fallback_does_not_write_chart_published_audit(self) -> None:
        fallback_entries = [
            {
                "source": "youtube_music_playlist",
                "rank": 1,
                "original_video_id": "aaaaaaaaaaa",
                "original_title": "Song A",
                "original_artist_or_channel": "Artist A",
                "duration_seconds_original": 180,
                "album": "Album A",
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
                "--use-source-playlist",
                "--chart-period-end",
                "2026-06-07",
                "--track-limit",
                "1",
                "--yt-auth",
                "dummy-browser.json",
            ]
            old_bypass = os.environ.get("BYPASS_TRACK_COUNT_VAL")
            os.environ["BYPASS_TRACK_COUNT_VAL"] = "true"
            try:
                with patch.object(sys, "argv", argv), patch.object(
                    ytmusic_crawl, "make_ytmusic", return_value=object()
                ), patch.object(
                    ytmusic_crawl, "fetch_ytmusic_playlist_entries", return_value=fallback_entries
                ), patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    self.assertEqual(ytmusic_crawl.main(), 0)
            finally:
                if old_bypass is None:
                    os.environ.pop("BYPASS_TRACK_COUNT_VAL", None)
                else:
                    os.environ["BYPASS_TRACK_COUNT_VAL"] = old_bypass

            conn = sqlite3.connect(db_path)
            try:
                playlist_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM playlist_order
                    WHERE service='ytmusic'
                      AND job_name='Weekly-Hot-100'
                      AND reference_period='2026-W23'
                    """
                ).fetchone()[0]
                self.assertEqual(playlist_count, 1)
                published_audit_count = conn.execute(
                    "SELECT COUNT(*) FROM chart_source_audit WHERE status='published'"
                ).fetchone()[0]
                self.assertEqual(published_audit_count, 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
