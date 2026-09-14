from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hype_db import (
    append_playlist_update_evidence,
    claim_playlist_update_recovery,
    connect,
    finish_playlist_update,
    get_pending_playlist_recovery,
    get_playlist_update_run,
    init_db,
    record_playlist_update,
)


def items(*video_ids: str) -> list[dict[str, str]]:
    return [
        {"videoId": video_id, "setVideoId": f"set-{index}"}
        for index, video_id in enumerate(video_ids, 1)
    ]


class PlaylistRecoveryEvidenceDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "audit.db"
        self.env = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def exact_run(self, *, playlist_id: str = "playlist") -> tuple[str, str]:
        token = "publisher-token"
        run_id = record_playlist_update(
            self.db_path,
            playlist_id=playlist_id,
            service="apple",
            job_name="Fixture-Daily",
            requested_video_ids=["new-1", "new-2"],
            existing_items=items("old-1", "old-2"),
            claim_token=token,
        )
        return run_id, token

    def partial_event(self, *, verified: bool) -> dict:
        return {
            "phase": "publish",
            "operation": "observe",
            "state": "verified",
            "publication_mode": "partial",
            "effective_video_ids": ["new-1"],
            "excluded_items": [{
                "position": 2,
                "requested_video_id": "new-2",
                "actual_video_id": "wrong-2",
                "reason": "artist_identity_mismatch",
            }],
            "items": items("new-1") if verified else items("new-1", "wrong-2"),
            "observation_complete": True,
            "verification_matches": verified,
        }

    def test_partial_publication_preserves_original_request_and_closes_only_after_verification(self) -> None:
        run_id, token = self.exact_run()
        append_playlist_update_evidence(
            self.db_path, run_id, self.partial_event(verified=False), claim_token=token,
        )
        pending = get_pending_playlist_recovery(self.db_path, "playlist")
        self.assertEqual(pending["requested_video_ids"], ["new-1", "new-2"])
        self.assertEqual(pending["effective_video_ids"], ["new-1"])
        self.assertEqual(pending["excluded_items"][0]["actual_video_id"], "wrong-2")
        self.assertEqual(pending["publication_mode"], "partial")
        self.assertFalse(pending["partial_verified"])
        with self.assertRaisesRegex(RuntimeError, "complete verified effective"):
            finish_playlist_update(
                self.db_path, run_id, status="published", actual_items=items("new-1"),
                observation_complete=True, claim_token=token,
            )
        append_playlist_update_evidence(
            self.db_path, run_id, {**self.partial_event(verified=True), "phase": "reconcile"},
            claim_token=token,
        )
        with self.assertRaisesRegex(RuntimeError, "complete verified effective"):
            finish_playlist_update(
                self.db_path, run_id, status="published", actual_items=items("wrong-1"),
                observation_complete=True, claim_token=token,
            )
        finish_playlist_update(
            self.db_path, run_id, status="published", actual_items=items("new-1"),
            observation_complete=True, claim_token=token,
        )
        result = get_playlist_update_run(self.db_path, run_id)
        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(result["actual_video_ids"], ["new-1"])
        self.assertTrue(result["partial_verified"])
        self.assertIsNone(get_pending_playlist_recovery(self.db_path, "playlist"))

    def test_partial_policy_rejects_invalid_or_reordered_exclusions(self) -> None:
        run_id, token = self.exact_run()
        for override in (
            {"effective_video_ids": []},
            {"effective_video_ids": ["new-2"]},
            {"effective_video_ids": ["new-1", "new-2"]},
            {"excluded_items": []},
            {"excluded_items": [{"position": 2, "requested_video_id": "wrong-request", "actual_video_id": "wrong-2", "reason": "mismatch"}]},
            {"excluded_items": [{"position": 3, "requested_video_id": "new-2", "actual_video_id": "wrong-2", "reason": "mismatch"}]},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                append_playlist_update_evidence(
                    self.db_path, run_id, {**self.partial_event(verified=False), **override},
                    claim_token=token,
                )
        self.assertEqual(get_playlist_update_run(self.db_path, run_id)["recovery_payload"]["events"], [])

    def test_exact_snapshot_survives_item_pruning_and_legacy_is_manual_only(self) -> None:
        run_id, token = self.exact_run()
        pending = get_pending_playlist_recovery(self.db_path, "playlist")
        self.assertEqual(pending["recovery_capability"], "exact")
        self.assertEqual(pending["claim_token"], token)
        self.assertEqual(pending["existing_video_ids"], ["old-1", "old-2"])
        self.assertEqual(
            [item["set_video_id"] for item in pending["existing_items"]],
            ["set-1", "set-2"],
        )

        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM playlist_update_items WHERE update_run_id = ?", (run_id,))
        pending = get_pending_playlist_recovery(self.db_path, "playlist")
        self.assertEqual(pending["recovery_capability"], "exact")
        self.assertEqual(pending["requested_video_ids"], ["new-1", "new-2"])

        finish_playlist_update(
            self.db_path,
            run_id,
            status="verification_failed",
            claim_token=token,
        )
        legacy_id = record_playlist_update(
            self.db_path,
            playlist_id="legacy",
            service="apple",
            job_name="Fixture-Daily",
            requested_video_ids=["new"],
            existing_video_ids=["old"],
        )
        legacy = get_playlist_update_run(self.db_path, legacy_id)
        self.assertEqual(legacy["recovery_capability"], "manual_only")
        self.assertFalse(legacy["evidence_complete"])

    def test_intent_receipt_and_finish_outcomes_are_append_only(self) -> None:
        run_id, token = self.exact_run()
        intent = append_playlist_update_evidence(
            self.db_path,
            run_id,
            {
                "phase": "publish",
                "operation": "remove",
                "state": "intent",
                "chunk_order": 1,
                "attempt": 1,
                "items": items("old-1", "old-2"),
            },
            claim_token=token,
        )
        receipt = append_playlist_update_evidence(
            self.db_path,
            run_id,
            {
                "phase": "publish",
                "operation": "remove",
                "state": "ack",
                "chunk_order": 1,
                "attempt": 1,
                "intent_seq": intent["seq"],
                "items": items("old-1", "old-2"),
                "provider_status": "STATUS_SUCCEEDED",
            },
            claim_token=token,
        )
        self.assertGreater(receipt["seq"], intent["seq"])
        with self.assertRaises(ValueError):
            append_playlist_update_evidence(
                self.db_path,
                run_id,
                {
                    "phase": "publish",
                    "operation": "add",
                    "state": "ack",
                    "chunk_order": 1,
                    "attempt": 1,
                    "intent_seq": intent["seq"],
                    "items": ["new-1"],
                },
                claim_token=token,
            )

        add_intent = append_playlist_update_evidence(
            self.db_path,
            run_id,
            {
                "phase": "publish",
                "operation": "add",
                "state": "intent",
                "chunk_order": 1,
                "attempt": 1,
                "items": ["new-1"],
            },
            claim_token=token,
        )
        with self.assertRaises(ValueError):
            append_playlist_update_evidence(
                self.db_path,
                run_id,
                {
                    "phase": "publish",
                    "operation": "add",
                    "state": "ack",
                    "chunk_order": 1,
                    "attempt": 1,
                    "intent_seq": add_intent["seq"],
                    "items": [{"videoId": "new-1", "setVideoId": "set-1"}],
                },
                claim_token=token,
            )
        add_receipt = append_playlist_update_evidence(
            self.db_path,
            run_id,
            {
                "phase": "publish",
                "operation": "add",
                "state": "ack",
                "chunk_order": 1,
                "attempt": 1,
                "intent_seq": add_intent["seq"],
                "items": [{"videoId": "new-1", "setVideoId": "new-set-1"}],
            },
            claim_token=token,
        )
        self.assertGreater(add_receipt["seq"], add_intent["seq"])

        finish_playlist_update(
            self.db_path,
            run_id,
            status="mutation_failed",
            actual_items=items("partial"),
            observation_complete=True,
            error="publish failed",
            differences=[{"position": 2, "reason": "missing"}],
            claim_token=token,
        )
        finish_playlist_update(
            self.db_path,
            run_id,
            status="recovery_required",
            actual_items=items("old-1", "old-2"),
            observation_complete=True,
            error="identity review required",
            differences=[{"position": 1, "reason": "author_mismatch"}],
            claim_token=token,
            restore_verified=True,
            identity_review_required=True,
        )
        pending = get_pending_playlist_recovery(self.db_path, "playlist")
        outcomes = pending["recovery_payload"]["outcomes"]
        self.assertEqual([outcome["status"] for outcome in outcomes], ["mutation_failed", "recovery_required"])
        self.assertEqual(outcomes[0]["error"], "publish failed")
        self.assertEqual(pending["error"], "identity review required")
        self.assertTrue(pending["restore_verified"])
        self.assertTrue(pending["identity_review_required"])

    def test_reconcile_claim_fences_stale_worker_and_needs_attestation(self) -> None:
        run_id, token = self.exact_run()
        with self.assertRaises(ValueError):
            claim_playlist_update_recovery(
                self.db_path,
                run_id,
                expected_claim_token=token,
                workers_quiescent=False,
                attestation="",
            )
        claimed = claim_playlist_update_recovery(
            self.db_path,
            run_id,
            expected_claim_token=token,
            workers_quiescent=True,
            attestation="workflow cancelled and process stopped",
        )
        with self.assertRaises(RuntimeError):
            finish_playlist_update(
                self.db_path,
                run_id,
                status="published",
                claim_token=token,
            )
        with self.assertRaises(RuntimeError):
            claim_playlist_update_recovery(
                self.db_path,
                run_id,
                expected_claim_token=claimed["claim_token"],
                workers_quiescent=True,
                attestation="second worker",
            )

    def test_audit_initialization_does_not_repair_canonical_identity(self) -> None:
        init_db(self.db_path)
        with connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO tracks(
                    track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
                    match_status, best_score, created_at, updated_at
                ) VALUES (?, 'same-video', 'Song', 'Artist', 'Album', 'matched', ?, ?, ?)
                """,
                [
                    ("indexed", 1.0, "2026-09-01", "2026-09-01"),
                    ("duplicate", 0.5, "2026-09-02", "2026-09-02"),
                ],
            )
            conn.execute(
                "INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES ('same-video','indexed',1)"
            )
            conn.execute(
                "INSERT INTO platform_song_ids(service,song_id,track_uid) VALUES ('apple','legacy-source','duplicate')"
            )

        def identity_rows() -> tuple[list[tuple], list[tuple], list[tuple]]:
            with connect(self.db_path, read_only=True) as conn:
                tracks = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT track_uid,canonical_yt_video_id,yt_title,yt_artist,yt_album "
                        "FROM tracks ORDER BY track_uid"
                    ).fetchall()
                ]
                videos = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT video_id,track_uid,is_canonical FROM yt_video_ids ORDER BY video_id"
                    ).fetchall()
                ]
                bindings = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT service,song_id,track_uid FROM platform_song_ids ORDER BY service,song_id"
                    ).fetchall()
                ]
            return tracks, videos, bindings

        before = identity_rows()
        run_id, token = self.exact_run(playlist_id="audit-only")
        finish_playlist_update(
            self.db_path,
            run_id,
            status="verification_failed",
            claim_token=token,
        )
        self.assertEqual(identity_rows(), before)

    def test_retention_deletes_items_but_keeps_active_payload_and_compacts_terminal(self) -> None:
        active_id, active_token = self.exact_run(playlist_id="active")
        terminal_id, terminal_token = self.exact_run(playlist_id="terminal")
        legacy_id = record_playlist_update(
            self.db_path,
            playlist_id="legacy-terminal",
            service="apple",
            job_name="Fixture-Daily",
            requested_video_ids=["new"],
            existing_video_ids=["old"],
        )
        finish_playlist_update(
            self.db_path,
            terminal_id,
            status="verification_failed",
            actual_items=items("old-1", "old-2"),
            observation_complete=True,
            error="historical error",
            differences=[{"reason": "metadata_error"}],
            claim_token=terminal_token,
        )
        finish_playlist_update(
            self.db_path,
            legacy_id,
            status="verification_failed",
            error="legacy historical error",
            differences=[{"reason": "legacy_metadata_error"}],
        )
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        old = (now - timedelta(days=32)).isoformat()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE playlist_update_runs SET created_at=?, completed_at=CASE WHEN update_run_id IN (?,?) THEN ? ELSE completed_at END WHERE update_run_id IN (?,?,?)",
                (old, terminal_id, legacy_id, old, active_id, terminal_id, legacy_id),
            )
            conn.execute(
                "UPDATE playlist_update_items SET created_at=? WHERE update_run_id IN (?,?,?)",
                (old, active_id, terminal_id, legacy_id),
            )
        with patch("hype_db_store.utc_now_iso", return_value=now.isoformat()):
            record_playlist_update(
                self.db_path,
                playlist_id="housekeeping",
                service="apple",
                job_name="Fixture-Daily",
                requested_video_ids=["new"],
            )
        with connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM playlist_update_items WHERE update_run_id IN (?,?,?)",
                    (active_id, terminal_id, legacy_id),
                ).fetchone()[0],
                0,
            )
            terminal = dict(
                conn.execute(
                    "SELECT error,differences_json,recovery_payload_json FROM playlist_update_runs WHERE update_run_id=?",
                    (terminal_id,),
                ).fetchone()
            )
            legacy = dict(
                conn.execute(
                    "SELECT error,differences_json,recovery_payload_json FROM playlist_update_runs WHERE update_run_id=?",
                    (legacy_id,),
                ).fetchone()
            )
        self.assertEqual(terminal["error"], "historical error")
        self.assertEqual(json.loads(terminal["differences_json"]), [{"reason": "metadata_error"}])
        self.assertEqual(json.loads(terminal["recovery_payload_json"]), {"version": 1, "pruned": True})
        self.assertEqual(legacy["error"], "legacy historical error")
        self.assertEqual(
            json.loads(legacy["differences_json"]),
            [{"reason": "legacy_metadata_error"}],
        )
        self.assertEqual(
            json.loads(legacy["recovery_payload_json"]),
            {"version": 0, "manual_only": True, "pruned": True},
        )
        active = get_pending_playlist_recovery(self.db_path, "active")
        self.assertEqual(active["recovery_capability"], "exact")
        self.assertEqual(active["claim_token"], active_token)

    def test_sqlite_rebuild_preserves_legacy_failure_fields(self) -> None:
        raw_path = Path(self.directory.name) / "legacy.db"
        conn = sqlite3.connect(raw_path)
        conn.executescript(
            """
            CREATE TABLE playlist_update_runs(
                update_run_id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL,
                service TEXT, job_name TEXT, started_at TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0, requested_count INTEGER DEFAULT 0,
                existing_count INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'unknown',
                completed_at TEXT, error TEXT, differences_json TEXT NOT NULL DEFAULT '[]',
                match_started_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE playlist_update_items(
                update_run_id TEXT NOT NULL REFERENCES playlist_update_runs(update_run_id) ON DELETE CASCADE,
                action TEXT NOT NULL, video_id TEXT NOT NULL,
                item_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                PRIMARY KEY(update_run_id,action,video_id,item_order)
            );
            INSERT INTO playlist_update_runs VALUES(
                'legacy','playlist','apple','Fixture-Daily','2026-09-01',0,1,1,
                'recovery_required','2026-09-01','old failure','[{"reason":"old"}]','',
                '2026-09-01'
            );
            INSERT INTO playlist_update_items VALUES(
                'legacy','existing','old',1,'2026-09-01'
            );
            """
        )
        conn.commit()
        conn.close()
        init_db(raw_path)
        with connect(raw_path) as migrated:
            run = dict(migrated.execute("SELECT * FROM playlist_update_runs WHERE update_run_id='legacy'").fetchone())
            item = dict(migrated.execute("SELECT * FROM playlist_update_items WHERE update_run_id='legacy'").fetchone())
        self.assertEqual((run["error"], run["differences_json"]), ("old failure", '[{"reason":"old"}]'))
        self.assertEqual((run["evidence_version"], run["recovery_payload_json"]), (0, "{}"))
        self.assertEqual(item["set_video_id"], "")

        pending = get_pending_playlist_recovery(raw_path, "playlist")
        claimed = claim_playlist_update_recovery(
            raw_path,
            "legacy",
            expected_claim_token="",
            expected_state_fingerprint=pending["state_fingerprint"],
            workers_quiescent=True,
            attestation="legacy publisher is stopped",
        )
        observed = append_playlist_update_evidence(
            raw_path,
            "legacy",
            {
                "phase": "reconcile",
                "operation": "observe",
                "state": "verified",
                "chunk_order": 0,
                "attempt": 1,
                "items": [],
                "observation_complete": True,
            },
            claim_token=claimed["claim_token"],
            expected_state_fingerprint=claimed["state_fingerprint"],
        )
        finish_playlist_update(
            raw_path,
            "legacy",
            status="verification_failed",
            actual_items=[],
            observation_complete=True,
            error="observed without mutation",
            claim_token=claimed["claim_token"],
            expected_state_fingerprint=observed["state_fingerprint"],
        )
        finalized = get_playlist_update_run(raw_path, "legacy")
        outcomes = finalized["recovery_payload"]["outcomes"]
        self.assertEqual(outcomes[0]["error"], "old failure")
        self.assertTrue(outcomes[0]["preserved_legacy_state"])
        self.assertEqual(finalized["recovery_capability"], "manual_only")


if __name__ == "__main__":
    unittest.main()
