"""A quota outage may reuse only a complete official chart for the requested week."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from test_youtube_charts_browser import chart_payload
import ytmusic_to_ytmusic_crawl as crawl


class WeeklySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("offline test")))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(crawl, "SNAPSHOT_DIR", directory))
        self.clock = self.enterContext(patch.object(crawl, "datetime", wraps=datetime))
        self.clock.now.return_value = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.rows = crawl.extract_chart_entries_from_browser_json(chart_payload())
        crawl.save_weekly_snapshot(self.rows)
        self.path = directory / "kr-weekly-2026-10-08.json"
        self.snapshot = json.loads(self.path.read_text())

    def write_snapshot(self, snapshot):
        snapshot["sha256"] = crawl.weekly_snapshot_hash(snapshot)
        self.path.write_text(json.dumps(snapshot))

    def test_current_week_preserves_source_ranks_video_and_audio_ids_and_capture_time(self):
        for options in ({}, {"chart_period_end": "20261008"}, {"chart_period_start": "2026-10-02"}):
            with self.subTest(options=options):
                rows = crawl.load_weekly_snapshot(**options)
                self.assertEqual([(r["rank"], r["video_id"], r["atv_external_video_id"]) for r in rows],
                                 [(r["rank"], r["video_id"], r["atv_external_video_id"]) for r in self.rows])
                self.assertTrue(all(r["source_collected_at"] == self.snapshot["collected_at"]
                                    and r["collection_method"] == "verified_snapshot" for r in rows))
        original = self.path.read_bytes()
        self.clock.now.return_value = datetime(2026, 10, 14, 13, tzinfo=timezone.utc)
        crawl.save_weekly_snapshot(rows)
        self.assertEqual(self.path.read_bytes(), original)

    def test_requested_other_week_and_next_reporting_week_cannot_reuse_old_chart(self):
        for options in ({"chart_period_end": "2026-10-15"}, {"chart_period_start": "2026-10-01"},
                        {"chart_period_start": "2026-10-01", "chart_period_end": "2026-10-08"}):
            with self.subTest(options=options):
                self.assertEqual(crawl.load_weekly_snapshot(**options), [])
        self.clock.now.return_value = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(len(crawl.load_weekly_snapshot()), 100)
        self.clock.now.return_value = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(crawl.load_weekly_snapshot(), [])

    def test_provenance_and_capture_time_are_validated_even_with_recomputed_checksum(self):
        mutations = [lambda s: s.update(schema_version=True), lambda s: s.update(schema_version=2),
                     lambda s: s.update(source_url="https://example.com/chart"), lambda s: s.update(locale="xx"),
                     lambda s: s.update(collected_at="2026-10-15T12:00:00+00:00"),
                     lambda s: s.update(collected_at="2026-10-14T12:00:00"),
                     lambda s: s.update(collected_at="2026-10-07T12:00:00+00:00"),
                     lambda s: s.update(payload=[])]
        for change in mutations:
            snapshot = deepcopy(self.snapshot)
            change(snapshot)
            self.write_snapshot(snapshot)
            self.assertEqual(crawl.load_weekly_snapshot(), [])
        self.path.write_text("not json")
        self.assertEqual(crawl.load_weekly_snapshot(), [])
        self.path.write_text("[]")
        self.assertEqual(crawl.load_weekly_snapshot(), [])
        snapshot = deepcopy(self.snapshot)
        snapshot["collected_at"] = "2026-10-14T11:00:00+00:00"
        self.path.write_text(json.dumps(snapshot))
        self.assertEqual(crawl.load_weekly_snapshot(), [])

    def test_cached_raw_chart_must_still_pass_country_period_and_all_100_source_ranks(self):
        for case in ("country", "videos", "daily", "99", "101", "duplicate_rank", "string_rank",
                     "duplicate_video", "missing_title", "wrong_end", "missing_end"):
            snapshot = deepcopy(self.snapshot)
            payload = snapshot["payload"]
            chart, tracks = payload["chart"], payload["chart"]["trackViews"]
            if case == "country": payload["chartParams"]["countryCode"] = "us"
            elif case == "videos": payload["chartParams"]["chartType"] = "CHART_TYPE_VIDEOS"
            elif case == "daily": chart["chartPeriodType"] = "CHART_PERIOD_TYPE_DAILY"
            elif case == "99": tracks.pop()
            elif case == "101": tracks.append(deepcopy(tracks[-1]))
            elif case == "duplicate_rank": tracks[-1]["chartEntryMetadata"]["currentPosition"] = 99
            elif case == "string_rank": tracks[-1]["chartEntryMetadata"]["currentPosition"] = "100"
            elif case == "duplicate_video": tracks[-1]["encryptedVideoId"] = tracks[0]["encryptedVideoId"]
            elif case == "missing_title": tracks[-1]["name"] = ""
            elif case == "wrong_end": chart["endDate"] = "2026-10-01"
            else: del chart["endDate"]
            self.write_snapshot(snapshot)
            with self.subTest(case=case):
                self.assertEqual(crawl.load_weekly_snapshot(), [])

    def test_both_http_and_browser_outage_reuse_valid_snapshot_but_not_a_different_source(self):
        with patch.object(crawl.http_session, "get", side_effect=requests.HTTPError("429")), \
                patch.object(crawl, "extract_chart_entries_from_browser", return_value=[]) as browser:
            rows = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
            self.assertEqual(len(rows), 100)
            self.assertEqual(rows[0]["collection_method"], "verified_snapshot")
            self.assertEqual(rows[0]["source_collected_at"], self.snapshot["collected_at"])
            browser.assert_called_once()
            self.assertEqual(crawl.extract_chart_entries_from_youtube_charts("https://example.com/chart"), [])
            self.assertEqual(crawl.extract_chart_entries_from_youtube_charts(
                crawl.DEFAULT_YOUTUBE_CHARTS_URL, chart_period_end="2026-10-15"), [])

    def test_api_429_and_browser_429_reuse_valid_snapshot_without_rewriting_it(self):
        original = self.path.read_bytes()
        with patch.object(crawl.http_session, "get", return_value=Mock(text="")), \
                patch.object(crawl, "extract_chart_entries_from_browse_api", side_effect=requests.HTTPError("429")), \
                patch.object(crawl, "extract_chart_entries_from_browser", return_value=[]):
            rows = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        self.assertEqual(len(rows), 100)
        self.assertEqual(self.path.read_bytes(), original)

    def test_incomplete_chart_cannot_be_persisted(self):
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            crawl.save_weekly_snapshot(self.rows[:-1])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
