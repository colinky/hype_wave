from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hype_db_schema import init_schema
import hype_db_store as store
from heal_split_tracks import _canonical_video_for_same_yt_metadata
from repair_ytmusic_chart_incident import apply_repair, manifest_hash, mark_repair_stage, plan_repair, verify_repair
from sync_validation import PlaybackBlocked, assert_no_active_repair

OLD, NEW, SOURCE, UID = "old00000001", "new00000001", "src00000001", "existing-song"
META = {"video_id": NEW, "title": "REDRED", "artist": "CORTIS", "album": "GREENGREEN", "verified": True}
RAW = {"song_id": SOURCE, "rank": 1, "title": "REDRED", "artist": "CORTIS", "album": "GREENGREEN"}


def evidence(video_id, state="playable"):
    now = datetime.now(timezone.utc)
    return {"video_id": video_id, "state": state, "exact_id": True,
            "observed_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat(),
            "environment": "isolated-test", "auth_state": "authenticated", "run_health": "healthy",
            "has_audio": state == "playable", "confirmed_unavailable": state == "unavailable",
            "title": "REDRED", "artist": "CORTIS - Topic"}


def decision(*, incident=False):
    result = {"expected_track_uid": UID, "expected_video_id": OLD, "selected_video_id": NEW,
              "same_recording": True, "reason": "replace_unavailable_recording",
              "current_evidence": evidence(OLD, "unavailable"),
              "candidate_evidence": evidence(NEW), "metadata": dict(META)}
    if incident:
        result.update(reason="restore_unintended_chart_switch", current_evidence=evidence(OLD),
                      incident={"repair_id": "incident-1", "case_id": "redred", "evidence_ref": "sha256:fixture",
                                "original_video_id": NEW, "changed_video_id": OLD})
    return result


def seed(conn):
    init_schema(conn)
    store.ensure_track(conn, track_uid=UID, video_id=OLD, yt_title="REDRED", yt_artist="CORTIS",
                       yt_album="Compilation", status="matched", score=1)
    for service, song_id in (("ytmusic", SOURCE), ("melon", "1234"), ("apple", "5678")):
        store.upsert_track_list_metadata(conn, service=service, song_id=song_id, track_uid=UID, row=RAW)
    conn.commit()


def canonical(conn, uid=UID):
    row = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (uid,)).fetchone()
    return row[0] if row else None


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        seed(self.conn)


