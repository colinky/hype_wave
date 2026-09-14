"""Offline receipt/crash/reconciliation contracts; all databases live under tests/."""
from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import hype_db
import ytmusic_playlist_sync as sync
from reconcile_playlist_update import reconcile_playlist_update
from test_playlist_verification import StatefulPlaylist


class DurableRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": "", "HYPE_MATCH_STARTED_AT": "current-run"}))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.enterContext(sync.bilingual_cache_read_only())
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.db = Path(directory) / "recovery.db"
        hype_db.init_db(self.db)

    def publish(self, client, requested=None):
        return sync.update_ytmusic_playlist(
            client, "fixture", requested or ["new"], dry_run=False,
            db_path=self.db, service="apple", job_name="Fixture",
        )

    def run_record(self):
        with hype_db.connect(self.db, read_only=True) as conn:
            row = conn.execute("SELECT update_run_id FROM playlist_update_runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return hype_db.get_playlist_update_run(self.db, row[0], read_only=True)

    def reconcile(self, client, run=None, **kwargs):
        return reconcile_playlist_update(client, self.db, (run or self.run_record())["update_run_id"], "fixture", **kwargs)

    def legacy(self, existing=None, requested=None):
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="fixture", service="apple", job_name="Fixture",
            existing_video_ids=existing or ["old"], requested_video_ids=requested or ["new"],
        )
        hype_db.finish_playlist_update(self.db, run_id, status="recovery_required", error="Original failure evidence")
        return hype_db.get_playlist_update_run(self.db, run_id, read_only=True)

    def pending_owned(self, client, requested):
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="fixture", service="apple", job_name="Fixture",
            requested_video_ids=requested, existing_video_ids=client.video_ids,
            existing_items=deepcopy(client._items), claim_token="fixture-initial",
        )
        hype_db.finish_playlist_update(self.db, run_id, status="recovery_required",
                                      error="Interrupted fixture", claim_token="fixture-initial")
        return self.run_record()

    def interrupted_restore(self, client, removed_indexes=(), added_ids=()):
        """Durable removal/addition ACKs can leave a partial original at recovery."""
        baseline = deepcopy(client._items)
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="fixture", service="apple", job_name="Fixture",
            requested_video_ids=["new-target"], existing_video_ids=client.video_ids,
            existing_items=baseline, claim_token="fixture-initial",
        )

        def event(operation, state, items, **fields):
            return hype_db.append_playlist_update_evidence(self.db, run_id, {
                "phase": "restore" if operation == "add" else "publish",
                "operation": operation, "state": state, "items": items,
                "chunk_order": 1, "attempt": 1, **fields,
            }, claim_token="fixture-initial")

        if removed_indexes:
            removed = [baseline[index] for index in removed_indexes]
            intent = event("remove", "intent", removed, before_items=baseline)
            client._items = [item for index, item in enumerate(baseline) if index not in removed_indexes]
            event("remove", "ack", removed, intent_seq=intent["seq"], after_items=deepcopy(client._items))
        if added_ids:
            intent = event("add", "intent", [{"videoId": value} for value in added_ids],
                           before_items=deepcopy(client._items))
            added = [{"videoId": value, "setVideoId": f"owned-added-{index}"}
                     for index, value in enumerate(added_ids)]
            client._items.extend(added)
            event("add", "ack", added, intent_seq=intent["seq"], after_items=deepcopy(client._items))
        hype_db.finish_playlist_update(self.db, run_id, status="recovery_required",
                                      error="Interrupted fixture", claim_token="fixture-initial")
        return self.run_record()

    def test_restore_readded_first_item_with_one_move_preserves_all_current_tokens(self):
        client = StatefulPlaylist(["a", "b", "c", "d", "e"])
        run = self.interrupted_restore(client, removed_indexes=(0,), added_ids=("a",))
        current_tokens = {item["videoId"]: item["setVideoId"] for item in client._items}
        self.assertEqual(client.video_ids, ["b", "c", "d", "e", "a"])
        self.assertEqual(self.reconcile(client, run)["action"], "restore_owned_items")
        result = self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertEqual((result["status"], client.video_ids), ("restored", ["a", "b", "c", "d", "e"]))
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 1))
        self.assertEqual({item["videoId"]: item["setVideoId"] for item in client._items}, current_tokens)
        latest = self.run_record()
        self.assertTrue(latest["restore_verified"])
        events = latest["recovery_payload"]["events"]
        self.assertEqual(events[:len(run["recovery_payload"]["events"])], run["recovery_payload"]["events"])
        moves = [event for event in events if event["operation"] == "move"]
        self.assertEqual([(event["phase"], event["state"]) for event in moves],
                         [("restore", "intent"), ("restore", "ack")])
        self.assertEqual(moves[0]["items"][0]["set_video_id"], current_tokens["a"])

    def test_duplicate_original_needing_restore_holds_before_claim(self):
        client = StatefulPlaylist(["a", "a", "b"])
        run = self.interrupted_restore(client, removed_indexes=(1,))
        before = self.db.read_bytes()
        with patch.object(hype_db, "claim_playlist_update_recovery") as claim:
            self.assertEqual(self.reconcile(client, run)["action"], "blocked")
            with self.assertRaisesRegex(RuntimeError, "duplicate original IDs"):
                self.reconcile(client, run, apply=True, workers_quiescent=True)
            claim.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 0))

    def test_exact_duplicate_original_can_finalize_without_item_mutation(self):
        client = StatefulPlaylist(["a", "a", "b"])
        run = self.pending_owned(client, ["new-target"])
        before = deepcopy(client._items)
        self.assertEqual(self.reconcile(client, run)["action"], "finalize_restored")
        result = self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(client._items, before)
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 0))

    def test_restore_mixed_missing_and_extra_slots_preserves_the_survivor(self):
        client = StatefulPlaylist(["a", "b", "c"])
        run = self.interrupted_restore(client, removed_indexes=(0, 2), added_ids=("bad",))
        survivor = deepcopy(client._items[0])
        client._hype_playability_verifier.states["bad"] = "unavailable"
        client._items[1]["isAvailable"] = False
        result = self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertEqual((result["status"], client.video_ids), ("restored", ["a", "b", "c"]))
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (1, [["a", "c"]], 1))
        self.assertEqual(client._items[1], survivor)
        self.assertEqual(sync._recoverable_playlist_items(self.run_record()), client._items)

    def test_restore_lost_move_response_holds_and_never_replays_acknowledged_changes(self):
        client = StatefulPlaylist(["a", "b", "c"])
        run = self.interrupted_restore(client, removed_indexes=(0,), added_ids=("a",))
        before_tokens = {item["videoId"]: item["setVideoId"] for item in client._items}
        edit = client.edit_playlist

        def lose_response(*args, **kwargs):
            edit(*args, **kwargs)
            raise TimeoutError("Response lost after actual move")

        client.edit_playlist = lose_response
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.reconcile(client, run, apply=True, workers_quiescent=True)
        latest = self.run_record()
        self.assertEqual(latest["status"], "recovery_required")
        self.assertEqual(client.video_ids, ["a", "b", "c"])
        self.assertEqual({item["videoId"]: item["setVideoId"] for item in client._items}, before_tokens)
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 1))
        before = self.db.read_bytes()
        self.assertEqual(self.reconcile(client, latest)["action"], "blocked")
        with self.assertRaises(RuntimeError):
            self.reconcile(client, latest, apply=True, workers_quiescent=True, reclaim_recovery=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 1))

    def test_optional_availability_keeps_owned_items_and_records_latest_read(self):
        for missing_read in (1, 2, 3, 4):
            with self.subTest(missing_read=missing_read):
                client = StatefulPlaylist(["a", "b"])
                run = self.pending_owned(client, ["a", "b"])
                original_read, observed = client.get_playlist, []

                def read(*args, **kwargs):
                    payload = deepcopy(original_read(*args, **kwargs))
                    for item in payload["tracks"]:
                        if len(observed) + 1 != missing_read:
                            item["isAvailable"] = True
                    observed.append(deepcopy(payload["tracks"]))
                    return payload

                client.get_playlist = read
                verifier = client._hype_playability_verifier
                append, written = hype_db.append_playlist_update_evidence, []

                def record(*args, **kwargs):
                    written.append(deepcopy(args[2]))
                    return append(*args, **kwargs)

                with patch.object(verifier, "verify", wraps=verifier.verify) as verify, patch.object(
                        hype_db, "append_playlist_update_evidence", side_effect=record):
                    result = self.reconcile(client, run, apply=True, workers_quiescent=True)
                self.assertEqual(result["status"], "published")
                self.assertEqual(result["actual_items"], observed[-1])
                self.assertEqual(written[-1]["items"], observed[-1])
                latest = self.run_record()["recovery_payload"]["events"][-1]
                self.assertEqual(sync._playlist_item_keys(sync._audit_playlist_items(latest["items"])),
                                 sync._playlist_item_keys(observed[-1]))
                self.assertEqual(len(observed), 4)
                self.assertEqual(sum(call.kwargs.get("force", False) for call in verify.call_args_list), 4)
                self.assertEqual((client.remove_calls, client.add_calls, client.edit_calls), (0, [], 0))

    def test_false_second_move_observation_blocks_before_claim_or_mutation(self):
        from test_playlist_transition_visibility import LaggingPlaylist

        client = LaggingPlaylist(["a", "b", "c"], lag=5)
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client, ["c", "b", "a"])
        run = self.run_record()
        client.pending_reads, client.lag = [], 0
        original_read, reads = client.get_playlist, []

        def read(*args, **kwargs):
            payload = deepcopy(original_read(*args, **kwargs))
            if len(reads) == 1:
                payload["tracks"][0]["isAvailable"] = False
            reads.append(payload)
            return payload

        client.get_playlist = read
        before = self.db.read_bytes()
        counts = client.remove_calls, list(client.add_calls), len(client.moves)
        with self.assertRaises(RuntimeError):
            self.reconcile(client, run, apply=True, workers_quiescent=True,
                           confirm_observed_move=True, complete_requested=True)
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual((client.remove_calls, client.add_calls, len(client.moves)), counts)

    def test_complete_requested_can_remove_unavailable_non_target(self):
        client = StatefulPlaylist(["old"])
        client._hype_playability_verifier.states["old"] = "unavailable"
        run = self.pending_owned(client, ["new"])
        original_read = client.get_playlist

        def read(*args, **kwargs):
            payload = deepcopy(original_read(*args, **kwargs))
            for item in payload["tracks"]:
                item["isAvailable"] = item["videoId"] != "old"
            return payload

        client.get_playlist = read
        result = self.reconcile(client, run, apply=True, workers_quiescent=True, complete_requested=True)
        self.assertEqual((result["status"], client.video_ids), ("published", ["new"]))
        self.assertEqual((client.remove_calls, client.add_calls), (1, [["new"]]))

    def test_publish_receipts_are_durable_and_second_run_changes_no_items(self):
        client = StatefulPlaylist(["old"])
        self.publish(client)
        run = self.run_record()
        self.assertEqual(run["status"], "published")
        events = run["recovery_payload"]["events"]
        self.assertEqual([(row["operation"], row["state"]) for row in events],
                         [("remove", "intent"), ("remove", "ack"), ("add", "intent"), ("add", "ack")])
        self.assertEqual(sync._recoverable_playlist_items(run), client.get_playlist("fixture")["tracks"])
        before = client.get_playlist("fixture")
        counts = client.remove_calls, list(client.add_calls)
        self.publish(client)
        self.assertEqual(client.get_playlist("fixture"), before)
        self.assertEqual((client.remove_calls, client.add_calls), counts)
        self.assertEqual(self.run_record()["status"], "skipped_current")

    def test_historical_unverified_substitution_is_removed_only_with_owned_receipt_and_review_remains(self):
        requested, substitute = "GxChUrrY4bc", "ZyI5bmN2p1I"
        client = StatefulPlaylist(["old"], requested_values=[requested], substitute_requested=[substitute])
        base = client.get_song
        def metadata(video_id):
            row = base(video_id)
            if video_id in {requested, substitute}:
                row["videoDetails"]["title"] = "The Adult I Dreamed Of" if video_id == requested else "꿈꾸던 어른이 되었나요?"
                row["videoDetails"]["lengthSeconds"] = "204"
            return row
        client.get_song = metadata
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.publish(client, [requested])
        self.assertEqual(client.video_ids, ["old"])
        run = self.run_record()
        self.assertEqual(run["status"], "recovery_required")
        self.assertTrue(run["identity_review_required"])
        self.assertTrue(run["restore_verified"])
        self.assertEqual(run["differences"][0]["reason"], "unexpected_video_id")
        with hype_db.connect(self.db, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM yt_video_ids").fetchone()[0], 0)
        calls = client.remove_calls, list(client.add_calls)
        client.get_playlist = Mock(side_effect=AssertionError("pending must block before network"))
        with self.assertRaisesRegex(RuntimeError, "pending recovery"):
            self.publish(client, [requested])
        self.assertEqual((client.remove_calls, client.add_calls), calls)
        client.get_playlist = lambda *args, **kwargs: {"tracks": list(client._items)}
        self.assertEqual(self.reconcile(client)["action"], "blocked")
        with self.assertRaisesRegex(RuntimeError, "identity review"):
            self.reconcile(client, apply=True, workers_quiescent=True)

    def test_lost_response_never_retries_or_automatically_restores(self):
        class LostResponse(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                super().add_playlist_items(*args, **kwargs)
                raise TimeoutError("Response lost after remote application")
        client = LostResponse(["old"])
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client)
        self.assertEqual(client.video_ids, ["new"])
        self.assertEqual(client.add_calls, [["new"]])
        run = self.run_record()
        self.assertEqual(run["status"], "recovery_required")
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            sync._recoverable_playlist_items(run)

    def test_incomplete_foreign_duplicate_and_absent_receipts_fail_closed(self):
        for result in (None, {"status": "STATUS_SUCCEEDED"},
                       {"status": "STATUS_SUCCEEDED", "playlistEditResults": [None]},
                       {"status": "STATUS_SUCCEEDED", "playlistEditResults": [{"videoId": "foreign", "setVideoId": "new-slot"}]}):
            with self.subTest(result=result), self.assertRaises((RuntimeError, ValueError)):
                sync._addition_receipts(result, ["new"])
        with self.assertRaisesRegex(RuntimeError, "duplicate setVideoIds"):
            sync._addition_receipts({"status": "STATUS_SUCCEEDED", "playlistEditResults": [
                {"videoId": "a", "setVideoId": "same"}, {"videoId": "b", "setVideoId": "same"},
            ]}, ["a", "b"])

    def test_receipt_database_failure_never_advances_to_another_mutation(self):
        client = StatefulPlaylist(["old"])
        original = hype_db.append_playlist_update_evidence
        def append(*args, **kwargs):
            if args[2]["state"] == "ack":
                raise RuntimeError("DB ACK commit unavailable")
            return original(*args, **kwargs)
        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=append), self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client)
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(client.add_calls, [])
        self.assertEqual(self.run_record()["status"], "recovery_required")

    def test_crash_after_committed_ack_can_only_be_explicitly_finalized(self):
        class CrashAfterAdd(StatefulPlaylist):
            crash = True
            def get_playlist(self, *args, **kwargs):
                if self.add_calls and self.crash:
                    self.crash = False
                    raise SystemExit("process stopped after ACK")
                return super().get_playlist(*args, **kwargs)
        client = CrashAfterAdd(["old"])
        with self.assertRaises(SystemExit):
            self.publish(client)
        run = self.run_record()
        self.assertEqual(run["status"], "running")
        before = self.db.read_bytes()
        self.assertEqual(self.reconcile(client)["action"], "finalize_requested")
        self.assertEqual(self.db.read_bytes(), before)
        calls = client.remove_calls, list(client.add_calls)
        result = self.reconcile(client, apply=True, workers_quiescent=True)
        self.assertTrue(result["applied"])
        self.assertEqual(result["item_mutations"], 0)
        self.assertEqual((client.remove_calls, client.add_calls), calls)
        self.assertEqual(self.run_record()["status"], "published")

    def test_crash_after_intent_without_receipt_stays_manual(self):
        class CrashAfterIntent(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                raise SystemExit("process stopped with request outcome unknown")
        client = CrashAfterIntent(["old"])
        with self.assertRaises(SystemExit):
            self.publish(client)
        run = self.run_record()
        self.assertEqual(run["status"], "running")
        self.assertEqual(self.reconcile(client)["action"], "blocked")
        self.assertEqual(client.video_ids, [])

    def test_external_same_video_id_with_new_slot_is_not_deleted(self):
        class ExternalReplacement(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                response = super().add_playlist_items(*args, **kwargs)
                self.video_ids = list(self.video_ids)
                return response
        client = ExternalReplacement(["old"])
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            self.publish(client)
        self.assertEqual(client.video_ids, ["new"])
        self.assertEqual(client.remove_calls, 1)
        self.assertEqual(self.run_record()["status"], "recovery_required")

    def test_legacy_exact_observation_can_finalize_without_fabricating_receipts(self):
        run = self.legacy()
        client = StatefulPlaylist(["new"])
        before = self.db.read_bytes()
        self.assertEqual(self.reconcile(client, run)["action"], "finalize_requested")
        self.assertEqual(self.db.read_bytes(), before)
        result = self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertTrue(result["applied"])
        after = self.run_record()
        self.assertEqual(after["evidence_version"], 0)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))
        self.assertIn("Original failure evidence", str(after["recovery_payload"]))

    def test_legacy_mixed_or_incomplete_snapshot_does_not_allow_destructive_recovery(self):
        run = self.legacy(existing=["old-a", "old-b"], requested=["new-a", "new-b"])
        client = StatefulPlaylist(["new-a", "old-b"])
        self.assertEqual(self.reconcile(client, run)["action"], "blocked")
        with self.assertRaises(RuntimeError):
            self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def test_reconciliation_requires_scope_quiescence_and_unchanged_items_after_claim(self):
        run = self.legacy()
        client = StatefulPlaylist(["new"])
        with self.assertRaises(ValueError):
            reconcile_playlist_update(client, self.db, run["update_run_id"], "wrong")
        with self.assertRaisesRegex(RuntimeError, "workers"):
            self.reconcile(client, run, apply=True)
        real_claim = hype_db.claim_playlist_update_recovery
        def changed_claim(*args, **kwargs):
            result = real_claim(*args, **kwargs)
            client.video_ids = ["external"]
            return result
        with patch.object(hype_db, "claim_playlist_update_recovery", side_effect=changed_claim), self.assertRaisesRegex(RuntimeError, "changed"):
            self.reconcile(client, run, apply=True, workers_quiescent=True)
        self.assertEqual(client.video_ids, ["external"])
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))
        self.assertEqual(self.run_record()["status"], "recovery_required")

    def test_dry_run_does_not_query_pending_or_create_audit_and_live_requires_audit(self):
        client = Mock()
        before = self.db.read_bytes()
        with patch.object(hype_db, "get_pending_playlist_recovery", side_effect=AssertionError("dry-run DB access")):
            sync.update_ytmusic_playlist(client, "fixture", ["new"], dry_run=True, db_path=self.db)
        self.assertEqual(self.db.read_bytes(), before)
        client.get_playlist.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "audit is required"):
            sync.update_ytmusic_playlist(client, "fixture", ["new"], dry_run=False)
        client.add_playlist_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
