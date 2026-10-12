"""Runtime chart dates follow the parent's UTC day, preserving explicit source dates."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apple_music_to_ytmusic_crawl as apple
import hype_db
import hype_db_common as common
from hype_db_reports import _history_date_from_hype_anchor_reference
import sync_all
import ytmusic_to_ytmusic_crawl as youtube
from ytmusic_playlist_sync import SourceTrack


class UtcChartDateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "HYPE_DB_BACKEND": "sqlite", "APPLE_CHART_LIMIT": "100",
        }))
        os.environ.pop("HYPE_SYNC_DATE", None)
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("external access forbidden")))

    def test_standalone_date_changes_at_utc_midnight_not_korean_midnight(self):
        for instant, expected in (
            ("2026-10-05T14:59:59+00:00", "2026-10-05"),
            ("2026-10-05T15:00:00+00:00", "2026-10-05"),
            ("2026-10-05T15:46:00+00:00", "2026-10-05"),
            ("2026-10-05T23:59:59+00:00", "2026-10-05"),
            ("2026-10-06T00:00:00+00:00", "2026-10-06"),
        ):
            with self.subTest(instant=instant), patch.object(common, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(instant)
                self.assertEqual(common.sync_date(), expected)
                clock.now.assert_called_once_with(timezone.utc)

    def test_parent_date_survives_next_utc_day_and_requires_strict_iso_format(self):
        with patch.dict(os.environ, {"HYPE_SYNC_DATE": "2026-10-05"}), patch.object(
                common, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
            self.assertEqual(common.sync_date(), "2026-10-05")
            clock.now.assert_not_called()
        for value in ("", "20261005", "2026-10-5", "2026-02-30", " 2026-10-05", "2026-10-05T00:00:00Z"):
            with self.subTest(value=value), patch.dict(os.environ, {"HYPE_SYNC_DATE": value}):
                with self.assertRaisesRegex(ValueError, "HYPE_SYNC_DATE"):
                    common.sync_date()

    def apple_pipeline_call(self, instant, *, reference_period=None):
        tracks = [SourceTrack(rank=rank, title=f"Song {rank}", artist="Artist",
                              service="apple", song_id=str(rank)) for rank in range(1, 101)]
        with tempfile.TemporaryDirectory() as directory:
            argv = ["apple_music_to_ytmusic_crawl.py", "--job-name", "KR-Top-100",
                    "--apple-playlist-urls", "https://music.apple.com/kr/playlist/fixture",
                    "--yt-auth", "unused.json", "--skip-playlist-update", "--no-db-cache",
                    "--db-path", str(Path(directory) / "unused.db")]
            if reference_period:
                argv.extend(["--reference-period", reference_period])
            with (
                patch.object(sys, "argv", argv),
                patch.object(apple, "load_dotenv"),
                patch.object(common, "datetime", wraps=datetime) as clock,
                patch.object(apple, "fetch_apple_tracks", return_value=(
                    "Fixture", "", tracks, "apple_web_playlist_api", {})),
                patch.object(apple, "make_ytmusic", return_value=Mock()),
                patch.object(apple, "process_matching_pipeline", return_value=["video"]) as pipeline,
                patch.object(apple, "update_ytmusic_playlist") as publish,
                self.assertLogs(apple.LOG, level="INFO"),
            ):
                clock.now.return_value = datetime.fromisoformat(instant)
                self.assertEqual(apple.main(), 0)
            publish.assert_not_called()
            return pipeline.call_args.kwargs

    def test_apple_main_passes_utc_date_and_keeps_previous_day_reference(self):
        values = self.apple_pipeline_call("2026-10-05T15:46:00+00:00")
        self.assertEqual(values["update_date_str"], "2026-10-05")
        self.assertIsNone(values["reference_period"])
        reference = common.reference_period_for_date(
            values["job_name"], values["update_date_str"], values["reference_period"])
        self.assertEqual(reference, "2026-10-04")
        self.assertEqual(_history_date_from_hype_anchor_reference(reference), "2026-10-05")
        self.assertEqual(common.parse_crawl_date("20261005T154600Z"), "2026-10-05")
        self.assertEqual(common.reference_period_for_date("KR-Top-100", "20261005T154600Z"),
                         "2026-10-04")

    def test_apple_main_uses_inherited_day_after_utc_midnight(self):
        with patch.dict(os.environ, {"HYPE_SYNC_DATE": "2026-10-05"}):
            values = self.apple_pipeline_call("2026-10-06T00:10:00+00:00")
        self.assertEqual(values["update_date_str"], "2026-10-05")

    def test_explicit_apple_backfill_date_is_not_replaced_by_run_date(self):
        with patch.dict(os.environ, {"HYPE_SYNC_DATE": "2026-10-05"}):
            values = self.apple_pipeline_call("2026-10-06T00:10:00+00:00", reference_period="2026-09-20")
        self.assertEqual(values["reference_period"], "2026-09-20")
        reference = common.reference_period_for_date(
            values["job_name"], values["update_date_str"], values["reference_period"])
        self.assertEqual(reference, "2026-09-20")
        self.assertEqual(_history_date_from_hype_anchor_reference(reference), "2026-09-21")

    def test_youtube_original_chart_end_still_determines_week(self):
        with patch.dict(os.environ, {"HYPE_SYNC_DATE": "2026-10-13"}):
            end = youtube.infer_chart_period_end_from_path("youtube-charts-top-songs-kr-weekly-20261001.csv")
            end = youtube.dashed_date(end)
            self.assertEqual(end, "2026-10-01")
            self.assertEqual(common.reference_period_for_date("Weekly-Hot-100", end), "2026-W40")

    def test_parent_pins_start_day_for_every_child_even_after_midnight(self):
        tasks = [{"service": service, "job_name": f"Fixture-{service}",
                  "target_id": f"target-{service}", "source_urls": [f"https://example.test/{service}"]}
                 for service in ("apple", "spotify")]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        child_dates = []

        def child(_command, *, env, **_kwargs):
            with patch.dict(os.environ, env), patch.object(common, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
                child_dates.append(common.sync_date())

        with (
            patch.dict(os.environ, {"HYPE_SYNC_DATE": "1999-01-01"}),
            patch.object(sync_all, "utc_now", return_value=datetime(2026, 10, 5, 23, 59, tzinfo=timezone.utc)),
            patch.object(sync_all, "load_env_file"),
            patch.object(sync_all, "postgres_url", return_value=""),
            patch.object(sync_all.json, "load", return_value=tasks),
            patch.object(sync_all.subprocess, "run", side_effect=child),
            patch.object(hype_db, "connect", return_value=conn),
            patch.object(sync_all, "data_snapshot_ready", return_value=False),
            self.assertLogs("sync_all", level="INFO"),
            self.assertRaises(SystemExit) as exited,
        ):
            sync_all.main()
        self.assertEqual(exited.exception.code, 1)
        self.assertEqual(child_dates, ["2026-10-05", "2026-10-05"])


if __name__ == "__main__":
    unittest.main()
