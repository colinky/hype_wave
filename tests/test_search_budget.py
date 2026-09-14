"""Search, retries and localized metadata share one authenticated run budget."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ytmusic_playlist_sync as matching
from sync_validation import PlaybackBlocked, require_playable
from ytmusic_playability import PlayabilityVerifier


class SearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.seconds = 0
        self.sleeps = []
        self.start = datetime.now(timezone.utc)
        self.client = SimpleNamespace(search=Mock(return_value=[]), get_album=Mock())
        self.verifier = PlayabilityVerifier(
            self.client, control_video_ids=["control0001", "control0002", "control0003"],
            environment="offline-budget", budget_seconds=600,
            clock=lambda: self.seconds, now=lambda: self.start + timedelta(seconds=self.seconds), sleep=self.sleep)
        self.verifier.health = {
            "run_health": "healthy", "auth_state": "authenticated", "controls": [],
            "environment": self.verifier.environment, "observed_at": self.start.isoformat(),
            "expires_at": (self.start + timedelta(minutes=30)).isoformat()}
        self.client._hype_playability_verifier = self.verifier
        self.track = matching.SourceTrack(1, "Fixture", "Artist", album="Album", service="spotify", song_id="source")
        self.enterContext(patch.object(matching.time, "sleep", side_effect=self.sleep))
        self.enterContext(patch.object(matching, "search_queries_for_track", return_value=["query1", "query2"]))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No network")))
        self.enterContext(matching.bilingual_cache_read_only())

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.seconds += delay

    def search(self):
        return matching.search_youtube_music(
            self.client, self.track, None, min_score=.8, min_title_score=.8,
            min_artist_score=.8, limit=2, playability_verifier=self.verifier)

    def test_expired_or_unhealthy_search_and_direct_helper_do_no_work(self):
        for expired in (True, False):
            self.seconds = 601 if expired else 0
            self.verifier.health.update(run_health="healthy" if expired else "unhealthy")
            for call in (self.search, lambda: matching.search_ytmusic_songs(self.client, "query", 2),
                         lambda: matching.resolve_video_to_song(self.client, video_id="sourcevideo",
                             title="Fixture", playability_verifier=self.verifier)):
                with self.subTest(expired=expired, call=call), self.assertRaises(PlaybackBlocked):
                    call()
            self.client.search.assert_not_called()
            self.assertEqual(self.sleeps, [])

    def test_request_crossing_deadline_stops_next_stage_and_retries(self):
        self.seconds = 599
        def expire(*args, **kwargs):
            self.seconds = 601
            raise TimeoutError("Provider exceeded remaining time")
        self.client.search.side_effect = expire
        with self.assertRaises(PlaybackBlocked):
            self.search()
        self.assertEqual(self.client.search.call_count, 1)
        self.assertEqual(self.sleeps, [.5])

    def test_retry_delay_that_does_not_fit_remaining_budget_is_not_started(self):
        self.seconds = 598
        self.client.search.side_effect = TimeoutError("Temporary provider failure")
        with self.assertRaises(PlaybackBlocked):
            matching.search_ytmusic_songs(self.client, "query", 2)
        self.assertEqual(self.client.search.call_count, 2)
        self.assertEqual(self.sleeps, [])

    def test_album_request_crossing_deadline_stops_remaining_candidates(self):
        self.client.search.side_effect = [[], [
            {"resultType": "album", "browseId": "album1", "title": "Album", "artists": [{"name": "Artist"}]},
            {"resultType": "album", "browseId": "album2", "title": "Album", "artists": [{"name": "Artist"}]},
        ]]
        def expire(*args, **kwargs):
            self.seconds = 601
            return {"tracks": []}
        self.client.get_album.side_effect = expire
        with self.assertRaises(PlaybackBlocked):
            self.search()
        self.assertEqual(self.client.get_album.call_count, 1)
        self.assertEqual(self.client.search.call_count, 2)
        self.assertEqual(self.sleeps, [.5])

    def test_manual_choice_is_recognized_but_cannot_publish_after_expiry(self):
        self.seconds = 601
        key = "|".join(matching.normalize_text(value) for value in (self.track.title, self.track.artist))
        with patch.dict(matching.ALIASES.overrides, {key: "selected001"}):
            self.assertEqual(self.search().status, "manual_override")
        with self.assertRaises(PlaybackBlocked):
            require_playable(self.verifier, ["selected001"])
        self.client.search.assert_not_called()
        self.assertEqual(self.sleeps, [])

    def test_explicit_verifier_bounds_nested_english_client_and_restores_context(self):
        response = Mock()
        request = Mock(return_value=response)
        english = SimpleNamespace(_session=SimpleNamespace(request=request))
        old_verifier = object()
        self.client._hype_playability_verifier = old_verifier
        english.get_artist = lambda _id: (english._session.request("GET", "fixture"), {"name": "Artist"})[1]
        self.client.get_artist = Mock(return_value={"name": "아티스트"})
        self.seconds = 597
        with patch.object(matching, "get_ytmusic_en", return_value=english):
            with matching._search_api(self.client, self.verifier):
                names = matching.resolve_bilingual_artist(self.client, "artist-id")
        self.assertIn("Artist", names)
        self.assertEqual(request.call_args.kwargs["timeout"], 3)
        self.assertIs(english._session.request, request)
        self.assertFalse(hasattr(english, "_hype_playability_verifier"))
        self.assertIs(self.client._hype_playability_verifier, old_verifier)
        request.side_effect = TimeoutError("Fixture failure")
        with self.assertRaises(TimeoutError):
            with matching._search_api(english, self.verifier):
                english._session.request("GET", "fixture")
        self.assertIs(english._session.request, request)
        self.assertFalse(hasattr(english, "_hype_playability_verifier"))

    def test_mid_resolution_expiry_never_starts_the_other_locale(self):
        def expire(*args, **kwargs):
            self.seconds = 601
            return {"tracks": []}
        self.client.get_watch_playlist = Mock(side_effect=expire)
        with patch.object(matching, "get_ytmusic_en") as english, self.assertRaises(PlaybackBlocked):
            matching.resolve_bilingual_song(self.client, "selected001")
        english.assert_not_called()
        self.assertEqual(self.client.get_watch_playlist.call_count, 1)

    def test_nested_same_session_never_relaxes_scalar_or_connect_read_timeouts(self):
        request = Mock(return_value=Mock())
        self.client._session = SimpleNamespace(request=request)
        self.verifier.timeout = 30
        inner = PlayabilityVerifier(self.client, control_video_ids=["control0001", "control0002", "control0003"],
                                   environment="offline-budget", timeout=2, budget_seconds=600,
                                   clock=lambda: self.seconds)
        for requested, expected in ((None, 2), (1, 1), (30, 2), ((.5, 20), (.5, 2)),
                                    ((None, .25), (2, .25)), ((None, None), (2, 2))):
            with self.subTest(timeout=requested):
                with self.verifier._bounded_session():
                    with inner._bounded_session():
                        self.client._session.request("GET", "fixture", timeout=requested)
                self.assertEqual(request.call_args.kwargs["timeout"], expected)
                self.assertIs(self.client._session.request, request)

    def test_real_installed_client_first_english_headers_request_is_bounded(self):
        self.seconds = 598
        self.client.headers = {"Accept-Language": "ko-KR"}
        response = Mock(text='ytcfg.set({"VISITOR_DATA":"offline"});')
        # Keep the installed YTMusic constructor and lazy headers implementation;
        # replace only its HTTP transport, not the code that triggers the GET.
        with patch.object(matching, "_YTMUSIC_EN_INSTANCE", None), patch(
                "requests.sessions.Session.request", return_value=response) as request:
            english = matching.get_ytmusic_en(self.client)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 2)
        self.assertEqual(english.headers["Accept-Language"], "en-US,en;q=0.9")

    def test_lazy_english_initialization_expiry_does_not_fall_back_or_cache_client(self):
        self.seconds = 598
        self.client.headers = {}
        def expire(*args, **kwargs):
            self.seconds = 601
            raise TimeoutError("Visitor request exhausted budget")
        with patch.object(matching, "_YTMUSIC_EN_INSTANCE", None), patch(
                "requests.sessions.Session.request", side_effect=expire) as request:
            with self.assertRaises(PlaybackBlocked):
                matching.get_ytmusic_en(self.client)
            self.assertIsNone(matching._YTMUSIC_EN_INSTANCE)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 2)

    def test_real_installed_factory_bounds_browser_constructor_and_public_headers(self):
        self.seconds = 598
        # Deliberately synthetic browser headers exercise visitor lookup during
        # __init__; they do not authenticate a real account or leave this test.
        browser = {"authorization": "SAPISIDHASH offline", "cookie": "__Secure-3PAPISID=offline;",
                   "origin": "https://music.youtube.com"}
        response = Mock(text='ytcfg.set({"VISITOR_DATA":"offline"});')
        for auth in (None, browser):
            with self.subTest(browser=auth is not None), patch(
                    "requests.sessions.Session.request", return_value=response) as request:
                client = matching.make_ytmusic(auth, language="ko", playability_verifier=self.verifier)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["timeout"], 2)
            self.assertTrue(client.headers["Accept-Language"].startswith("ko-KR"))

    def test_standalone_factory_without_run_verifier_keeps_default_request_timeout(self):
        response = Mock(text='ytcfg.set({"VISITOR_DATA":"offline"});')
        with patch("requests.sessions.Session.request", return_value=response) as request:
            matching.make_ytmusic(None)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
