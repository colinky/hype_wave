"""Explicit delayed-move observation; no database or provider calls are allowed."""
from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

import hype_db
import hype_db_store as store
import reconcile_playlist_update as recovery
import ytmusic_playlist_sync as sync
from sync_validation import PlaybackBlocked
from test_playlist_verification import StatefulPlaylist


class ObservedMoveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect", "hype_db.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("External access forbidden")))
        self.prepare()
        self.enterContext(patch.object(hype_db, "get_playlist_update_run", side_effect=lambda *a, **kw: deepcopy(self.run)))

        def claim(*args, **kwargs):
            self.assertEqual(kwargs["expected_claim_token"], "publisher-token")
            self.assertEqual(kwargs["expected_state_fingerprint"], "before-fingerprint")
            self.run["recovery_payload"]["claim"] = {"role": "reconcile", "token": "recovery-token"}
            return {"claim_token": "recovery-token", "state_fingerprint": "claimed-fingerprint"}

        def append(_db, _run_id, event, **kwargs):
            self.assertEqual(kwargs["claim_token"], "recovery-token")
            normalized = store._normalize_playlist_evidence_event(event)
            store._assert_receipt_matches_intent(self.run["recovery_payload"], normalized)
            events = self.run["recovery_payload"]["events"]
            stored = {**normalized, "seq": max((item["seq"] for item in events), default=0) + 1}
            events.append(stored)
            return {**stored, "state_fingerprint": f"state-{stored['seq']}"}

        def finish(_db, _run_id, **kwargs):
            self.run["status"] = kwargs["status"]

        self.claim = self.enterContext(patch.object(hype_db, "claim_playlist_update_recovery", side_effect=claim))
        self.append = self.enterContext(patch.object(hype_db, "append_playlist_update_evidence", side_effect=append))
        self.finish = self.enterContext(patch.object(hype_db, "finish_playlist_update", side_effect=finish))

    def prepare(self, *, requested=None, ambiguous=True):
        self.client = StatefulPlaylist(["a", "b", "c"])
        before = deepcopy(self.client._items)
        after = [before[1], before[2], before[0]]
        self.client._items = deepcopy(after)
        self.client.get_playlist = Mock(wraps=self.client.get_playlist)
        self.intent = store._normalize_playlist_evidence_event({
            "phase": "publish", "operation": "move", "state": "intent", "chunk_order": 1, "attempt": 1,
            "items": [before[0]], "target_before_set_video_id": "", "before_items": before, "after_items": after,
        })
        self.intent["seq"] = 1
        events = [self.intent]
        if ambiguous:
            receipt = store._normalize_playlist_evidence_event({**self.intent, "state": "ambiguous", "intent_seq": 1,
                                                                 "error": "Result was not yet observable"})
            events.append({**receipt, "seq": 2})
        self.run = {
            "playlist_id": "fixture", "status": "recovery_required", "evidence_version": 1,
            "requested_video_ids": requested or ["c", "b", "a"], "requested_count": 3,
            "existing_video_ids": ["a", "b", "c"], "existing_count": 3,
            "claim_token": "publisher-token", "state_fingerprint": "before-fingerprint",
            "recovery_payload": {"version": 1, "snapshot": {"complete": True,
                "existing_items": self.intent["before_items"], "requested_video_ids": requested or ["c", "b", "a"]},
                "claim": {"role": "publisher", "token": "publisher-token"}, "events": events},
        }

    def reconcile(self, *, apply=True, confirm=True, workers=True, **kwargs):
        return recovery.reconcile_playlist_update(
            self.client, "never-open.db", "run", "fixture", apply=apply,
            workers_quiescent=workers, confirm_observed_move=confirm, **kwargs,
        )

    def assert_no_provider_mutations(self):
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (0, 0, []))

    def test_default_reconcile_does_not_resolve_an_unacknowledged_move(self):
        report = self.reconcile(apply=False, confirm=False)
        self.assertEqual(report["action"], "blocked")
        with self.assertRaises(RuntimeError):
            self.reconcile(confirm=False)
        self.claim.assert_not_called()
        self.append.assert_not_called()
        self.assert_no_provider_mutations()

    def test_readonly_confirmation_plans_owned_recovery_after_two_exact_observations(self):
        report = self.reconcile(apply=False)
        self.assertEqual(report["action"], "restore_owned_items")
        self.assertEqual(report["confirmed_move_intent_seq"], 1)
        self.assertEqual(self.client.get_playlist.call_count, 2)
        self.claim.assert_not_called()
        self.append.assert_not_called()
        self.assert_no_provider_mutations()

    def test_explicit_confirmation_preserves_ambiguity_then_restores_owned_items(self):
        slots = {item["videoId"]: item["setVideoId"] for item in self.client._items}
        report = self.reconcile()
        self.assertEqual(report["status"], "restored")
        self.assertEqual(self.client.video_ids, ["a", "b", "c"])
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (1, 0, []))
        self.assertEqual({item["videoId"]: item["setVideoId"] for item in self.client._items}, slots)
        events = self.run["recovery_payload"]["events"]
        self.assertEqual(events[1]["state"], "ambiguous")
        observed = events[2]
        self.assertEqual((observed["phase"], observed["operation"], observed["state"]), ("reconcile", "observe", "verified"))
        self.assertEqual(observed["reconciliation_action"], "confirm_observed_move")
        self.assertEqual(observed["attestation"], "workers_quiescent=true; explicit_confirmation=true")
        self.assertEqual(len(observed["review_checks"]), 2)
        self.assertEqual(sync._recoverable_playlist_items(self.run), self.client._items)

    def test_complete_requested_state_can_finalize_without_replaying_a_lost_move(self):
        self.prepare(requested=["b", "c", "a"], ambiguous=False)
        report = self.reconcile()
        self.assertEqual(report["status"], "published")
        self.assert_no_provider_mutations()
        self.assertEqual([event["operation"] for event in self.run["recovery_payload"]["events"]],
                         ["move", "observe", "observe"])

    def test_complete_requested_moves_only_remaining_owned_slot_into_original_order(self):
        slots = {item["videoId"]: item["setVideoId"] for item in self.client._items}
        report = self.reconcile(complete_requested=True)
        self.assertEqual(report["action"], "complete_requested")
        self.assertEqual(report["status"], "published")
        self.assertEqual(self.client.video_ids, ["c", "b", "a"])
        self.assertEqual({item["videoId"]: item["setVideoId"] for item in self.client._items}, slots)
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (1, 0, []))
        events = self.run["recovery_payload"]["events"]
        new_moves = [event for event in events if event["operation"] == "move" and event["state"] == "intent" and event["seq"] != 1]
        self.assertEqual(len(new_moves), 1)
        self.assertEqual(new_moves[0]["items"][0]["video_id"], "c")
        self.assertEqual(sync._recoverable_playlist_items(self.run), self.client._items)

    def test_forward_recovery_uses_playable_requested_ids_without_restoring_bad_baseline(self):
        bad = [{"videoId": "bad", "setVideoId": "old-bad-slot"}]
        before = sync._audit_playlist_items(self.intent["before_items"])
        prefix = [
            {"operation": "remove", "state": "intent", "items": bad, "before_items": bad},
            {"operation": "remove", "state": "ack", "intent_seq": 1, "items": bad, "after_items": []},
            {"operation": "add", "state": "intent", "items": [{"videoId": value} for value in ("a", "b", "c")], "before_items": []},
            {"operation": "add", "state": "ack", "intent_seq": 3, "items": before, "after_items": before},
        ]
        events = [{**store._normalize_playlist_evidence_event({"phase": "publish", "chunk_order": 1, "attempt": 1, **event}), "seq": seq}
                  for seq, event in enumerate(prefix, 1)]
        events.extend([{**self.intent, "seq": 5}, {**self.run["recovery_payload"]["events"][1], "seq": 6, "intent_seq": 5}])
        self.run.update(existing_count=1, existing_video_ids=["bad"])
        self.run["recovery_payload"]["snapshot"]["existing_items"] = events[0]["before_items"]
        self.run["recovery_payload"]["events"] = events
        self.client._hype_playability_verifier.states["bad"] = "unavailable"
        self.assertEqual(self.reconcile(apply=False)["action"], "blocked")
        self.append.assert_not_called()
        report = self.reconcile(complete_requested=True)
        self.assertEqual(report["status"], "published")
        self.assertEqual(self.client.video_ids, ["c", "b", "a"])
        self.assertEqual((self.client.remove_calls, self.client.add_calls), (0, []))
        self.assertEqual(sync._recoverable_playlist_items(self.run), self.client._items)

    def test_complete_requested_needs_resolved_ownership_and_playable_entire_request(self):
        with self.assertRaisesRegex(RuntimeError, "complete owned slots"):
            self.reconcile(confirm=False, complete_requested=True)
        self.assert_no_provider_mutations()
        self.claim.assert_not_called()
        self.prepare(requested=["b", "c", "a"])
        self.run["recovery_payload"]["events"][-1]["state"] = "ack"
        self.client.video_ids = ["b", "c", "a"]  # Exact IDs with foreign replacement tokens.
        with self.assertRaisesRegex(RuntimeError, "complete owned slots"):
            self.reconcile(confirm=False, complete_requested=True)
        self.assert_no_provider_mutations()
        self.claim.assert_not_called()
        self.prepare()
        self.client._hype_playability_verifier.states["b"] = "unknown"
        with self.assertRaises(RuntimeError):
            self.reconcile(complete_requested=True)
        self.assert_no_provider_mutations()
        self.append.assert_not_called()

    def test_resume_after_confirmation_commit_reuses_observation_without_replaying_move(self):
        guard = Mock(side_effect=[None, PlaybackBlocked("input check paused recovery")])
        with self.assertRaisesRegex(PlaybackBlocked, "paused recovery"):
            self.reconcile(complete_requested=True, before_mutation=guard)
        self.assert_no_provider_mutations()
        self.assertEqual(self.run["recovery_payload"]["events"][-1]["reconciliation_action"], "confirm_observed_move")
        report = self.reconcile(confirm=False, complete_requested=True, reclaim_recovery=True)
        self.assertEqual(report["status"], "published")
        self.assertEqual((self.client.edit_calls, self.client.remove_calls, self.client.add_calls), (1, 0, []))
        confirmations = [event for event in self.run["recovery_payload"]["events"]
                         if event.get("reconciliation_action") == "confirm_observed_move"]
        self.assertEqual(len(confirmations), 1)

    def test_either_observation_with_wrong_id_token_or_order_blocks_confirmation(self):
        for changed in ("id", "token", "order", "before"):
            with self.subTest(changed=changed):
                self.prepare()
                first = deepcopy(self.client._items)
                second = deepcopy(first)
                if changed == "id":
                    second[0]["videoId"] = "foreign"
                elif changed == "token":
                    second[0]["setVideoId"] = "foreign-slot"
                elif changed == "order":
                    second.reverse()
                else:
                    second = sync._audit_playlist_items(self.intent["before_items"])
                self.client.get_playlist.side_effect = [{"tracks": first}, {"tracks": second}]
                with self.assertRaisesRegex(RuntimeError, "two exact observations"):
                    self.reconcile()
                self.claim.assert_not_called()
                self.append.assert_not_called()
                self.assert_no_provider_mutations()

    def test_workers_legacy_and_nonmove_or_later_mutations_cannot_gain_ownership(self):
        with self.assertRaises(ValueError):
            self.reconcile(workers=False)
        for case in ("legacy", "later_add", "acknowledged"):
            with self.subTest(case=case):
                self.prepare()
                if case == "legacy":
                    self.run["evidence_version"] = 0
                elif case == "later_add":
                    self.run["recovery_payload"]["events"].append({"seq": 3, "operation": "add", "state": "intent"})
                else:
                    self.run["recovery_payload"]["events"][-1]["state"] = "ack"
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                self.append.assert_not_called()
                self.assert_no_provider_mutations()

    def test_missing_before_or_wrong_destination_cannot_fabricate_move_evidence(self):
        for key, value in (("before_items", []), ("target_before_set_video_id", "foreign-slot")):
            with self.subTest(key=key):
                self.prepare()
                self.run["recovery_payload"]["events"][0][key] = value
                with self.assertRaises(RuntimeError):
                    self.reconcile()
                self.append.assert_not_called()
                self.assert_no_provider_mutations()

    def test_unknown_playback_and_changed_frozen_inputs_hold_before_observation_commit(self):
        self.client._hype_playability_verifier.states["a"] = "unknown"
        with self.assertRaises(RuntimeError):
            self.reconcile()
        self.append.assert_not_called()
        self.assert_no_provider_mutations()
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.reconcile(before_mutation=Mock(side_effect=PlaybackBlocked("inputs changed")))
        self.append.assert_not_called()
        self.assert_no_provider_mutations()

    def test_claim_race_or_failed_observation_commit_never_mutates_playlist(self):
        self.claim.side_effect = RuntimeError("claim changed")
        with self.assertRaisesRegex(RuntimeError, "claim changed"):
            self.reconcile()
        self.append.assert_not_called()
        self.assert_no_provider_mutations()
        self.claim.side_effect = None
        self.claim.return_value = {"claim_token": "recovery-token", "state_fingerprint": "claimed"}
        self.append.side_effect = RuntimeError("observation commit failed")
        with self.assertRaisesRegex(RuntimeError, "observation commit failed"):
            self.reconcile()
        self.assert_no_provider_mutations()
        self.assertEqual(self.finish.call_args.kwargs["status"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
