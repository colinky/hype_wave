"""Successful mutations may lag in reads; uncertain writes are never replayed."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import hype_db
from reconcile_playlist_update import reconcile_playlist_update
import ytmusic_playlist_sync as sync
from test_playlist_playability_safety import MovingPlaylist


class LaggingPlaylist(MovingPlaylist):
    def __init__(self, values, operation="move", lag=2, error=None):
        super().__init__(values)
        self.operation, self.lag, self.error = operation, lag, error
        self.pending_reads = []
        self.transition_reads = 0

    def delay(self, before):
        self.pending_reads = [deepcopy(before) for _ in range(self.lag)]
        if self.error:
            self.pending_reads[0] = self.error

    def get_playlist(self, *args, **kwargs):
        if self.pending_reads:
            self.transition_reads += 1
            value = self.pending_reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return {"tracks": deepcopy(value)}
        return super().get_playlist(*args, **kwargs)

    def edit_playlist(self, *args, **kwargs):
        before = deepcopy(self._items)
        result = super().edit_playlist(*args, **kwargs)
        if "moveItem" in kwargs and self.operation == "move":
            self.delay(before)
        return result

    def remove_playlist_items(self, *args, **kwargs):
        before = deepcopy(self._items)
        result = super().remove_playlist_items(*args, **kwargs)
        if self.operation == "remove":
            self.delay(before)
        return result

    def add_playlist_items(self, *args, **kwargs):
        before = deepcopy(self._items)
        result = super().add_playlist_items(*args, **kwargs)
        if self.operation == "add":
            self.delay(before)
        return result


class PlaylistTransitionVisibilityTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("external access forbidden")))
        self.sleep = self.enterContext(patch.object(sync.time, "sleep"))
        self.events = []

    def evidence(self, event):
        event = {**deepcopy(event), "seq": len(self.events) + 1}
        self.events.append(event)
        return event

    def preserve(self, client, target):
        return sync._preserve_playlist_slots(client, "fixture", deepcopy(client._items),
            target, evidence=self.evidence)

    def test_successful_move_old_old_after_records_one_ack_without_replaying(self):
        client = LaggingPlaylist(["a", "b"])
        slots = {row["videoId"]: row["setVideoId"] for row in client._items}
        actual = self.preserve(client, ["b", "a"])
        self.assertEqual([row["videoId"] for row in actual], ["b", "a"])
        self.assertEqual({row["videoId"]: row["setVideoId"] for row in actual}, slots)
        self.assertEqual(len(client.moves), 1)
        self.assertEqual([row["state"] for row in self.events], ["intent", "ack"])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.5, 1.0])

    def test_replacing_first_of_100_tracks_moves_only_new_slot_and_preserves_other_99(self):
        client = LaggingPlaylist([f"song-{i}" for i in range(100)], lag=0)
        before = deepcopy(client._items)
        target = ["replacement"] + client.video_ids[1:]
        guard = Mock()
        actual = sync._preserve_playlist_slots(client, "fixture", before, target,
            evidence=self.evidence, before_mutation=guard)
        self.assertEqual([item["videoId"] for item in actual], target)
        self.assertEqual(actual[1:], before[1:])
        self.assertEqual((client.remove_calls, client.add_calls, len(client.moves)), (1, [["replacement"]], 1))
        self.assertEqual(guard.call_count, 3)  # Remove, add and one move only.

    def test_missing_tail_needs_no_move_and_reverse_needs_four_moves(self):
        client = LaggingPlaylist(["a", "b", "c", "d"], lag=0)
        self.preserve(client, ["a", "b", "c", "d", "e"])
        self.assertEqual(len(client.moves), 0)
        self.preserve(client, ["e", "d", "c", "b", "a"])
        self.assertEqual(len(client.moves), 4)

    def test_temporary_read_timeout_after_success_only_retries_read(self):
        client = LaggingPlaylist(["a", "b"], error=TimeoutError("read timed out"))
        self.preserve(client, ["b", "a"])
        self.assertEqual(len(client.moves), 1)
        self.assertEqual(self.events[-1]["state"], "ack")

    def test_unchanged_move_after_three_reads_remains_ambiguous(self):
        client = LaggingPlaylist(["a", "b"], lag=5)
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.preserve(client, ["b", "a"])
        self.assertEqual((len(client.moves), client.transition_reads), (1, 3))
        self.assertEqual([row["state"] for row in self.events], ["intent", "ambiguous"])

    def test_unexpected_id_or_foreign_slot_is_not_treated_as_a_delayed_read(self):
        before = [{"videoId": "a", "setVideoId": "A"}, {"videoId": "b", "setVideoId": "B"}]
        after = list(reversed(before))
        for field, value in (("videoId", "alias"), ("setVideoId", "FOREIGN")):
            with self.subTest(field=field):
                actual = deepcopy(after)
                actual[0][field] = value
                client = Mock()
                client.get_playlist.return_value = {"tracks": actual}
                with self.assertRaisesRegex(RuntimeError, "unexpected"):
                    sync._observe_playlist_transition(client, "fixture", before, after)
                self.assertEqual(client.get_playlist.call_count, 1)
        self.sleep.assert_not_called()

    def test_malformed_or_auth_denied_read_is_not_retried(self):
        for response in ({"tracks": [{"videoId": "a"}]}, PermissionError("403 denied")):
            client = Mock()
            if isinstance(response, Exception):
                client.get_playlist.side_effect = response
            else:
                client.get_playlist.return_value = response
            with self.assertRaises(RuntimeError):
                sync._observe_playlist_transition(client, "fixture", [], [{"videoId": "a", "setVideoId": "A"}])
            self.assertEqual(client.get_playlist.call_count, 1)
        self.sleep.assert_not_called()

    def test_count_lag_never_hides_foreign_or_malformed_items(self):
        before = [{"videoId": str(i), "setVideoId": f"slot-{i}"} for i in range(5)]
        after = before[:-1]
        invalid = [
            [*after[:-1], {"videoId": "other", "setVideoId": "foreign"}],
            [*after[:-1], {"videoId": "other", "setVideoId": "slot-3"}],
            list(reversed(after)), after[:-1],
            [*after[:-1], {"videoId": "3"}],
            [*after[:-1], {"videoId": "3", "setVideoId": "slot-0"}],
        ]
        for actual in invalid:
            with self.subTest(actual=actual):
                client = Mock()
                client.get_playlist.side_effect = [
                    {"tracks": actual, "trackCount": 5}, {"tracks": after, "trackCount": 4}]
                with self.assertRaises(RuntimeError):
                    sync._observe_playlist_transition(client, "fixture", before, after,
                                                      require_exact_ids=False)
                self.assertEqual(client.get_playlist.call_count, 1)
        self.sleep.assert_not_called()

    def test_persistent_count_lag_is_not_accepted_after_three_reads(self):
        before = [{"videoId": str(i), "setVideoId": f"slot-{i}"} for i in range(5)]
        after = before[:-1]
        client = Mock()
        client.get_playlist.return_value = {"tracks": after, "trackCount": 5}
        with self.assertRaises(sync.PlaylistMutationUncertain):
            sync._observe_playlist_transition(client, "fixture", before, after)
        self.assertEqual(client.get_playlist.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_sqlite_remove_count_lag_retries_only_known_state_and_never_replays(self):
        class CountLaggingPlaylist(MovingPlaylist):
            bad_seen = False

            def get_playlist(self, *args, **kwargs):
                result = super().get_playlist(*args, **kwargs)
                result["trackCount"] = len(result["tracks"])
                if self.remove_calls and not self.bad_seen:
                    self.bad_seen = True
                    result["trackCount"] += 1
                    if foreign:
                        result["tracks"][-1] = {"videoId": "other", "setVideoId": "foreign"}
                return result

        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as directory, patch.dict(
                    os.environ, {"SUPABASE_DB_URL": ""}):
                db = Path(directory) / "offline.db"
                hype_db.init_db(db)
                client = CountLaggingPlaylist([str(i) for i in range(5)])
                retained = deepcopy(client._items[:-1])
                def publish():
                    return sync.update_ytmusic_playlist(client, "fixture", [str(i) for i in range(4)],
                        dry_run=False, db_path=db, service="ytmusic", job_name="Count lag fixture")
                if foreign:
                    with self.assertRaises(sync.PlaylistMutationUncertain):
                        publish()
                else:
                    publish()
                self.assertEqual(client._items, retained)
                self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (1, [], 0))
                with hype_db.connect(db, read_only=True) as conn:
                    run_id = conn.execute("SELECT update_run_id FROM playlist_update_runs").fetchone()[0]
                run = hype_db.get_playlist_update_run(db, run_id, read_only=True)
                self.assertEqual(run["status"], "recovery_required" if foreign else "published")
                self.assertEqual([e["state"] for e in run["recovery_payload"]["events"]], ["intent", "ack"])
                if foreign:
                    before_bytes = db.read_bytes()
                    client.get_playlist = Mock(side_effect=AssertionError("Pending must block before API"))
                    with self.assertRaises(sync.PlaylistMutationUncertain):
                        publish()
                    client.get_playlist.assert_not_called()
                    self.assertEqual(db.read_bytes(), before_bytes)

    def test_lost_move_response_never_enters_read_confirmation_or_replay(self):
        client = LaggingPlaylist(["a", "b"])
        client.lose_move_response = True
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.preserve(client, ["b", "a"])
        self.assertEqual((len(client.moves), client.transition_reads), (1, 0))
        self.assertEqual(self.events[-1]["state"], "ambiguous")

    def test_add_and_remove_lag_wait_for_owned_state_before_next_write(self):
        for operation in ("add", "remove"):
            with self.subTest(operation=operation):
                self.events.clear()
                client = LaggingPlaylist(["old"], operation=operation)
                result = self.preserve(client, ["new"])
                self.assertEqual([row["videoId"] for row in result], ["new"])
                self.assertEqual((client.remove_calls, client.add_calls), (1, [["new"]]))
                self.assertEqual([row["state"] for row in self.events], ["intent", "ack", "intent", "ack"])

    def test_add_or_remove_visibility_timeout_keeps_real_receipt_without_new_mutation(self):
        for operation in ("add", "remove"):
            with self.subTest(operation=operation):
                self.events.clear()
                client = LaggingPlaylist(["old"], operation=operation, lag=5)
                with self.assertRaises(sync.PlaylistMutationUncertain):
                    self.preserve(client, ["new"])
                self.assertEqual(client.remove_calls, 1)
                self.assertEqual(client.add_calls, [["new"]] if operation == "add" else [])
                self.assertEqual(self.events[-1]["state"], "ack")
                self.assertFalse(any(row["state"] == "ambiguous" for row in self.events))

    def test_sqlite_delayed_move_confirmation_and_forward_completion_keep_original_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
            db = Path(directory) / "offline.db"
            hype_db.init_db(db)
            client = LaggingPlaylist(["a", "b", "c"], lag=5)
            target = ["c", "b", "a"]
            original_tokens = {item["videoId"]: item["setVideoId"] for item in client._items}
            with self.assertRaises(sync.PlaylistMutationUncertain):
                sync.update_ytmusic_playlist(client, "fixture", target, dry_run=False, db_path=db,
                    service="ytmusic", job_name="Offline visibility fixture")
            with hype_db.connect(db, read_only=True) as conn:
                run_id = conn.execute("SELECT update_run_id FROM playlist_update_runs").fetchone()[0]
            before = hype_db.get_playlist_update_run(db, run_id, read_only=True)
            self.assertEqual(before["status"], "recovery_required")
            self.assertEqual(len(client.moves), 1)
            client.pending_reads = []
            client.lag = 0
            result = reconcile_playlist_update(client, db, run_id, "fixture", apply=True,
                workers_quiescent=True, confirm_observed_move=True, complete_requested=True)
            self.assertEqual(result["status"], "published")
            self.assertEqual(client.video_ids, target)
            self.assertEqual({item["videoId"]: item["setVideoId"] for item in client._items}, original_tokens)
            after = hype_db.get_playlist_update_run(db, run_id, read_only=True)
            self.assertEqual(after["status"], "published")
            prior_events = before["recovery_payload"]["events"]
            self.assertEqual(after["recovery_payload"]["events"][:len(prior_events)], prior_events)
            observations = [event for event in after["recovery_payload"]["events"]
                            if event.get("reconciliation_action") == "confirm_observed_move"]
            self.assertEqual(len(observations), 1)
            self.assertEqual(sync._recoverable_playlist_items(after), client._items)
            self.assertEqual((client.remove_calls, client.add_calls), (0, []))


if __name__ == "__main__":
    unittest.main()
