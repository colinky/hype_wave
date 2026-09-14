from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import hype_db_reports as reports
from hype_db_schema import init_db


INPUTS = {
    "KR-Top-100": {
        "service": "apple", "frequency": "daily", "list_type": "chart",
        "hype_group": "apple", "hype_weight": 0.4,
    },
    "Weekly-Hot-100": {
        "service": "ytmusic", "frequency": "weekly", "list_type": "chart",
        "schedule": "Monday", "hype_group": "ytmusic", "hype_weight": 0.2,
    },
}


class HistoryRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = patch.object(reports, "hype_inputs", return_value=INPUTS)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.directory = tempfile.TemporaryDirectory(
            prefix=".history-rebuild-", dir=Path(__file__).resolve().parent,
        )
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixture.db"
        init_db(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def seed(self, service: str, period: str, *, status: str = "completed") -> str:
        job = "KR-Top-100" if service == "apple" else "Weekly-Hot-100"
        asset = f"{service}:{period}"
        # Deliberately newer than historical chart dates: a migration/recovery
        # timestamp is readiness evidence, not the chart's effective date.
        timestamp = "2030-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO tracks(track_uid, canonical_yt_video_id, yt_title, yt_artist, "
            "yt_album, created_at, updated_at) VALUES (?, ?, ?, 'Artist', 'Album', ?, ?)",
            (asset, asset, asset, timestamp, timestamp),
        )
        self.conn.execute("INSERT INTO platform_song_ids VALUES (?, ?, ?)", (service, asset, asset))
        self.conn.execute(
            "INSERT INTO track_list(service, song_id, title_ko, artist_ko, album_ko) "
            "VALUES (?, ?, ?, 'Artist', 'Album')", (service, asset, asset),
        )
        self.conn.execute(
            "INSERT INTO playlist_order VALUES (?, ?, 'default', ?, ?, 1)",
            (service, job, period, asset),
        )
        self.conn.execute(
            "INSERT INTO match_runs(run_id, service, job_name, source_variant, reference_period, "
            "started_at, status, completed_at, created_at) VALUES (?, ?, ?, 'default', ?, ?, ?, ?, ?)",
            (asset, service, job, period, timestamp, status,
             timestamp if status == "completed" else None, timestamp),
        )
        self.conn.execute(
            "INSERT INTO match_attempts(run_id,service,song_id,track_uid,rank_order,video_id,status,created_at) "
            "VALUES (?,?,?,?,1,?,'matched',?)",
            (asset, service, asset, asset, asset, timestamp),
        )
        self.conn.commit()
        return asset

    def selected(self, day: str, service: str) -> list[str]:
        return sorted(
            row["song_id"] for row in reports.fetch_hype_rows_for_dates(self.conn, [day])[day]
            if row["service"] == service
        )

    def test_monday_collection_uses_prior_thursday_week(self) -> None:
        expected = {
            "2026-08-24": "2026-W34", "2026-08-29": "2026-W34",
            "2026-08-30": "2026-W34", "2026-08-31": "2026-W35",
            "2026-09-05": "2026-W35", "2026-09-06": "2026-W35",
            "2026-09-07": "2026-W36",
        }
        for period in ("2026-W34", "2026-W35", "2026-W36", "2026-W37"):
            self.seed("ytmusic", period)
        for day, period in expected.items():
            with self.subTest(day=day):
                self.assertEqual(reports._weekly_reference_cutoff_for_history_date(day, INPUTS), period)
                self.assertEqual(self.selected(day, "ytmusic"), [f"ytmusic:{period}"])

    def test_iso_year_boundary_not_calendar_year(self) -> None:
        for period in ("2026-W52", "2026-W53", "2027-W01"):
            self.seed("ytmusic", period)
        for day, period in {
            "2026-12-31": "2026-W52", "2027-01-01": "2026-W52",
            "2027-01-03": "2026-W52", "2027-01-04": "2026-W53",
            "2027-01-10": "2026-W53", "2027-01-11": "2027-W01",
        }.items():
            with self.subTest(day=day):
                self.assertEqual(reports._weekly_reference_cutoff_for_history_date(day, INPUTS), period)
                self.assertEqual(self.selected(day, "ytmusic"), [f"ytmusic:{period}"])

    def test_configured_schedule_is_used(self) -> None:
        friday = copy.deepcopy(INPUTS)
        friday["Weekly-Hot-100"]["schedule"] = "Friday"
        for day, expected in (("2026-08-27", "2026-W34"), ("2026-08-28", "2026-W35")):
            with self.subTest(day=day):
                self.assertEqual(reports._weekly_reference_cutoff_for_history_date(day, friday), expected)
        sunday = copy.deepcopy(INPUTS)
        sunday["Weekly-Hot-100"]["schedule"] = "Sunday"
        self.assertEqual(reports._weekly_reference_cutoff_for_history_date("2026-08-30", sunday), "2026-W35")
        self.assertEqual(reports._weekly_reference_cutoff_for_history_date("2026-09-06", sunday), "2026-W36")

    def test_raw_row_order_is_stable_when_sqlite_reverses_unordered_results(self) -> None:
        self.seed("ytmusic", "2026-W35")
        self.seed("apple", "2026-08-29")
        self.seed("apple", "2026-08-30")
        days = ["2026-08-30", "2026-08-31"]
        before = reports.fetch_hype_rows_for_dates(self.conn, days)
        self.conn.execute("PRAGMA reverse_unordered_selects = ON")
        after = reports.fetch_hype_rows_for_dates(self.conn, list(reversed(days)))
        for day in days:
            keys = lambda row: tuple(row[key] for key in (
                "service", "job_name", "source_variant", "rank_order", "song_id",
            ))
            self.assertEqual([keys(row) for row in before[day]], sorted(keys(row) for row in before[day]))
            self.assertEqual(before[day], after[day])

    def test_daily_d_minus_one_and_completed_snapshot_gate(self) -> None:
        self.seed("apple", "2026-08-27")
        pending = self.seed("apple", "2026-08-28", status="running")
        self.seed("apple", "2026-08-29")
        self.seed("ytmusic", "2026-W34")
        weekly_pending = self.seed("ytmusic", "2026-W35", status="running")
        for status, completed_at, apple_period, weekly_period in (
            ("running", None, "2026-08-27", "2026-W34"),
            ("completed", None, "2026-08-27", "2026-W34"),
            ("completed", "2030-01-01T00:00:00+00:00", "2026-08-28", "2026-W35"),
        ):
            with self.subTest(status=status, completed_at=completed_at):
                self.conn.execute(
                    "UPDATE match_runs SET status=?, completed_at=? WHERE run_id IN (?, ?)",
                    (status, completed_at, pending, weekly_pending),
                )
                self.assertEqual(self.selected("2026-08-29", "apple"), [f"apple:{apple_period}"])
                self.assertEqual(self.selected("2026-08-31", "ytmusic"), [f"ytmusic:{weekly_period}"])

    def test_missing_week_carries_forward_and_single_matches_batch(self) -> None:
        self.seed("ytmusic", "2026-W34")
        self.seed("ytmusic", "2026-W36")
        apple_video = self.seed("apple", "2026-08-28")
        days = ["2026-08-29", "2026-08-30", "2026-09-05", "2026-09-06", "2026-09-07"]
        batch = reports.fetch_hype_rows_for_dates(self.conn, days)
        for day in days:
            with self.subTest(day=day):
                previous = {apple_video}
                self.assertEqual(
                    reports.hype_report_for_date(self.conn, day, previous_apple_videos=previous),
                    reports.build_hype_report_from_rows(batch[day], previous_apple_videos=previous),
                )
        self.assertEqual(self.selected("2026-09-05", "ytmusic"), ["ytmusic:2026-W34"])
        self.assertEqual(self.selected("2026-09-06", "ytmusic"), ["ytmusic:2026-W34"])
        self.assertEqual(self.selected("2026-09-07", "ytmusic"), ["ytmusic:2026-W36"])

    def test_full_rebuild_latest_31_days_is_repeatable_and_preserves_source(self) -> None:
        newest = date(2026, 9, 7)
        for offset in range(32):
            self.seed("apple", (newest - timedelta(days=offset + 1)).isoformat())
        for week in range(31, 38):
            self.seed("ytmusic", f"2026-W{week:02d}")
        raw_before = [tuple(row) for row in self.conn.execute("SELECT * FROM playlist_order ORDER BY service, reference_period")]
        output = Path(self.directory.name) / "history.json"
        payload = reports.export_frontend_history(self.path, output, days=31, full_rebuild=True)
        expected_dates = [(newest - timedelta(days=offset)).isoformat() for offset in range(31)]
        self.assertEqual(payload["dates"], expected_dates)
        self.assertEqual((min(payload["dates"]), max(payload["dates"])), ("2026-08-08", "2026-09-07"))
        self.assertEqual(set(payload["rankings"]), set(expected_dates))
        inflated = reports.inflate_frontend_history(payload)
        referenced = set()
        for day, rows in inflated.items():
            d = date.fromisoformat(day)
            monday = d - timedelta(days=d.weekday())
            previous_thursday = monday - timedelta(days=4)
            year, week, _ = previous_thursday.isocalendar()
            expected_ids = {f"apple:{d - timedelta(days=1)}", f"ytmusic:{year}-W{week:02d}"}
            self.assertEqual({row["video_id"] for row in rows}, expected_ids)
            referenced.update(expected_ids)
        self.assertEqual(set(payload["tracks"]), referenced)
        second = reports.export_frontend_history(self.path, output, days=31, full_rebuild=True)
        for key in ("dates", "tracks", "rankings", "days", "schema_version"):
            self.assertEqual(second[key], payload[key])
        self.assertEqual(
            [tuple(row) for row in self.conn.execute("SELECT * FROM playlist_order ORDER BY service, reference_period")],
            raw_before,
        )


if __name__ == "__main__":
    unittest.main()
