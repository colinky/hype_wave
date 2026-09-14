"""Rejected additions retain the original slots; subsets never become publication success."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import hype_db
import ytmusic_playlist_sync as publisher
from reconcile_playlist_update import reconcile_playlist_update
from test_playlist_verification import StatefulPlaylist


REQUESTED = ["keep-a", "GxChUrrY4bc", "keep-b"]
EXPOSED = ["keep-a", "ZyI5bmN2p1I", "keep-b"]


class PartialFallbackTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.db = Path(directory) / "audit.db"
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(publisher.time, "sleep"))
        self.enterContext(publisher.bilingual_cache_read_only())

    def client(self, client_type=StatefulPlaylist):
        return client_type(["old"], requested_values=REQUESTED, substitute_requested=EXPOSED)

    def publish(self, client, requested=None):
        return publisher.update_ytmusic_playlist(client, "playlist", requested or REQUESTED, dry_run=False,
            db_path=self.db, service="apple", job_name="Fixture-Daily")

    def latest(self):
        with hype_db.connect(self.db, read_only=True) as conn:
            run_id = conn.execute("SELECT update_run_id FROM playlist_update_runs ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()[0]
        return hype_db.get_playlist_update_run(self.db, run_id, read_only=True)

    def assert_rejected(self, client, exposed=EXPOSED):
        run = self.latest()
        before = publisher._audit_playlist_items(run["existing_items"])
        self.assertEqual(client._items[:len(before)], before)
        self.assertEqual(client.video_ids, ["old", *exposed])
        self.assertEqual(client.remove_calls, 0)
        self.assertEqual(client.edit_calls, 0)
        self.assertEqual(run["status"], "recovery_required")
        self.assertEqual(run["requested_video_ids"], REQUESTED)
        self.assertEqual(run["effective_video_ids"], REQUESTED)
        self.assertNotEqual(run["publication_mode"], "partial")
        self.assertFalse(run["partial_verified"])
        self.assertFalse(run["restore_verified"])
        self.assertTrue(run["identity_review_required"])
        self.assertTrue(hype_db.get_pending_playlist_recovery(self.db, "playlist"))
        return run

    def test_bad_pair_is_rejected_without_changing_original_chart_identity(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client)
        self.assert_rejected(client)
        self.assertEqual(client.add_calls, [REQUESTED])
        with hype_db.connect(self.db, read_only=True) as conn:
            for table in ("tracks", "yt_video_ids", "platform_song_ids", "playlist_order"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_same_bad_pair_next_run_is_blocked_before_any_new_mutation(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            self.publish(client)
        first = self.assert_rejected(client)
        calls = client.remove_calls, list(client.add_calls)
        with self.assertRaisesRegex(RuntimeError, "pending recovery"):
            self.publish(client)
        self.assertEqual((client.remove_calls, client.add_calls), calls)
        self.assertEqual(self.latest()["update_run_id"], first["update_run_id"])

    def test_explicit_complete_cannot_retry_rejected_pair_or_publish_subset(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            self.publish(client)
        run = self.assert_rejected(client)
        owned = deepcopy(client._items)
        # Even a playable subset of owned additions cannot complete the request.
        client._items = [row for row in owned if row["videoId"] in {"keep-a", "keep-b"}]
        preview = reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist")
        self.assertEqual(preview["action"], "blocked")
        self.assertEqual(client.add_calls, [REQUESTED])
        self.assertEqual(client.remove_calls, 0)
        client._items = owned
        # Quiescence and exact ownership do not authorize retrying a rejected identity.
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
                apply=True, workers_quiescent=True, complete_requested=True)
        self.assertEqual(client._items, owned)
        self.assertEqual(client.add_calls, [REQUESTED])
        self.assert_rejected(client)

    def test_missing_substitution_metadata_cannot_justify_omission(self):
        client = self.client()
        client.get_song = Mock(side_effect=AssertionError("Do not infer a publication alias from metadata"))
        with self.assertRaises(RuntimeError):
            self.publish(client)
        self.assert_rejected(client)
        client.get_song.assert_not_called()

    def test_even_equivalent_aliases_are_rejected_as_a_full_request_mismatch(self):
        client = self.client()
        client.substitute_requested = ["keep-a-playable", "ZyI5bmN2p1I", "keep-b"]
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client)
        run = self.assert_rejected(client, client.substitute_requested)
        rejection = next(event for event in run["recovery_payload"]["events"]
                         if event.get("identity_review_required"))
        self.assertEqual([(row["expected_id"], row["actual_id"]) for row in rejection["differences"]],
                         [("keep-a", "keep-a-playable"), ("GxChUrrY4bc", "ZyI5bmN2p1I")])
        self.assertTrue(all(row["reason"] == "unexpected_video_id" for row in rejection["differences"]))

    def test_first_substituted_chunk_blocks_remaining_additions_without_partial_success(self):
        requested = [f"song-{index:03d}" for index in range(102)]
        rejected = set(requested[:51])
        class ManySubstitutions(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                for item in self._items:
                    if item["videoId"] in rejected:
                        item["videoId"] = "substituted-" + item["videoId"]
                return result
        client = ManySubstitutions(["old"])
        before = deepcopy(client._items)
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client, requested)
        self.assertEqual(client._items[:len(before)], before)
        self.assertEqual(client.video_ids, ["old", *["substituted-" + video for video in requested[:50]]])
        self.assertEqual(client.add_calls, [requested[:50]])
        self.assertEqual(client.remove_calls, 0)
        self.assertEqual(client.edit_calls, 0)
        run = self.latest()
        self.assertEqual(run["status"], "recovery_required")
        self.assertFalse(run["partial_verified"])
        self.assertEqual(run["requested_video_ids"], requested)
        rejection = next(event for event in run["recovery_payload"]["events"]
                         if event.get("identity_review_required"))
        self.assertEqual(len(rejection["differences"]), 50)
        self.assertEqual(publisher._playlist_slots(publisher._recoverable_playlist_items(run)),
                         publisher._playlist_slots(client._items))
        ack = next(event for event in run["recovery_payload"]["events"] if event["state"] == "ack")
        self.assertEqual([row["videoId"] for row in publisher._audit_playlist_items(ack["items"])], requested[:50])

    def test_same_owned_slot_alias_change_after_rejection_never_uses_partial_plan(self):
        client = self.client()
        append = hype_db.append_playlist_update_evidence
        def changed_after_rejection(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event.get("identity_review_required") and event.get("verification_matches") is False:
                client._items[1]["videoId"] = "JJx_WQXOeK0"
            return result
        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=changed_after_rejection):
            with self.assertRaises(RuntimeError):
                self.publish(client)
        self.assert_rejected(client, ["JJx_WQXOeK0", *EXPOSED[1:]])
        events = self.latest()["recovery_payload"]["events"]
        self.assertFalse(any(event.get("publication_mode") == "partial" for event in events))

    def test_foreign_slot_cannot_be_erased_by_recovery(self):
        class ForeignSlot(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                self._items[1]["setVideoId"] = "foreign-slot"
                return result
        client = self.client(ForeignSlot)
        with self.assertRaises(publisher.PlaylistMutationUncertain):
            self.publish(client)
        self.assertEqual(client.video_ids, ["old", *EXPOSED])
        self.assertEqual(client._items[0], {"videoId": "old", "setVideoId": "set-1"})
        self.assertEqual(client._items[1]["setVideoId"], "foreign-slot")
        self.assertEqual(client.remove_calls, 0)
        self.assertEqual(self.latest()["status"], "recovery_required")
        report = reconcile_playlist_update(client, self.db, self.latest()["update_run_id"], "playlist")
        self.assertEqual(report["action"], "blocked")
        self.assertEqual(client.remove_calls, 0)

    def test_missing_slot_is_not_treated_as_an_excluded_substitution(self):
        class MissingSlot(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                self._items.pop()
                return result
        client = self.client(MissingSlot)
        with self.assertRaises(RuntimeError):
            self.publish(client)
        self.assertEqual(client._items[0], {"videoId": "old", "setVideoId": "set-1"})
        self.assertEqual(client.video_ids, ["old", *EXPOSED[:-1]])
        self.assertEqual(client.remove_calls, 0)
        self.assertEqual(self.latest()["status"], "recovery_required")
        self.assertFalse(self.latest()["partial_verified"])

    def test_wrong_order_is_not_mislabeled_as_partial_publication(self):
        client = self.client()
        client.substitute_requested = ["keep-b", "ZyI5bmN2p1I", "keep-a"]
        with self.assertRaises(RuntimeError):
            self.publish(client)
        self.assert_rejected(client, client.substitute_requested)

    def test_rejecting_every_song_does_not_publish_an_empty_playlist(self):
        client = StatefulPlaylist(["old"], requested_values=["new"], substitute_requested=["unverified"])
        with self.assertRaises(RuntimeError):
            self.publish(client, ["new"])
        self.assertEqual(client.video_ids, ["old", "unverified"])
        self.assertEqual(client._items[0], {"videoId": "old", "setVideoId": "set-1"})
        self.assertEqual(client.remove_calls, 0)
        self.assertNotEqual(self.latest()["status"], "published")

    def test_uncertain_restore_deletion_is_not_retried_or_auto_restored(self):
        class LostRestoreResponse(StatefulPlaylist):
            def remove_playlist_items(self, *args, **kwargs):
                result = super().remove_playlist_items(*args, **kwargs)
                if self.remove_calls == 1:
                    raise TimeoutError("restore deletion applied but response lost")
                return result
        client = self.client(LostRestoreResponse)
        with self.assertRaises(publisher.PlaylistMutationUncertain):
            self.publish(client)
        run = self.assert_rejected(client)
        with self.assertRaises(publisher.PlaylistMutationUncertain):
            reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
                apply=True, workers_quiescent=True)
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(client.add_calls, [REQUESTED])
        self.assertEqual(client._items, publisher._audit_playlist_items(run["existing_items"]))
        self.assertEqual(self.latest()["status"], "recovery_required")
        with self.assertRaises(RuntimeError):
            reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
                apply=True, workers_quiescent=True)
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(client.add_calls, [REQUESTED])

    def test_committed_explicit_cleanup_crash_preserves_originals_and_identity_hold(self):
        client = self.client()
        with self.assertRaises(publisher.PlaylistMutationUncertain):
            self.publish(client)
        run = self.assert_rejected(client)
        append = hype_db.append_playlist_update_evidence
        def crash_after_restore_ack(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event["phase"] == "restore" and event["operation"] == "remove" and event["state"] == "ack":
                raise SystemExit("process stopped after restore removal ACK committed")
            return result
        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=crash_after_restore_ack):
            with self.assertRaises(SystemExit):
                reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
                    apply=True, workers_quiescent=True)
        self.assertEqual(client._items, publisher._audit_playlist_items(run["existing_items"]))
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(client.add_calls, [REQUESTED])
        report = reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist")
        self.assertEqual(report["action"], "blocked")
        self.assertTrue(report["identity_review_required"])
        self.assertFalse(self.latest()["partial_verified"])
        self.assertTrue(hype_db.get_pending_playlist_recovery(self.db, "playlist"))


if __name__ == "__main__":
    unittest.main()
