"""Independent offline crash-window checks using real SQLite audit persistence."""
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


class RecoveryAdversarialVerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "audit.db"
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(publisher.time, "sleep"))

    def test_identity_rejection_survives_crash_after_restore_ack(self):
        client = StatefulPlaylist(["old"], substitute_requested=["unverified-new"])
        append = hype_db.append_playlist_update_evidence

        def crash_after_restore_ack(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event["phase"] == "restore" and event["operation"] == "add" and event["state"] == "ack":
                raise SystemExit("simulated process death after durable restore acknowledgement")
            return result

        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=crash_after_restore_ack):
            with self.assertRaises(SystemExit):
                publisher.update_ytmusic_playlist(
                    client, "playlist", ["new"], description="", dry_run=False,
                    db_path=self.db, service="apple", job_name="Fixture-Daily",
                )
        self.assertEqual(client.video_ids, ["old"])
        pending = hype_db.get_pending_playlist_recovery(self.db, "playlist")
        report = reconcile_playlist_update(client, self.db, pending["update_run_id"], "playlist")
        self.assertEqual(report["action"], "blocked", report)

    def test_reconcile_persists_new_identity_rejection_after_publish_ack_crash(self):
        client = StatefulPlaylist(["old"], substitute_requested=["unverified-new"])
        append = hype_db.append_playlist_update_evidence

        def crash_after_publish_ack(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event["phase"] == "publish" and event["operation"] == "add" and event["state"] == "ack":
                raise SystemExit("simulated process death before identity verification")
            return result

        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=crash_after_publish_ack):
            with self.assertRaises(SystemExit):
                publisher.update_ytmusic_playlist(
                    client, "playlist", ["new"], description="", dry_run=False,
                    db_path=self.db, service="apple", job_name="Fixture-Daily",
                )
        self.assertEqual(client.video_ids, ["unverified-new"])
        pending = hype_db.get_pending_playlist_recovery(self.db, "playlist")
        report = reconcile_playlist_update(
            client, self.db, pending["update_run_id"], "playlist",
            apply=True, workers_quiescent=True,
        )
        self.assertEqual(client.video_ids, ["old"])
        self.assertEqual(report["status"], "recovery_required", report)

    def test_explicit_reclaim_recovers_a_crash_after_recovery_claim(self):
        client = StatefulPlaylist(["new"])
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="playlist", service="apple", job_name="Fixture-Daily",
            requested_video_ids=["new"],
            existing_items=[{"videoId": "old", "setVideoId": "old-slot"}],
            claim_token="publisher-token",
        )
        claim = hype_db.claim_playlist_update_recovery

        def crash_after_claim(*args, **kwargs):
            claim(*args, **kwargs)
            raise SystemExit("simulated recovery process death after claim")

        with patch.object(hype_db, "claim_playlist_update_recovery", side_effect=crash_after_claim):
            with self.assertRaises(SystemExit):
                reconcile_playlist_update(client, self.db, run_id, "playlist", apply=True, workers_quiescent=True)
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            reconcile_playlist_update(client, self.db, run_id, "playlist", apply=True, workers_quiescent=True)
        report = reconcile_playlist_update(
            client, self.db, run_id, "playlist", apply=True,
            workers_quiescent=True, reclaim_recovery=True,
        )
        self.assertEqual(report["status"], "published")
        self.assertEqual((client.remove_calls, client.add_calls), (0, []))

    def test_rejected_publication_matching_original_ids_still_needs_review(self):
        client = StatefulPlaylist(["old"], substitute_requested=["old"])
        append = hype_db.append_playlist_update_evidence

        def crash_after_publish_ack(db, run_id, event, **kwargs):
            result = append(db, run_id, event, **kwargs)
            if event["phase"] == "publish" and event["operation"] == "add" and event["state"] == "ack":
                raise SystemExit("simulated process death before comparing substitution")
            return result

        with patch.object(hype_db, "append_playlist_update_evidence", side_effect=crash_after_publish_ack):
            with self.assertRaises(SystemExit):
                publisher.update_ytmusic_playlist(
                    client, "playlist", ["new"], description="", dry_run=False,
                    db_path=self.db, service="apple", job_name="Fixture-Daily",
                )
        pending = hype_db.get_pending_playlist_recovery(self.db, "playlist")
        report = reconcile_playlist_update(client, self.db, pending["update_run_id"], "playlist")
        self.assertTrue(report["identity_review_required"], report)
        self.assertEqual(report["action"], "blocked", report)

    def test_resolved_legacy_payload_is_compacted_after_31_days(self):
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="legacy", service="apple", job_name="Fixture-Daily",
            requested_video_ids=["new"], existing_video_ids=["old"],
        )
        hype_db.finish_playlist_update(self.db, run_id, status="recovery_required", error="original legacy failure")
        reconcile_playlist_update(
            StatefulPlaylist(["new"]), self.db, run_id, "legacy",
            apply=True, workers_quiescent=True,
        )
        with hype_db.connect(self.db) as conn:
            conn.execute("UPDATE playlist_update_runs SET created_at=?,completed_at=? WHERE update_run_id=?",
                         ("2026-08-09T00:00:00+00:00", "2026-08-09T00:00:00+00:00", run_id))
            conn.execute("UPDATE playlist_update_items SET created_at=? WHERE update_run_id=?",
                         ("2026-08-09T00:00:00+00:00", run_id))
        with patch("hype_db_store.utc_now_iso", return_value="2026-09-11T00:00:00+00:00"):
            hype_db.record_playlist_update(
                self.db, playlist_id="housekeeping", service="apple", job_name="Fixture-Daily",
                requested_video_ids=["other"],
            )
        run = hype_db.get_playlist_update_run(self.db, run_id)
        self.assertEqual(run["evidence_version"], 0)
        self.assertTrue(run["recovery_payload"].get("pruned"), run["recovery_payload"])

    def test_unresolved_mutation_blocks_original_only_finalization(self):
        client = StatefulPlaylist(["old"])
        run_id = hype_db.record_playlist_update(
            self.db, playlist_id="playlist", service="apple", job_name="Fixture-Daily",
            requested_video_ids=["new"], existing_items=client._items, claim_token="publisher-token",
        )
        intent = hype_db.append_playlist_update_evidence(
            self.db, run_id,
            {"phase": "publish", "operation": "remove", "state": "intent", "chunk_order": 1,
             "attempt": 1, "items": client._items, "before_items": client._items},
            claim_token="publisher-token",
        )
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                if ambiguous:
                    hype_db.append_playlist_update_evidence(
                        self.db, run_id,
                        {"phase": "publish", "operation": "remove", "state": "ambiguous", "chunk_order": 1,
                         "attempt": 1, "intent_seq": intent["seq"], "items": client._items},
                        claim_token="publisher-token",
                    )
                result = reconcile_playlist_update(client, self.db, run_id, "playlist")
                self.assertEqual(result["action"], "blocked", result)
                with self.assertRaises(RuntimeError):
                    reconcile_playlist_update(client, self.db, run_id, "playlist", apply=True, workers_quiescent=True)
                self.assertEqual((client.remove_calls, client.add_calls), (0, []))


if __name__ == "__main__":
    unittest.main()
