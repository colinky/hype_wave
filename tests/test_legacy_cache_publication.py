"""Offline contract tests for legacy cache refresh and verified ID substitutions."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import crawler_common
from hype_db import connect, init_db
import hype_db_store as store
import ytmusic_playlist_sync as sync
from test_playlist_verification import StatefulPlaylist
from playlist_playability_fixture import PlaylistVerifier


SOURCE_ID = "8224585"
OMV = "uG2se-8-BzE"
ATV = "fsYE8vJRTj4"
ARTIST_ID = "UCR90rtDqmA4FV1bVnD6o0nQ"
TITLE = "기다린 만큼, 더"
ARTIST = "검정치마"
ALBUM = "또 오해영 OST Part.7"
TESTS = Path(__file__).resolve().parent


def source_track():
    return sync.SourceTrack(1, TITLE, ARTIST, service="melon", album=ALBUM,
                            song_id=SOURCE_ID, locale="ko")


def verified_row(video_id=ATV):
    return {"video_id": video_id, "title": TITLE, "artist": ARTIST, "album": ALBUM,
            "title_ko": TITLE, "artist_ko": ARTIST, "album_ko": ALBUM,
            "title_en": "As Much As I Waited", "artist_en": "The Black Skirts",
            "album_en": "Another Miss Oh OST Part.7"}


def seed_legacy(conn):
    store.ensure_track(conn, track_uid="legacy", video_id=ATV,
                       status="matched", score=0.7)
    store.upsert_track_list_metadata(conn, service="melon", song_id=SOURCE_ID,
                                    track_uid="legacy", row=vars(source_track()))
    conn.execute("INSERT INTO playlist_order(service,job_name,source_variant,reference_period,rank_order,song_id) "
                 "VALUES ('melon','Alias-Fixture','combined','2026-09-07',1,?)", (SOURCE_ID,))
    conn.commit()


def database_rows(conn):
    return {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
            for name in ("tracks", "track_list", "platform_song_ids", "yt_video_ids",
                         "playlist_order", "metadata_lookup_index", "manual_overrides")}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": "",
                                                  "HYPE_DEFER_HISTORY_EXPORT": "1"}))
        self.directory = self.enterContext(tempfile.TemporaryDirectory(dir=TESTS))
        self.db_path = Path(self.directory) / "fixture.db"
        init_db(self.db_path)


class CacheTransactionBoundaryTests(unittest.TestCase):
    def connection(self):
        conn = Mock(in_transaction=False)
        conn.commit.side_effect = lambda: setattr(conn, "in_transaction", False)
        return conn

    def test_metadata_io_has_no_transaction_and_second_read_uses_current_policy(self):
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                conn = self.connection()
                passes = []
                memos = []

                def bulk(_conn, **kwargs):
                    conn.in_transaction = True
                    passes.append(kwargs["read_only"])
                    if len(passes) == 1:
                        self.assertIsNone(kwargs["metadata_resolver"](ATV))
                        self.assertIsNone(kwargs["metadata_resolver"](OMV))
                        return {}
                    self.assertEqual(kwargs["metadata_resolver"](ATV)["video_id"], ATV)
                    return {SOURCE_ID: {"status": "manual_blocked"}}

                def metadata(_client, video_id, *, metadata_cache):
                    self.assertFalse(conn.in_transaction)
                    memos.append(metadata_cache)
                    return verified_row(video_id)

                with patch("hype_db.get_bulk_cached_matches", side_effect=bulk), patch.object(
                    crawler_common, "get_verified_video_metadata", side_effect=metadata
                ):
                    result = crawler_common.load_verified_matching_cache(
                        conn, service="melon", tracks=[vars(source_track())],
                        ytmusic=object(), read_only=read_only)
                self.assertEqual(result[SOURCE_ID]["status"], "manual_blocked")
                self.assertEqual(passes, [True, read_only])
                self.assertIs(memos[0], memos[1])
                conn.commit.assert_called_once()

    def test_warm_cache_closes_its_only_read_without_metadata_requests(self):
        conn = self.connection()

        def bulk(_conn, **kwargs):
            self.assertTrue(kwargs["read_only"])
            conn.in_transaction = True
            return {SOURCE_ID: {"video_id": ATV}}

        with patch("hype_db.get_bulk_cached_matches", side_effect=bulk) as read, patch.object(
            crawler_common, "get_verified_video_metadata", side_effect=AssertionError("warm cache requested network")
        ) as metadata:
            result = crawler_common.load_verified_matching_cache(
                conn, service="melon", tracks=[vars(source_track())], ytmusic=object())
        self.assertEqual(result[SOURCE_ID]["video_id"], ATV)
        self.assertFalse(conn.in_transaction)
        read.assert_called_once()
        metadata.assert_not_called()

    def test_metadata_outage_never_reaches_the_second_database_pass(self):
        conn = self.connection()

        def bulk(_conn, **kwargs):
            self.assertTrue(kwargs["read_only"])
            conn.in_transaction = True
            kwargs["metadata_resolver"](ATV)
            return {}

        def outage(*args, **kwargs):
            self.assertFalse(conn.in_transaction)
            raise RuntimeError("metadata unavailable")

        with patch("hype_db.get_bulk_cached_matches", side_effect=bulk) as read, patch.object(
            crawler_common, "get_verified_video_metadata", side_effect=outage
        ), self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            crawler_common.load_verified_matching_cache(
                conn, service="melon", tracks=[vars(source_track())], ytmusic=object())
        read.assert_called_once()
        self.assertFalse(conn.in_transaction)


class LegacyCacheRefreshTests(OfflineCase):
    def test_existing_sqlite_rows_receive_only_an_unverified_marker_on_upgrade(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            conn.execute("ALTER TABLE tracks DROP COLUMN yt_metadata_verified_key")
            before = dict(conn.execute("SELECT * FROM tracks WHERE track_uid='legacy'").fetchone())
            conn.commit()
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            after = dict(conn.execute("SELECT * FROM tracks WHERE track_uid='legacy'").fetchone())
            self.assertIsNone(after.pop("yt_metadata_verified_key"))
            self.assertEqual(after, before)
            state = database_rows(conn)
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            self.assertEqual(database_rows(conn), state)

    def test_exact_existing_video_refreshes_empty_metadata_once(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            before = database_rows(conn)
            resolver = Mock(return_value=verified_row())
            cached = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                   metadata_resolver=resolver)
            self.assertEqual(cached[SOURCE_ID]["video_id"], ATV)
            self.assertEqual(cached[SOURCE_ID]["cache_origin"], "service_song_id")
            resolver.assert_called_once_with(ATV)
            row = conn.execute("SELECT * FROM tracks WHERE track_uid='legacy'").fetchone()
            self.assertEqual((row["canonical_yt_video_id"], row["yt_title"], row["yt_artist"], row["yt_album"]),
                             (ATV, TITLE, ARTIST, ALBUM))
            self.assertTrue(row["yt_metadata_verified_key"])
            after = database_rows(conn)
            for table in ("track_list", "platform_song_ids", "yt_video_ids", "playlist_order", "manual_overrides"):
                self.assertEqual(after[table], before[table], table)
            cached_again = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                         metadata_resolver=resolver)
            self.assertEqual(cached_again[SOURCE_ID]["video_id"], ATV)
            resolver.assert_called_once_with(ATV)
            self.assertEqual(database_rows(conn), after)
            conn.execute("UPDATE tracks SET yt_title='Old writer changed the title' WHERE track_uid='legacy'")
            stale = database_rows(conn)
            rejected = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                      metadata_resolver=lambda _id: None)
            self.assertNotIn(SOURCE_ID, rejected)
            self.assertEqual(database_rows(conn), stale)
            repaired = store.get_bulk_cached_matches(conn, "melon", [source_track()], metadata_resolver=resolver)
            self.assertEqual(repaired[SOURCE_ID]["video_id"], ATV)
            self.assertEqual(resolver.call_count, 2)
            self.assertEqual(conn.execute("SELECT yt_title FROM tracks WHERE track_uid='legacy'").fetchone()[0], TITLE)

    def test_refresh_stores_verified_artist_not_an_omv_uploader(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            metadata = {**verified_row(), "artist": "Record Label Channel"}
            cached = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                   metadata_resolver=lambda _id: metadata)
            self.assertEqual(cached[SOURCE_ID]["video_id"], ATV)
            self.assertEqual(conn.execute("SELECT yt_artist FROM tracks WHERE track_uid='legacy'").fetchone()[0], ARTIST)

    def test_invalid_refresh_cannot_write_or_validate_its_own_source(self):
        cases = [None, {}, {**verified_row(), "video_id": OMV},
                 {**verified_row(), "title": "", "title_ko": "", "title_en": ""},
                 {**verified_row(), "artist": "", "artist_ko": "", "artist_en": ""},
                 {"video_id": ATV, "title": TITLE, "artist": "Unrelated Artist", "album": ALBUM},
                 {"video_id": ATV, "title": TITLE + " (Live)", "artist": ARTIST, "album": ALBUM}]
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            for candidate in cases:
                with self.subTest(candidate=candidate):
                    before = database_rows(conn)
                    result = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                           metadata_resolver=lambda _id: candidate)
                    self.assertNotIn(SOURCE_ID, result)
                    self.assertEqual(database_rows(conn), before)

    def test_refresh_error_propagates_and_read_only_success_does_not_write(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            before = database_rows(conn)
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                              metadata_resolver=Mock(side_effect=RuntimeError("provider unavailable")))
            self.assertEqual(database_rows(conn), before)
            result = store.get_bulk_cached_matches(conn, "melon", [source_track()],
                                                   metadata_resolver=lambda _id: verified_row(), read_only=True)
            self.assertEqual(result[SOURCE_ID]["video_id"], ATV)
            self.assertEqual(database_rows(conn), before)

    def test_unmarked_or_wrong_video_provenance_never_self_validates_canonical_metadata(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            for marker in (None, OMV):
                with self.subTest(marker=marker):
                    conn.execute("UPDATE tracks SET yt_title=?,yt_artist=?,yt_album=?,yt_metadata_verified_key=? "
                                 "WHERE track_uid='legacy'", (TITLE, ARTIST, ALBUM, marker))
                    before = database_rows(conn)
                    resolver = Mock(return_value=None)
                    cached = store.get_bulk_cached_matches(conn, "melon", [source_track()], metadata_resolver=resolver)
                    self.assertNotIn(SOURCE_ID, cached)
                    resolver.assert_called_once_with(ATV)
                    self.assertEqual(database_rows(conn), before)

    def test_manual_block_precedes_network_refresh(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            conn.execute("INSERT INTO manual_overrides(service,song_id,action,reason,updated_at) "
                         "VALUES ('melon',?,'block','fixture','2026-09-07')", (SOURCE_ID,))
            before = database_rows(conn)
            resolver = Mock(side_effect=AssertionError("blocked source must not request metadata"))
            result = store.get_bulk_cached_matches(conn, "melon", [source_track()], metadata_resolver=resolver)
            self.assertEqual(result[SOURCE_ID]["status"], "manual_blocked")
            resolver.assert_not_called()
            self.assertEqual(database_rows(conn), before)

    def test_alias_metadata_cannot_overwrite_a_different_canonical_video(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
            store.ensure_track(conn, track_uid="legacy", video_id=OMV,
                               yt_title="OMV-specific title", yt_artist="OMV-specific artist",
                               yt_album="OMV-specific album", status="matched", score=0.9)
            row = conn.execute("SELECT canonical_yt_video_id,yt_title,yt_artist,yt_album "
                               "FROM tracks WHERE track_uid='legacy'").fetchone()
            self.assertEqual(tuple(value or "" for value in row), (ATV, "", "", ""))
            self.assertEqual([tuple(row) for row in conn.execute(
                "SELECT video_id,is_canonical FROM yt_video_ids WHERE track_uid='legacy' ORDER BY video_id")],
                [(ATV, 1)])
            store.get_bulk_cached_matches(conn, "melon", [source_track()], metadata_resolver=lambda _id: verified_row())
            verified_key = conn.execute("SELECT yt_metadata_verified_key FROM tracks WHERE track_uid='legacy'").fetchone()[0]
            self.assertTrue(verified_key)
            store.ensure_track(conn, track_uid="legacy", video_id=OMV,
                               yt_title="Unrelated title", yt_artist="Unrelated artist", yt_album="Unrelated album")
            row = conn.execute("SELECT canonical_yt_video_id,yt_title,yt_artist,yt_album,yt_metadata_verified_key "
                               "FROM tracks WHERE track_uid='legacy'").fetchone()
            self.assertEqual(tuple(row), (ATV, TITLE, ARTIST, ALBUM, verified_key))


class IdentityClient(StatefulPlaylist):
    def __init__(self, ids=None):
        super().__init__(ids or [], substitute_requested=[ATV], requested_values=[OMV])
        self.calls = Counter()
        self.missing_atv_once = False
        self.metadata = {
            OMV: {"videoId": OMV, "title": TITLE, "author": ARTIST,
                  "channelId": "same-uploader", "lengthSeconds": "268", "musicVideoType": "MUSIC_VIDEO_TYPE_OMV"},
            ATV: {"videoId": ATV, "title": TITLE, "author": ARTIST + " (The Black Skirts)",
                  "channelId": "same-uploader", "lengthSeconds": "269", "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"},
        }
        self.watch = {video_id: {"videoId": video_id, "title": TITLE,
                                 "artists": [{"name": ARTIST, "id": ARTIST_ID}],
                                 "album": {"id": "album-id", "name": ALBUM}}
                      for video_id in (OMV, ATV)}
        self.english = Mock()
        self.english.get_watch_playlist.side_effect = lambda videoId, **kwargs: {"tracks": [copy.deepcopy(self.watch[videoId])]}
        self.english.get_artist.return_value = {"name": "The Black Skirts"}

    def get_song(self, video_id):
        self.calls[("get_song", video_id)] += 1
        value = copy.deepcopy(self.metadata[video_id])
        if video_id == ATV and self.missing_atv_once:
            self.missing_atv_once = False
            value["title"] = ""
        return {"videoDetails": value}

    def get_watch_playlist(self, videoId, **kwargs):
        self.calls[("watch", videoId)] += 1
        return {"tracks": [copy.deepcopy(self.watch[videoId])]}

    def get_artist(self, artist_id):
        return {"name": ARTIST}

    def search(self, query, **kwargs):
        self.calls["search"] += 1
        return [{"resultType": "song", **copy.deepcopy(self.watch[OMV]), "duration_seconds": 268}]


class VerifiedAuthorIdentityTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.client = IdentityClient()
        self.verifier = PlaylistVerifier(metadata={video_id: verified_row(video_id) for video_id in (ATV, OMV)})
        self.enterContext(patch("sync_validation.verifier_for", return_value=self.verifier))
        self.enterContext(patch.object(sync, "get_ytmusic_en", return_value=self.client.english))
        self.enterContext(patch.object(sync, "make_ytmusic", side_effect=lambda *args, language="en", **kwargs:
                                      self.client if language == "ko" else self.client.english))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.cache = self.enterContext(patch.object(sync, "BILINGUAL_CACHE", sync.BilingualCache(Path(self.directory) / "bilingual.db")))
        self.enterContext(sync.bilingual_cache_read_only())

    def compare(self):
        return sync._compare_playlist_video_ids(self.client, [OMV], [ATV])

    def test_same_full_artist_identity_accepts_korean_english_display_alias(self):
        result = self.compare()
        self.assertTrue(result["matches"], result)
        self.assertTrue(result["differences"][0]["checks"]["author"])
        self.assertIsNone(self.cache.backend)

    def test_verified_metadata_artist_is_not_the_video_uploader(self):
        self.client.metadata[OMV]["author"] = "Record Label Channel"
        metadata = sync.get_verified_video_metadata(self.client, OMV)
        self.assertEqual(metadata["artist"], ARTIST)
        self.assertEqual(metadata["artist_ids"], [ARTIST_ID])

    def test_primary_verified_metadata_respects_the_callers_english_locale(self):
        self.client.language = "en"
        english_track = copy.deepcopy(self.client.watch[ATV])
        english_track.update(title="As Much As I Waited", album={"name": "Another Miss Oh OST Part.7"})
        english_track["artists"] = [{"id": ARTIST_ID, "name": "The Black Skirts"}]
        self.client.english.get_watch_playlist.side_effect = lambda **kwargs: {"tracks": [english_track]}
        metadata = sync.get_verified_video_metadata(self.client, ATV)
        self.assertEqual((metadata["title"], metadata["artist"], metadata["album"]),
                         ("As Much As I Waited", "The Black Skirts", "Another Miss Oh OST Part.7"))

    def test_known_watch_statistics_are_not_artists_but_unknown_names_are(self):
        for name in ("조회수 2484만회", "좋아요 7만개", "2016년", "24M views", "70K likes"):
            self.client.watch[ATV]["artists"].append({"name": name, "id": None})
        self.assertTrue(self.compare()["matches"])
        self.client.watch[ATV]["artists"].append({"name": "진영", "id": None})
        self.assertFalse(self.compare()["matches"])

    def test_fresh_identity_ignores_polluted_persistent_aliases_and_errors_can_retry(self):
        with patch.object(self.cache, "get_song", return_value={"title_ko": "Wrong Song", "artist_ko": "Wrong Artist"}) as cached_song:
            self.assertTrue(self.compare()["matches"])
            cached_song.assert_not_called()
        memo = {}
        original = self.client.get_song
        self.client.get_song = Mock(side_effect=RuntimeError("temporary metadata outage"))
        with self.assertRaisesRegex(RuntimeError, "temporary metadata outage"):
            sync.get_verified_video_metadata(self.client, ATV, metadata_cache=memo)
        self.client.get_song = original
        self.assertEqual(sync.get_verified_video_metadata(self.client, ATV, metadata_cache=memo)["video_id"], ATV)
        self.assertIsNone(self.cache.backend)

    def test_incomplete_response_can_retry_but_api_decode_errors_are_not_cache_misses(self):
        memo = {}
        self.client.missing_atv_once = True
        self.assertIsNone(sync.get_verified_video_metadata(self.client, ATV, metadata_cache=memo))
        self.assertEqual(sync.get_verified_video_metadata(self.client, ATV, metadata_cache=memo)["video_id"], ATV)
        self.client.get_song = Mock(side_effect=ValueError("provider returned invalid JSON"))
        with self.assertRaisesRegex(RuntimeError, "provider returned invalid JSON"):
            sync.get_verified_video_metadata(self.client, OMV, metadata_cache={})

    def test_subset_disjoint_or_missing_artist_identity_is_not_equivalent(self):
        for artists in ([{"name": ARTIST, "id": ARTIST_ID}, {"name": "Guest", "id": "guest-id"}],
                        [{"name": ARTIST, "id": "other-artist-id"}], [{"name": ARTIST}]):
            with self.subTest(artists=artists):
                self.client.watch[ATV]["artists"] = artists
                result = self.compare()
                self.assertFalse(result["matches"], result)

    def test_equal_author_text_is_not_enough_when_artist_ids_disagree(self):
        self.client.metadata[ATV]["author"] = ARTIST
        self.client.watch[ATV]["artists"] = [{"name": ARTIST, "id": "unrelated-same-name"}]
        self.assertFalse(self.compare()["matches"])

    def test_title_substrings_are_not_artist_identity_evidence(self):
        for video_id, author in ((OMV, "Love"), (ATV, "Me")):
            self.client.metadata[video_id].update(title="LOVE ME", author=author, lengthSeconds="180")
            self.client.watch[video_id].update(title="LOVE ME", artists=[{"name": author, "id": author}])
        self.assertFalse(self.compare()["matches"])

    def test_foreign_watch_id_and_locale_identity_conflict_fail_closed(self):
        self.client.watch[ATV]["videoId"] = "foreign-id"
        self.assertFalse(self.compare()["matches"])
        self.client.watch[ATV]["videoId"] = ATV
        foreign = copy.deepcopy(self.client.watch[ATV])
        foreign["artists"] = [{"name": "Different Artist", "id": "foreign-artist"}]
        self.client.english.get_watch_playlist.side_effect = lambda videoId, **kwargs: {
            "tracks": [foreign if videoId == ATV else self.client.watch[videoId]]}
        self.assertFalse(self.compare()["matches"])

    def test_identity_does_not_bypass_title_version_duration_or_wrong_video(self):
        for changes in ({"title": "Different Song"}, {"title": TITLE + " (Live)"},
                        {"lengthSeconds": "320"}, {"videoId": "foreign-id"}):
            with self.subTest(changes=changes):
                previous = copy.deepcopy(self.client.metadata[ATV])
                self.client.metadata[ATV].update(changes)
                self.assertFalse(self.compare()["matches"])
                self.client.metadata[ATV] = previous

    def test_exact_requested_publication_needs_no_pair_metadata_and_dry_run_never_writes(self):
        self.client.video_ids = [ATV]
        self.client.substitute_requested = None
        self.client.get_watch_playlist = Mock(side_effect=RuntimeError("provider unavailable"))
        before = self.db_path.read_bytes()
        sync.update_ytmusic_playlist(self.client, "fixture", [OMV], dry_run=True, db_path=self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (0, 0, []))
        sync.update_ytmusic_playlist(self.client, "fixture", [OMV], dry_run=False,
                                    db_path=self.db_path, job_name="Alias-Fixture")
        self.assertEqual(self.client.video_ids, [OMV])
        self.client.get_watch_playlist.assert_not_called()

    def test_pipeline_refresh_and_cache_bypass_obey_dry_run_without_writes(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
        before = self.db_path.read_bytes()
        common = dict(all_tracks=[source_track()], ytmusic=self.client, db_path=self.db_path,
                      service="melon", job_name="Alias-Fixture", source_variant="combined",
                      update_date_str="2026-09-08", reference_period="2026-09-07",
                      started_at="2026-09-08T12:00:00+00:00", dry_run=True)
        self.assertEqual(crawler_common.process_matching_pipeline(**common), [ATV])
        self.assertEqual(self.client.calls["search"], 0)
        metadata_calls = self.client.calls[("get_song", ATV)]
        self.assertGreater(metadata_calls, 0)
        self.assertEqual(crawler_common.process_matching_pipeline(**common, no_db_cache=True), [ATV])
        self.assertGreater(self.client.calls["search"], 0)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertIsNone(self.cache.backend)

    def test_metadata_gap_cannot_publish_or_record_a_different_id(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
        self.client.missing_atv_once = True
        result = crawler_common.process_matching_pipeline(
                all_tracks=[source_track()], ytmusic=self.client, db_path=self.db_path,
                service="melon", job_name="Alias-Fixture", source_variant="combined",
                update_date_str="2026-09-08", reference_period="2026-09-07",
                started_at="2026-09-08T12:00:00+00:00")
        self.assertEqual(result, [ATV])
        self.assertGreater(self.client.calls["search"], 0)
        with connect(self.db_path, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid='legacy'").fetchone()[0], ATV)
            self.assertEqual([row[0] for row in conn.execute("SELECT video_id FROM match_attempts")], [ATV])
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (0, 0, []))

    def test_persistent_metadata_gap_cannot_record_a_fallback_id(self):
        with connect(self.db_path) as conn:
            seed_legacy(conn)
        self.client.metadata[ATV]['title'] = ''
        before = self.db_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'identity needs review'):
            crawler_common.process_matching_pipeline(
                all_tracks=[source_track()], ytmusic=self.client, db_path=self.db_path,
                service='melon', job_name='Alias-Fixture', source_variant='combined',
                update_date_str='2026-09-08', reference_period='2026-09-07',
                started_at='2026-09-08T12:00:00+00:00')
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (0, 0, []))


if __name__ == "__main__":
    unittest.main()
