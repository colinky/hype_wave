"""Read-only Charts retries and locale failures must preserve valid source data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import MaxRetryError
from urllib3.response import HTTPResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hype_db
import ytmusic_to_ytmusic_crawl as crawl
from ytmusic_playlist_sync import get_resilient_session


HTML = 'ytcfg.set(' + json.dumps({"INNERTUBE_API_KEY": "fixture-public-key",
    "INNERTUBE_CONTEXT": {"client": {"clientName": "WEB_MUSIC_ANALYTICS", "clientVersion": "2.0", "hl": "ko", "gl": "KR"}}}) + ');'
DATA = {
    "chartParams": {"countryCode": "kr", "chartType": "CHART_TYPE_TRACKS", "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY",
                    "endDate": "20261008"},
    "listType": "TOP_VIEWS_CHART", "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY", "endDate": "2026-10-08",
    "trackViews": [{"encryptedVideoId": f"video{i:06d}", "name": "Song" if i == 1 else f"Song {i}",
                    "artists": [{"name": "Artist"}], "chartEntryMetadata": {"currentPosition": i}}
                   for i in range(1, 101)],
}


def response(data=DATA):
    result = Mock()
    result.json.return_value = data
    return result


class YouTubeChartsFetchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("offline test")))
        self.enterContext(patch.object(crawl, "SNAPSHOT_DIR", Path(self.enterContext(tempfile.TemporaryDirectory()))))

    def test_only_chart_session_retries_read_only_post_with_bounded_backoff(self):
        with get_resilient_session() as default:
            self.assertFalse(default.get_adapter("https://").max_retries.is_retry("POST", 429))
        policy = crawl.http_session.get_adapter("https://").max_retries
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(policy.is_retry("POST", code))
        self.assertFalse(policy.is_retry("POST", 403))
        reply = HTTPResponse(status=429, headers={"Retry-After": "5"})
        self.assertEqual(policy.get_retry_after(reply), 5)
        delays = []
        for _ in range(3):
            policy = policy.increment(method="POST", response=reply)
            delays.append(policy.get_backoff_time())
        self.assertEqual(delays, [0, 4, 8])
        with self.assertRaises(MaxRetryError):
            policy.increment(method="POST", response=reply)

    def test_either_successful_locale_survives_other_request_failure(self):
        for failed_locale in (0, 1):
            for error in (requests.HTTPError("429 Too Many Requests"), requests.Timeout("timeout"),
                          ValueError("invalid JSON")):
                replies = [response(), response()]
                replies[failed_locale].raise_for_status.side_effect = error
                with self.subTest(locale=failed_locale, error=error), patch.object(
                        crawl.http_session, "post", side_effect=replies) as post, self.assertLogs(crawl.LOG, "WARNING"):
                    rows = crawl.extract_chart_entries_from_browse_api(HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL)
                self.assertEqual(len(rows), 100)
                self.assertEqual((rows[0]["rank"], rows[0]["video_id"], rows[0]["chart_period_end"]),
                                 (1, "video000001", "2026-10-08"))
                self.assertEqual(post.call_count, 2)

    def test_successful_locales_merge_by_video_id(self):
        ko = json.loads(json.dumps(DATA))
        ko["trackViews"][0]["name"] = "노래"
        ko["trackViews"][0]["artists"][0]["name"] = "가수"
        with patch.object(crawl.http_session, "post", side_effect=[response(), response(ko)]) as post:
            rows = crawl.extract_chart_entries_from_browse_api(
                HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL, chart_period_end="2026-10-08")
        self.assertEqual((rows[0]["title_en"], rows[0]["title_ko"]), ("Song", "노래"))
        self.assertEqual(post.call_args.kwargs["json"]["context"]["client"]["hl"], "ko")
        self.assertIn("chart_params_end_date=20261008", post.call_args.kwargs["json"]["query"])

    def test_missing_context_and_empty_api_responses_are_visible(self):
        with patch.object(crawl.http_session, "post") as post, self.assertLogs(crawl.LOG, "WARNING"):
            self.assertEqual(crawl.extract_chart_entries_from_browse_api("", crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])
        post.assert_not_called()
        with patch.object(crawl.http_session, "post", return_value=response({})), self.assertLogs(crawl.LOG, "WARNING"):
            self.assertEqual(crawl.extract_chart_entries_from_browse_api(HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])

    def test_public_configuration_merges_json_calls_without_truncating_text(self):
        html = 'ytcfg.set({"text":"embedded }); text"});ytcfg.set(broken);ytcfg.set({"INNERTUBE_API_KEY":"rotated"});'
        self.assertEqual(crawl.extract_public_innertube_key(html), "rotated")
        for html in ('', 'ytcfg.set({"INNERTUBE_API_KEY":""});', 'ytcfg.set({"INNERTUBE_API_KEY":null});'):
            with self.assertRaises(ValueError):
                crawl.extract_public_innertube_key(html)

    def test_empty_chart_key_loads_official_config_once_for_both_locales(self):
        chart = HTML.replace('"fixture-public-key"', '""')
        with patch.object(crawl.http_session, "get", return_value=Mock(text=HTML)) as get, \
                patch.object(crawl.http_session, "post", return_value=response()) as post:
            rows = crawl.extract_chart_entries_from_browse_api(chart, crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        self.assertEqual(len(rows), 100)
        get.assert_called_once_with(crawl.YOUTUBE_PUBLIC_CONFIG_URL, timeout=30)
        self.assertEqual([call.kwargs["params"] for call in post.call_args_list], [{"key": "fixture-public-key"}] * 2)

    def test_existing_chart_key_needs_no_bootstrap_and_missing_config_prevents_posts(self):
        with patch.object(crawl.http_session, "get") as get, patch.object(crawl.http_session, "post", return_value=response()):
            self.assertEqual(len(crawl.extract_chart_entries_from_browse_api(HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL)), 100)
        get.assert_not_called()
        with patch.object(crawl.http_session, "get", return_value=Mock(text="")), patch.object(crawl.http_session, "post") as post:
            with self.assertRaises(ValueError):
                crawl.extract_chart_entries_from_browse_api(HTML.replace('"fixture-public-key"', '""'), crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        post.assert_not_called()

    def test_public_configuration_key_is_redacted_from_failed_request_logs(self):
        with patch.object(crawl.http_session, "post", side_effect=requests.HTTPError("429 https://charts.youtube.com/?key=fixture-public-key")), \
                self.assertLogs(crawl.LOG, "WARNING") as logs:
            self.assertEqual(crawl.extract_chart_entries_from_browse_api(HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])
        self.assertNotIn("fixture-public-key", "\n".join(logs.output))
        self.assertIn("429", "\n".join(logs.output))

    def test_api_uses_browser_request_context_without_bootstrap_visitor_fields(self):
        config = crawl.extract_ytcfg_from_html(HTML)
        config["INNERTUBE_CONTEXT"]["client"].update(visitorData="bootstrap-visitor", remoteHost="bootstrap-host")
        config["INNERTUBE_CONTEXT"]["clickTracking"] = {"clickTrackingParams": "bootstrap-only"}
        with patch.object(crawl.http_session, "post", return_value=response()) as post:
            crawl.extract_chart_entries_from_browse_api('ytcfg.set('+json.dumps(config)+');', crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        for call in post.call_args_list:
            context = call.kwargs["json"]["context"]
            self.assertNotIn("visitorData", context["client"])
            self.assertNotIn("remoteHost", context["client"])
            self.assertNotIn("clickTracking", context)
            self.assertEqual(context["client"]["clientName"], "WEB_MUSIC_ANALYTICS")

    def test_api_rejects_foreign_chart_type_country_and_incomplete_source_ranks(self):
        for case in ("country", "videos", "99_rows", "missing_rank", "wrong_week"):
            data = json.loads(json.dumps(DATA))
            if case == "country": data["chartParams"]["countryCode"] = "us"
            elif case == "videos": data["chartParams"]["chartType"] = "CHART_TYPE_VIDEOS"
            elif case == "99_rows": data["trackViews"].pop()
            elif case == "missing_rank": del data["trackViews"][-1]["chartEntryMetadata"]["currentPosition"]
            else: data["chartPeriodType"] = "CHART_PERIOD_TYPE_DAILY"
            with self.subTest(case=case), patch.object(crawl.http_session, "post", return_value=response(data)), self.assertLogs(crawl.LOG, "WARNING"):
                self.assertEqual(crawl.extract_chart_entries_from_browse_api(HTML, crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])

    def test_exhausted_api_preserves_existing_chart_and_blocks_publication(self):
        page = Mock(text=HTML)
        failed = Mock()
        failed.raise_for_status.side_effect = requests.HTTPError("429 Too Many Requests")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}):
            db = Path(directory) / "chart.db"
            hype_db.init_db(db)
            with hype_db.connect(db) as conn:
                conn.execute("INSERT INTO playlist_order(service, job_name, source_variant, reference_period, song_id, rank_order) "
                             "VALUES ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W41', 'old-video', 1)")
            argv = ["crawl", "--db-path", str(db), "--db-only", "--no-resolve"]
            with (patch.object(sys, "argv", argv), patch.object(crawl, "load_dotenv"),
                  patch.object(crawl.http_session, "get", return_value=page),
                  patch.object(crawl.http_session, "post", return_value=failed),
                  patch.object(crawl, "extract_chart_entries_from_browser", return_value=[]) as browser,
                  patch.object(crawl, "update_ytmusic_playlist") as publish,
                  self.assertLogs(crawl.LOG, "WARNING") as logs):
                self.assertEqual(crawl.main(), 1)
            publish.assert_not_called()
            browser.assert_called_once()
            self.assertIn("429 Too Many Requests", "\n".join(logs.output))
            with hype_db.connect(db, read_only=True) as conn:
                rows = conn.execute("SELECT song_id, reference_period FROM playlist_order").fetchall()
                self.assertEqual([(row[0], row[1]) for row in rows], [("old-video", "2026-W41")])
                self.assertEqual(conn.execute("SELECT status, entry_count FROM chart_source_audit").fetchone()[0], "source_error")


if __name__ == "__main__":
    unittest.main()
