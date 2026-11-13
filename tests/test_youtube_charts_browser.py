"""Browser source validation and fail-closed fallback orchestration, without network."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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

    def test_missing_browser_preserves_failure_without_installing(self):
        manager = MagicMock()
        manager.__enter__.return_value.webkit.launch.side_effect = RuntimeError("browser executable missing")
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            self.assertEqual(crawl.extract_chart_entries_from_browser(crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])

    def test_live_check_uses_fresh_browser_results_and_fails_without_them(self):
        import check_youtube_charts_browser as check

        normal = crawl.extract_chart_entries_from_browser_json(chart_payload())
        for row in normal:
            row["collection_method"] = "api"
        browser = [{**row, "collection_method": "browser"} for row in normal]
        with (patch.object(crawl, "extract_chart_entries_from_youtube_charts", side_effect=[normal, browser]),
              patch("builtins.print")):
            check.main()
        with patch.object(crawl, "extract_chart_entries_from_youtube_charts", side_effect=[normal, []]):
            with self.assertRaises(ValueError):
                check.main()

    def test_api_success_does_not_start_browser(self):
        rows = crawl.extract_chart_entries_from_browser_json(chart_payload())
        with (patch.object(crawl.http_session, "get", return_value=Mock(text="")),
              patch.object(crawl, "extract_chart_entries_from_browse_api", return_value=rows),
              patch("playwright.sync_api.sync_playwright") as browser):
            result = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        self.assertEqual(len(result), 100)
        browser.assert_not_called()

    def test_invalid_browser_source_cannot_start_browser(self):
        with patch("playwright.sync_api.sync_playwright") as browser:
            self.assertEqual(crawl.extract_chart_entries_from_browser("https://example.com/chart"), [])
        browser.assert_not_called()

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
                     "missing_video", "invalid_video", "missing_artist"):
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
            elif case == "missing_video": tracks[-1]["encryptedVideoId"] = ""
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
            self.assertEqual([{key: value for key, value in row.items() if key != "collection_method"} for row in result], rows)
            self.assertEqual(fallback.call_count, 0 if case == "success" else 1)

    def test_browser_checks_displayed_period_and_closes_on_success_or_failure(self):
        for case in ("success", "wrong_label", "http_error", "historical_request", "cross_month", "cross_year", "missing_config", "config_429", "config_redirect", "config_timeout"):
            manager = MagicMock()
            playwright = manager.__enter__.return_value
            browser = playwright.webkit.launch.return_value
            page = browser.new_page.return_value
            page.context.request.get.side_effect = RuntimeError("Parse Error: Header overflow")
            bootstrap = Mock(status_code=200)
            bootstrap.text = 'ytcfg.set({"INNERTUBE_API_KEY":"fixture-public-key"});'
            if case == "missing_config": bootstrap.text = 'ytcfg.set({"INNERTUBE_API_KEY":""});'
            if case == "config_429": bootstrap.status_code = 429
            if case == "config_redirect": bootstrap.status_code = 302
            response = Mock(url="https://charts.youtube.com/youtubei/v1/browse?key=fixture-public-key")
            page.goto.side_effect = lambda *args, **kwargs: [
                call.args[1](response) for call in page.on.call_args_list if call.args[0] == "response"
            ]
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
            with (self.subTest(case=case), patch("playwright.sync_api.sync_playwright", return_value=manager),
                  patch.object(crawl.http_session, "get", return_value=bootstrap,
                               side_effect=requests.Timeout("timeout") if case == "config_timeout" else None) as get):
                rows = crawl.extract_chart_entries_from_browser(crawl.DEFAULT_YOUTUBE_CHARTS_URL, limit=10, **options)
            get.assert_called_once_with(crawl.YOUTUBE_PUBLIC_CONFIG_URL, timeout=30, allow_redirects=False)
            page.context.request.get.assert_not_called()
            self.assertEqual(len(rows), 10 if case in {"success", "cross_month", "cross_year"} else 0)
            browser.close.assert_called_once()
            playwright.webkit.launch.assert_called_once_with(headless=True)
            if case == "success":
                route = Mock()
                route.request.url = "https://charts.youtube.com/youtubei/v1/browse?alt=json&key=old"
                page.route.call_args.args[1](route)
                self.assertEqual(parse_qs(urlparse(route.continue_.call_args.kwargs["url"]).query),
                                 {"alt": ["json"], "key": ["fixture-public-key"]})
                self.assertEqual(set(route.continue_.call_args.kwargs), {"url"})

    def test_public_configuration_is_never_applied_to_other_hosts_or_endpoints(self):
        for url in ("http://charts.youtube.com/youtubei/v1/browse", "https://example.com/youtubei/v1/browse",
                    "https://charts.youtube.com/youtubei/v1/player"):
            with self.assertRaises(ValueError):
                crawl.chart_request_with_key(url, "fixture-key")

    def test_browser_wait_distinguishes_visible_challenge_hidden_frame_and_missing_response(self):
        for case in ("visible_challenge", "hidden_frame", "missing_response"):
            manager = MagicMock()
            browser = manager.__enter__.return_value.webkit.launch.return_value
            page = browser.new_page.return_value
            challenge = Mock()
            challenge.count.return_value = 1 if case == "visible_challenge" else 0
            calendar = Mock()
            calendar.inner_text.return_value = "Oct 2–8, 2026"
            page.locator.side_effect = lambda selector: challenge if selector.startswith("iframe") else calendar
            bootstrap = Mock(status_code=200, text='ytcfg.set({"INNERTUBE_API_KEY":"fixture-key"});')
            response = Mock(url="https://charts.youtube.com/youtubei/v1/browse", status=200)
            response.json.return_value = chart_payload()

            def emit_response(response):
                for call in page.on.call_args_list:
                    if call.args[0] == "response":
                        call.args[1](response)

            page.goto.side_effect = lambda *args, **kwargs: emit_response(
                Mock(url="https://example.com/youtubei/v1/browse", status=200))
            page.wait_for_timeout.side_effect = lambda milliseconds: emit_response(response)
            with (self.subTest(case=case), patch("playwright.sync_api.sync_playwright", return_value=manager),
                  patch.object(crawl.http_session, "get", return_value=bootstrap),
                  patch.object(crawl.time, "monotonic", side_effect=[0, 46] if case == "missing_response" else [0, 0]),
                  self.assertLogs(crawl.LOG, "INFO") as logs):
                rows = crawl.extract_chart_entries_from_browser(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
            self.assertEqual(len(rows), 100 if case == "hidden_frame" else 0)
            browser.close.assert_called_once()
            if case == "hidden_frame":
                page.wait_for_timeout.assert_called_once_with(100)
            else:
                page.wait_for_timeout.assert_not_called()
                self.assertIn("interactive reCAPTCHA" if case == "visible_challenge" else "within 45 seconds", "\n".join(logs.output))

    def test_main_uses_one_verification_locale_without_changing_bilingual_source_collection(self):
        with (patch.object(sys, "argv", ["crawl", "--yt-auth", "unused.json", "--yt-playlist-id", "fixture", "--dry-run"]),
              patch.object(crawl, "load_dotenv"),
              patch.object(crawl, "make_ytmusic") as client,
              patch.object(crawl, "extract_chart_entries_from_youtube_charts", return_value=[]),
              patch.object(crawl, "discover_youtube_charts_source_info", return_value={}),
              self.assertLogs(crawl.LOG, "WARNING")):
            self.assertEqual(crawl.main(), 1)
        self.assertEqual(client.call_args.kwargs["language"], "en")


if __name__ == "__main__":
    unittest.main()
