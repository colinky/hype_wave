"""Exact native IDs preserve existing entities without approving substitutions."""
from contextlib import contextmanager
from copy import deepcopy
import os
import sqlite3
import unittest
from unittest.mock import patch

from hype_db_schema import init_schema
import hype_db_store as store
import crawler_common
import sync_validation as validation
import ytmusic_playlist_sync as matching
import ytmusic_to_ytmusic_crawl as chart
from tests.playlist_playability_fixture import PlaylistVerifier

VIDEO = "1rAos7HClwI"


class NativeVerifier(PlaylistVerifier):
    @contextmanager
    def _bounded_session(self):
        yield


class NativeRecordingPreservationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.source = {"service": "ytmusic", "song_id": VIDEO, "rank": 1,
                       "title": "REDRED (Live)", "artist": "ITZY", "album": ""}
        self.player = {"title": "RYUJIN 'REDRED' @ ITZY The 5th Fan Meeting", "artist": "ITZY",
                       "music_video_type": "MUSIC_VIDEO_TYPE_OMV"}
        self.verifier = NativeVerifier(metadata={VIDEO: self.player})
        self.client = object()
        self.metadata = self.enterContext(patch.object(matching, "get_verified_video_metadata", return_value=None))
        self.watch = self.enterContext(patch.object(matching, "_watch_playlist_for_metadata",
            return_value={"tracks": [{"videoId": VIDEO, "isAvailable": True}]}))

    def seed(self, service="ytmusic", song_id=VIDEO):
        store.ensure_track(self.conn, track_uid="existing", video_id=VIDEO,
            yt_title="Stored upload title", yt_artist="Stored artist", yt_album="Stored album",
            status="matched", score=.75)
        store.upsert_track_list_metadata(self.conn, service=service, song_id=song_id,
            track_uid="existing", row=self.source)
        self.conn.commit()

    def snapshot(self):
        return "\n".join(self.conn.iterdump())

    def validate(self, selected=VIDEO, source=None, status="cached_match"):
        source = source or self.source
        return validation.validate_matches(self.conn, service=source["service"], sources=[source],
            matches=[{"song_id": source["song_id"], "video_id": selected, "status": status,
                      "yt_title": "Unverified incoming title", "score": .75}],
            client=self.client, verifier=self.verifier)

    def test_same_native_entity_keeps_stored_identity_despite_curated_chart_title(self):
        self.seed()
        before, source_before = self.snapshot(), deepcopy(self.source)
        result = self.validate()[0]
        self.assertEqual(result["video_id"], VIDEO)
        self.assertEqual(result["yt_title"], "Stored upload title")
        self.assertEqual(result["yt_artist"], "Stored artist")
        self.assertEqual(result["yt_album"], "Stored album")
        self.assertEqual(result["score"], .75)
        self.assertEqual(result["query"], "preserved_exact_native_id")
        self.assertNotIn("canonical_decision", result)
        self.assertEqual(self.source, source_before)
        self.assertEqual(self.snapshot(), before)
        self.metadata.assert_not_called()
        self.watch.assert_called_once_with(self.client, VIDEO)

    def test_unknown_unavailable_or_unhealthy_player_cannot_use_native_identity(self):
        self.seed()
        before = self.snapshot()
        for state in ("unknown", "unavailable"):
            self.verifier.states[VIDEO] = state
            with self.subTest(state=state), self.assertRaises(validation.PlaybackBlocked):
                self.validate()
        self.verifier.states.clear()
        self.verifier.health = "unknown"
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.assertEqual(self.snapshot(), before)

    def test_exact_watch_conflict_missing_or_ambiguous_id_blocks_without_writes(self):
        self.seed()
        before = self.snapshot()
        for payload in ({"tracks": [{"videoId": VIDEO, "isAvailable": False}]},
                        {"tracks": []}, {"tracks": [{"videoId": "AnotherId11"}]},
                        {"tracks": [{"videoId": VIDEO}, {"videoId": VIDEO}]}, None):
            self.watch.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(validation.PlaybackBlocked):
                self.validate()
            self.assertEqual(self.snapshot(), before)

    def test_watch_failure_propagates_without_data_changes(self):
        self.seed()
        before = self.snapshot()
        self.watch.side_effect = TimeoutError("fixture timeout")
        with self.assertRaises(TimeoutError):
            self.validate()
        self.assertEqual(self.snapshot(), before)

    def test_native_identity_does_not_authorize_unbound_or_other_service_source(self):
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.seed(service="apple")
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(source={**self.source, "service": "apple"})
        self.watch.assert_not_called()

    def test_other_native_source_id_cannot_inherit_the_preservation_exception(self):
        self.seed(song_id="OtherSrc111")
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(source={**self.source, "song_id": "OtherSrc111"})
        self.watch.assert_not_called()

    def test_a_different_initial_selection_or_unavailable_replacement_stays_strict(self):
        self.seed()
        for state in ("playable", "unavailable"):
            self.verifier.states[VIDEO] = state
            with self.subTest(state=state), self.assertRaises(validation.PlaybackBlocked):
                self.validate(selected="AnotherId11")
        self.watch.assert_not_called()

    def test_native_cache_cannot_prove_old_identity_for_a_valid_replacement_candidate(self):
        self.seed()
        before = self.snapshot()
        target = "AnotherId11"
        candidate = {"video_id": target, "title": self.source["title"],
                     "artist": self.source["artist"], "album": "",
                     "artist_identity_complete": True}
        self.verifier.metadata[target] = {"title": candidate["title"], "artist": candidate["artist"]}
        for state in ("playable", "unavailable"):
            for old_metadata in (None, {**candidate, "video_id": VIDEO, "artist": "Another Artist"}):
                with self.subTest(state=state, old_metadata=old_metadata):
                    self.verifier.states[VIDEO] = state
                    self.metadata.reset_mock()
                    self.metadata.side_effect = lambda client, video_id, **kwargs: (
                        candidate if video_id == target else old_metadata)
                    with self.assertRaisesRegex(validation.PlaybackBlocked, "Existing recording identity needs review"):
                        self.validate(selected=target)
                    self.assertEqual([call.args[1] for call in self.metadata.call_args_list], [VIDEO])
                    self.assertEqual(self.snapshot(), before)
        self.watch.assert_not_called()

    def test_unavailable_native_can_change_after_both_exact_identities_are_verified(self):
        self.seed()
        before = self.snapshot()
        target = "AnotherId11"
        metadata = {"title": self.source["title"], "artist": self.source["artist"],
                    "album": "", "artist_identity_complete": True}
        self.verifier.states[VIDEO] = "unavailable"
        self.verifier.metadata.update({VIDEO: metadata, target: metadata})
        self.metadata.side_effect = lambda client, video_id, **kwargs: {**metadata, "video_id": video_id}
        result = self.validate(selected=target)[0]
        self.assertEqual(result["canonical_decision"]["reason"], "replace_unavailable_recording")
        self.assertEqual(result["canonical_decision"]["expected_video_id"], VIDEO)
        self.assertEqual(result["canonical_decision"]["selected_video_id"], target)
        self.assertEqual([call.args[1] for call in self.metadata.call_args_list], [VIDEO, target])
        self.assertEqual(self.snapshot(), before)
        self.watch.assert_not_called()

    def test_final_forced_player_read_rejects_changed_identity(self):
        self.seed()
        before = self.snapshot()
        original = self.verifier.verify
        forced = []
        def verify(video_id, **kwargs):
            evidence = original(video_id, **kwargs)
            if kwargs.get("force"):
                forced.append(video_id)
                evidence["title"] = "A changed upload"
            return evidence
        self.verifier.verify = verify
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.assertEqual(forced, [VIDEO])
        self.assertEqual(self.snapshot(), before)

    def test_manual_block_is_preserved_without_native_reads(self):
        self.seed()
        self.conn.execute("INSERT INTO manual_overrides(service,song_id,action,updated_at) VALUES (?,?,?,?)",
                          ("ytmusic", VIDEO, "block", "2026-09-15"))
        self.conn.commit()
        result = self.validate(selected="", status="manual_blocked")
        self.assertEqual(result[0]["status"], "manual_blocked")
        self.watch.assert_not_called()

    def test_native_preservation_does_not_forward_a_stale_canonical_decision(self):
        self.seed()
        result = validation.validate_matches(self.conn, service="ytmusic", sources=[self.source],
            matches=[{"song_id": VIDEO, "video_id": VIDEO, "status": "cached_match",
                      "canonical_decision": {"selected_video_id": "OtherId1111"}}],
            client=self.client, verifier=self.verifier)
        self.assertNotIn("canonical_decision", result[0])

    def test_normal_cache_path_keeps_exact_native_id_without_artist_profile_lookup(self):
        self.seed()
        before = self.snapshot()
        with patch.object(crawler_common, "get_verified_video_metadata", side_effect=AssertionError("Native ID needs no artist lookup")):
            cache = crawler_common.load_verified_matching_cache(self.conn, service="ytmusic",
                tracks=[self.source], ytmusic=self.client, read_only=True)
        self.assertEqual(cache[VIDEO]["video_id"], VIDEO)
        self.assertEqual(cache[VIDEO]["cache_origin"], "native_source_id")
        self.assertEqual(self.snapshot(), before)
        self.verifier.states[VIDEO] = "unknown"
        with self.assertRaises(validation.PlaybackBlocked):
            validation.playable_cache(cache, self.verifier)

    def test_empty_stored_identity_cannot_use_native_cache_or_preservation(self):
        self.seed()
        for title, artist in (("", "Stored artist"), ("Stored title", ""), ("  ", "Stored artist")):
            with self.subTest(title=title, artist=artist):
                self.conn.execute("UPDATE tracks SET yt_title=?,yt_artist=? WHERE track_uid='existing'",
                                  (title, artist))
                self.conn.commit()
                before = self.snapshot()
                cached = store.get_bulk_cached_matches(self.conn, service="ytmusic",
                                                       tracks=[self.source], read_only=True)
                self.assertNotIn(VIDEO, cached)
                with self.assertRaises(validation.PlaybackBlocked):
                    self.validate()
                self.assertEqual(self.snapshot(), before)
        self.watch.assert_not_called()

    def test_native_cache_exception_cannot_reuse_a_different_source_binding(self):
        self.seed(song_id="OtherSrc111")
        for service, sid in (("ytmusic", "OtherSrc111"), ("apple", VIDEO)):
            source = {**self.source, "service": service, "song_id": sid}
            if service == "apple":
                store.upsert_track_list_metadata(self.conn, service=service, song_id=sid,
                                                track_uid="existing", row=source)
                self.conn.commit()
            cache = store.get_bulk_cached_matches(self.conn, service=service, tracks=[source], read_only=True)
            self.assertNotEqual(cache.get(sid, {}).get("cache_origin"), "native_source_id")

    def test_chart_relation_preflight_keeps_native_id_before_inspecting_another_atv(self):
        self.seed()
        before = self.snapshot()
        entry = {**self.source, "original_video_id": VIDEO, "source": "youtube_charts_weekly_browse_api",
                 "atv_external_video_id": "AnotherId11"}
        with patch.object(validation, "verifier_for", return_value=self.verifier):
            result = chart.preflight_chart_relations(self.conn, [entry], self.client, "2026-W37")
        self.assertEqual(result[VIDEO]["video_id"], VIDEO)
        self.metadata.assert_not_called()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
