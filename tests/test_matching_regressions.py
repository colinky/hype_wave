from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import ytmusic_playlist_sync as playlist_sync
from ytmusic_playlist_sync import (
    SourceTrack,
    resolve_bilingual_song,
    search_youtube_music,
    search_ytmusic_songs,
)


def song(video_id: str, title: str, artist: str, album: str = "") -> dict:
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}] if artist else [],
        "album": {"name": album} if album else None,
    }


class SearchCandidateRegressionTests(unittest.TestCase):
    def test_both_search_stages_outage_retries_then_raises(self) -> None:
        ytmusic = Mock()
        ytmusic.search.side_effect = RuntimeError("provider unavailable")

        with patch("ytmusic_playlist_sync.time.sleep") as sleep:
            with self.assertRaisesRegex(
                RuntimeError, "YouTube Music search unavailable"
            ) as raised:
                search_ytmusic_songs(
                    ytmusic,
                    "Fixture Song Artist",
                    5,
                    track_title="Fixture Song",
                    track_artist="Artist",
                )

        self.assertEqual(ytmusic.search.call_count, 6)
        self.assertLessEqual(sleep.call_count, 2)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_empty_results_from_both_search_stages_are_a_normal_no_match(self) -> None:
        ytmusic = Mock()
        ytmusic.search.side_effect = [[], []]

        self.assertEqual(
            search_ytmusic_songs(
                ytmusic,
                "Fixture Song Artist",
                5,
                track_title="Fixture Song",
                track_artist="Artist",
            ),
            [],
        )
        self.assertEqual(ytmusic.search.call_count, 2)

    def test_all_query_failures_propagate_from_search_youtube_music(self) -> None:
        track = SourceTrack(
            rank=1,
            title="Fixture Song",
            artist="Artist",
            service="apple",
            song_id="fixture-source",
        )
        with (
            patch(
                "ytmusic_playlist_sync.search_queries_for_track",
                return_value=["query one", "query two"],
            ),
            patch(
                "ytmusic_playlist_sync.search_ytmusic_songs",
                side_effect=RuntimeError("provider unavailable"),
            ) as search,
            patch("ytmusic_playlist_sync.time.sleep"),
            self.assertRaisesRegex(
                RuntimeError, "YouTube Music search incomplete"
            ) as raised,
        ):
            search_youtube_music(
                Mock(),
                track,
                None,
                min_score=0.60,
                min_title_score=0.65,
                min_artist_score=0.55,
                limit=25,
            )

        self.assertEqual(search.call_count, 2)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_stage_one_overall_score_cannot_hide_failed_artist_gate(self) -> None:
        weak_artist = song(
            "diagnostic-only",
            "The Adult I Dreamed Of",
            "Unrelated Artist",
            "The Adult I Dreamed Of",
        )
        fallback = song(
            "GxChUrrY4bc",
            "The Adult I Dreamed Of",
            "meomuru",
            "The Adult I Dreamed Of",
        )
        ytmusic = Mock()
        ytmusic.search.side_effect = [[weak_artist], [fallback]]

        with patch(
            "ytmusic_playlist_sync.score_result",
            return_value=(0.80, 1.0, 0.20, 1.0),
        ):
            results = search_ytmusic_songs(
                ytmusic,
                "The Adult I Dreamed Of meomuru",
                5,
                track_title="The Adult I Dreamed Of",
                track_artist="meomuru",
                track_album="The Adult I Dreamed Of",
                min_score=0.60,
                min_title_score=0.65,
                min_artist_score=0.55,
            )

        self.assertEqual(ytmusic.search.call_count, 2)
        self.assertIn("GxChUrrY4bc", [row["videoId"] for row in results])

    def test_later_search_stage_candidate_is_not_cut_off_before_evaluation(self) -> None:
        target = song(
            "GxChUrrY4bc",
            "The Adult I Dreamed Of",
            "meomuru",
            "The Adult I Dreamed Of",
        )
        first_stage = [
            song("wrong-1", "Other One", "Other Artist"),
            song("wrong-2", "Other Two", "Other Artist"),
        ]
        ytmusic = Mock()
        ytmusic.search.side_effect = [first_stage, [target]]

        results = search_ytmusic_songs(
            ytmusic,
            "The Adult I Dreamed Of meomuru",
            2,
            track_title="The Adult I Dreamed Of",
            track_artist="meomuru",
            track_album="The Adult I Dreamed Of",
            track_title_ko="꿈꾸던 어른이 되었나요?",
            track_artist_ko="머무르",
        )

        self.assertLessEqual(len(results), 4)
        self.assertIn("GxChUrrY4bc", [row["videoId"] for row in results])

    def test_duplicate_video_id_keeps_the_richer_later_candidate(self) -> None:
        sparse = song("GxChUrrY4bc", "The Adult I Dreamed Of", "")
        rich = song(
            "GxChUrrY4bc",
            "The Adult I Dreamed Of",
            "meomuru",
            "The Adult I Dreamed Of",
        )
        ytmusic = Mock()
        ytmusic.search.side_effect = [[sparse], [rich]]

        # This tests candidate replacement, with no extra metadata available.
        with patch("ytmusic_playlist_sync.resolve_bilingual_song", return_value={}):
            results = search_ytmusic_songs(
                ytmusic,
                "The Adult I Dreamed Of meomuru",
                5,
                track_title="The Adult I Dreamed Of",
                track_artist="meomuru",
                track_album="The Adult I Dreamed Of",
            )

        candidate = next(row for row in results if row["videoId"] == "GxChUrrY4bc")
        self.assertEqual(candidate["artists"], [{"name": "meomuru"}])
        self.assertEqual(candidate["album"], {"name": "The Adult I Dreamed Of"})

    def test_best_candidate_that_passes_all_gates_wins(self) -> None:
        track = SourceTrack(
            rank=1,
            title="The Adult I Dreamed Of",
            artist="meomuru",
            album="The Adult I Dreamed Of",
            service="apple",
            song_id="apple-meomuru",
        )
        high_score_wrong_artist = song(
            "diagnostic-only", track.title, "Unrelated Artist", track.album
        )
        lower_score_valid = song("GxChUrrY4bc", track.title, track.artist, track.album)

        def scores(_track, result, *_args, **_kwargs):
            if result["videoId"] == "diagnostic-only":
                return 0.95, 1.0, 0.20, 1.0
            return 0.90, 0.95, 0.95, 0.90

        with (
            patch(
                "ytmusic_playlist_sync.search_queries_for_track",
                return_value=["single query"],
            ),
            patch(
                "ytmusic_playlist_sync.search_ytmusic_songs",
                return_value=[high_score_wrong_artist, lower_score_valid],
            ),
            patch("ytmusic_playlist_sync.score_result", side_effect=scores),
            patch("ytmusic_playlist_sync.time.sleep"),
        ):
            result = search_youtube_music(
                Mock(),
                track,
                None,
                min_score=0.60,
                min_title_score=0.65,
                min_artist_score=0.55,
                limit=25,
            )

        self.assertEqual((result.status, result.video_id), ("matched", "GxChUrrY4bc"))


