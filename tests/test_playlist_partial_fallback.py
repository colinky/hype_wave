"""Strict replacement supersedes legacy partial omission; audit ownership still gates recovery."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
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

    def assert_rejected(self, client):
        run = self.latest()
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual(run["status"], "recovery_required")
        self.assertEqual(run["requested_video_ids"], REQUESTED)
        self.assertNotEqual(run["publication_mode"], "partial")
        self.assertFalse(run["partial_verified"])
        self.assertTrue(run["restore_verified"])
        self.assertTrue(run["identity_review_required"])
        self.assertTrue(hype_db.get_pending_playlist_recovery(self.db, "playlist"))
        return run

    def test_bad_pair_is_rejected_without_changing_original_chart_identity(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client)
        self.assert_rejected(client)
        self.assertEqual(client.add_calls, [REQUESTED, ["old"]])
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

    def test_only_explicit_exact_full_recovery_can_clear_the_pending_request(self):
        client = self.client()
        with self.assertRaises(RuntimeError):
            self.publish(client)
        run = self.assert_rejected(client)
        # A later operator repair is observed, never inferred from a playable subset.
        client.video_ids = REQUESTED
        calls = client.remove_calls, list(client.add_calls)
        result = reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
            apply=True, workers_quiescent=True)
        self.assertEqual(result["status"], "published")
        self.assertEqual((client.remove_calls, client.add_calls), calls)
        self.assertFalse(hype_db.get_pending_playlist_recovery(self.db, "playlist"))

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
        run = self.assert_rejected(client)
        self.assertEqual([row["position"] for row in run["differences"]], [1, 2])
        self.assertTrue(all(row["reason"] == "unexpected_video_id" for row in run["differences"]))

    def test_substitutions_across_fifty_item_boundary_restore_exactly_without_partial_success(self):
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
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client, requested)
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual(client.remove_calls, 4)  # Original item, then 50 + 50 + 2 owned new items.
        run = self.latest()
        self.assertEqual(run["status"], "recovery_required")
        self.assertFalse(run["partial_verified"])
        self.assertEqual(len(run["differences"]), 51)
        self.assertEqual(publisher._recoverable_playlist_items(run), client._items)

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
        self.assert_rejected(client)
        events = self.latest()["recovery_payload"]["events"]
        self.assertFalse(any(event.get("publication_mode") == "partial" for event in events))

    def test_foreign_slot_cannot_be_erased_by_recovery(self):
        class ForeignSlot(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                self._items[1]["setVideoId"] = "foreign-slot"
                return result
        client = self.client(ForeignSlot)
        with self.assertRaisesRegex(RuntimeError, "ownership|owned"):
            self.publish(client)
        self.assertEqual(client.video_ids, EXPOSED)
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(self.latest()["status"], "recovery_required")

    def test_missing_slot_is_not_treated_as_an_excluded_substitution(self):
        class MissingSlot(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                self._items.pop()
                return result
        client = self.client(MissingSlot)
        with self.assertRaises(RuntimeError):
            self.publish(client)
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(self.latest()["status"], "recovery_required")

    def test_wrong_order_is_not_mislabeled_as_partial_publication(self):
        client = self.client()
        client.substitute_requested = ["keep-b", "ZyI5bmN2p1I", "keep-a"]
        with self.assertRaises(RuntimeError):
            self.publish(client)
        self.assertNotEqual(self.latest()["publication_mode"], "partial")

    def test_rejecting_every_song_does_not_publish_an_empty_playlist(self):
        client = StatefulPlaylist(["old"], requested_values=["new"], substitute_requested=["unverified"])
        with self.assertRaises(RuntimeError):
            self.publish(client, ["new"])
        self.assertEqual(client.video_ids, ["old"])
        self.assertNotEqual(self.latest()["status"], "published")

    def test_uncertain_restore_deletion_is_not_retried_or_auto_restored(self):
        class LostRestoreResponse(StatefulPlaylist):
            def remove_playlist_items(self, *args, **kwargs):
                result = super().remove_playlist_items(*args, **kwargs)
                if self.remove_calls == 2:
                    raise TimeoutError("restore deletion applied but response lost")
                return result
        client = self.client(LostRestoreResponse)
        with self.assertRaises(publisher.PlaylistMutationUncertain):
            self.publish(client)
        self.assertEqual(client.remove_calls, 2)
        self.assertEqual(client.add_calls, [REQUESTED])
        self.assertEqual(client.video_ids, [])
        self.assertEqual(self.latest()["status"], "recovery_required")

    def test_committed_restore_delete_crash_can_restore_owned_old_items_but_not_publish_subset(self):
        client = self.client()
        append = hype_db.append_playlist_update_evidence
        def crash_after_restore_ack(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event["phase"] == "restore" and event["operation"] == "remove" and event["state"] == "ack":
                raise SystemExit("process stopped after restore removal ACK committed")
            return result
        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=crash_after_restore_ack):
            with self.assertRaises(SystemExit):
                self.publish(client)
        self.assertEqual(client.video_ids, [])
        run = self.latest()
        report = reconcile_playlist_update(client, self.db, run["update_run_id"], "playlist",
            apply=True, workers_quiescent=True)
        self.assertEqual(report["status"], "recovery_required")
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual(client.add_calls, [REQUESTED, ["old"]])
        self.assert_rejected(client)


if __name__ == "__main__":
    unittest.main()