class CanonicalEvidenceTests(StorageFixture):
    def test_unavailable_replacement_updates_exact_metadata_and_rolls_back(self):
        result = store.apply_canonical_decision(self.conn, track_uid=UID, decision=decision(), source_row=RAW)
        self.assertEqual(result["video_id"], NEW)
        self.assertEqual(canonical(self.conn), NEW)
        self.assertEqual(tuple(self.conn.execute("SELECT yt_title,yt_artist,yt_album FROM tracks WHERE track_uid=?", (UID,)).fetchone()),
                         ("REDRED", "CORTIS", "GREENGREEN"))
        self.assertEqual(dict(self.conn.execute("SELECT video_id,is_canonical FROM yt_video_ids")), {OLD: 0, NEW: 1})
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(canonical(self.conn), OLD)

    def test_normal_current_playable_cannot_be_replaced(self):
        proposal = decision()
        proposal["current_evidence"] = evidence(OLD)
        with self.assertRaisesRegex(store.CanonicalDecisionError, "must be preserved"):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
        self.assertEqual(canonical(self.conn), OLD)

    def test_incident_allows_scoped_playable_to_playable_only(self):
        proposal = decision(incident=True)
        self.assertEqual(store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)["video_id"], NEW)
        self.conn.rollback()
        proposal["incident"]["original_video_id"] = OLD
        with self.assertRaises(store.CanonicalDecisionError):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
        self.assertEqual(canonical(self.conn), OLD)

    def test_unknown_stale_foreign_environment_and_unconfirmed_fail_without_writes(self):
        changes = (
            ("current_evidence", "state", "unknown"),
            ("current_evidence", "confirmed_unavailable", False),
            ("candidate_evidence", "auth_state", "unknown"),
            ("candidate_evidence", "video_id", OLD),
            ("candidate_evidence", "exact_id", False),
            ("candidate_evidence", "has_audio", False),
            ("candidate_evidence", "environment", "other-environment"),
            ("candidate_evidence", "expires_at", "2000-01-01T00:00:00+00:00"),
        )
        before = list(self.conn.iterdump())
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                proposal = decision()
                proposal[section][key] = value
                with self.assertRaises(store.CanonicalDecisionError):
                    store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_changed_binding_manual_policy_and_other_version_are_rejected(self):
        proposal = decision()
        proposal["expected_video_id"] = "stale000001"
        with self.assertRaises(store.CanonicalDecisionError):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
        wrong = {**RAW, "title": "REDRED (Live)"}
        with self.assertRaises(store.CanonicalDecisionError):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=decision(), source_row=wrong)
        self.conn.execute("INSERT INTO manual_overrides(service,song_id,action,updated_at) VALUES ('melon','1234','block','2026-09-14')")
        with self.assertRaises(store.CanonicalDecisionError):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=decision(), source_row=RAW)

    def test_single_and_bulk_store_the_verified_selection(self):
        for mode in ("single", "bulk"):
            with self.subTest(mode=mode):
                match = {**RAW, "video_id": NEW, "yt_title": META["title"], "yt_artist": META["artist"],
                         "yt_album": META["album"], "status": "matched", "score": 1.0, "canonical_decision": decision()}
                if mode == "single":
                    store.upsert_track_match(self.conn, service="ytmusic", source_row=RAW, match_row=match)
                else:
                    store._persist_crawled_tracks_impl(self.conn, "ytmusic", "Fixture", "default", "2026-09-14", "2026-W37", None, [RAW])
                    store.persist_crawl_run("unused", service="ytmusic", job_name="Fixture", chart_date="2026-09-14",
                                            reference_period="2026-W37", started_at="20260914T000000Z", tracks=[RAW], matches=[match],
                                            conn=self.conn, skip_playlist_order=True, commit=False)
                    self.assertEqual(tuple(self.conn.execute("SELECT video_id,status FROM match_attempts").fetchone()), (NEW, "matched"))
                    self.assertEqual(self.conn.execute("SELECT status FROM match_runs").fetchone()[0], "completed")
                self.assertEqual(canonical(self.conn), NEW)
                self.conn.rollback()
                self.assertEqual(canonical(self.conn), OLD)

    def test_bulk_unapproved_disagreement_cannot_complete(self):
        store._persist_crawled_tracks_impl(self.conn, "ytmusic", "Fixture", "default", "2026-09-14", "2026-W37", None, [RAW])
        match = {**RAW, "video_id": NEW, "status": "matched", "score": 1.0}
        with self.assertRaisesRegex(store.CanonicalDecisionError, "cannot complete"):
            store.persist_crawl_run("unused", service="ytmusic", job_name="Fixture", chart_date="2026-09-14",
                                    reference_period="2026-W37", started_at="20260914T000000Z", tracks=[RAW], matches=[match],
                                    conn=self.conn, skip_playlist_order=True, commit=False)
        self.assertEqual(canonical(self.conn), OLD)
        self.assertFalse(self.conn.execute("SELECT 1 FROM match_runs WHERE status='completed'").fetchone())

    def test_expected_alias_owner_merge_preserves_old_attempt_evidence(self):
        store.ensure_track(self.conn, track_uid="candidate-owner", video_id=NEW, yt_title="REDRED", yt_artist="CORTIS", yt_album="GREENGREEN")
        store.upsert_track_list_metadata(self.conn, service="ytmusic", song_id=NEW, track_uid="candidate-owner", row=RAW)
        self.conn.execute("INSERT INTO match_runs(run_id,service,job_name,started_at,created_at) VALUES ('old-run','ytmusic','Fixture','old','old')")
        self.conn.execute("INSERT INTO match_attempts(run_id,service,song_id,track_uid,rank_order,video_id,created_at) VALUES ('old-run','ytmusic',?,'candidate-owner',1,?,'old')", (NEW, NEW))
        proposal = decision()
        proposal["expected_target_uid"] = "candidate-owner"
        store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
        self.assertEqual(canonical(self.conn), NEW)
        self.assertEqual(self.conn.execute("SELECT track_uid FROM match_attempts WHERE run_id='old-run'").fetchone()[0], "candidate-owner")

    def test_heal_uses_stable_winner_not_latest_updated_id(self):
        rows = [dict(track_uid="old", canonical_yt_video_id=OLD, binding_count=3, match_status="matched", best_score=1,
                     created_at="2025", updated_at="2025"),
                dict(track_uid="new", canonical_yt_video_id=NEW, binding_count=1, match_status="matched", best_score=1,
                     created_at="2026", updated_at="2099")]
        self.assertEqual(_canonical_video_for_same_yt_metadata(rows), OLD)

    def test_raw_batches_share_the_callers_transaction_without_schema_init(self):
        before = list(self.conn.iterdump())
        with patch.object(store, "init_schema", side_effect=AssertionError("hidden schema initialization")):
            for variant in ("gen10", "gen20", "combined"):
                store.persist_crawled_tracks("unused", service="ytmusic", job_name="Fixture", source_variant=variant,
                                            chart_date="2026-09-14", reference_period="2026-W37", tracks=[RAW],
                                            conn=self.conn, commit=False)
        self.conn.rollback()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_merge_holds_different_recordings_even_when_metadata_is_equal(self):
        store.ensure_track(self.conn, track_uid="candidate-owner", video_id=NEW, yt_title="REDRED", yt_artist="CORTIS")
        self.conn.commit()
        before = list(self.conn.iterdump())
        for dry_run in (True, False):
            with self.assertRaisesRegex(store.CanonicalDecisionError, "unverified recording"):
                store._merge_track_uids(self.conn, loser_uid="candidate-owner", winner_uid=UID, canonical_video=NEW, dry_run=dry_run)
            self.assertEqual(list(self.conn.iterdump()), before)

    def test_identical_decision_across_lanes_records_one_proof_and_holds_changed_metadata(self):
        proposal = decision()
        for variant in ("gen10", "gen20"):
            store.persist_crawled_tracks("unused", service="ytmusic", job_name="Fixture", source_variant=variant,
                                        chart_date="2026-09-14", reference_period="2026-W37", tracks=[RAW], conn=self.conn, commit=False)
            match = {**RAW, "video_id": NEW, "status": "matched", "score": 1.0, "canonical_decision": proposal}
            store.persist_crawl_run("unused", service="ytmusic", job_name="Fixture", source_variant=variant,
                                    chart_date="2026-09-14", reference_period="2026-W37", started_at="20260914T000000Z",
                                    tracks=[RAW], matches=[match], conn=self.conn, skip_playlist_order=True, commit=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM match_runs WHERE status='completed'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM migration_reports WHERE source='canonical_decision'").fetchone()[0], 1)
        unchanged = list(self.conn.iterdump())
        store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)
        self.assertEqual(list(self.conn.iterdump()), unchanged)
        self.conn.execute("UPDATE tracks SET yt_album='unexpected edit' WHERE track_uid=?", (UID,))
        with self.assertRaisesRegex(store.CanonicalDecisionError, "different metadata"):
            store.apply_canonical_decision(self.conn, track_uid=UID, decision=proposal, source_row=RAW)


