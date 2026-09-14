"""Exact publication, preserved slots, unknown holds and truthful rollback receipts."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hype_db
from playlist_playability_fixture import PlaylistVerifier
import sync_validation
import ytmusic_playlist_sync as sync
from test_playlist_verification import StatefulPlaylist


class MovingPlaylist(StatefulPlaylist):
    def __init__(self, values, **kwargs):
        super().__init__(values, **kwargs)
        self.moves = []
        self.lose_move_response = False

    def edit_playlist(self, playlist_id, **kwargs):
        self.edit_calls += 1
        move = kwargs.get("moveItem")
        if move is not None:
            self.moves.append(move)
            slot, target = move if isinstance(move, tuple) else (move, "")
            item = next(row for row in self._items if row["setVideoId"] == slot)
            self._items.remove(item)
            index = next((i for i, row in enumerate(self._items) if row["setVideoId"] == target), len(self._items))
            self._items.insert(index, item)
            if self.lose_move_response:
                raise TimeoutError("Move applied but response was lost")
        return {"status": "STATUS_SUCCEEDED"}


class PlaylistPlayabilitySafetyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "fixture.db"
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("external access forbidden")))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.verifier = PlaylistVerifier()
        hype_db.init_db(self.db)

    def publish(self, client, requested, **kwargs):
        return sync.update_ytmusic_playlist(client, "fixture", requested, dry_run=False,
            db_path=self.db, service="ytmusic", job_name="Fixture", playability_verifier=self.verifier, **kwargs)

    def run_record(self):
        with hype_db.connect(self.db, read_only=True) as conn:
            row = conn.execute("SELECT update_run_id FROM playlist_update_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return hype_db.get_playlist_update_run(self.db, row[0], read_only=True) if row else None

    def test_404_exact_current_list_keeps_tokens_and_all_external_mutations_zero(self):
        client = MovingPlaylist(["MUny_GDYIDM", "QbsbqekMkCU"])
        before = deepcopy(client._items)
        self.publish(client, client.video_ids, description="new description", playlist_name="new title")
        self.assertEqual(client._items, before)
        self.assertEqual((client.edit_calls, client.remove_calls, client.add_calls), (0, 0, []))
        self.assertEqual(self.run_record()["status"], "skipped_current")

    def test_reordering_preserves_all_owned_slots_and_replays_exact_move_receipts(self):
        client = MovingPlaylist(["MUny_GDYIDM", "QbsbqekMkCU", "z9ifbheDGFM"])
        before = {row["videoId"]: row["setVideoId"] for row in client._items}
        target = list(reversed(client.video_ids))
        self.publish(client, target)
        self.assertEqual(client.video_ids, target)
        self.assertEqual({row["videoId"]: row["setVideoId"] for row in client._items}, before)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))
        self.assertTrue(client.moves)
        self.assertEqual(sync._recoverable_playlist_items(self.run_record()), client._items)

    def test_unknown_or_unavailable_requested_prevents_metadata_items_and_new_audits(self):
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                client = MovingPlaylist(["old"])
                self.verifier.states["new"] = state
                with self.assertRaises(sync_validation.PlaybackBlocked):
                    self.publish(client, ["new"], description="never written")
                self.assertEqual((client.edit_calls, client.remove_calls, client.add_calls), (0, 0, []))
                self.assertIsNone(self.run_record())

    def test_retained_unavailable_flag_blocks_even_when_target_order_changes(self):
        client = MovingPlaylist(["MUny_GDYIDM", "QbsbqekMkCU"])
        client._items[0]["isAvailable"] = False
        with self.assertRaises(sync_validation.PlaybackBlocked):
            self.publish(client, list(reversed(client.video_ids)))
        self.assertEqual((client.edit_calls, client.remove_calls, client.add_calls), (0, 0, []))
        self.assertIsNone(self.run_record())

    def test_metadata_guard_rejection_propagates_without_becoming_a_warning(self):
        client = MovingPlaylist(["old"])
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise sync_validation.PlaybackBlocked("frozen metadata changed")
        with self.assertRaises(sync_validation.PlaybackBlocked):
            self.publish(client, ["new"], description="never written", before_mutation=guard)
        self.assertEqual((client.edit_calls, client.remove_calls, client.add_calls), (0, 0, []))
        self.assertEqual(self.run_record()["status"], "recovery_required")

    def test_response_lost_move_is_held_without_retry_or_restore(self):
        client = MovingPlaylist(["a", "b", "c"])
        client.lose_move_response = True
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client, ["c", "b", "a"])
        self.assertEqual(len(client.moves), 1)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))
        run = self.run_record()
        self.assertEqual(run["status"], "recovery_required")
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            sync._recoverable_playlist_items(run)

    def test_forged_move_ack_with_wrong_order_cannot_authorize_recovery(self):
        client = MovingPlaylist(["a", "b", "c"])
        self.publish(client, ["c", "b", "a"])
        run = self.run_record()
        ack = next(event for event in run["recovery_payload"]["events"] if event["state"] == "ack")
        ack["after_items"] = list(reversed(ack["after_items"]))
        with self.assertRaisesRegex(RuntimeError, "exact intent"):
            sync._recoverable_playlist_items(run)

    def test_unavailable_original_is_not_reinstated_after_wrong_provider_substitution(self):
        client = MovingPlaylist(["old"], requested_values=["new"], substitute_requested=["alias"])
        self.verifier.states["old"] = "unavailable"
        with self.assertRaises(sync_validation.PlaybackBlocked):
            self.publish(client, ["new"])
        self.assertEqual(client.video_ids, ["alias"])
        self.assertEqual(client.add_calls, [["new"]])
        run = self.run_record()
        self.assertEqual(run["status"], "recovery_required")
        self.assertFalse(run["restore_verified"])

    def test_wrong_provider_alias_does_not_publish_a_subset_and_safe_restore_is_exact(self):
        client = MovingPlaylist(["old"], requested_values=["new", "keep"], substitute_requested=["alias", "keep"])
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client, ["new", "keep"])
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual(client.add_calls, [["new", "keep"], ["old"]])
        run = self.run_record()
        self.assertEqual(run["status"], "recovery_required")
        self.assertNotEqual(run["publication_mode"], "partial")
        self.assertEqual(run["differences"][0]["reason"], "unexpected_video_id")

    def test_unknown_after_add_holds_new_state_without_unsafe_rollback(self):
        verifier = self.verifier
        class UncertainAfterAdd(MovingPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                response = super().add_playlist_items(*args, **kwargs)
                verifier.health = "unknown"
                return response
        client = UncertainAfterAdd(["old"])
        with self.assertRaises(sync_validation.PlaybackBlocked):
            self.publish(client, ["new"])
        self.assertEqual(client.video_ids, ["new"])
        self.assertEqual(client.add_calls, [["new"]])
        self.assertEqual(self.run_record()["status"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
