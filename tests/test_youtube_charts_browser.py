"""Browser source validation and fail-closed fallback orchestration, without network."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ytmusic_to_ytmusic_crawl as crawl


def chart_payload():
    return {
        "chartParams": {"countryCode": "kr", "chartType": "CHART_TYPE_TRACKS",
                        "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY"},
        "chart": {"listType": "TOP_VIEWS_CHART", "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY",
                  "endDate": "2026-10-08", "trackViews": [
                      {"encryptedVideoId": f"video{i:06d}", "name": f"Song {i}",
                       "artists": [{"name": "Artist"}], "atvExternalVideoId": f"audio{i:06d}",
                       "chartEntryMetadata": {"currentPosition": i}}
                      for i in range(1, 101)
                  ]},
    }


class YouTubeChartsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("offline test")))

    def test_browser_payload_preserves_official_ids_audio_links_and_source_period(self):
        payload = chart_payload()
        payload["availableChartsInfo"] = [{"chartType": "CHART_TYPE_TRACKS",
            "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY", "latestEndDate": "2026-10-15"}]
        rows = crawl.extract_chart_entries_from_browser_json(payload)
        self.assertEqual(len(rows), 100)
        self.assertEqual(crawl.fetched_period_from_entries(rows), ("2026-10-02", "2026-10-08"))
        self.assertEqual((rows[-1]["rank"], rows[-1]["video_id"], rows[-1]["atv_external_video_id"]),
                         (100, "video000100", "audio000100"))

    def test_browser_rejects_wrong_country_type_period_count_and_source_ranks(self):
        for case in ("country", "type", "daily", "missing_date", "invalid_date", "future_date",
                     "99_rows", "101_rows", "duplicate_rank", "missing_rank", "string_rank", "bool_rank",
                     "duplicate_video", "invalid_video", "missing_artist"):
            payload = chart_payload()
            chart, tracks = payload["chart"], payload["chart"]["trackViews"]
            if case == "country": payload["chartParams"]["countryCode"] = "us"
            elif case == "type": payload["chartParams"]["chartType"] = "CHART_TYPE_VIDEOS"
            elif case == "daily": chart["chartPeriodType"] = "CHART_PERIOD_TYPE_DAILY"
            elif case == "missing_date": del chart["endDate"]
            elif case == "invalid_date": chart["endDate"] = "2026-02-30"
            elif case == "future_date": chart["endDate"] = "2999-01-01"
            elif case == "99_rows": tracks.pop()
            elif case == "101_rows": tracks.append(deepcopy(tracks[-1]))
            elif case == "duplicate_rank": tracks[-1]["chartEntryMetadata"]["currentPosition"] = 99
            elif case == "missing_rank": del tracks[-1]["chartEntryMetadata"]["currentPosition"]
            elif case == "string_rank": tracks[-1]["chartEntryMetadata"]["currentPosition"] = "100"
            elif case == "bool_rank": tracks[0]["chartEntryMetadata"]["currentPosition"] = True
            elif case == "duplicate_video": tracks[-1]["encryptedVideoId"] = tracks[0]["encryptedVideoId"]
            elif case == "invalid_video": tracks[-1]["encryptedVideoId"] = "invalid"
            elif case == "missing_artist": tracks[-1]["artists"] = []
            with self.subTest(case=case), self.assertRaises(ValueError):
                crawl.extract_chart_entries_from_browser_json(payload)

    def test_mixed_period_short_week_and_explicit_period_mismatch_are_rejected(self):
        for case in ("mixed", "short_week", "requested_start", "requested_end"):
            rows = crawl.extract_chart_entries_from_browser_json(chart_payload())
            options = {}
            if case == "mixed": rows[-1]["chart_period_end"] = "2026-10-01"
            elif case == "short_week":
                for row in rows: row["chart_period_start"] = "2026-10-03"
            elif case == "requested_start": options["chart_period_start"] = "2026-10-01"
            else: options["chart_period_end"] = "2026-10-01"
            with self.subTest(case=case), self.assertRaises(ValueError):
                crawl.validate_weekly_chart(rows, **options)

    def test_api_success_skips_browser_and_invalid_api_uses_browser(self):
        rows = crawl.extract_chart_entries_from_browser_json(chart_payload())
        for case in ("success", "empty", "partial", "undated", "429"):
            primary = deepcopy(rows)
            if case == "empty": primary = []
            elif case == "partial": primary.pop()
            elif case == "undated":
                for row in primary: row["chart_period_end"] = ""
            with (self.subTest(case=case),
                  patch.object(crawl.http_session, "get", return_value=Mock(text="")),
                  patch.object(crawl, "extract_chart_entries_from_browse_api", return_value=primary,
                               side_effect=requests.HTTPError("429") if case == "429" else None),
                  patch.object(crawl, "extract_chart_entries_from_browser", return_value=rows) as fallback):
                result = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
            self.assertEqual(result, rows)
            self.assertEqual(fallback.call_count, 0 if case == "success" else 1)

    def test_browser_checks_displayed_period_and_closes_on_success_or_failure(self):
        for case in ("success", "wrong_label", "http_error", "historical_request", "cross_month", "cross_year"):
            manager = MagicMock()
            playwright = manager.__enter__.return_value
            browser = playwright.webkit.launch.return_value
            page = browser.new_page.return_value
            response = page.expect_response.return_value.__enter__.return_value.value
            payload = chart_payload()
            label = "Oct 2\u2009–\u20098, 2026"
            if case == "cross_month":
                payload["chart"]["endDate"], label = "2026-10-01", "Sep 25 – Oct 1, 2026"
            elif case == "cross_year":
                payload["chart"]["endDate"], label = "2026-01-01", "Dec 26, 2025 – Jan 1, 2026"
            response.status = 429 if case == "http_error" else 200
            response.json.return_value = payload
            page.locator.return_value.inner_text.return_value = "Oct 9–15, 2026" if case == "wrong_label" else label
            options = {"chart_period_end": "2026-10-01"} if case == "historical_request" else {}
            with self.subTest(case=case), patch("playwright.sync_api.sync_playwright", return_value=manager):
                rows = crawl.extract_chart_entries_from_browser(crawl.DEFAULT_YOUTUBE_CHARTS_URL, limit=10, **options)
            self.assertEqual(len(rows), 10 if case in {"success", "cross_month", "cross_year"} else 0)
            browser.close.assert_called_once()
            playwright.webkit.launch.assert_called_once_with(headless=True)


if __name__ == "__main__":
    unittest.main()