class IncidentRepairTests(StorageFixture):
    def spec(self):
        return {"repair_id": "incident-1", "implementation_revision": "fixture-revision", "cases": [
            {"case_id": "redred", "service": "ytmusic", "song_id": SOURCE,
             "decision": decision(incident=True), "source_row": dict(RAW)}]}

    def test_plan_apply_receipt_commit_and_resume_are_consistent(self):
        before = list(self.conn.iterdump())
        manifest = plan_repair(self.conn, self.spec())
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(apply_repair(self.conn, manifest)["status"], "db_verified")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(list(self.conn.iterdump()), before)
        apply_repair(self.conn, manifest)
        self.conn.commit()
        after = list(self.conn.iterdump())
        self.assertEqual(apply_repair(self.conn, manifest)["status"], "already_applied")
        self.assertEqual(list(self.conn.iterdump()), after)
        self.assertEqual(verify_repair(self.conn, manifest)["publication_status"], "pending")

    def test_stale_manifest_and_tampered_scope_are_rejected(self):
        manifest = plan_repair(self.conn, self.spec())
        tampered = copy.deepcopy(manifest)
        tampered["cases"][0]["decision"]["selected_video_id"] = OLD
        with self.assertRaises(ValueError):
            apply_repair(self.conn, tampered)
        self.conn.execute("UPDATE tracks SET yt_album='concurrent edit' WHERE track_uid=?", (UID,))
        with self.assertRaises(store.CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM migration_reports WHERE source='ytmusic_chart_incident'").fetchone())

    def test_receipt_insertion_failure_rolls_back_the_recording_change(self):
        manifest = plan_repair(self.conn, self.spec())
        self.conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON migration_reports BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_repair(self.conn, manifest)
        self.assertEqual(canonical(self.conn), OLD)

    def test_explicit_split_moves_only_proved_alias_and_sources(self):
        self.conn.execute("INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES (?,?,0)", (NEW, UID))
        self.conn.commit()
        proposal = decision()
        proposal.update(expected_track_uid="split-song", expected_video_id="", reason="new_match")
        case = {"case_id": "redred", "action": "split_binding", "service": "ytmusic", "song_id": SOURCE,
                "decision": proposal, "source_row": RAW, "evidence_ref": "sha256:before-merge",
                "bindings": [{"service": "ytmusic", "song_id": SOURCE, "expected_track_uid": UID}],
                "aliases": [{"video_id": NEW, "expected_track_uid": UID}]}
        spec = self.spec()
        spec["cases"] = [case]
        manifest = plan_repair(self.conn, spec)
        apply_repair(self.conn, manifest)
        self.assertEqual(canonical(self.conn), OLD)
        self.assertEqual(canonical(self.conn, "split-song"), NEW)
        self.assertEqual(store.find_track_by_service_song(self.conn, "melon", "1234"), UID)
        self.assertEqual(store.find_track_by_service_song(self.conn, "ytmusic", SOURCE), "split-song")

    def test_source_correction_and_translation_invalidation_are_explicit_and_scoped(self):
        self.conn.execute("UPDATE track_list SET title_en='Corrupt copy' WHERE service='ytmusic' AND song_id=?", (SOURCE,))
        self.conn.execute("CREATE TABLE ytmusic_song_translations(video_id TEXT PRIMARY KEY,title TEXT)")
        self.conn.executemany("INSERT INTO ytmusic_song_translations VALUES (?,?)", [(SOURCE, "wrong"), (OLD, "keep")])
        self.conn.commit()
        spec = self.spec()
        spec["cases"][0].update(
            source_metadata_repairs=[{"service": "ytmusic", "song_id": SOURCE, "values": {"title_en": "REDRED"},
                                      "evidence_ref": "sha256:upstream-source", "source_metadata_verified": True}],
            invalidate_translations=[{"video_id": SOURCE, "evidence_ref": "sha256:bad-video-translation"}],
        )
        before = list(self.conn.iterdump())
        apply_repair(self.conn, plan_repair(self.conn, spec))
        self.assertEqual(self.conn.execute("SELECT title_en FROM track_list WHERE service='ytmusic' AND song_id=?", (SOURCE,)).fetchone()[0], "REDRED")
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM ytmusic_song_translations")], [(OLD, "keep")])
        self.conn.rollback()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_unrelated_storage_mutation_and_code_drift_abort(self):
        store.ensure_track(self.conn, track_uid="unrelated", video_id="other000001", yt_title="Other", yt_artist="Someone")
        self.conn.commit()
        manifest = plan_repair(self.conn, self.spec())
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="different-code"):
            with self.assertRaisesRegex(store.CanonicalDecisionError, "implementation changed"):
                apply_repair(self.conn, manifest)
        self.conn.execute("CREATE TRIGGER poison_scope AFTER UPDATE ON tracks WHEN NEW.track_uid='existing-song' "
                          "BEGIN UPDATE tracks SET yt_album='bad' WHERE track_uid='unrelated'; END")
        with self.assertRaisesRegex(store.CanonicalDecisionError, "outside the reviewed scope"):
            apply_repair(self.conn, manifest)
        self.assertEqual(canonical(self.conn), OLD)
        self.assertNotEqual(self.conn.execute("SELECT yt_album FROM tracks WHERE track_uid='unrelated'").fetchone()[0], "bad")

    def test_expired_evidence_refresh_keeps_the_manifest_immutable(self):
        manifest = plan_repair(self.conn, self.spec())
        original = copy.deepcopy(manifest)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        refreshed = {vid: {**evidence(vid), "observed_at": (later - timedelta(seconds=1)).isoformat(),
                           "expires_at": (later + timedelta(minutes=20)).isoformat()} for vid in (OLD, NEW)}
        with patch.object(store, "datetime") as clock:
            clock.now.return_value = later
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.assertRaises(store.CanonicalDecisionError):
                apply_repair(self.conn, manifest)
            apply_repair(self.conn, manifest, evidence_refresh={"manifest_hash": manifest["manifest_hash"], "evidence": refreshed})
        self.assertEqual(manifest, original)
        receipt = json.loads(self.conn.execute("SELECT payload_json FROM migration_reports WHERE source='ytmusic_chart_incident'").fetchone()[0])
        self.assertEqual(receipt["evidence_refresh"]["manifest_hash"], manifest_hash(manifest))

    def test_surface_receipts_require_exact_readbacks_and_keep_followups_pending(self):
        digest = "a" * 64
        spec = self.spec()
        spec["outputs"] = {"playlists": [{"playlist_id": "playlist-1", "video_ids": [NEW],
                                          "preserved_items": [{"video_id": NEW, "set_video_id": "existing-slot"}]}],
                           "history": [{"path": "history/2026-09-14.json", "sha256": digest,
                                        "public_url": "https://example.test/history/2026-09-14.json"}]}
        manifest = plan_repair(self.conn, spec)
        with self.assertRaises(ValueError):
            mark_repair_stage(self.conn, manifest, "publication", {"evidence_ref": "no-db"})
        apply_repair(self.conn, manifest)
        self.conn.commit()
        observed_at = datetime.now(timezone.utc).isoformat()
        publication = {"evidence_ref": "sha256:published-read", "playlists": [
            {"playlist_id": "playlist-1", "video_ids": [NEW], "items": [{"video_id": NEW, "set_video_id": "existing-slot"}],
             "observed_at": observed_at, "readback_verified": True}]}
        wrong = copy.deepcopy(publication)
        wrong["playlists"][0]["items"][0]["set_video_id"] = "replacement-slot"
        with self.assertRaisesRegex(store.CanonicalDecisionError, "deleted and re-added"):
            mark_repair_stage(self.conn, manifest, "publication", wrong)
        mark_repair_stage(self.conn, manifest, "publication", publication)
        with self.assertRaises(PlaybackBlocked):
            assert_no_active_repair(self.conn)
        history = {"evidence_ref": "sha256:public-history", "files": [
            {"path": "history/2026-09-14.json", "sha256": digest, "public_sha256": digest,
             "public_url": "https://example.test/history/2026-09-14.json", "observed_at": observed_at,
             "readback_verified": True}]}
        wrong = copy.deepcopy(history)
        wrong["files"][0]["public_sha256"] = "b" * 64
        with self.assertRaises(store.CanonicalDecisionError):
            mark_repair_stage(self.conn, manifest, "history", wrong)
        result = mark_repair_stage(self.conn, manifest, "history", history)
        self.assertEqual(result["repair_status"], "surfaces_verified")
        self.assertEqual(result["followups"], {"daily": "pending", "monday": "pending", "device": "pending"})
        assert_no_active_repair(self.conn)
        before = list(self.conn.iterdump())
        self.assertEqual(mark_repair_stage(self.conn, manifest, "history", history)["status"], "already_verified")
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.rollback()
        with self.assertRaises(PlaybackBlocked):
            assert_no_active_repair(self.conn)

    def test_active_incident_blocks_standalone_raw_and_match_writers(self):
        apply_repair(self.conn, plan_repair(self.conn, self.spec()))
        with self.assertRaises(PlaybackBlocked):
            store.persist_crawled_tracks("unused", service="ytmusic", job_name="Fixture", chart_date="2026-09-14",
                                        reference_period="2026-W37", tracks=[RAW], conn=self.conn, commit=False)
        with self.assertRaises(PlaybackBlocked):
            store.upsert_track_match(self.conn, service="ytmusic", source_row=RAW,
                                     match_row={**RAW, "video_id": NEW, "status": "matched"})


