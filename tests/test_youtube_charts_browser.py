"""Browser source validation and fail-closed fallback orchestration, without network."""
from copy import deepcopy
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import sys
import tempfile
import unittest
import uuid
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
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": ""}))
        self.enterContext(patch.object(crawl, "_CHART_BROWSER_INSTALL_ATTEMPTED", False))

    def test_browser_install_requires_opt_in_and_skips_existing_browser(self):
        executable = self.directory / "webkit"
        with patch.object(crawl.subprocess, "run") as install:
            crawl.ensure_chart_browser(executable)
            executable.touch()
            with patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}):
                crawl.ensure_chart_browser(executable)
            install.assert_not_called()

    def test_browser_install_is_bounded_and_reused(self):
        executable = self.directory / "webkit"
        with (patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
              patch.object(crawl.subprocess, "run", side_effect=lambda *a, **k: executable.touch()) as install):
            crawl.ensure_chart_browser(executable)
            crawl.ensure_chart_browser(executable)
        install.assert_called_once_with(
            ["sudo", "-n", "systemd-run", "--wait", "--pipe", "--collect",
             "--property=Type=exec", "--property=RuntimeMaxSec=170s",
             "--property=TimeoutStopSec=10s", "--property=KillMode=control-group",
             "env", f"PLAYWRIGHT_BROWSERS_PATH={executable.parent.parent}", sys.executable,
             "-m", "playwright", "install", "--with-deps", "webkit"], check=True)

    @unittest.skipUnless(sys.platform == "linux" and Path("/run/systemd/system").is_dir(),
                         "requires Linux with systemd and passwordless sudo")
    def test_install_service_kills_detached_child_after_timeout_or_parent_exit(self):
        run = crawl.subprocess.run
        child_code = (
            "import os, pathlib, signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        )
        parent_code = (
            "import pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True)\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
            "if sys.argv[3] == 'parent_exit': sys.exit(1)\n"
            "child.wait()\n"
        )
        for case in ("timeout", "parent_exit"):
            unit = f"hype-chart-browser-test-{uuid.uuid4().hex}.service"
            pid_file = self.directory / f"{case}.pid"

            def run_installer(command, **kwargs):
                # Exercise the production supervisor with a short, network-free installer.
                command = [{"--property=RuntimeMaxSec=170s": "--property=RuntimeMaxSec=2s",
                            "--property=TimeoutStopSec=10s": "--property=TimeoutStopSec=1s"}.get(arg, arg)
                           for arg in command]
                command.insert(command.index("systemd-run") + 1, f"--unit={unit}")
                command[command.index("env"):] = [sys.executable, "-c", parent_code, child_code, str(pid_file), case]
                return run(command, **kwargs, timeout=15, capture_output=True, text=True)

            try:
                with (self.subTest(case=case), patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
                      patch.object(crawl, "_CHART_BROWSER_INSTALL_ATTEMPTED", False),
                      patch.object(crawl.subprocess, "run", side_effect=run_installer)):
                    with self.assertRaises(crawl.subprocess.CalledProcessError):
                        crawl.ensure_chart_browser(self.directory / "missing-webkit")
                    self.assertTrue(pid_file.is_file(), "installer did not start its child")
                    stat = Path(f"/proc/{int(pid_file.read_text())}/stat")
                    try:
                        state = stat.read_text().rsplit(") ", 1)[1].split()[0]
                    except FileNotFoundError:
                        continue
                    self.assertEqual(state, "Z", "installer child survived service shutdown")
            finally:
                run(["sudo", "-n", "systemctl", "stop", unit], capture_output=True, timeout=15)

    def test_failed_browser_install_is_not_retried(self):
        executable = self.directory / "webkit"
        with (patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
              patch.object(crawl.subprocess, "run", side_effect=OSError("installer unavailable")) as install):
            with self.assertRaises(OSError):
                crawl.ensure_chart_browser(executable)
            with self.assertRaisesRegex(RuntimeError, "already attempted"):
                crawl.ensure_chart_browser(executable)
        install.assert_called_once()

    def test_install_failure_or_timeout_returns_no_chart_without_retry(self):
        for exit_code in (1, 124, 137):
            manager = MagicMock()
            browser = manager.__enter__.return_value.webkit
            browser.executable_path = str(self.directory / "missing-webkit")
            with (self.subTest(exit_code=exit_code),
                  patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
                  patch.object(crawl, "_CHART_BROWSER_INSTALL_ATTEMPTED", False),
                  patch.object(crawl.http_session, "get", side_effect=requests.HTTPError("429")),
                  patch("playwright.sync_api.sync_playwright", return_value=manager),
                  patch.object(crawl.subprocess, "run",
                               side_effect=crawl.subprocess.CalledProcessError(exit_code, "install")) as install):
                for _ in range(2):
                    self.assertEqual(crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL), [])
            install.assert_called_once()
            browser.launch.assert_not_called()

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

    def test_api_success_does_not_prepare_browser_even_when_install_is_enabled(self):
        rows = crawl.extract_chart_entries_from_browser_json(chart_payload())
        with (patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
              patch.object(crawl.http_session, "get", return_value=Mock(text="")),
              patch.object(crawl, "extract_chart_entries_from_browse_api", return_value=rows),
              patch.object(crawl, "ensure_chart_browser") as prepare,
              patch("playwright.sync_api.sync_playwright") as browser):
            result = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
        self.assertEqual(len(result), 100)
        prepare.assert_not_called()
        browser.assert_not_called()

    def test_invalid_browser_source_cannot_trigger_installation(self):
        with (patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1"}),
              patch.object(crawl.subprocess, "run") as install,
              patch("playwright.sync_api.sync_playwright") as browser):
            self.assertEqual(crawl.extract_chart_entries_from_browser("https://example.com/chart"), [])
        install.assert_not_called()
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
            self.assertEqual([{key: value for key, value in row.items() if key != "collection_method"} for row in result], rows)
            self.assertEqual(fallback.call_count, 0 if case == "success" else 1)

    def test_browser_checks_displayed_period_and_closes_on_success_or_failure(self):
        for case in ("success", "wrong_label", "http_error", "historical_request", "cross_month", "cross_year", "missing_config", "config_429"):
            manager = MagicMock()
            playwright = manager.__enter__.return_value
            browser = playwright.webkit.launch.return_value
            page = browser.new_page.return_value
            bootstrap = page.context.request.get.return_value
            bootstrap.status = 200
            bootstrap.text.return_value = 'ytcfg.set({"INNERTUBE_API_KEY":"fixture-public-key"});'
            if case == "missing_config": bootstrap.text.return_value = 'ytcfg.set({"INNERTUBE_API_KEY":""});'
            if case == "config_429": bootstrap.status = 429
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
            playwright.webkit.executable_path = str(self.directory / "webkit")
            with (self.subTest(case=case), patch("playwright.sync_api.sync_playwright", return_value=manager),
                  patch.dict(os.environ, {"HYPE_INSTALL_CHART_BROWSER": "1" if case == "success" else ""}),
                  patch.object(crawl.subprocess, "run") as install):
                rows = crawl.extract_chart_entries_from_browser(crawl.DEFAULT_YOUTUBE_CHARTS_URL, limit=10, **options)
            self.assertEqual(install.call_count, 1 if case == "success" else 0)
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
