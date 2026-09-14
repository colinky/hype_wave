"""Deferred collection still validates/persists; no test opens a database or network."""
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import apple_music_to_ytmusic_crawl as apple
import crawler_common
import hype_db
import hype_db_store
import melon_gen_to_ytmusic_crawl as genz
import melon_to_ytmusic_crawl as melon
import spotify_to_ytmusic_crawl as spotify
import sync_validation
import ytmusic_playlist_sync
from ytmusic_playlist_sync import MatchResult, SourceTrack


class DeferredCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1",
            "APPLE_CHART_LIMIT": "1", "SPOTIFY_TRACK_LIMIT": "1",
            "BYPASS_TRACK_COUNT_VAL": "false", "YTMUSIC_PLAYLIST_ID": "",
        }))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("External access forbidden")))
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.execute.return_value.fetchone.return_value = None
        self.enterContext(patch.object(hype_db, "connect", return_value=self.conn))
        self.enterContext(patch.object(crawler_common, "load_verified_matching_cache", return_value={}))
        self.enterContext(patch.object(hype_db_store, "get_bulk_cached_matches", return_value={}))
        self.enterContext(patch.object(hype_db_store, "manual_override", return_value=None))
        self.enterContext(patch.object(hype_db, "get_expected_track_count", return_value=None))
        self.enterContext(patch.object(crawler_common.time, "sleep"))
        self.raw = self.enterContext(patch.object(hype_db, "persist_crawled_tracks"))
        self.matches = self.enterContext(patch.object(hype_db, "persist_crawl_run"))
        self.export = self.enterContext(patch.object(hype_db, "export_frontend_history"))
        self.verifier = Mock()
        self.verifier.environment = "fixture"
        self.verifier.check_health.return_value = {
            "run_health": "healthy", "auth_state": "authenticated",
        }
        observed_at = datetime.now(timezone.utc)
        self.verifier.verify.side_effect = lambda video_id, **kw: {
            "video_id": video_id, "state": "playable", "reason_code": "player_ok",
            "title": f"Song {video_id.removeprefix('video-')}", "artist": "Artist",
            "exact_id": True, "has_audio": True,
            "environment": "fixture", "run_health": "healthy", "auth_state": "authenticated",
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(minutes=5)).isoformat(),
        }
        self.enterContext(patch.object(sync_validation, "verifier_for", return_value=self.verifier))
        self.enterContext(patch.object(ytmusic_playlist_sync, "get_verified_video_metadata", side_effect=(
            lambda _client, video_id, **kw: {
                "video_id": video_id, "title": f"Song {video_id.removeprefix('video-')}",
                "artist": "Artist", "album": "Album",
            }
        )))
        self.search = self.enterContext(patch.object(crawler_common, "search_youtube_music", side_effect=(
            lambda _client, track, **kw: MatchResult(
                rank=track.rank, title=track.title, artist=track.artist, album=track.album,
                service=track.service, song_id=track.song_id, video_id=f"video-{track.song_id}",
                status="matched", score=1.0,
            )
        )))
        self.updates = {}
        for module in (apple, spotify, melon):
            self.enterContext(patch.object(module, "make_ytmusic", return_value=Mock()))
            self.enterContext(patch.object(module, "load_dotenv"))
            self.updates[module] = self.enterContext(patch.object(module, "update_ytmusic_playlist"))
        self.enterContext(patch.object(genz, "load_dotenv"))
        for module in (melon, genz):
            self.enterContext(patch.object(module, "load_album_cache"))
            self.enterContext(patch.object(module, "save_album_cache"))
        self.enterContext(patch.object(spotify, "load_targeted_spotify_cache", return_value={}))

    @staticmethod
    def track(service, song_id="one", rank=1):
        return SourceTrack(rank, f"Song {song_id}", "Artist", service=service,
                           song_id=song_id, album="Album", locale="en")

    def arguments(self, module, *flags):
        with patch.object(sys, "argv", [module.__file__, "--yt-auth", "fixture-auth",
                                       "--db-path", "never-open.db", *flags]):
            return module.parse_args()

    def run_crawler(self, module, args):
        track = self.track("apple" if module is apple else "spotify" if module is spotify else "melon")
        self.enterContext(patch.object(module, "parse_args", return_value=args))
        if module is apple:
            self.enterContext(patch.object(apple, "fetch_apple_tracks", return_value=(
                "Fixture", "", [track], "apple_web_playlist_api", {},
            )))
        elif module is spotify:
            self.enterContext(patch.object(spotify, "fetch_spotify_tracks_scraped", return_value=(
                "Fixture", "", [track], "spotify_embed_scrape",
            )))
        else:
            args.melon_urls = ["https://fixture.invalid/melon"]
            args.track_limit = 1
            self.enterContext(patch.object(melon, "fetch_melon_tracks", return_value=(
                "Fixture", "", "2026.09.14", [track],
            )))
        return module.main()

    def prepare_genz(self, *flags):
        args = self.arguments(genz, "--track-limit", "1", "--top-k", "2", *flags)
        self.enterContext(patch.object(genz, "parse_args", return_value=args))
        tracks = {gen: [self.track("melon", gen)] for gen in ("1", "2")}
        self.enterContext(patch.object(genz, "fetch_melon_generation_tracks", side_effect=(
            lambda gen, **kw: (f"Generation {gen}", "", "2026-09-14", tracks[gen])
        )))
        return tracks

    def assert_matched_and_persisted(self, count=1):
        self.assertEqual(self.search.call_count, count)
        self.verifier.check_health.assert_called()
        self.matches.assert_called_once()
        persisted = self.matches.call_args.kwargs["matches"]
        self.assertEqual(len(persisted), count)
        self.assertEqual({call.args[0] for call in self.verifier.verify.call_args_list},
                         {row["video_id"] for row in persisted})
        self.assertTrue(all(row["playability_evidence"]["state"] == "playable" for row in persisted))
        self.assertFalse(self.matches.call_args.kwargs["commit"])
        self.assertIs(self.matches.call_args.kwargs["conn"], self.conn)

    def test_defer_matches_validates_and_persists_each_crawler(self):
        for module in (apple, spotify, melon):
            with self.subTest(crawler=module.__name__):
                self.raw.reset_mock()
                self.matches.reset_mock()
                self.search.reset_mock()
                self.verifier.reset_mock()
                args = self.arguments(module, "--defer-publish")
                self.assertEqual(self.run_crawler(module, args), 0)
                self.assert_matched_and_persisted()
                self.raw.assert_called_once()
                self.updates[module].assert_not_called()

    def test_existing_apple_skip_option_is_the_defer_alias(self):
        args = self.arguments(apple, "--skip-playlist-update")
        self.assertTrue(args.skip_playlist_update)
        self.assertEqual(self.run_crawler(apple, args), 0)
        self.assert_matched_and_persisted()
        self.updates[apple].assert_not_called()

    def test_normal_run_still_publishes_validated_selection(self):
        for module in (apple, spotify, melon):
            with self.subTest(crawler=module.__name__):
                args = self.arguments(module, "--yt-playlist-id", "fixture-target")
                self.assertEqual(self.run_crawler(module, args), 0)
                self.updates[module].assert_called_once()
                self.assertEqual(self.updates[module].call_args.args[2], ["video-one"])

    def test_melon_db_only_still_skips_matching_and_validation(self):
        args = self.arguments(melon, "--db-only", "--defer-publish")
        self.assertEqual(self.run_crawler(melon, args), 0)
        self.raw.assert_called_once()
        self.matches.assert_not_called()
        self.search.assert_not_called()
        self.verifier.check_health.assert_not_called()
        self.updates[melon].assert_not_called()

    def test_genz_defer_saves_three_variants_once_after_validation_in_same_transaction(self):
        tracks = self.prepare_genz("--defer-publish")

        def persist(*args, **kwargs):
            self.assertEqual({call.args[0] for call in self.verifier.verify.call_args_list},
                             {"video-1", "video-2"})
            self.assertIs(kwargs["conn"], self.conn)
            self.assertFalse(kwargs["commit"])

        self.raw.side_effect = persist
        self.assertEqual(genz.main(), 0)
        self.assert_matched_and_persisted(2)
        variants = [call.kwargs["source_variant"] for call in self.raw.call_args_list]
        self.assertCountEqual(variants, ["combined", "gen10", "gen20"])
        for gen, variant in (("1", "gen10"), ("2", "gen20")):
            saved = next(call.kwargs for call in self.raw.call_args_list if call.kwargs["source_variant"] == variant)
            rows = [vars(row) if isinstance(row, SourceTrack) else row for row in saved["tracks"]]
            self.assertEqual([(r["song_id"], r["rank"]) for r in rows], [(tracks[gen][0].song_id, 1)])
            self.assertEqual(saved["reference_period"], "2026-09-14")
        self.updates[melon].assert_not_called()

    def test_genz_unknown_keeps_all_preexisting_raw_snapshots_and_matches(self):
        self.prepare_genz("--defer-publish")
        self.verifier.verify.return_value = None
        self.verifier.verify.side_effect = lambda video_id, **kw: {
            "video_id": video_id, "state": "unknown", "reason_code": "timeout",
        }
        with self.assertRaises(sync_validation.PlaybackBlocked):
            genz.main()
        self.assertEqual(self.search.call_count, 2)
        self.raw.assert_not_called()
        self.matches.assert_not_called()
        self.export.assert_not_called()
        self.updates[melon].assert_not_called()

    def test_genz_db_only_saves_raw_variants_once_without_matching(self):
        self.prepare_genz("--db-only", "--defer-publish")
        self.assertEqual(genz.main(), 0)
        self.assertCountEqual(
            [call.kwargs["source_variant"] for call in self.raw.call_args_list],
            ["gen10", "gen20", "combined"],
        )
        self.matches.assert_not_called()
        self.search.assert_not_called()
        self.verifier.check_health.assert_not_called()
        self.updates[melon].assert_not_called()


if __name__ == "__main__":
    unittest.main()
