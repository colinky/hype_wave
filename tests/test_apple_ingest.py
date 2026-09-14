from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import apple_music_to_ytmusic_crawl as apple
import crawler_common
from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    _playlist_video_ids_match,
    localized_source_fields,
    passes_match_gates,
    score_result,
    search_queries_for_track,
    update_ytmusic_playlist,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def song(
    song_id: str,
    title: str,
    artist: str = "artist",
    *,
    isrc: str = "",
    album: str = "",
) -> dict:
    item = {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": title,
            "artistName": artist,
            "albumName": album or f"{title} album",
        },
    }
    if isrc:
        item["attributes"]["isrc"] = isrc
    return item


class AppleIngestTests(unittest.TestCase):
    def test_chart_pagination_preserves_duplicate_slots_and_ranks(self):
        first = {
            "results": {
                "songs": [
                    {
                        "data": [song("kr-a", "가"), song("kr-b", "나")],
                        "next": "/v1/catalog/kr/charts?offset=2",
                    }
                ]
            }
        }
        second = {"data": [song("kr-a", "가")]}
        session = Mock()
        session.get.side_effect = [FakeResponse(first), FakeResponse(second)]

        def localize(items, **_kwargs):
            tracks = [
                SourceTrack(
                    rank=index,
                    title=item["attributes"]["name"],
                    artist=item["attributes"]["artistName"],
                    service="apple",
                    song_id=item["id"],
                    locale="ko",
                )
                for index, item in enumerate(items, 1)
            ]
            return tracks, {}

        with (
            patch.object(apple, "fetch_html", return_value=""),
            patch.object(apple, "find_apple_web_token", return_value="token"),
            patch.object(apple, "find_chart_url", return_value="https://api.music.apple.com/first"),
            patch.object(apple, "_localized_tracks_from_items", side_effect=localize),
            patch.object(apple, "http_session", session),
        ):
            _, _, tracks, _, _ = apple.fetch_apple_chart_tracks(
                "https://music.apple.com/kr/new/top-charts/songs",
                limit=3,
            )

        self.assertEqual([track.song_id for track in tracks], ["kr-a", "kr-b", "kr-a"])
        self.assertEqual([track.rank for track in tracks], [1, 2, 3])

    def test_enrichment_binds_us_equivalent_to_kr_id(self):
        items = [
            song("kr-a", "가", isrc="ISRC-A"),
            song("kr-b", "나", isrc="ISRC-B"),
            song("kr-a", "가", isrc="ISRC-A"),
        ]

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            self.assertTrue(strict)
            if storefront == "kr" and locale == "ko-KR":
                return {
                    "kr-a": song("kr-a", "가 상세", "한국 가수", isrc="ISRC-A"),
                    "kr-b": song("kr-b", "나 상세", "한국 가수", isrc="ISRC-B"),
                }
            self.assertTrue(equivalents)
            self.assertEqual(storefront, "us")
            self.assertEqual(song_ids, ["kr-a", "kr-b"])
            return {
                "kr-a": song("us-a", "A English", "English Artist", isrc="ISRC-A"),
                "kr-b": song("us-b", "B English", "English Artist", isrc="ISRC-B"),
            }

        with patch.object(apple, "_fetch_song_resource_map", side_effect=resources):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                items,
                token="token",
                source="test",
            )

        self.assertEqual([track.song_id for track in tracks], ["kr-a", "kr-b", "kr-a"])
        self.assertEqual([track.title for track in tracks], ["A English", "B English", "A English"])
        self.assertEqual([track.rank for track in tracks], [1, 2, 3])
        self.assertEqual(tracks_ko["kr-a"].title, "가 상세")
        self.assertEqual(tracks_ko["kr-b"].title, "나 상세")

    def test_nostalgia_uses_verified_us_metadata_for_storage_and_search(self):
        items = [
            song(
                "6805367598",
                "노스탈지아",
                "BIG Naughty",
                isrc="KRA382603592",
                album="노스탈지아 - Single",
            )
        ]

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            self.assertEqual(song_ids, ["6805367598"])
            self.assertTrue(strict)
            if storefront == "kr":
                return {"6805367598": items[0]}
            self.assertTrue(equivalents)
            return {
                "6805367598": song(
                    "1842517454",
                    "Nostalgia",
                    "BIG Naughty",
                    isrc="KRA382603592",
                    album="Nostalgia - Single",
                )
            }

        with patch.object(apple, "_fetch_song_resource_map", side_effect=resources):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                items,
                token="token",
                source="test",
            )

        track = tracks[0]
        track_ko = tracks_ko[track.song_id]
        self.assertEqual((track.title, track.locale, track.song_id), ("Nostalgia", "en", "6805367598"))
        self.assertEqual(track_ko.title, "노스탈지아")
        self.assertEqual(
            localized_source_fields(track, track_ko),
            {
                "title_en": "Nostalgia",
                "artist_en": "BIG Naughty",
                "album_en": "Nostalgia - Single",
                "title_ko": "노스탈지아",
                "artist_ko": "BIG Naughty",
                "album_ko": "노스탈지아 - Single",
            },
        )

        queries = search_queries_for_track(track, track_ko)
        self.assertIn("Nostalgia BIG Naughty", queries)
        self.assertIn("노스탈지아 BIG Naughty", queries)
        scores = score_result(
            track,
            {
                "title": "Nostalgia",
                "artists": [{"name": "BIG Naughty"}],
                "album": {"name": "Nostalgia - Single"},
                "resultType": "song",
                "videoId": "EIc13cn7tWo",
            },
            track_ko=track_ko,
        )
        self.assertEqual(scores, (1.0, 1.0, 1.0, 1.0))
        self.assertTrue(
            passes_match_gates(
                track,
                score=scores[0],
                title_score=scores[1],
                artist_score=scores[2],
                min_score=0.60,
                min_title_score=0.65,
                min_artist_score=0.55,
            )
        )

    def test_isrc_mismatch_uses_exact_us_recording(self):
        items = [song("kr-a", "DANCING ALONE", "키키", isrc="SOURCE-ISRC")]
        wrong_equivalent = song(
            "us-remix",
            "DANCING ALONE ('90s Ver.)",
            "KiiiKiii",
            isrc="REMIX-ISRC",
        )
        exact = song("us-original", "DANCING ALONE", "KiiiKiii", isrc="SOURCE-ISRC")

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            if storefront == "kr":
                return {"kr-a": items[0]}
            return {"kr-a": wrong_equivalent}

        with (
            patch.object(apple, "_fetch_song_resource_map", side_effect=resources),
            patch.object(
                apple,
                "_fetch_song_resources_by_isrc",
                return_value={"SOURCE-ISRC": exact},
            ) as exact_fetch,
        ):
            tracks, _ = apple._localized_tracks_from_items(
                items,
                token="token",
                source="test",
            )

        self.assertEqual(tracks[0].title, "DANCING ALONE")
        self.assertEqual(tracks[0].artist, "KiiiKiii")
        self.assertEqual(tracks[0].song_id, "kr-a")
        self.assertEqual(tracks[0].source, "test_us_isrc")
        exact_fetch.assert_called_once_with(
            "token",
            storefront="us",
            isrcs=["SOURCE-ISRC"],
            locale="en-US",
            strict=True,
        )

    def test_unverified_us_metadata_falls_back_to_korean_locale(self):
        items = [song("kr-a", "한국 제목", "한국 가수", isrc="SOURCE-ISRC")]

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            if storefront == "kr":
                return {"kr-a": items[0]}
            return {
                "kr-a": song("us-wrong", "Wrong Live", "Wrong", isrc="OTHER-ISRC")
            }

        with (
            patch.object(apple, "_fetch_song_resource_map", side_effect=resources),
            patch.object(apple, "_fetch_song_resources_by_isrc", return_value={}),
        ):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                items,
                token="token",
                source="test",
            )

        track = tracks[0]
        self.assertEqual((track.title, track.locale, track.source), ("한국 제목", "ko", "test_kr_fallback"))
        fields = localized_source_fields(track, tracks_ko["kr-a"])
        self.assertEqual(fields["title_ko"], "한국 제목")
        self.assertEqual(fields["title_en"], "")

    def test_source_dedup_keeps_first_rank_without_reranking(self):
        raw = [
            SourceTrack(1, "a", "x", song_id="a"),
            SourceTrack(2, "a duplicate", "x", song_id="a"),
            SourceTrack(3, "b", "x", song_id="b"),
        ]
        effective = apple.dedupe_source_tracks(raw, "apple")
        self.assertEqual([track.song_id for track in effective], ["a", "b"])
        self.assertEqual([track.rank for track in effective], [1, 3])
        self.assertEqual([track.rank for track in raw], [1, 2, 3])

    def test_official_jobs_require_one_kr_url_and_no_shuffle(self):
        valid = ["https://music.apple.com/kr/new/top-charts/songs"]
        apple.validate_official_options("KR-Top-Songs", valid, shuffle=False)
        with self.assertRaises(ValueError):
            apple.validate_official_options("KR-Top-Songs", valid * 2, shuffle=False)
        with self.assertRaises(ValueError):
            apple.validate_official_options(
                "KR-Top-100",
                ["https://music.apple.com/us/playlist/test/pl.1"],
                shuffle=False,
            )
        with self.assertRaises(ValueError):
            apple.validate_official_options("KR-Top-Songs", valid, shuffle=True)

    def test_reference_period_is_strict_iso_date(self):
        self.assertEqual(apple.parse_reference_period("2026-08-28"), "2026-08-28")
        for invalid in ("2026-8-28", "2026-02-30", "28-08-2026"):
            with self.subTest(invalid=invalid), self.assertRaises(argparse.ArgumentTypeError):
                apple.parse_reference_period(invalid)

    def test_locale_fields_do_not_treat_ko_fallback_as_english(self):
        korean = SourceTrack(1, "한국 제목", "한국 가수", album="한국 앨범", locale="ko")
        fields = localized_source_fields(korean)
        self.assertEqual(fields["title_ko"], "한국 제목")
        self.assertEqual(fields["title_en"], "")

        english = SourceTrack(1, "English Title", "English Artist", locale="en")
        fields = localized_source_fields(english, korean)
        self.assertEqual(fields["title_en"], "English Title")
        self.assertEqual(fields["title_ko"], "한국 제목")

    def test_matching_dedups_target_only_after_each_best_match(self):
        import sqlite3
        from hype_db_schema import init_schema
        from playlist_playability_fixture import PlaylistVerifier

        tracks = [
            SourceTrack(1, "one", "artist", service="apple", song_id="one"),
            SourceTrack(2, "one", "artist", service="apple", song_id="two"),
        ]
        search_calls = []

        def search(_ytmusic, track, **kwargs):
            search_calls.append(kwargs)
            return MatchResult(
                rank=track.rank,
                title=track.title,
                artist=track.artist,
                album=track.album,
                service="apple",
                song_id=track.song_id,
                video_id="same-video",
                status="matched",
            )

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        init_schema(conn)
        conn.commit()
        metadata = {"video_id": "same-video", "title": "one", "artist": "artist"}
        verifier = PlaylistVerifier(metadata={"same-video": metadata})

        @contextmanager
        def connect(_path, *, read_only=False):
            yield conn

        persist_run = Mock()
        with (
            patch("hype_db.connect", side_effect=connect),
            patch("hype_db.get_bulk_cached_matches", return_value={}),
            patch("hype_db.persist_crawled_tracks"),
            patch("hype_db.persist_crawl_run", persist_run),
            patch("hype_db.export_frontend_history"),
            patch("sync_validation.verifier_for", return_value=verifier),
            patch("ytmusic_playlist_sync.get_verified_video_metadata", return_value=metadata),
            patch.object(crawler_common, "search_youtube_music", side_effect=search),
            patch.object(crawler_common.time, "sleep"),
        ):
            video_ids = crawler_common.process_matching_pipeline(
                all_tracks=tracks,
                ytmusic=object(),
                db_path=Path("unused.db"),
                service="apple",
                job_name="test",
                update_date_str="2026-08-30",
                started_at="run",
                no_db_cache=True,
                dry_run=False,
            )

        self.assertEqual(video_ids, ["same-video"])
        self.assertEqual(len(search_calls), 2)
        self.assertTrue(all("ignore_video_ids" not in call for call in search_calls))
        persisted_matches = persist_run.call_args.kwargs["matches"]
        self.assertEqual(
            [match["video_id"] for match in persisted_matches],
            ["same-video", "same-video"],
        )

    def test_same_target_video_binds_both_source_ids_to_one_canonical_track(self):
        from hype_db import connect, persist_crawled_tracks, persist_crawl_run

        tracks = [
            SourceTrack(1, "one", "artist", service="apple", song_id="one", locale="en"),
            SourceTrack(2, "two", "artist", service="apple", song_id="two", locale="en"),
        ]
        matches = [
            MatchResult(
                rank=track.rank,
                title=track.title,
                artist=track.artist,
                album=track.album,
                service="apple",
                song_id=track.song_id,
                video_id="same-video",
                status="matched",
                score=1.0,
            )
            for track in tracks
        ]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, patch.dict(
            os.environ, {"SUPABASE_DB_URL": ""}
        ):
            db_path = Path(directory) / "aliases.db"
            persist_crawled_tracks(
                db_path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=tracks,
            )
            persist_crawl_run(
                db_path,
                service="apple",
                job_name="Fixture-Daily",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                started_at="20260830T000000Z",
                tracks=tracks,
                matches=matches,
                skip_playlist_order=True,
            )
            with connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT song_id, track_uid FROM platform_song_ids "
                    "WHERE service = 'apple' ORDER BY song_id"
                ).fetchall()
                canonical = conn.execute(
                    "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
                    (rows[0]["track_uid"],),
                ).fetchone()

        self.assertEqual({row["track_uid"] for row in rows}, {rows[0]["track_uid"]})
        self.assertEqual(canonical["canonical_yt_video_id"], "same-video")

    def test_backfill_skip_still_runs_matching_persistence(self):
        args = SimpleNamespace(
            env_file=".env.none",
            apple_playlist_urls=["https://music.apple.com/kr/playlist/test/pl.1"],
            apple_chart_limit=1,
            yt_auth="auth",
            yt_oauth_client_id=None,
            yt_oauth_client_secret=None,
            yt_playlist_id=None,
            job_name="test",
            playlist_name=None,
            db_path="unused.db",
            history_json="history.json",
            no_db_cache=True,
            min_score=0.6,
            min_title_score=0.65,
            min_artist_score=0.55,
            search_limit=1,
            shuffle=False,
            reference_period="2026-08-28",
            skip_playlist_update=True,
            dry_run=False,
        )
        track = SourceTrack(1, "title", "artist", service="apple", song_id="kr-1")
        pipeline = Mock(return_value=["video"])
        update = Mock()
        with (
            patch.object(apple, "parse_args", return_value=args),
            patch.object(apple, "load_dotenv"),
            patch.object(apple, "env_or_arg", side_effect=["auth", ""]),
            patch.object(
                apple,
                "fetch_apple_tracks",
                return_value=("name", "", [track], "apple_web_playlist_api", {}),
            ),
            patch.object(apple, "make_ytmusic", return_value=object()),
            patch.object(apple, "process_matching_pipeline", pipeline),
            patch.object(apple, "update_ytmusic_playlist", update),
            patch.dict(os.environ, {"APPLE_CHART_LIMIT": "1"}),
        ):
            self.assertEqual(apple.main(), 0)

        self.assertEqual(pipeline.call_args.kwargs["reference_period"], "2026-08-28")
        self.assertEqual(pipeline.call_args.kwargs["raw_tracks"], [track])
        update.assert_not_called()

        args.reference_period = None
        pipeline.reset_mock()
        with (
            patch.object(apple, "parse_args", return_value=args),
            patch.object(apple, "load_dotenv"),
            patch.object(apple, "env_or_arg", side_effect=["auth", ""]),
            patch.object(
                apple,
                "fetch_apple_tracks",
                return_value=("name", "", [track], "apple_web_playlist_api", {}),
            ),
            patch.object(apple, "make_ytmusic", return_value=object()),
            patch.object(apple, "process_matching_pipeline", pipeline),
            patch.object(apple, "update_ytmusic_playlist", update),
            patch.dict(os.environ, {"APPLE_CHART_LIMIT": "1"}),
        ):
            self.assertEqual(apple.main(), 0)
        self.assertIsNone(pipeline.call_args.kwargs["reference_period"])


class PlaylistUpdateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.db_path = Path(directory) / "publisher.db"

    def update(self, ytmusic, playlist_id, video_ids, **kwargs):
        from playlist_playability_fixture import PlaylistVerifier
        return update_ytmusic_playlist(
            ytmusic, playlist_id, video_ids, db_path=self.db_path,
            service="apple", job_name="Fixture-Daily", playability_verifier=PlaylistVerifier(), **kwargs,
        )
    def test_playlist_read_failure_raises(self):
        ytmusic = Mock()
        ytmusic.get_playlist.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            self.update(
                ytmusic,
                "playlist",
                ["new"],
                description="new description",
                dry_run=False,
            )
        ytmusic.edit_playlist.assert_not_called()

    def test_ambiguous_remove_is_not_retried(self):
        ytmusic = Mock()
        ytmusic.get_playlist.return_value = {
            "tracks": [{"videoId": "old", "setVideoId": "set-old"}]
        }
        ytmusic.get_song.side_effect = lambda video_id: {
            "videoDetails": {
                "videoId": video_id,
                "title": video_id,
                "author": "Artist",
                "lengthSeconds": "200",
            }
        }
        ytmusic.remove_playlist_items.side_effect = RuntimeError("remove failed")
        with patch("ytmusic_playlist_sync.time.sleep"), self.assertRaises(RuntimeError):
            self.update(
                ytmusic,
                "playlist",
                ["new"],
                dry_run=False,
            )
        self.assertEqual(ytmusic.remove_playlist_items.call_count, 1)

    def test_ambiguous_add_is_not_retried(self):
        ytmusic = Mock()
        ytmusic.get_playlist.return_value = {"tracks": []}
        ytmusic.add_playlist_items.side_effect = RuntimeError("add failed")
        with patch("ytmusic_playlist_sync.time.sleep"), self.assertRaises(RuntimeError):
            self.update(
                ytmusic,
                "playlist",
                ["new"],
                dry_run=False,
            )
        self.assertEqual(ytmusic.add_playlist_items.call_count, 1)

    def test_final_order_mismatch_raises(self):
        ytmusic = Mock()
        ytmusic.get_playlist.return_value = {"tracks": []}
        ytmusic.add_playlist_items.return_value = "STATUS_SUCCEEDED"
        with patch("ytmusic_playlist_sync.time.sleep"), self.assertRaises(RuntimeError):
            self.update(
                ytmusic,
                "playlist",
                ["new"],
                dry_run=False,
            )
        # Success status alone is not an item ownership receipt.
        self.assertEqual(ytmusic.add_playlist_items.call_count, 1)
        ytmusic.remove_playlist_items.assert_not_called()

    def test_final_order_verification_succeeds(self):
        from test_playlist_verification import StatefulPlaylist
        ytmusic = StatefulPlaylist(["old"])
        with patch("ytmusic_playlist_sync.time.sleep"):
            self.update(
                ytmusic,
                "playlist",
                ["first", "second"],
                dry_run=False,
            )
        self.assertEqual(ytmusic.video_ids, ["first", "second"])

    def test_equivalent_metadata_does_not_make_a_different_id_idempotent(self):
        from test_playlist_verification import StatefulPlaylist
        ytmusic = StatefulPlaylist(["replacement"])
        ytmusic.get_song = Mock(side_effect=AssertionError("Publication must not guess aliases from metadata"))
        self.update(ytmusic, "playlist", ["requested"], dry_run=False)
        self.assertEqual(ytmusic.video_ids, ["requested"])
        self.assertEqual(ytmusic.remove_calls, 1)
        self.assertEqual(ytmusic.add_calls, [["requested"]])

    def test_same_title_and_duration_from_other_artist_is_not_equivalent(self):
        ytmusic = Mock()
        ytmusic.get_song.side_effect = [
            {"videoDetails": {
                "videoId": "requested",
                "title": "Same Song",
                "author": "Artist One",
                "lengthSeconds": "200",
            }},
            {"videoDetails": {
                "videoId": "replacement",
                "title": "Same Song",
                "author": "Artist Two",
                "lengthSeconds": "200",
            }},
        ]

        artists = {"requested": "Artist One", "replacement": "Artist Two"}
        ytmusic.get_watch_playlist.side_effect = lambda videoId, **kwargs: {"tracks": [{
            "videoId": videoId, "title": "Same Song",
            "artists": [{"id": artists[videoId], "name": artists[videoId]}],
        }]}
        ytmusic.get_artist.side_effect = lambda artist_id: {"name": artist_id}
        with patch("ytmusic_playlist_sync.make_ytmusic", return_value=ytmusic):
            self.assertFalse(
                _playlist_video_ids_match(ytmusic, ["requested"], ["replacement"])
            )

    def test_different_featured_artist_is_not_equivalent(self):
        ytmusic = Mock()
        ytmusic.get_song.side_effect = [
            {"videoDetails": {
                "videoId": "requested",
                "title": "Same Song (feat. Artist One)",
                "author": "Main Artist",
                "lengthSeconds": "200",
            }},
            {"videoDetails": {
                "videoId": "replacement",
                "title": "Same Song (feat. Artist Two)",
                "author": "Main Artist",
                "lengthSeconds": "200",
            }},
        ]

        self.assertFalse(
            _playlist_video_ids_match(ytmusic, ["requested"], ["replacement"])
        )


if __name__ == "__main__":
    unittest.main()
