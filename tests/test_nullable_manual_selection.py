"""A source-specific UID choice is not a cross-language recording identity."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import sqlite3
import unittest
from unittest.mock import patch

import hype_db_store as store
from hype_db_schema import init_schema
import sync_validation as validation
from tests.playlist_playability_fixture import PlaylistVerifier

JP, KR, NEXT = "0iiW__6izcs", "kjNGAVT_ySg", "nextKorean1"
JP_UID, KR_UID = "japanese-recording", "korean-recording"


class NullableManualSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn, repair_source_bindings=False)
        self.source = {"service": "ytmusic", "song_id": JP, "rank": 1,
                       "title": "DAY BY DAY (Japanese ver.)", "artist": "티아라", "album": "",
                       "title_ko": "DAY BY DAY (Japanese ver.)", "artist_ko": "티아라", "album_ko": "",
                       "title_en": "DAY BY DAY (Japanese ver.)", "artist_en": "T-Ara", "album_en": ""}
        self.korean = {"video_id": KR, "title": "DAY BY DAY", "artist": "티아라", "album": "DAY BY DAY"}
        for uid, video, metadata in ((JP_UID, JP, self.source), (KR_UID, KR, self.korean)):
            store.ensure_track(self.conn, track_uid=uid, video_id=video, yt_title=metadata["title"],
                               yt_artist=metadata["artist"], yt_album=metadata["album"], status="matched", score=.9)
        store.upsert_track_list_metadata(self.conn, service="ytmusic", song_id=JP,
                                         track_uid=JP_UID, row=self.source)
        self.conn.execute("INSERT INTO manual_overrides(service,song_id,action,target_track_uid,canonical_yt_video_id,reason,updated_at) "
                          "VALUES (?,?,'set_canonical',?,NULL,'User-approved current available recording; no language priority','fixture')",
                          ("ytmusic", JP, KR_UID))
        self.now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("INSERT INTO match_runs(run_id,service,job_name,started_at,created_at) "
                          "VALUES ('prior','ytmusic','PriorFixture',?,?)", (self.now, self.now))
        self.conn.execute("INSERT INTO match_attempts(run_id,service,song_id,track_uid,rank_order,video_id,status,created_at) "
                          "VALUES ('prior','ytmusic',?, ?,1,?,'matched',?)", (JP, JP_UID, JP, self.now))
        self.conn.commit()
        self.verifier = PlaylistVerifier()

    def table(self, name):
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]

    def cache_match(self, source=None):
        source = source or self.source
        cached = store.get_bulk_cached_matches(self.conn, service=source["service"], tracks=[source], read_only=True)[source["song_id"]]
        # The native chart caller keeps raw fields and puts the selected
        # recording's fields only in yt_* display fields.
        return {**source, **{key: cached[key] for key in ("video_id", "yt_title", "yt_artist", "yt_album", "status", "score")},
                "query": cached["query"]}

    def validate(self, match=None, source=None):
        source = source or self.source
        return validation.validate_matches(self.conn, service=source["service"], sources=[source],
            matches=[match or self.cache_match(source)], client=object(), verifier=self.verifier)[0]

    def test_real_cache_validation_and_bulk_save_preserve_distinct_recordings_and_history(self):
        before_source = self.table("track_list")
        old_attempts = self.table("match_attempts")
        old_aliases = self.table("yt_video_ids")
        source = deepcopy(self.source)
        self.assertFalse(validation.source_recording_matches(source, self.korean))
        match = self.validate()
        self.assertEqual((match["video_id"], match["status"]), (KR, "manual_override"))
        self.assertNotIn("canonical_decision", match)
        self.assertNotIn("same_recording", match)
        self.assertEqual(self.source, source)
        store.persist_crawled_tracks(":memory:", service="ytmusic", job_name="SelectionFixture", chart_date="2026-09-15",
                                     tracks=[source], conn=self.conn, commit=False)
        store.persist_crawl_run(":memory:", service="ytmusic", job_name="SelectionFixture", chart_date="2026-09-15",
                                started_at=self.now, tracks=[source], matches=[match], conn=self.conn,
                                skip_playlist_order=True, commit=False)
        self.assertEqual(store.find_track_by_service_song(self.conn, "ytmusic", JP), KR_UID)
        self.assertEqual(dict(self.conn.execute("SELECT track_uid,canonical_yt_video_id FROM tracks")), {JP_UID: JP, KR_UID: KR})
        self.assertEqual(self.table("yt_video_ids"), old_aliases)
        self.assertEqual(self.table("track_list"), before_source)
        self.assertEqual([r for r in self.table("match_attempts") if r["run_id"] == "prior"], old_attempts)
        attempt = next(r for r in self.table("match_attempts") if r["run_id"] != "prior")
        self.assertEqual((attempt["track_uid"], attempt["video_id"], attempt["status"]), (KR_UID, KR, "manual_override"))
        self.assertEqual(self.table("migration_reports"), [])
        policy = self.table("manual_overrides")[0]
        self.assertIsNone(policy["canonical_yt_video_id"])
        self.assertFalse(any("language" in key or "preference" in key for key in policy))
        frozen = validation.freeze_outputs(self.conn, [{"service": "ytmusic", "job_name": "SelectionFixture", "target_id": "offline-only"}],
                                            history_date="2026-09-15")
        self.assertEqual(frozen["outputs"][0]["video_ids"], [KR])
        self.conn.rollback()
        self.assertEqual(store.find_track_by_service_song(self.conn, "ytmusic", JP), JP_UID)
        self.assertEqual(self.table("match_attempts"), old_attempts)

    def test_single_save_uses_the_same_uid_only_policy_without_merging_aliases(self):
        aliases = self.table("yt_video_ids")
        self.assertEqual(store.upsert_track_match(self.conn, service="ytmusic", source_row=self.source,
                                                  match_row=self.validate()), KR_UID)
        self.assertEqual(self.table("yt_video_ids"), aliases)
        self.assertEqual(dict(self.conn.execute("SELECT track_uid,canonical_yt_video_id FROM tracks")), {JP_UID: JP, KR_UID: KR})

    def test_null_video_follows_the_target_uids_later_canonical(self):
        self.assertEqual(self.cache_match()["video_id"], KR)
        # A separately approved future target state; this test does not grant
        # authority to change that recording or infer an inter-language link.
        self.conn.execute("UPDATE tracks SET canonical_yt_video_id=? WHERE track_uid=?", (NEXT, KR_UID))
        self.conn.execute("UPDATE yt_video_ids SET is_canonical=0 WHERE track_uid=?", (KR_UID,))
        self.conn.execute("INSERT INTO yt_video_ids VALUES (?,?,1)", (NEXT, KR_UID))
        self.conn.commit()
        match = self.validate()
        self.assertEqual(match["video_id"], NEXT)
        self.assertNotIn("canonical_decision", match)
        self.assertIsNone(self.table("manual_overrides")[0]["canonical_yt_video_id"])
        self.assertEqual(store.upsert_track_match(self.conn, service="ytmusic", source_row=self.source, match_row=match), KR_UID)
        self.assertEqual(store.find_track_by_video(self.conn, JP), JP_UID)

    def test_unknown_or_unavailable_target_does_not_bypass_playability(self):
        before = list(self.conn.iterdump())
        for state in ("unknown", "unavailable"):
            self.verifier.states[KR] = state
            with self.subTest(state=state), self.assertRaises(validation.PlaybackBlocked):
                self.validate()
            self.assertEqual(list(self.conn.iterdump()), before)

    def test_policy_is_exact_source_scoped_and_requires_the_selected_uid_owner(self):
        match = self.cache_match()
        for source in ({**self.source, "song_id": "otherSong11"}, {**self.source, "service": "apple"}):
            with self.subTest(source=source), self.assertRaises(validation.PlaybackBlocked):
                self.validate({**match, "song_id": source["song_id"]}, source)
        self.conn.execute("UPDATE yt_video_ids SET track_uid=? WHERE video_id=?", (JP_UID, KR))
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(match)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_policy_change_during_observation_blocks_before_persistence(self):
        real_verify = self.verifier.verify
        def changed(video, **kwargs):
            result = real_verify(video, **kwargs)
            self.conn.execute("UPDATE manual_overrides SET target_track_uid=? WHERE song_id=?", (JP_UID, JP))
            return result
        with patch.object(self.verifier, "verify", side_effect=changed), self.assertRaisesRegex(validation.PlaybackBlocked, "policy changed"):
            self.validate()
        self.assertEqual(store.find_track_by_service_song(self.conn, "ytmusic", JP), JP_UID)
        self.assertEqual(len(self.table("match_attempts")), 1)


if __name__ == "__main__":
    unittest.main()
