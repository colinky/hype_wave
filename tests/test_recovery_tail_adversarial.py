"""Independent legacy tail-append fault injection; no production database or network."""
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hype_db
import ytmusic_playlist_sync as publisher
from reconcile_playlist_update import reconcile_playlist_update
from test_playlist_verification import StatefulPlaylist


class LegacyTailAdversarialTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.db = Path(directory) / "legacy.db"
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(publisher.time, "sleep"))
        self.run_id = hype_db.record_playlist_update(
            self.db, playlist_id="playlist", service="apple", job_name="Fixture-Daily",
            requested_video_ids=["a", "b", "tail"], existing_video_ids=["old"],
        )
        hype_db.finish_playlist_update(self.db, self.run_id, status="recovery_required", error="original legacy failure")

    def reconcile(self, client, **kwargs):
        return reconcile_playlist_update(client, self.db, self.run_id, "playlist", append_missing_last=True, **kwargs)

    def test_dry_run_then_append_preserves_prefix_tokens_and_legacy_version(self):
        client = StatefulPlaylist(["a", "b"], requested_values=["tail"])
        original_prefix = list(client._items)
        before = self.db.read_bytes()
        self.reconcile(client)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))
        result = self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(result["status"], "published")
        self.assertEqual(client._items[:2], original_prefix)
        self.assertEqual(client.video_ids, ["a", "b", "tail"])
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))
        run = hype_db.get_playlist_update_run(self.db, self.run_id)
        self.assertEqual(run["evidence_version"], 0)
        self.assertFalse(run["evidence_complete"])
        self.assertTrue(any(row.get("error") == "original legacy failure" for row in run["recovery_payload"]["outcomes"]))
        with self.assertRaises(RuntimeError):
            self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(client.add_calls, [["tail"]])

    def test_wrong_middle_foreign_reordered_and_short_prefixes_never_append(self):
        for values in (["a", "tail"], ["foreign", "b"], ["b", "a"], ["a"]):
            client = StatefulPlaylist(list(values), requested_values=["tail"])
            with self.subTest(values=values):
                result = self.reconcile(client)
                self.assertEqual(result["action"], "blocked", result)
                with self.assertRaises(RuntimeError):
                    self.reconcile(client, apply=True, workers_quiescent=True)
                self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def test_same_ids_with_changed_tokens_after_planning_never_append(self):
        class ReplacedPrefix(StatefulPlaylist):
            reads = 0

            def get_playlist(self, *args, **kwargs):
                self.reads += 1
                if self.reads == 2:
                    self.video_ids = list(self.video_ids)
                return super().get_playlist(*args, **kwargs)

        client = ReplacedPrefix(["a", "b"], requested_values=["tail"])
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def test_same_title_but_different_performer_prefix_never_appends(self):
        client = StatefulPlaylist(["wrong-a", "b"], requested_values=["tail"])
        original_song = client.get_song

        def song(video_id):
            row = original_song(video_id)
            if video_id in {"a", "wrong-a"}:
                row["videoDetails"]["title"] = "Same Song Title"
            return row

        client.get_song = song
        client.get_watch_playlist = lambda videoId, **kwargs: {"tracks": [{
            "videoId": videoId, "title": song(videoId)["videoDetails"]["title"],
            "artists": [{"id": "performer-a" if videoId == "a" else "performer-b", "name": "Artist"}],
        }]}
        client.get_artist = lambda artist_id: {"name": artist_id}
        with patch.object(publisher, "make_ytmusic", return_value=client):
            result = self.reconcile(client)
            self.assertEqual(result["action"], "blocked")
            self.assertEqual(result["requested_comparison"]["differences"][0]["reason"], "unexpected_video_id")
            with self.assertRaises(RuntimeError):
                self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def test_lost_response_after_application_blocks_finalization_and_second_append(self):
        class LostResponse(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                super().add_playlist_items(*args, **kwargs)
                raise TimeoutError("lost append response after provider applied tail")

        client = LostResponse(["a", "b"], requested_values=["tail"])
        with self.assertRaises(RuntimeError):
            self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(client.video_ids, ["a", "b", "tail"])
        before = self.db.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unresolved mutation"):
            self.reconcile(client, apply=True, workers_quiescent=True, reclaim_recovery=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))

    def test_unresolved_attempt_never_reappends_even_if_tail_is_still_missing(self):
        class DelayedResponse(StatefulPlaylist):
            def add_playlist_items(self, _playlist_id, video_ids, **kwargs):
                self.add_calls.append(list(video_ids))
                raise TimeoutError("provider may apply later; no receipt")

        client = DelayedResponse(["a", "b"], requested_values=["tail"])
        with self.assertRaises(RuntimeError):
            self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(client.video_ids, ["a", "b"])
        with self.assertRaises(RuntimeError):
            self.reconcile(client, apply=True, workers_quiescent=True, reclaim_recovery=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))

    def test_lost_receipt_persistence_does_not_duplicate_the_added_tail(self):
        client = StatefulPlaylist(["a", "b"], requested_values=["tail"])
        append = hype_db.append_playlist_update_evidence

        def lose_ack(db, run_id, event, **kwargs):
            if event["operation"] == "add" and event["state"] == "ack":
                raise RuntimeError("receipt commit failed")
            return append(db, run_id, event, **kwargs)

        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=lose_ack):
            with self.assertRaises(RuntimeError):
                self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(client.video_ids, ["a", "b", "tail"])
        before = self.db.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unresolved mutation"):
            self.reconcile(client, apply=True, workers_quiescent=True, reclaim_recovery=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))

    def test_wrong_provider_tail_keeps_fresh_identity_rejection_evidence(self):
        client = StatefulPlaylist(["a", "b"], requested_values=["tail"], substitute_requested=["wrong-tail"])
        with self.assertRaises(RuntimeError):
            self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertEqual(client.video_ids, ["a", "b", "wrong-tail"])
        run = hype_db.get_playlist_update_run(self.db, self.run_id)
        rejected = [row for row in run["differences"] if row.get("actual_id") == "wrong-tail"]
        self.assertTrue(rejected, run["differences"])
        self.assertEqual(rejected[0]["reason"], "unexpected_video_id")
        self.assertTrue(run["identity_review_required"])
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))


if __name__ == "__main__":
    unittest.main()