class MoveEvidenceTests(unittest.TestCase):
    def event(self):
        before = [{"video_id": "A", "set_video_id": "slot-a", "position": 1},
                  {"video_id": "B", "set_video_id": "slot-b", "position": 2}]
        after = [{**before[1], "position": 1}, {**before[0], "position": 2}]
        return {"phase": "publish", "operation": "move", "state": "intent", "items": [before[1]],
                "before_items": before, "after_items": after, "target_before_set_video_id": "slot-a"}

    def test_move_ack_preserves_every_slot_and_matches_exact_intent(self):
        intent = store._normalize_playlist_evidence_event(self.event())
        intent["seq"] = 1
        ack = store._normalize_playlist_evidence_event({**self.event(), "state": "ack", "intent_seq": 1})
        store._assert_receipt_matches_intent({"events": [intent]}, ack)
        ack["target_before_set_video_id"] = ""
        with self.assertRaises(ValueError):
            store._assert_receipt_matches_intent({"events": [intent]}, ack)

    def test_move_cannot_substitute_an_id_or_ownership_token(self):
        for field in ("video_id", "set_video_id"):
            event = self.event()
            event["after_items"][0][field] = "other"
            with self.assertRaises(ValueError):
                store._normalize_playlist_evidence_event(event)

    def test_verified_full_recovery_supersedes_partial_policy_without_erasing_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
            path = Path(directory) / "audit.db"
            token = "test-owner"
            one = [{"video_id": OLD, "set_video_id": "slot-old"}]
            both = [*one, {"video_id": NEW, "set_video_id": "slot-new"}]
            run_id = store.record_playlist_update(path, playlist_id="playlist", service="ytmusic", job_name="Fixture",
                                                 requested_video_ids=[OLD, NEW], existing_items=one, claim_token=token)
            partial = {"phase": "publish", "operation": "observe", "state": "verified", "publication_mode": "partial",
                       "items": one, "observation_complete": True, "verification_matches": True,
                       "effective_video_ids": [OLD], "excluded_items": [
                           {"position": 2, "requested_video_id": NEW, "actual_video_id": "wrong", "reason": "identity_mismatch"}]}
            store.append_playlist_update_evidence(path, run_id, partial, claim_token=token)
            store.finish_playlist_update(path, run_id, status="recovery_required", actual_items=one, claim_token=token)
            with self.assertRaisesRegex(RuntimeError, "Partial publication"):
                store.finish_playlist_update(path, run_id, status="published", actual_items=both, claim_token=token)
            full = {"phase": "reconcile", "operation": "observe", "state": "verified", "items": both,
                    "observation_complete": True, "verification_matches": True}
            store.append_playlist_update_evidence(path, run_id, full, claim_token=token)
            with self.assertRaisesRegex(RuntimeError, "Full recovery finish"):
                store.finish_playlist_update(path, run_id, status="published", actual_items=one, claim_token=token)
            store.finish_playlist_update(path, run_id, status="published", actual_items=both, claim_token=token)
            result = store.get_playlist_update_run(path, run_id)
            self.assertEqual(result["publication_mode"], "full")
            self.assertEqual(result["effective_video_ids"], [OLD, NEW])
            self.assertEqual(result["recovery_payload"]["events"][0]["publication_mode"], "partial")


if __name__ == "__main__":
    unittest.main()