class BilingualWatchPlaylistRegressionTests(unittest.TestCase):
    @staticmethod
    def watch_track(video_id: str, title: str, artist: str, album: str) -> dict:
        return {
            "videoId": video_id,
            "title": title,
            "artists": [{"name": artist, "id": f"artist-{video_id}"}],
            "album": {"name": album},
        }

    @staticmethod
    def next_payload() -> dict:
        return {
            "contents": {
                "singleColumnMusicWatchNextResultsRenderer": {
                    "tabbedRenderer": {
                        "watchNextTabbedResultsRenderer": {
                            "tabs": [{
                                "tabRenderer": {
                                    "content": {
                                        "musicQueueRenderer": {
                                            "content": {
                                                "playlistPanelRenderer": {
                                                    "contents": [{"fixture": True}]
                                                }
                                            }
                                        }
                                    }
                                }
                            }]
                        }
                    }
                }
            }
        }

    def test_optional_endpoint_fallback_still_selects_only_the_exact_video(self) -> None:
        target = "pHciG9_2xXM"
        ko = Mock()
        en = Mock()
        for client in (ko, en):
            client.get_watch_playlist.side_effect = KeyError("endpoint")
            client._send_request.return_value = self.next_payload()
        parsed = [
            [
                self.watch_track("wrong-ko-id", "다른 노래", "다른 가수", "다른 앨범"),
                self.watch_track(target, "GRLS", "튜이드", "TUNE & PLAY"),
            ],
            [
                self.watch_track("wrong-en-id", "Meet my GRLS", "FKA twigs", ""),
                self.watch_track(target, "GRLS", "TUIDE", "TUNE & PLAY"),
            ],
        ]
        with (
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.get_song", return_value=None),
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.set_song") as cache_set,
            patch("ytmusic_playlist_sync.get_ytmusic_en", return_value=en),
            patch(
                "ytmusicapi.parsers.watch.parse_watch_playlist",
                side_effect=parsed,
            ),
        ):
            details = resolve_bilingual_song(ko, target)

        self.assertEqual(details["title_ko"], "GRLS")
        self.assertEqual(details["artist_ko"], "튜이드")
        self.assertEqual(details["title_en"], "GRLS")
        self.assertEqual(details["artist_en"], "TUIDE")
        cache_set.assert_called_once_with(target, details)

    def test_optional_endpoint_fallback_never_uses_a_foreign_first_track(self) -> None:
        target = "pHciG9_2xXM"
        ko = Mock()
        en = Mock()
        for client in (ko, en):
            client.get_watch_playlist.side_effect = KeyError("endpoint")
            client._send_request.return_value = self.next_payload()
        with (
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.get_song", return_value=None),
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.set_song") as cache_set,
            patch("ytmusic_playlist_sync.get_ytmusic_en", return_value=en),
            patch(
                "ytmusicapi.parsers.watch.parse_watch_playlist",
                side_effect=[
                    [self.watch_track("wrong-ko-id", "다른 노래", "다른 가수", "")],
                    [self.watch_track("wrong-en-id", "Meet my GRLS", "FKA twigs", "")],
                ],
            ),
        ):
            details = resolve_bilingual_song(ko, target)

        self.assertTrue(all(not value for value in details.values()))
        cache_set.assert_not_called()

    def test_watch_fallback_does_not_swallow_unrelated_key_errors(self) -> None:
        client = Mock()
        client.get_watch_playlist.side_effect = KeyError("unrelated-field")

        with self.assertRaisesRegex(KeyError, "unrelated-field"):
            playlist_sync._watch_playlist_for_metadata(client, "pHciG9_2xXM")
        client._send_request.assert_not_called()

    def test_exact_requested_video_is_selected_even_when_it_is_not_first(self) -> None:
        target = "pHciG9_2xXM"
        ko = Mock()
        en = Mock()
        ko.get_watch_playlist.return_value = {
            "tracks": [
                self.watch_track("wrong-ko-id", "다른 노래", "다른 가수", "다른 앨범"),
                self.watch_track(target, "GRLS", "TUIDE", "TUNE & PLAY"),
            ]
        }
        en.get_watch_playlist.return_value = {
            "tracks": [
                self.watch_track("wrong-en-id", "Meet my GRLS", "FKA twigs", ""),
                self.watch_track(target, "GRLS", "TUIDE", "TUNE & PLAY"),
            ]
        }
        with (
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.get_song", return_value=None),
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.set_song") as cache_set,
            patch("ytmusic_playlist_sync.get_ytmusic_en", return_value=en),
        ):
            details = resolve_bilingual_song(ko, target)

        self.assertEqual(
            details,
            {
                "title_ko": "GRLS",
                "title_en": "GRLS",
                "artist_ko": "TUIDE",
                "artist_en": "TUIDE",
                "album_ko": "TUNE & PLAY",
                "album_en": "TUNE & PLAY",
            },
        )
        cache_set.assert_called_once_with(target, details)

    def test_unrelated_first_track_is_not_used_or_cached_when_target_is_absent(self) -> None:
        target = "pHciG9_2xXM"
        ko = Mock()
        en = Mock()
        ko.get_watch_playlist.return_value = {
            "tracks": [
                self.watch_track("wrong-ko-id", "다른 노래", "다른 가수", "다른 앨범")
            ]
        }
        en.get_watch_playlist.return_value = {
            "tracks": [
                self.watch_track("wrong-en-id", "Meet my GRLS", "FKA twigs", "")
            ]
        }
        with (
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.get_song", return_value=None),
            patch("ytmusic_playlist_sync.BILINGUAL_CACHE.set_song") as cache_set,
            patch("ytmusic_playlist_sync.get_ytmusic_en", return_value=en),
        ):
            details = resolve_bilingual_song(ko, target)

        self.assertTrue(all(not value for value in details.values()))
        cache_set.assert_not_called()

    def test_read_only_bilingual_cache_does_not_initialize_or_write_backend(self) -> None:
        with patch.object(
            playlist_sync,
            "BILINGUAL_CACHE",
            playlist_sync.BilingualCache("unused-read-only-cache.db"),
        ) as cache:
            self.assertIsNone(cache.backend)
            with playlist_sync.bilingual_cache_read_only():
                self.assertTrue(cache.read_only)
                self.assertIsNone(cache.get_song("video"))
                self.assertIsNone(cache.get_artist("artist"))
                cache.set_song("video", {"title_en": "must not persist"})
                cache.set_artist("artist", ["must not persist"])
                cache.flush()
                with playlist_sync.bilingual_cache_read_only():
                    self.assertTrue(cache.read_only)
            self.assertFalse(cache.read_only)
            self.assertIsNone(cache.backend)

    def test_same_video_id_with_different_locale_metadata_is_reevaluated(self) -> None:
        track = SourceTrack(
            rank=1,
            title="The Adult I Dreamed Of",
            artist="meomuru",
            service="apple",
            song_id="6781521710",
        )
        track_ko = SourceTrack(
            rank=1,
            title="꿈꾸던 어른이 되었나요?",
            artist="머무르",
            service="apple",
            song_id="6781521710",
            locale="ko",
        )
        english_wrong_artist = song(
            "GxChUrrY4bc", track.title, "Unrelated Artist", "Release"
        )
        korean_valid = song(
            "GxChUrrY4bc", track_ko.title, track_ko.artist, "Release"
        )

        def scores(_track, result, *_args, **_kwargs):
            if result["artists"][0]["name"] == "Unrelated Artist":
                return 0.90, 1.0, 0.1, 1.0
            return 0.88, 1.0, 1.0, 1.0

        with (
            patch(
                "ytmusic_playlist_sync.search_queries_for_track",
                return_value=["english", "korean"],
            ),
            patch(
                "ytmusic_playlist_sync.search_ytmusic_songs",
                side_effect=[[english_wrong_artist], [korean_valid]],
            ),
            patch("ytmusic_playlist_sync.score_result", side_effect=scores),
            patch("ytmusic_playlist_sync.time.sleep"),
        ):
            result = search_youtube_music(
                Mock(),
                track,
                track_ko,
                min_score=0.60,
                min_title_score=0.65,
                min_artist_score=0.55,
                limit=25,
            )

        self.assertEqual((result.status, result.video_id), ("matched", "GxChUrrY4bc"))


if __name__ == "__main__":
    unittest.main()
