"""Offline reconciliation safety: exact IDs, fresh playback, durable owned slots."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import Mock, patch

import hype_db
import reconcile_playlist_update as recovery
from sync_validation import PlaybackBlocked
from test_playlist_verification import StatefulPlaylist


class FixtureVerifier:
    environment = "fixture"

    def __init__(self):
        self.health = {"run_health": "healthy", "auth_state": "authenticated"}
        self.states = {}
        self.expired = False
        self.check_health = Mock(side_effect=lambda: self.health.copy())
        self.verify = Mock(side_effect=self.observe)

    def observe(self, video_id, *, availability=None, force=False):
        now = datetime.now(timezone.utc)
        state = "unknown" if availability is False else self.states.get(video_id, "playable")
        return {
            "video_id": video_id, "state": state, "reason_code": "fixture_" + state,
            "environment": self.environment, **self.health,
            "observed_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(seconds=-1 if self.expired else 300)).isoformat(),
        }


class ReconcilePlayabilityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect", "hype_db.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("External access forbidden")))
        self.verifier = FixtureVerifier()
        self.run = self.audit()
        self.enterContext(patch.object(hype_db, "get_playlist_update_run", side_effect=lambda *a, **kw: deepcopy(self.run)))
        self.claim = self.enterContext(patch.object(hype_db, "claim_playlist_update_recovery", return_value={
            "claim_token": "recovery-owner", "state_fingerprint": "claimed-fingerprint",
        }))
        self.events = []

        def append(_db, _run_id, event, **kwargs):
            self.assertEqual(kwargs["claim_token"], "recovery-owner")
            recorded = {**deepcopy(event), "seq": len(self.run["recovery_payload"]["events"]) + 1}
            self.events.append(recorded)
            self.run["recovery_payload"]["events"].append(recorded)
            return {"seq": recorded["seq"], "state_fingerprint": f"state-{recorded['seq']}"}

        def finish(_db, _run_id, **kwargs):
            self.assertEqual(kwargs["claim_token"], "recovery-owner")
            self.run["status"] = kwargs["status"]

        self.append = self.enterContext(patch.object(hype_db, "append_playlist_update_evidence", side_effect=append))
        self.finish = self.enterContext(patch.object(hype_db, "finish_playlist_update", side_effect=finish))

    @staticmethod
    def audit(requested=None, existing=None):
        requested = ["new"] if requested is None else requested
        existing = ["old"] if existing is None else existing
        return {
            "playlist_id": "fixture", "status": "recovery_required",
            "requested_video_ids": requested, "requested_count": len(requested),
            "existing_video_ids": existing, "existing_count": len(existing),
            "evidence_version": 0, "recovery_payload": {"events": []},
            "claim_token": "prior-owner", "state_fingerprint": "prior-fingerprint",
        }

    def reconcile(self, client, *, apply=True, **kwargs):
        return recovery.reconcile_playlist_update(
            client, "never-open.db", "run", "fixture", apply=apply,
            workers_quiescent=True, playability_verifier=self.verifier, **kwargs,
        )

    def own_current(self, client):
        old = [{"videoId": "old", "setVideoId": "old-slot"}]
        current = deepcopy(client._items)
        self.run.update(evidence_version=1, recovery_payload={
            "snapshot": {"existing_items": old},
            "events": [
                {"seq": 1, "phase": "publish", "operation": "remove", "state": "intent", "items": old, "before_items": old},
                {"seq": 2, "phase": "publish", "operation": "remove", "state": "ack", "intent_seq": 1, "items": old, "after_items": []},
                {"seq": 3, "phase": "publish", "operation": "add", "state": "intent", "items": [{"videoId": row["videoId"]} for row in current], "before_items": []},
                {"seq": 4, "phase": "publish", "operation": "add", "state": "ack", "intent_seq": 3, "items": current, "after_items": current},
            ],
        })

    def assert_no_mutations(self, client):
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def owned_append(self, observed="JJx_WQXOeK0"):
        old, new = "JJx_WQXOeK0", "GxChUrrY4bc"
        self.run = self.audit([new], [old])
        client = StatefulPlaylist([old], requested_values=[new], substitute_requested=[observed])
        before = deepcopy(client._items)
        response = client.add_playlist_items("fixture", [new])
        acknowledged = response["playlistEditResults"]
        self.run.update(evidence_version=1, recovery_payload={
            "snapshot": {"existing_items": before},
            "events": [
                {"seq": 1, "phase": "publish", "operation": "add", "state": "intent",
                 "items": [{"videoId": new}], "before_items": before},
                {"seq": 2, "phase": "publish", "operation": "add", "state": "ack",
                 "intent_seq": 1, "items": acknowledged, "after_items": before + acknowledged},
            ],
        })
        client.add_calls.clear()
        return client, before

    def test_rejected_append_cannot_repeat_original_request_even_after_crash_before_rejection_event(self):
        for recorded_rejection in (False, True):
            with self.subTest(recorded_rejection=recorded_rejection):
                client, before = self.owned_append()
                self.run["identity_review_required"] = recorded_rejection
                snapshot = deepcopy(client._items)
                report = self.reconcile(client, apply=False, complete_requested=True)
                self.assertEqual(report["action"], "blocked")
                self.assertTrue(report["identity_review_required"])
                with self.assertRaisesRegex(RuntimeError, "identity review"):
                    self.reconcile(client, complete_requested=True)
                self.assertEqual(client._items, snapshot)
                self.assertEqual(client._items[:1], before)
                self.assert_no_mutations(client)
                self.claim.assert_not_called()
                self.finish.assert_not_called()

    def test_rejected_append_restore_removes_only_acknowledged_duplicate_slot_and_keeps_rejection(self):
        client, before = self.owned_append()
        report = self.reconcile(client)
        self.assertEqual(report["action"], "restore_owned_items")
        self.assertEqual(report["status"], "recovery_required")
        self.assertTrue(report["identity_review_required"])
        self.assertEqual(client._items, before)
        self.assertEqual((client.remove_calls, client.add_calls), (1, []))
        rejection = next(event for event in self.events if event.get("verification_matches") is False)
        self.assertEqual(rejection["differences"], [{
            "position": 2, "expected_id": "GxChUrrY4bc", "actual_id": "JJx_WQXOeK0",
            "accepted": False, "reason": "unexpected_video_id",
        }])

    def test_lost_add_ack_cannot_finalize_even_when_current_ids_equal_complete_target(self):
        for explicit_completion in (False, True):
            with self.subTest(explicit_completion=explicit_completion):
                client, _ = self.owned_append(observed="GxChUrrY4bc")
                self.run.update(requested_video_ids=client.video_ids, requested_count=2)
                self.run["recovery_payload"]["events"].pop()  # The request remains unresolved.
                snapshot = deepcopy(client._items)
                report = self.reconcile(client, apply=False, complete_requested=explicit_completion)
                self.assertEqual(report["action"], "blocked")
                with self.assertRaisesRegex(RuntimeError, "unresolved mutation"):
                    self.reconcile(client, complete_requested=explicit_completion)
                self.assertEqual(client._items, snapshot)
                self.assert_no_mutations(client)
                self.claim.assert_not_called()
                self.finish.assert_not_called()

    def test_fresh_exact_acknowledged_target_resolves_old_rejection_without_mutation(self):
        client, _ = self.owned_append(observed="GxChUrrY4bc")
        self.run.update(requested_video_ids=client.video_ids, requested_count=2,
                        identity_review_required=True)
        snapshot = deepcopy(client._items)
        report = self.reconcile(client, complete_requested=True)
        self.assertEqual(report["action"], "finalize_requested")
        self.assertEqual(report["status"], "published")
        self.assertFalse(report["identity_review_required"])
        self.assertEqual(client._items, snapshot)
        self.assert_no_mutations(client)

    def test_restore_add_substitution_cannot_be_retried_after_crash_before_rejection(self):
        client = StatefulPlaylist(["before"])
        self.run = self.audit(["new"], ["before"])
        before = deepcopy(client._items)
        client.remove_playlist_items("fixture", before)
        response = client.add_playlist_items("fixture", ["before"])
        acknowledged = response["playlistEditResults"]
        client._items[-1]["videoId"] = "wrong-version"
        self.run.update(evidence_version=1, recovery_payload={
            "snapshot": {"existing_items": before},
            "events": [
                {"seq": 1, "phase": "publish", "operation": "remove", "state": "intent",
                 "items": before, "before_items": before},
                {"seq": 2, "phase": "publish", "operation": "remove", "state": "ack",
                 "intent_seq": 1, "items": before, "after_items": []},
                {"seq": 3, "phase": "restore", "operation": "add", "state": "intent",
                 "items": [{"videoId": "before"}], "before_items": []},
                {"seq": 4, "phase": "restore", "operation": "add", "state": "ack",
                 "intent_seq": 3, "items": acknowledged, "after_items": acknowledged},
            ],
        })
        client.remove_calls, client.add_calls = 0, []
        snapshot = deepcopy(client._items)
        report = self.reconcile(client, apply=False)
        self.assertEqual(report["action"], "blocked")
        self.assertTrue(report["identity_review_required"])
        with self.assertRaisesRegex(RuntimeError, "repeat a rejected addition"):
            self.reconcile(client)
        self.assertEqual(client._items, snapshot)
        self.assert_no_mutations(client)
        self.claim.assert_not_called()
        self.finish.assert_not_called()

    def test_exact_healthy_request_finalizes_without_mutation_and_keeps_claim_guards(self):
        client = StatefulPlaylist(["new"])
        report = self.reconcile(client)
        self.assertEqual(report["status"], "published")
        self.assert_no_mutations(client)
        self.assertEqual(self.claim.call_args.kwargs["expected_claim_token"], "prior-owner")
        self.assertEqual(self.claim.call_args.kwargs["expected_state_fingerprint"], "prior-fingerprint")
        self.assertEqual(self.finish.call_args.kwargs["expected_state_fingerprint"], "state-1")
        self.assertTrue(any(call.kwargs["force"] for call in self.verifier.verify.call_args_list))

    def test_unknown_auth_expired_and_unavailable_observations_cannot_finalize(self):
        for case in ("unknown", "auth", "expired", "availability"):
            with self.subTest(case=case):
                self.verifier = FixtureVerifier()
                client = StatefulPlaylist(["new"])
                if case == "unknown":
                    self.verifier.states["new"] = "unknown"
                elif case == "auth":
                    self.verifier.health["auth_state"] = "unknown"
                elif case == "expired":
                    self.verifier.expired = True
                else:
                    client._items[0]["isAvailable"] = False
                self.assertEqual(self.reconcile(client, apply=False)["action"], "blocked")
                with self.assertRaises(RuntimeError):
                    self.reconcile(client)
                self.assert_no_mutations(client)
                self.claim.assert_not_called()
                self.finish.assert_not_called()
                if case == "availability":
                    self.assertTrue(any(call.kwargs.get("availability") is False
                                        for call in self.verifier.verify.call_args_list))

    def test_same_recording_alias_and_historical_partial_subset_never_equal_original_request(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                self.run = self.audit(["a", "b"] if partial else ["a"])
                if partial:
                    self.run.update(publication_mode="partial", effective_video_ids=["a"])
                client = StatefulPlaylist(["a"] if partial else ["alias-a"])
                client.get_song = Mock(return_value={"videoDetails": {"title": "Same Song", "author": "Same Artist"}})
                self.assertEqual(self.reconcile(client, apply=False)["action"], "blocked")
                with self.assertRaises(RuntimeError):
                    self.reconcile(client)
                client.get_song.assert_not_called()
                self.assert_no_mutations(client)

    def test_unavailable_original_cannot_be_restored_even_with_complete_owned_receipts(self):
        client = StatefulPlaylist(["intermediate"])
        self.own_current(client)
        self.verifier.states["old"] = "unavailable"
        self.assertEqual(self.reconcile(client, apply=False)["action"], "blocked")
        with self.assertRaises(RuntimeError):
            self.reconcile(client)
        self.assert_no_mutations(client)
        self.claim.assert_not_called()

    def test_owned_restore_requires_fresh_playability_before_each_mutation_and_afterward(self):
        client = StatefulPlaylist(["intermediate"])
        self.own_current(client)
        guard = Mock()
        report = self.reconcile(client, before_mutation=guard)
        self.assertEqual(report["status"], "restored")
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual((client.remove_calls, client.add_calls), (1, [["old"]]))
        self.assertGreaterEqual(guard.call_count, 3)
        self.assertTrue(all(call.args[0] == "old" for call in self.verifier.verify.call_args_list))

    def test_legacy_tail_checks_full_target_and_preserves_exact_prefix_tokens(self):
        self.run = self.audit(["a", "b", "tail"])
        client = StatefulPlaylist(["a", "b"], requested_values=["tail"])
        prefix = deepcopy(client._items)
        report = self.reconcile(client, append_missing_last=True)
        self.assertEqual(report["status"], "published")
        self.assertEqual(client.video_ids, ["a", "b", "tail"])
        self.assertEqual(client._items[:2], prefix)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))
        self.assertEqual({call.args[0] for call in self.verifier.verify.call_args_list}, {"a", "b", "tail"})
        self.assertEqual([event["state"] for event in self.events], ["intent", "ack", "verified"])

    def test_expiry_during_tail_intent_commit_blocks_the_provider_write(self):
        self.run = self.audit(["a", "tail"])
        client = StatefulPlaylist(["a"], requested_values=["tail"])
        original = self.append.side_effect

        def append(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2].get("state") == "intent":
                self.verifier.expired = True
            return result

        self.append.side_effect = append
        with self.assertRaises(PlaybackBlocked):
            self.reconcile(client, append_missing_last=True)
        self.assert_no_mutations(client)
        self.assertEqual([event["state"] for event in self.events], ["intent"])
        self.assertEqual(self.finish.call_args.kwargs["status"], "recovery_required")

    def test_expiry_during_external_guard_blocks_recovery(self):
        self.run = self.audit(["a", "tail"])
        client = StatefulPlaylist(["a"], requested_values=["tail"])
        with self.assertRaises(PlaybackBlocked):
            self.reconcile(client, append_missing_last=True,
                           before_mutation=lambda: setattr(self.verifier, "expired", True))
        self.assert_no_mutations(client)

    def test_unknown_after_tail_add_holds_recovery_without_retry_or_old_restore(self):
        self.run = self.audit(["a", "tail"])
        client = StatefulPlaylist(["a"], requested_values=["tail"])
        original = client.add_playlist_items

        def add(*args, **kwargs):
            result = original(*args, **kwargs)
            self.verifier.states["tail"] = "unknown"
            return result

        client.add_playlist_items = add
        with self.assertRaises(PlaybackBlocked):
            self.reconcile(client, append_missing_last=True)
        self.assertEqual(self.finish.call_args.kwargs["status"], "recovery_required")
        with self.assertRaises(RuntimeError):
            self.reconcile(client, append_missing_last=True, reclaim_recovery=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))

    def test_provider_tail_substitution_is_never_finalized_as_requested(self):
        self.run = self.audit(["a", "tail"])
        client = StatefulPlaylist(["a"], requested_values=["tail"], substitute_requested=["alias-tail"])
        with self.assertRaises(RuntimeError):
            self.reconcile(client, append_missing_last=True)
        self.assertEqual(client.video_ids, ["a", "alias-tail"])
        self.assertEqual(self.finish.call_args.kwargs["status"], "recovery_required")
        self.assertTrue(self.finish.call_args.kwargs["identity_review_required"])
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))

    def test_ambiguous_legacy_append_cannot_be_repeated(self):
        self.run = self.audit(["a", "tail"])
        client = StatefulPlaylist(["a"], requested_values=["tail"])

        def lose_response(_playlist_id, video_ids, **kwargs):
            client.add_calls.append(video_ids)
            raise TimeoutError("Provider outcome is unknown")

        client.add_playlist_items = lose_response
        with self.assertRaises(recovery.PlaylistMutationUncertain):
            self.reconcile(client, append_missing_last=True)
        with self.assertRaises(RuntimeError):
            self.reconcile(client, append_missing_last=True, reclaim_recovery=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, [["tail"]]))
        self.assertEqual([event["state"] for event in self.events], ["intent", "ambiguous"])

    def test_foreign_ownership_and_changed_input_guard_cannot_authorize_restore(self):
        client = StatefulPlaylist(["intermediate"])
        self.own_current(client)
        client.video_ids = ["intermediate"]
        self.assertEqual(self.reconcile(client, apply=False)["action"], "blocked")
        self.assert_no_mutations(client)
        client = StatefulPlaylist(["intermediate"])
        self.own_current(client)
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.reconcile(client, before_mutation=Mock(side_effect=RuntimeError("inputs changed")))
        self.assert_no_mutations(client)
        self.assertEqual(self.finish.call_args.kwargs["status"], "recovery_required")


if __name__ == "__main__":
    unittest.main()
