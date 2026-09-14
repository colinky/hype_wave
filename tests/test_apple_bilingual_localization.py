import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from playlist_playability_fixture import PlaylistVerifier

import apple_music_to_ytmusic_crawl as apple
from crawler_common import process_matching_pipeline
from hype_db import connect, init_db
from hype_db_common import metadata_key
import hype_db_store as store
from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    localized_source_fields,
    passes_match_gates,
    score_result,
    search_queries_for_track,
    search_youtube_music,
)


def song(song_id, title, artist, album, isrc):
    return {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": title,
            "artistName": artist,
            "albumName": album,
            "isrc": isrc,
        },
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class AppleBilingualLocalizationTest(unittest.TestCase):
    def setUp(self):
        self.verifier = PlaylistVerifier(metadata={
            "GxChUrrY4bc": {"title": "The Adult I Dreamed Of", "artist": "meomuru"},
            "EIc13cn7tWo": {"title": "Nostalgia", "artist": "BIG Naughty"},
        })
        self.enterContext(patch("sync_validation.verifier_for", return_value=self.verifier))

    def test_kr_playlist_localization_error_reaches_outer_retry(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse({"data": []}),
            FakeResponse({"data": [song("kr-a", "가", "가수", "앨범", "ISRC-A")]}),
        ]
        with (
            patch.object(apple, "fetch_html", return_value=""),
            patch.object(apple, "find_apple_web_token", return_value="token"),
            patch.object(apple, "http_session", session),
            patch.object(
                apple,
                "_localized_tracks_from_items",
                side_effect=RuntimeError("localization failed"),
            ),
            self.assertRaises(apple.AppleLocalizationError),
        ):
            apple.fetch_apple_tracks(
                "https://music.apple.com/kr/playlist/test/pl.test",
                chart_limit=1,
            )

    def test_exact_isrc_lookup_batches_25_and_propagates_required_errors(self):
        isrcs = [f"isrc-{index:02d}" for index in range(26)]
        session = Mock()
        session.get.side_effect = [
            FakeResponse(
                {"data": [song("us-00", "First", "Artist", "Album", "ISRC-00")]}
            ),
            FakeResponse(
                {"data": [song("us-25", "Last", "Artist", "Album", "ISRC-25")]}
            ),
        ]
        with patch.object(apple, "http_session", session):
            resources = apple._fetch_song_resources_by_isrc(
                "token",
                storefront="us",
                isrcs=isrcs,
                locale="en-US",
                strict=True,
            )

        self.assertEqual(set(resources), {"ISRC-00", "ISRC-25"})
        self.assertEqual(session.get.call_count, 2)
        first_query = parse_qs(urlparse(session.get.call_args_list[0].args[0]).query)
        second_query = parse_qs(urlparse(session.get.call_args_list[1].args[0]).query)
        self.assertEqual(len(first_query["filter[isrc]"][0].split(",")), 25)
        self.assertEqual(len(second_query["filter[isrc]"][0].split(",")), 1)
        self.assertEqual(first_query["limit"], ["25"])

        with (
            patch.object(
                apple.http_session, "get", side_effect=RuntimeError("network")
            ),
            self.assertRaisesRegex(RuntimeError, "network"),
        ):
            apple._fetch_song_resources_by_isrc(
                "token",
                storefront="us",
                isrcs=["ISRC-00"],
                locale="en-US",
                strict=True,
            )

    def test_duplicate_slots_keep_kr_ids_and_ranks(self):
        first = song("kr-a", "가", "가수", "가 앨범", "ISRC-A")
        second = song("kr-b", "나", "가수", "나 앨범", "ISRC-B")

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            self.assertEqual(song_ids, ["kr-a", "kr-b"])
            source = {"kr-a": first, "kr-b": second}
            if storefront == "kr":
                return source
            return {
                key: song(
                    f"us-{key}",
                    "A" if key == "kr-a" else "B",
                    "Artist",
                    "Album",
                    value["attributes"]["isrc"],
                )
                for key, value in source.items()
            }

        with patch.object(apple, "_fetch_song_resource_map", side_effect=resources):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                [first, second, first], token="token", source="test"
            )

        self.assertEqual([track.song_id for track in tracks], ["kr-a", "kr-b", "kr-a"])
        self.assertEqual([track.rank for track in tracks], [1, 2, 3])
        self.assertEqual(tracks_ko["1"].title, "가")
        self.assertEqual(tracks_ko["3"].title, "가")

    def test_required_localization_request_propagates_errors(self):
        with (
            patch.object(
                apple.http_session, "get", side_effect=RuntimeError("network")
            ),
            self.assertRaisesRegex(RuntimeError, "network"),
        ):
            apple._fetch_song_resource_map(
                "token",
                storefront="us",
                song_ids=["kr-a"],
                locale="en-US",
                equivalents=True,
                strict=True,
            )

    def test_nostalgia_uses_verified_us_metadata_for_storage_and_search(self):
        korean = song(
            "6805367598",
            "노스탈지아",
            "BIG Naughty",
            "노스탈지아 - Single",
            "KRA382603592",
        )
        english = song(
            "1842517454",
            "Nostalgia",
            "BIG Naughty",
            "Nostalgia - Single",
            "KRA382603592",
        )

        def resources(
            _token, *, storefront, song_ids, locale, equivalents=False, strict=False
        ):
            self.assertEqual(song_ids, ["6805367598"])
            self.assertTrue(strict)
            if storefront == "kr":
                return {"6805367598": korean}
            self.assertTrue(equivalents)
            return {"6805367598": english}

        with patch.object(apple, "_fetch_song_resource_map", side_effect=resources):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                [korean], token="token", source="test"
            )

        track = tracks[0]
        track_ko = tracks_ko[track.song_id]
        self.assertEqual(
            (track.title, track.locale, track.song_id),
            ("Nostalgia", "en", "6805367598"),
        )
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
        self.assertEqual(queries[0], "Nostalgia BIG Naughty")
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

    def test_isrc_mismatch_uses_exact_us_recording_or_korean_fallback(self):
        korean = song(
            "kr-a", "DANCING ALONE", "키키", "DANCING ALONE - Single", "SOURCE-ISRC"
        )
        wrong = song(
            "us-remix",
            "DANCING ALONE ('90s Ver.)",
            "KiiiKiii",
            "DANCING ALONE Mix - EP",
            "REMIX-ISRC",
        )
        exact = song(
            "us-original",
            "DANCING ALONE",
            "KiiiKiii",
            "DANCING ALONE - Single",
            "SOURCE-ISRC",
        )

        def resources(_token, *, storefront, **_kwargs):
            return {"kr-a": korean if storefront == "kr" else wrong}

        with (
            patch.object(apple, "_fetch_song_resource_map", side_effect=resources),
            patch.object(
                apple,
                "_fetch_song_resources_by_isrc",
                return_value={"SOURCE-ISRC": exact},
            ),
        ):
            tracks, _ = apple._localized_tracks_from_items(
                [korean], token="token", source="test"
            )
        self.assertEqual(
            (tracks[0].title, tracks[0].artist), ("DANCING ALONE", "KiiiKiii")
        )
        self.assertEqual(
            (tracks[0].song_id, tracks[0].source), ("kr-a", "test_us_isrc")
        )

        with (
            patch.object(apple, "_fetch_song_resource_map", side_effect=resources),
            patch.object(apple, "_fetch_song_resources_by_isrc", return_value={}),
        ):
            tracks, tracks_ko = apple._localized_tracks_from_items(
                [korean], token="token", source="test"
            )
        self.assertEqual((tracks[0].title, tracks[0].locale), ("DANCING ALONE", "ko"))
        self.assertEqual(
            localized_source_fields(tracks[0], tracks_ko["kr-a"])["title_en"], ""
        )

    def test_search_matches_when_only_the_english_query_returns_a_candidate(self):
        track = SourceTrack(
            1,
            "Nostalgia",
            "BIG Naughty",
            service="apple",
            album="Nostalgia - Single",
            song_id="6805367598",
            locale="en",
        )
        track_ko = SourceTrack(
            1,
            "노스탈지아",
            "BIG Naughty",
            service="apple",
            album="노스탈지아 - Single",
            song_id="6805367598",
            locale="ko",
        )
        candidate = {
            "title": "Nostalgia",
            "artists": [{"name": "BIG Naughty"}],
            "album": {"name": "Nostalgia - Single"},
            "resultType": "song",
            "videoId": "EIc13cn7tWo",
        }

        def search(_ytmusic, query, _limit, **_kwargs):
            return [candidate] if query == "Nostalgia BIG Naughty" else []

        with (
            patch("ytmusic_playlist_sync.search_ytmusic_songs", side_effect=search),
            patch("ytmusic_playlist_sync.resolve_bilingual_song", return_value={}),
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

        self.assertEqual((result.status, result.video_id), ("matched", "EIc13cn7tWo"))
        self.assertEqual((result.query, result.score), ("Nostalgia BIG Naughty", 1.0))

    def test_completed_matching_transaction_stores_both_raw_locales(self):
        track = SourceTrack(
            1,
            "Nostalgia",
            "BIG Naughty",
            service="apple",
            album="Nostalgia - Single",
            song_id="6805367598",
            locale="en",
        )
        track_ko = SourceTrack(
            1,
            "노스탈지아",
            "BIG Naughty",
            service="apple",
            album="노스탈지아 - Single",
            song_id="6805367598",
            locale="ko",
        )
        failed = MatchResult(
            rank=1,
            title=track.title,
            artist=track.artist,
            album=track.album,
            service="apple",
            song_id=track.song_id,
        )

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "bilingual.db"
            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                patch("crawler_common.search_youtube_music", return_value=failed),
                patch("crawler_common.time.sleep"),
                patch("hype_db.export_frontend_history"),
            ):
                init_db(db_path)
                process_matching_pipeline(
                    all_tracks=[track],
                    raw_tracks=[track],
                    tracks_ko_map={track.song_id: track_ko},
                    ytmusic=None,
                    db_path=db_path,
                    service="apple",
                    job_name="Bilingual-Test",
                    update_date_str="2026-09-01",
                    reference_period="2026-09-01",
                    started_at="20260901T000000Z",
                    no_db_cache=True,
                )
                with connect(db_path) as conn:
                    row = conn.execute(
                        """
                        SELECT title_ko, artist_ko, album_ko,
                               title_en, artist_en, album_en
                        FROM track_list WHERE service='apple' AND song_id='6805367598'
                        """
                    ).fetchone()

        self.assertEqual(
            tuple(row),
            (
                "노스탈지아",
                "BIG Naughty",
                "노스탈지아 - Single",
                "Nostalgia",
                "BIG Naughty",
                "Nostalgia - Single",
            ),
        )

    def test_bilingual_metadata_cache_is_used_before_search(self):
        track = SourceTrack(
            1,
            "The Adult I Dreamed Of",
            "meomuru",
            service="apple",
            album="The Adult I Dreamed Of - Single",
            song_id="6781521710",
            locale="en",
        )
        track_ko = SourceTrack(
            1,
            "꿈꾸던 어른이 되었나요?",
            "머무르",
            service="apple",
            album="꿈꾸던 어른이 되었나요?",
            song_id="6781521710",
            locale="ko",
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "meomuru.db"
            with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                init_db(db_path)
            with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}), connect(
                db_path
            ) as conn:
                store.ensure_track(
                    conn,
                    track_uid="uid-meomuru",
                    video_id="GxChUrrY4bc",
                    yt_title="The Adult I Dreamed Of",
                    yt_artist="meomuru",
                    yt_album="The Adult I Dreamed Of",
                    status="matched",
                    score=0.85,
                )
                korean = {
                    "title_ko": track_ko.title,
                    "artist_ko": track_ko.artist,
                    "album_ko": track_ko.album,
                    "locale": "ko",
                }
                store.upsert_track_list_metadata(
                    conn,
                    service="ytmusic",
                    song_id="GxChUrrY4bc",
                    track_uid="uid-meomuru",
                    row=korean,
                )
                store.upsert_metadata_lookup(
                    conn,
                    track_uid="uid-meomuru",
                    row=korean,
                    source="fixture",
                    score=0.85,
                )
                conn.commit()

                localized = vars(track).copy()
                localized.update(localized_source_fields(track, track_ko))
                cached = store.get_bulk_cached_matches(
                    conn, service="apple", tracks=[localized]
                )[track.song_id]
                self.assertEqual(cached["video_id"], "GxChUrrY4bc")
                self.assertEqual(cached["cache_origin"], "metadata_lookup")
                self.assertEqual(cached["track_uid"], "uid-meomuru")
                self.assertEqual(
                    cached["lookup_key"],
                    metadata_key(track_ko.title, track_ko.artist, track_ko.album),
                )

            with (
                patch.dict(
                    os.environ,
                    {"SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1"},
                ),
                patch(
                    "crawler_common.search_youtube_music",
                    side_effect=AssertionError("bilingual cache miss triggered search"),
                ) as search,
                patch("ytmusic_playlist_sync.get_verified_video_metadata", return_value={
                    "video_id": "GxChUrrY4bc", "title": track.title, "artist": track.artist,
                    "album": track.album, "title_ko": track_ko.title,
                    "artist_ko": track_ko.artist, "album_ko": track_ko.album,
                }),
            ):
                video_ids = process_matching_pipeline(
                    all_tracks=[track],
                    raw_tracks=[track],
                    tracks_ko_map={track.song_id: track_ko},
                    ytmusic=Mock(),
                    db_path=db_path,
                    service="apple",
                    job_name="Bilingual-Cache-Fixture",
                    update_date_str="2026-09-07",
                    reference_period="2026-09-07",
                    started_at="2026-09-07T12:00:00.000001+00:00",
                )

            self.assertEqual(video_ids, ["GxChUrrY4bc"])
            search.assert_not_called()

    def test_no_db_cache_still_enforces_manual_block_and_override_without_writes(self):
        track = SourceTrack(
            1,
            "Policy Fixture",
            "Artist",
            service="apple",
            song_id="policy-source",
        )
        cases = [
            ("block", None, []),
            ("set_canonical", "GxChUrrY4bc", ["GxChUrrY4bc"]),
        ]
        for action, video_id, expected in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                db_path = Path(directory) / "manual-policy.db"
                with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                    init_db(db_path)
                    with connect(db_path) as conn:
                        conn.execute(
                            """
                            INSERT INTO manual_overrides(
                                service, song_id, action, canonical_yt_video_id,
                                reason, updated_at
                            ) VALUES ('apple', ?, ?, ?, 'qa fixture',
                                      '2026-09-07T00:00:00Z')
                            """,
                            (track.song_id, action, video_id),
                        )
                        conn.commit()
                before = db_path.read_bytes()
                with (
                    patch.dict(
                        os.environ,
                        {"SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1"},
                    ),
                    patch(
                        "crawler_common.search_youtube_music",
                        side_effect=AssertionError("manual policy fell through to search"),
                    ) as search,
                ):
                    result = process_matching_pipeline(
                        all_tracks=[track],
                        raw_tracks=[track],
                        ytmusic=Mock(),
                        db_path=db_path,
                        service="apple",
                        job_name="Manual-Policy-Fixture",
                        update_date_str="2026-09-07",
                        reference_period="2026-09-07",
                        started_at="2026-09-07T12:00:00.000001+00:00",
                        no_db_cache=True,
                        dry_run=True,
                    )

                self.assertEqual(result, expected)
                search.assert_not_called()
                self.assertEqual(db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
