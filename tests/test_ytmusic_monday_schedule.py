"""UTC weekly scheduling and catch-up require complete source/publication evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hype_db_common import load_sync_config, reference_period_for_date
import sync_all


WEEKLY = {
    "job_name": "Weekly-Hot-100", "service": "ytmusic", "frequency": "weekly",
    "list_type": "chart", "schedule": "Monday", "enabled": True,
    "target_id": "weekly-target",
}
START = "2026-09-28T15:00:00+00:00"
ANCHOR = datetime(2026, 9, 28, tzinfo=timezone.utc)
TUESDAY = ANCHOR + timedelta(days=1, hours=7)


class YouTubeMondayScheduleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "HYPE_DB_BACKEND": "sqlite", "BYPASS_TRACK_COUNT_VAL": "false",
        }))
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("external access forbidden")))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "schedule.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE playlist_update_runs (
                playlist_id TEXT, job_name TEXT, service TEXT, status TEXT,
                dry_run INTEGER, completed_at TEXT, match_started_at TEXT,
                started_at TEXT, created_at TEXT
            );
            CREATE TABLE match_runs (
                run_id TEXT, service TEXT, job_name TEXT, source_variant TEXT,
                reference_period TEXT, started_at TEXT, created_at TEXT,
                status TEXT, completed_at TEXT, total_tracks INTEGER,
                matched_tracks INTEGER, failed_tracks INTEGER
            );
            CREATE TABLE playlist_order (
                service TEXT, job_name TEXT, source_variant TEXT,
                reference_period TEXT, song_id TEXT, rank_order INTEGER
            );
            CREATE TABLE match_attempts (
                run_id TEXT, service TEXT, song_id TEXT, rank_order INTEGER,
                status TEXT, video_id TEXT
            );
        """)

    def seed_source(self, *, started=START, period="2026-W39", task=WEEKLY):
        service, job = task["service"], task["job_name"]
        total = 100 if job == WEEKLY["job_name"] else 1
        self.conn.execute("INSERT INTO match_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (started, service, job, "default", period, started, started,
                           "completed", started, total, total, 0))
        self.conn.executemany("INSERT INTO playlist_order VALUES (?, ?, ?, ?, ?, ?)",
                             [(service, job, "default", period, f"song-{rank}", rank)
                              for rank in range(1, total + 1)])
        self.conn.executemany("INSERT INTO match_attempts VALUES (?, ?, ?, ?, ?, ?)",
                             [(started, service, f"song-{rank}", rank, "matched", f"video-{rank}")
                              for rank in range(1, total + 1)])
        self.conn.commit()
        self.assertTrue(sync_all.data_snapshot_ready(
            self.conn, service=service, job_name=job, started_at=started))

    def seed_publication(self, *, status="published", match_started=START,
                         started=START, task=WEEKLY, dry_run=0):
        self.conn.execute("INSERT INTO playlist_update_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (task["target_id"], task["job_name"], task["service"], status,
                           dry_run, started, match_started, started, started))
        self.conn.commit()

    def assert_due(self, expected, *, task=WEEKLY, now=TUESDAY):
        before = self.db.read_bytes()
        self.assertEqual(sync_all.task_enabled(task, now, db_path=self.db), expected)
        self.assertEqual(self.db.read_bytes(), before)

    def test_config_and_weekday_check_use_utc_monday(self):
        configured = next(task for task in load_sync_config() if task["job_name"] == "Weekly-Hot-100")
        self.assertEqual(configured["schedule"], "Monday")
        for offset in range(14):
            now = ANCHOR + timedelta(days=offset, hours=15)
            with self.subTest(now=now):
                self.assertEqual(sync_all.task_enabled(configured, now), offset % 7 == 0)

    def test_utc_midnight_boundaries_are_independent_of_korean_day(self):
        for instant, expected in (
            ("2026-09-27T23:59:59+00:00", False),
            ("2026-09-28T00:00:00+00:00", True),
            ("2026-09-28T23:59:59+00:00", True),
            ("2026-09-29T00:00:00+00:00", False),
            ("2026-09-29T00:00:00+09:00", True),
        ):
            with self.subTest(instant=instant), patch.object(
                    sync_all, "utc_now", return_value=datetime.fromisoformat(instant)):
                self.assertEqual(sync_all.task_enabled(WEEKLY), expected)

    def test_actual_delayed_mondays_at_korean_tuesday_midnight_remain_due(self):
        for instant in ("2026-09-28T15:00:00+00:00", "2026-10-05T15:00:00+00:00"):
            with self.subTest(instant=instant):
                now = datetime.fromisoformat(instant)
                self.assertEqual(now.astimezone(timezone(timedelta(hours=9))).weekday(), 1)
                self.assert_due(True, now=now)

    def test_tuesday_and_remaining_week_catch_up_without_completion(self):
        for days in range(1, 7):
            with self.subTest(days=days):
                self.assert_due(True, now=ANCHOR + timedelta(days=days, hours=7))

    def test_missing_database_still_allows_catch_up_without_creating_it(self):
        missing = self.db.with_name("missing.db")
        self.assertTrue(sync_all.task_enabled(WEEKLY, TUESDAY, db_path=missing))
        self.assertFalse(missing.exists())

    def test_completed_publication_skips_cycle_but_next_week_is_due(self):
        self.seed_source()
        self.seed_publication()
        for status in ("published", "skipped_current"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE playlist_update_runs SET status = ?", (status,))
                self.conn.commit()
                self.assertTrue(sync_all.scheduled_task_completed(self.conn, WEEKLY, ANCHOR))
                self.assert_due(False, now=datetime.fromisoformat(START) + timedelta(minutes=30))
                self.assert_due(False)
                self.assert_due(True, now=ANCHOR + timedelta(days=7, hours=15))
        compact_start = "20260928T150000Z"
        self.conn.execute("UPDATE match_runs SET started_at = ?", (compact_start,))
        self.conn.execute("UPDATE playlist_update_runs SET match_started_at = ?", (compact_start,))
        self.conn.commit()
        self.assert_due(False)

    def test_db_only_import_does_not_complete_publication(self):
        self.seed_source()
        self.assert_due(True)

    def test_failed_or_incomplete_publication_remains_due(self):
        self.seed_source()
        self.seed_publication()
        for status in ("running", "verification_failed", "mutation_failed", "restored", "recovery_required"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE playlist_update_runs SET status = ?", (status,))
                self.conn.commit()
                self.assert_due(True)
        self.conn.execute("UPDATE playlist_update_runs SET status = 'published', completed_at = NULL")
        self.conn.commit()
        self.assert_due(True)
        self.conn.execute("UPDATE playlist_update_runs SET completed_at = ?, dry_run = 1", (START,))
        self.conn.commit()
        self.assert_due(True)

    def test_newer_publication_failure_takes_precedence_over_earlier_success(self):
        self.seed_source()
        self.seed_publication()
        self.assert_due(False)
        self.seed_publication(status="recovery_required", started="2026-09-29T06:00:00+00:00")
        self.assert_due(True)

    def test_previous_cycle_or_unlinked_match_cannot_complete_current_cycle(self):
        self.seed_source()
        self.seed_publication()
        for started in (None, "", "2026-09-27T23:59:59+00:00",
                        "2026-10-05T00:00:00+00:00", "2026-09-28T16:00:00+00:00"):
            with self.subTest(started=started):
                self.conn.execute("UPDATE playlist_update_runs SET match_started_at = ?", (started,))
                self.conn.commit()
                self.assert_due(True)

    def test_old_source_period_does_not_complete_new_cycle(self):
        self.seed_source(period="2026-W38")
        self.seed_publication()
        self.assert_due(True)

    def test_changed_target_or_latest_target_owner_remains_due(self):
        self.seed_source()
        self.seed_publication()
        self.assert_due(True, task={**WEEKLY, "target_id": "new-target"})
        for field, value in (("job_name", "Other-Job"), ("service", "spotify")):
            with self.subTest(field=field):
                self.conn.execute("DELETE FROM playlist_update_runs")
                self.seed_publication(task={**WEEKLY, field: value})
                self.assert_due(True)

    def test_source_status_counts_and_final_attempts_must_all_be_complete(self):
        self.seed_source()
        self.seed_publication()
        mutations = (
            "UPDATE match_runs SET status = 'running'",
            "UPDATE match_runs SET completed_at = NULL",
            "UPDATE match_runs SET matched_tracks = 99",
            "DELETE FROM playlist_order WHERE rank_order = 100",
            "DELETE FROM match_attempts WHERE rank_order = 100",
            "UPDATE match_attempts SET status = 'pending' WHERE rank_order = 100",
            "UPDATE match_attempts SET rank_order = 101 WHERE rank_order = 100",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.conn.execute("SAVEPOINT incomplete_source")
                self.conn.execute(mutation)
                self.assertFalse(sync_all.scheduled_task_completed(self.conn, WEEKLY, ANCHOR))
                self.conn.execute("ROLLBACK TO incomplete_source")
                self.conn.execute("RELEASE incomplete_source")
        self.assert_due(False)

    def test_weekly_playlist_requires_scheduled_date_instead_of_chart_week(self):
        task = {"job_name": "Hot-Hits-Korea", "service": "spotify", "frequency": "weekly",
                "list_type": "playlist", "schedule": "Friday", "target_id": "spotify-target"}
        started = "2026-10-02T15:00:00+00:00"
        self.seed_source(started=started, period="2026-09-25", task=task)
        self.seed_publication(match_started=started, started=started, task=task)
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.assert_due(True, task=task, now=now)
        for table in ("match_runs", "playlist_order"):
            self.conn.execute(f"UPDATE {table} SET reference_period = '2026-10-02'")
        self.conn.commit()
        self.assert_due(False, task=task, now=now)

    def test_disabled_invalid_schedule_and_daily_tasks_never_access_database(self):
        with patch("hype_db.connect", side_effect=AssertionError("database must not be opened")):
            for enabled in (False, "false", "0", "no"):
                self.assert_due(False, task={**WEEKLY, "enabled": enabled})
            self.assert_due(False, task={**WEEKLY, "schedule": "Not-a-day"})
            self.assert_due(True, task={"job_name": "KR-Top-100", "service": "apple"})

    def test_stored_chart_period_still_comes_from_fetched_thursday_end(self):
        for end, period in (
            ("2026-08-27", "2026-W35"), ("2026-09-03", "2026-W36"),
            ("2026-09-24", "2026-W39"), ("2026-10-01", "2026-W40"),
            ("2026-12-31", "2026-W53"),
        ):
            with self.subTest(end=end):
                self.assertEqual(reference_period_for_date("Weekly-Hot-100", end), period)


if __name__ == "__main__":
    unittest.main()
