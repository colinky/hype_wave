"""Publication acceptance checks using SQLite and an in-memory provider only."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import publish_chart_repair as publish
import ytmusic_playlist_sync as playlist_sync
from hype_db_reports import compact_frontend_history
from hype_db_schema import init_schema
from hype_db_store import ensure_track, upsert_track_list_metadata
from repair_ytmusic_chart_incident import (
    _receipt, _validate_manifest, apply_repair, covered_parent_repair_ids, fingerprint, manifest_hash,
    mark_repair_stage, plan_repair, verify_repair,
)
from sync_validation import PlaybackBlocked

OLD, NEW, KEEP, UID = "old00000001", "new00000001", "keep0000001", "fixture-track"
DAY = "2026-09-14"


def evidence(video, state="playable"):
    now = datetime.now(timezone.utc)
    return {"video_id": video, "state": state, "exact_id": True, "environment": "fixture",
            "auth_state": "authenticated", "run_health": "healthy", "has_audio": state == "playable",
            "confirmed_unavailable": state == "unavailable", "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat()}


class Verifier:
    environment = "fixture"

    def __init__(self):
        self.unavailable = set()

    def check_health(self):
        return {"run_health": "healthy", "auth_state": "authenticated"}

    def verify(self, video, **kwargs):
        return evidence(video, "unknown" if video in self.unavailable else "playable")


class PublishRepairTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("External access forbidden")))
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.folder / "repair.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn)
        self.conn.execute("CREATE TABLE ytmusic_song_translations(video_id TEXT PRIMARY KEY,title TEXT,updated_at TEXT)")
        ensure_track(self.conn, track_uid=UID, video_id=OLD, yt_title="REDRED", yt_artist="CORTIS", yt_album="Album")
        raw = {"song_id": "source-song", "title": "REDRED", "artist": "CORTIS", "album": "Album"}
        upsert_track_list_metadata(self.conn, service="ytmusic", song_id="source-song", track_uid=UID, row=raw)
        self.conn.execute("INSERT INTO match_runs(run_id,service,job_name,started_at,created_at) "
                          "VALUES ('fixture-run','ytmusic','Fixture','before','before')")
        self.conn.execute("INSERT INTO match_attempts(run_id,service,song_id,track_uid,video_id,created_at) "
                          "VALUES ('fixture-run','ytmusic','source-song',?,?,'before')", (UID, OLD))
        self.conn.commit()
        self.before = [{"video_id": OLD, "set_video_id": "old-slot"},
                       {"video_id": KEEP, "set_video_id": "keep-slot"}]
        self.actual = {"playlist-1": copy.deepcopy(self.before)}
        self.history_path = self.folder / "history.json"
        self.preview_path = self.folder / "preview.json"
        def payload(video):
            return compact_frontend_history({DAY: [{"video_id": video, "title": "REDRED", "artist": "CORTIS",
                                                    "hype_rank": 1, "hype_index": 1.0}]}, generated_at="fixture")
        self.history_path.write_text(json.dumps(payload(OLD), ensure_ascii=False, indent=2))
        self.preview_path.write_text(json.dumps(payload(NEW), ensure_ascii=False, indent=2))
        outputs = {"business_fingerprint": "projecting", "playlists": [{"playlist_id": "playlist-1", "service": "ytmusic",
                   "job_name": "Fixture", "video_ids": [NEW, KEEP], "before_items": self.before,
                   "preserved_items": [self.before[1]]}], "history": [{"path": str(self.history_path),
                   "preview_path": str(self.preview_path), "expected_date": DAY,
                   "sha256": hashlib.sha256(self.preview_path.read_bytes()).hexdigest(),
                   "before_sha256": hashlib.sha256(self.history_path.read_bytes()).hexdigest(),
                   "public_url": "https://example.invalid/history.json"}]}
        proposal = {"expected_track_uid": UID, "expected_video_id": OLD, "selected_video_id": NEW,
                    "same_recording": True, "reason": "replace_unavailable_recording",
                    "current_evidence": evidence(OLD, "unavailable"), "candidate_evidence": evidence(NEW),
                    "metadata": {"video_id": NEW, "title": "REDRED", "artist": "CORTIS", "album": "Album", "verified": True}}
        self.spec = {"repair_id": "publish-fixture", "implementation_revision": "fixture", "outputs": outputs,
                     "cases": [{"case_id": "redred", "service": "ytmusic", "song_id": "source-song",
                                "source_row": raw, "decision": proposal}]}
        projected = plan_repair(self.conn, self.spec)
        apply_repair(self.conn, projected)
        outputs["business_fingerprint"] = publish.business_fingerprint(self.conn)
        self.conn.rollback()
        self.manifest = plan_repair(self.conn, self.spec)
        apply_repair(self.conn, self.manifest)
        self.conn.commit()
        self.verifier = Verifier()
        self.client = object()
        self.mutations = []
        self.read = self.enterContext(patch.object(publish, "get_existing_playlist_items",
                                                  side_effect=lambda client, playlist: copy.deepcopy(self.actual[playlist])))
        def update(client, playlist, videos, **kwargs):
            kwargs["before_mutation"]()
            self.mutations.append(playlist)
            existing = {row["video_id"]: row for row in self.actual[playlist]}
            self.actual[playlist] = [existing.get(video, {"video_id": video, "set_video_id": "new-" + video})
                                     for video in videos]
        self.update = self.enterContext(patch.object(publish, "update_ytmusic_playlist", side_effect=update))

    def publish(self):
        return publish.publish(self.manifest, self.db, self.client, verifier=self.verifier)

    def receipt(self):
        row = self.conn.execute("SELECT payload_json FROM migration_reports WHERE source='ytmusic_chart_incident'").fetchone()
        return json.loads(row[0])

    def alter_receipt(self, change):
        receipt = self.receipt()
        change(receipt)
        self.conn.execute("UPDATE migration_reports SET payload_json=? WHERE source='ytmusic_chart_incident'",
                          (json.dumps(receipt),))
        self.conn.commit()

    def pending(self):
        self.conn.execute("INSERT INTO playlist_update_runs(update_run_id,playlist_id,service,job_name,status,started_at,created_at) "
                          "VALUES ('pending','playlist-1','ytmusic','Fixture','recovery_required','now','now')")
        self.conn.commit()

    def public_response(self, *, status=200, content=None):
        return Mock(status_code=status, content=self.preview_path.read_bytes() if content is None else content,
                    raise_for_status=Mock())

    def execution_release(self, new_fingerprint="a" * 64):
        release = {"version": 1, "manifest_hash": self.manifest["manifest_hash"],
                   "old_implementation_fingerprint": self.manifest["implementation_fingerprint"],
                   "new_implementation_fingerprint": new_fingerprint,
                   "applied_receipt_core_hash": publish.applied_receipt_core_hash(self.receipt()),
                   "outputs_fingerprint": fingerprint(self.manifest["outputs"]),
                   "evidence_ref": "fixture:reviewed-audit-preservation-regression"}
        release["release_hash"] = fingerprint(release)
        return release

    def test_execution_release_publishes_and_verifies_without_reapplying_or_changing_core(self):
        release = self.execution_release()
        original = copy.deepcopy((self.manifest, release))
        original_core = publish.applied_receipt_core_hash(self.receipt())
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="a" * 64), \
                patch("repair_ytmusic_chart_incident.apply_repair", side_effect=AssertionError("No DB reapply")), \
                patch("requests.get", return_value=self.public_response()):
            with self.assertRaisesRegex(PlaybackBlocked, "implementation"):
                self.publish()
            with self.assertRaisesRegex(PlaybackBlocked, "verified playlist publication"):
                publish.install_history(self.manifest, self.db, execution_release=release)
            result = publish.publish(self.manifest, self.db, self.client, verifier=self.verifier,
                                     execution_release=release)
            self.assertEqual(result["evidence"]["execution_release_hash"], release["release_hash"])
            publish.install_history(self.manifest, self.db, execution_release=release)
            result = publish.verify_public_history(self.manifest, self.db, self.client,
                                                   verifier=self.verifier, execution_release=release)
        self.assertEqual(result["repair_status"], "surfaces_verified")
        self.assertEqual(result["evidence"]["execution_release_hash"], release["release_hash"])
        self.assertEqual(publish.applied_receipt_core_hash(self.receipt()), original_core)
        self.assertEqual((self.manifest, release), original)
        self.assertEqual(self.mutations, ["playlist-1"])
        self.assertEqual(self.actual["playlist-1"][1], self.before[1])

    def test_execution_release_rejects_wrong_bindings_missing_evidence_and_stale_runtime(self):
        original = self.execution_release()
        invalid = []
        for key in ("manifest_hash", "old_implementation_fingerprint", "new_implementation_fingerprint",
                    "applied_receipt_core_hash", "outputs_fingerprint"):
            release = copy.deepcopy(original)
            release[key] = "b" * 64
            release["release_hash"] = fingerprint({k: v for k, v in release.items() if k != "release_hash"})
            invalid.append(release)
        for key, value in (("evidence_ref", " "), ("version", True), ("release_hash", "b" * 64)):
            release = {**original, key: value}
            invalid.append(release)
        invalid.append({k: v for k, v in original.items() if k != "evidence_ref"})
        before = list(self.conn.iterdump())
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="a" * 64):
            for release in invalid:
                with self.subTest(release=release), self.assertRaisesRegex(PlaybackBlocked, "Execution release"):
                    publish._guard(self.db, self.manifest, execution_release=release)
        # A subsequent code or identity-policy change has a different composite fingerprint.
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="c" * 64), \
                self.assertRaisesRegex(PlaybackBlocked, "Execution release"):
            publish._guard(self.db, self.manifest, execution_release=original)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.mutations, [])

    def test_execution_release_rejects_receipt_core_changes_but_allows_normal_stage_fields(self):
        release = self.execution_release()
        original = self.receipt()
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="a" * 64):
            for field in ("repair_id", "manifest_hash", "implementation_fingerprint", "created_at",
                          "before", "after", "outputs", "supersedes", "implementation_revision",
                          "evidence_refresh", "future_apply_evidence"):
                self.alter_receipt(lambda row, field=field: row.update({field: "changed"}))
                with self.subTest(field=field), self.assertRaises(PlaybackBlocked):
                    publish._guard(self.db, self.manifest, execution_release=release)
                self.alter_receipt(lambda row: (row.clear(), row.update(copy.deepcopy(original))))
            publish.publish(self.manifest, self.db, self.client, verifier=self.verifier, execution_release=release)
            self.assertEqual(publish.applied_receipt_core_hash(self.receipt()), release["applied_receipt_core_hash"])
            publish._guard(self.db, self.manifest, publication_required=True, execution_release=release)
            self.alter_receipt(lambda row: row["stages"].update(db="not_applied"))
            with self.assertRaisesRegex(PlaybackBlocked, "applied DB receipt"):
                publish._guard(self.db, self.manifest, execution_release=release)

    def test_execution_release_keeps_pending_business_and_playability_guards(self):
        release = self.execution_release()
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="a" * 64):
            self.pending()
            with self.assertRaisesRegex(PlaybackBlocked, "pending durable"):
                publish.publish(self.manifest, self.db, self.client, verifier=self.verifier,
                                execution_release=release)
            self.conn.execute("UPDATE playlist_update_runs SET status='verification_failed' WHERE update_run_id='pending'")
            self.conn.commit()
            with self.assertRaisesRegex(PlaybackBlocked, "original publication audit changed"):
                publish._guard(self.db, self.manifest, execution_release=release)
            self.conn.execute("DELETE FROM playlist_update_runs WHERE update_run_id='pending'")
            self.conn.commit()
            self.verifier.unavailable.add(NEW)
            with self.assertRaises(PlaybackBlocked):
                publish.publish(self.manifest, self.db, self.client, verifier=self.verifier,
                                execution_release=release)
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.receipt()["stages"]["publication"], "pending")

    def test_cli_passes_explicit_execution_release_to_every_phase(self):
        from contextlib import nullcontext, redirect_stdout
        import io
        manifest_path, release_path = self.folder / "manifest.json", self.folder / "release.json"
        manifest_path.write_text(json.dumps(self.manifest))
        release = self.execution_release()
        release_path.write_text(json.dumps(release))
        for phase, function in (("publish", "publish"), ("install-history", "install_history"),
                                ("verify-public-history", "verify_public_history")):
            args = ["publish_chart_repair.py", phase, "--manifest", str(manifest_path),
                    "--execution-release", str(release_path), "--db-path", str(self.db),
                    "--output", str(self.folder / (phase + ".json"))]
            with self.subTest(phase=phase), patch("sys.argv", args), patch("sync_all.load_env_file"), \
                    patch.object(publish, "sync_run_lock", return_value=nullcontext()), \
                    patch.object(publish, "make_ytmusic", return_value=self.client), \
                    patch.object(publish, function, return_value={"status": "fixture"}) as run, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(publish.main(), 0)
            self.assertEqual(run.call_args.kwargs["execution_release"], release)

    def child_spec(self):
        """Fixture-only compensating choice; no actual provider identity approval."""
        parent_receipt = _receipt(self.conn, self.manifest)
        spec = copy.deepcopy(self.spec)
        spec.update(repair_id="followup-fixture", supersedes={"manifest": self.manifest,
                                                            "receipt_fingerprint": fingerprint(parent_receipt)})
        change = spec["cases"][0]["decision"]
        change.update(expected_video_id=NEW, selected_video_id=OLD, current_evidence=evidence(NEW),
                      candidate_evidence=evidence(OLD), reason="restore_unintended_chart_switch",
                      metadata={**change["metadata"], "video_id": OLD},
                      incident={"repair_id": spec["repair_id"], "case_id": "redred", "evidence_ref": "fixture-only",
                                "original_video_id": OLD, "changed_video_id": NEW})
        spec["outputs"]["playlists"][0]["video_ids"] = [OLD, KEEP]
        spec["outputs"]["playlists"][0]["preserved_items"] = copy.deepcopy(self.before)
        from hype_db_reports import inflate_frontend_history
        rows = inflate_frontend_history(json.loads(self.preview_path.read_text()))
        rows[DAY][0].update(video_id=OLD, identity_key=NEW)
        payload = compact_frontend_history(rows, generated_at="child-fixture")
        path = self.folder / "child-preview.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        spec["outputs"]["history"][0].update(preview_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return spec

    def test_followup_applies_once_preserves_parent_and_requires_both_full_surfaces(self):
        parent = _receipt(self.conn, self.manifest)
        child = plan_repair(self.conn, self.child_spec())
        apply_repair(self.conn, child)
        self.assertEqual(_receipt(self.conn, self.manifest), parent)
        after = list(self.conn.iterdump())
        self.assertEqual(apply_repair(self.conn, child)["status"], "already_applied")
        self.assertEqual(list(self.conn.iterdump()), after)
        receipts = lambda: [parent, _receipt(self.conn, child)]
        self.assertEqual(covered_parent_repair_ids(receipts()), set())
        now = datetime.now(timezone.utc).isoformat()
        pub = {"evidence_ref": "fixture-pub", "playlists": [{"playlist_id": "playlist-1",
               "video_ids": [OLD, KEEP], "items": self.before, "observed_at": now, "readback_verified": True}]}
        mark_repair_stage(self.conn, child, "publication", pub)
        self.assertEqual(covered_parent_repair_ids(receipts()), set())
        history = child["outputs"]["history"][0]
        mark_repair_stage(self.conn, child, "history", {"evidence_ref": "fixture-history", "files": [
            {"path": history["path"], "sha256": history["sha256"], "public_sha256": history["sha256"],
             "public_url": history["public_url"], "observed_at": now, "readback_verified": True}]})
        self.assertEqual(covered_parent_repair_ids(receipts()), {self.manifest["repair_id"]})
        self.assertEqual(_receipt(self.conn, self.manifest), parent)
        forged = copy.deepcopy(receipts())
        forged[1]["stage_evidence"]["publication"]["playlists"] = []
        with self.assertRaisesRegex(ValueError, "every parent surface"):
            covered_parent_repair_ids(forged)

    def test_followup_refuses_missing_scope_reorder_and_unrelated_selection(self):
        for mutate in (
            lambda spec: spec["outputs"].update(playlists=[]),
            lambda spec: spec["outputs"].update(history=[]),
            lambda spec: spec["outputs"]["playlists"][0].update(video_ids=[KEEP, OLD]),
            lambda spec: spec["outputs"]["playlists"][0].update(video_ids=[OLD]),
            lambda spec: spec["cases"][0].update(source_metadata_repairs=[{"service": "ytmusic", "song_id": "source-song"}]),
        ):
            spec = self.child_spec()
            mutate(spec)
            before = list(self.conn.iterdump())
            with self.subTest(spec=spec["outputs"]), self.assertRaises(ValueError):
                plan_repair(self.conn, spec)
            self.assertEqual(list(self.conn.iterdump()), before)

    def test_followup_stale_parent_fork_and_grandchild_are_blocked(self):
        child = plan_repair(self.conn, self.child_spec())
        self.alter_receipt(lambda receipt: receipt.update(note="concurrent change"))
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, "parent receipt changed"):
            apply_repair(self.conn, child)
        self.assertEqual(list(self.conn.iterdump()), before)
        child = plan_repair(self.conn, self.child_spec())
        apply_repair(self.conn, child)
        self.conn.execute("INSERT INTO migration_reports(report_id,source,created_at,payload_json) VALUES (?,?,?,?)",
                          ("chart-repair:fork", "ytmusic_chart_incident", "fixture",
                           json.dumps({"repair_id": "fork", "supersedes": {"repair_id": self.manifest["repair_id"]}})))
        with self.assertRaisesRegex(ValueError, "forks"):
            verify_repair(self.conn, child)
        spec = copy.deepcopy(child)
        spec["supersedes"]["manifest"] = child
        spec["manifest_hash"] = manifest_hash(spec)
        with self.assertRaisesRegex(ValueError, "original parent"):
            _validate_manifest(spec)

    def test_followup_history_preserves_identity_scores_sources_and_all_dates(self):
        from hype_db_reports import inflate_frontend_history
        for change in (lambda rows: rows[DAY][0].update(hype_index=999),
                       lambda rows: rows[DAY][0].update(identity_key="wrong-recording"),
                       lambda rows: rows[DAY][0].update(apple_url="https://wrong.invalid/song"),
                       lambda rows: rows.update({"2026-09-13": copy.deepcopy(rows[DAY])})):
            spec = self.child_spec()
            target = spec["outputs"]["history"][0]
            path = Path(target["preview_path"])
            rows = inflate_frontend_history(json.loads(path.read_text()))
            change(rows)
            path.write_text(json.dumps(compact_frontend_history(rows), ensure_ascii=False, indent=2))
            target["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            child = plan_repair(self.conn, spec)
            with self.assertRaises(PlaybackBlocked):
                publish._read_previews(child["outputs"], manifest=child)
        child = plan_repair(self.conn, self.child_spec())
        self.assertEqual(publish._read_previews(child["outputs"], manifest=child)[1], [OLD])

    def test_followup_cannot_claim_a_new_counterpart_was_the_original_canonical(self):
        spec = self.child_spec()
        proposal = spec["cases"][0]["decision"]
        proposal.update(selected_video_id=KEEP, candidate_evidence=evidence(KEEP),
                        metadata={**proposal["metadata"], "video_id": KEEP})
        proposal["incident"]["original_video_id"] = KEEP
        spec["outputs"]["playlists"][0]["video_ids"] = [KEEP]
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, "parent's original canonical"):
            plan_repair(self.conn, spec)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_followup_checks_parent_before_apply_and_after_changes(self):
        child = plan_repair(self.conn, self.child_spec())
        ensure_track(self.conn, track_uid="unrelated", video_id=KEEP)
        self.conn.execute("UPDATE platform_song_ids SET track_uid='unrelated' WHERE service='ytmusic'")
        before = list(self.conn.iterdump())
        with self.assertRaises(ValueError):
            apply_repair(self.conn, child)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.rollback()
        child = plan_repair(self.conn, self.child_spec())
        apply_repair(self.conn, child)
        self.conn.execute("DELETE FROM yt_video_ids WHERE video_id=?", (NEW,))
        with self.assertRaises(ValueError):
            verify_repair(self.conn, child)

    def test_followup_receipt_link_cannot_be_swapped_after_apply(self):
        child = plan_repair(self.conn, self.child_spec())
        apply_repair(self.conn, child)
        receipt = _receipt(self.conn, child)
        receipt["supersedes"]["repair_id"] = "unrelated-parent"
        self.conn.execute("UPDATE migration_reports SET payload_json=? WHERE report_id=?",
                          (json.dumps(receipt), "chart-repair:" + child["repair_id"]))
        with self.assertRaisesRegex(ValueError, "Effective parent"):
            verify_repair(self.conn, child)

    def test_publish_resume_preserves_shared_slots_and_never_repeats_mutation(self):
        before = publish.business_fingerprint(self.conn)
        self.assertEqual(self.publish()["status"], "verified")
        self.assertEqual(self.mutations, ["playlist-1"])
        self.assertEqual(self.actual["playlist-1"][1], self.before[1])
        self.assertEqual(self.update.call_args.kwargs["expected_before_items"], self.before)
        self.assertEqual(self.publish()["status"], "already_verified")
        self.assertEqual(self.mutations, ["playlist-1"])
        self.assertEqual(publish.business_fingerprint(self.conn), before)

    def test_managed_publication_starts_fresh_observations_after_each_phase(self):
        phases = []
        class BoundedVerifier(Verifier):
            expired = False
            def check_health(self):
                return {"run_health": "unknown" if self.expired else "healthy",
                        "auth_state": "authenticated"}
        def factory(client, *, fresh=False):
            self.assertIs(client, self.client)
            self.assertTrue(fresh)
            for previous in phases:
                previous.expired = True
            verifier = BoundedVerifier()
            phases.append(verifier)
            return verifier
        original_update = self.update.side_effect
        def update(*args, **kwargs):
            self.assertIs(kwargs["playability_verifier"], phases[-1])
            original_update(*args, **kwargs)
            phases[-1].expired = True
        self.update.side_effect = update
        with patch.object(publish, "verifier_for", side_effect=factory):
            result = publish.publish(self.manifest, self.db, self.client)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(phases), 3)
        self.assertEqual(self.actual["playlist-1"][1], self.before[1])

    def test_new_phase_still_rejects_unknown_target_before_any_mutation(self):
        phases = []
        def factory(client, *, fresh=False):
            verifier = Verifier()
            phases.append(verifier)
            if len(phases) == 2:
                verifier.unavailable.add(NEW)
            return verifier
        with patch.object(publish, "verifier_for", side_effect=factory), self.assertRaises(PlaybackBlocked):
            publish.publish(self.manifest, self.db, self.client)
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.receipt()["stages"]["publication"], "pending")

    def test_fresh_factory_constructs_a_new_verifier_with_the_current_policy(self):
        from sync_validation import verifier_for
        client = Mock()
        first, second = Verifier(), Verifier()
        with patch("ytmusic_playability.PlayabilityVerifier", side_effect=[first, second]) as constructor:
            self.assertIs(verifier_for(client), first)
            self.assertIs(verifier_for(client), first)
            self.assertIs(verifier_for(client, fresh=True), second)
            self.assertIs(verifier_for(client), second)
        self.assertEqual(constructor.call_count, 2)
        self.assertEqual(constructor.call_args_list[0], constructor.call_args_list[1])

    def test_exact_target_with_pending_audit_is_blocked_without_mutation_or_stage(self):
        self.actual["playlist-1"] = [{"video_id": NEW, "set_video_id": "new-slot"}, self.before[1]]
        self.pending()
        with self.assertRaisesRegex(PlaybackBlocked, "pending durable"):
            self.publish()
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.receipt()["stages"]["publication"], "pending")

    def test_interruption_after_publication_resumes_without_republishing(self):
        with patch.object(publish, "mark_repair_stage", side_effect=RuntimeError("interrupted receipt")):
            with self.assertRaisesRegex(RuntimeError, "interrupted receipt"):
                self.publish()
        self.assertEqual(self.mutations, ["playlist-1"])
        self.assertEqual(self.receipt()["stages"]["publication"], "pending")
        self.assertEqual(self.publish()["status"], "verified")
        self.assertEqual(self.mutations, ["playlist-1"])

    def test_external_change_and_readded_preserved_slot_are_blocked(self):
        for actual in ([{"video_id": OLD, "set_video_id": "changed-owner"}, self.before[1]],
                       [{"video_id": NEW, "set_video_id": "new-slot"}, {"video_id": KEEP, "set_video_id": "readded"}]):
            self.actual["playlist-1"] = actual
            with self.assertRaises(PlaybackBlocked):
                self.publish()
        self.assertEqual(self.mutations, [])

    def test_matching_evidence_and_run_order_changes_invalidate_business_snapshot(self):
        before = publish.business_fingerprint(self.conn)
        self.conn.execute("UPDATE match_attempts SET video_id=?", (KEEP,))
        self.assertNotEqual(before, publish.business_fingerprint(self.conn))
        self.conn.rollback()
        self.conn.execute("UPDATE match_runs SET created_at='changed'")
        self.conn.commit()
        with self.assertRaisesRegex(PlaybackBlocked, "DB source"):
            self.publish()
        self.assertEqual(self.mutations, [])

    def test_only_this_repairs_new_audit_is_excluded_from_business_fingerprint(self):
        def add(run, job):
            self.conn.execute("INSERT INTO playlist_update_runs(update_run_id,playlist_id,service,job_name,status,started_at,created_at) "
                              "VALUES (?,'playlist-1','ytmusic',?,'published','now','now')", (run, job))
            self.conn.execute("INSERT INTO playlist_update_items(update_run_id,action,video_id,set_video_id,item_order,created_at) "
                              "VALUES (?,'actual',?,'slot',1,'now')", (run, NEW))
        repair_id = self.manifest["repair_id"]
        before = publish.business_fingerprint(self.conn, repair_id=repair_id)
        add("own", "repair:" + repair_id + ":Fixture")
        self.assertEqual(before, publish.business_fingerprint(self.conn, repair_id=repair_id))
        add("other", "repair:" + repair_id + "-other:Fixture")
        self.assertNotEqual(before, publish.business_fingerprint(self.conn, repair_id=repair_id))
        prior = publish.business_fingerprint(self.conn, repair_id=repair_id)
        self.conn.execute("UPDATE playlist_update_items SET video_id=? WHERE update_run_id='other'", (OLD,))
        self.assertNotEqual(prior, publish.business_fingerprint(self.conn, repair_id=repair_id))

    def test_provider_reread_must_match_reviewed_video_and_token_order_before_audit(self):
        variants = ([{"video_id": NEW, "set_video_id": "old-slot"}, self.before[1]],
                    list(reversed(self.before)),
                    [{"video_id": OLD, "set_video_id": "changed"}, self.before[1]])
        for actual in variants:
            provider_items = [{"videoId": row["video_id"], "setVideoId": row["set_video_id"]} for row in actual]
            with self.subTest(actual=actual), patch.object(playlist_sync, "get_existing_playlist_items", return_value=provider_items), \
                    patch("hype_db.record_playlist_update", side_effect=AssertionError("No audit before before-state validation")), \
                    self.assertRaisesRegex(PlaybackBlocked, "reviewed before-image"):
                playlist_sync.update_ytmusic_playlist(self.client, "playlist-1", [NEW, KEEP], dry_run=False,
                    db_path=self.db, playability_verifier=self.verifier, expected_before_items=self.before)

    def test_rehearsal_timestamp_drift_is_ignored_but_decision_evidence_is_not(self):
        before = publish.business_fingerprint(self.conn)
        self.conn.execute("UPDATE tracks SET updated_at='commit-time'")
        self.conn.execute("UPDATE migration_reports SET created_at='commit-time' WHERE source='canonical_decision'")
        self.assertEqual(before, publish.business_fingerprint(self.conn))
        self.conn.execute("UPDATE migration_reports SET payload_json='{}' WHERE source='canonical_decision'")
        self.assertNotEqual(before, publish.business_fingerprint(self.conn))

    def test_postgres_json_numbers_and_timestamp_format_match_the_sqlite_projection(self):
        self.conn.execute("INSERT INTO ytmusic_song_translations VALUES (?, 'Translated', '2026-09-14 10:00:00+00:00')", (KEEP,))
        records = []
        for table in publish.BUSINESS_TABLES:
            for row in self.conn.execute("SELECT * FROM " + table):
                record = {key: int(value) if isinstance(value, float) and value.is_integer() else value
                          for key, value in dict(row).items()}
                if table == "ytmusic_song_translations":
                    record["updated_at"] = "2026-09-14T19:00:00+09:00"
                records.append({"table_name": table, "payload": json.dumps(record)})
        class PostgresConnectionWrapper:
            def execute(self, query, params):
                return Mock(fetchall=lambda: records)
        self.assertEqual(publish.business_fingerprint(self.conn),
                         publish.business_fingerprint(PostgresConnectionWrapper()))

    def test_unapplied_or_changed_db_cannot_publish(self):
        self.alter_receipt(lambda receipt: receipt["stages"].update(db="pending"))
        with self.assertRaisesRegex(PlaybackBlocked, "applied DB"):
            self.publish()
        self.alter_receipt(lambda receipt: receipt["stages"].update(db="applied"))
        self.conn.execute("UPDATE tracks SET canonical_yt_video_id=?", (KEEP,))
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.mutations, [])

    def test_changed_identity_policy_blocks_reviewed_publication(self):
        with patch("repair_ytmusic_chart_incident.implementation_fingerprint", return_value="changed-policy"):
            with self.assertRaisesRegex(PlaybackBlocked, "identity policy changed"):
                self.publish()
        self.assertEqual(self.mutations, [])

    def test_scope_and_expected_history_date_are_validated_before_external_mutation(self):
        for change in (lambda outputs: outputs["playlists"].append(copy.deepcopy(outputs["playlists"][0])),
                       lambda outputs: outputs["playlists"][0].update(preserved_items=[]),
                       lambda outputs: outputs["history"][0].update(expected_date="2026-09-15")):
            changed = copy.deepcopy(self.manifest)
            change(changed["outputs"])
            changed["manifest_hash"] = manifest_hash(changed)
            with self.assertRaises(ValueError):
                publish.publish(changed, self.db, self.client, verifier=self.verifier)
        self.assertEqual(self.mutations, [])

    def test_history_requires_publication_and_fresh_readback_then_preserves_exact_bytes(self):
        before = self.history_path.read_bytes()
        with self.assertRaises(PlaybackBlocked):
            publish.install_history(self.manifest, self.db)
        self.publish()
        self.alter_receipt(lambda receipt: receipt["stage_evidence"]["publication"]["playlists"][0].update(
            observed_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
        with self.assertRaisesRegex(PlaybackBlocked, "expired"):
            publish.install_history(self.manifest, self.db)
        self.assertEqual(self.history_path.read_bytes(), before)
        publish.install_history(self.manifest, self.db, client=self.client, verifier=self.verifier)
        self.assertEqual(self.history_path.read_bytes(), self.preview_path.read_bytes())

    def test_final_history_check_rechecks_external_lists_before_unlocking(self):
        self.publish()
        publish.install_history(self.manifest, self.db)
        self.actual["playlist-1"].reverse()
        with patch("requests.get", return_value=self.public_response()), self.assertRaises(PlaybackBlocked):
            publish.verify_public_history(self.manifest, self.db, self.client, verifier=self.verifier)
        self.assertEqual(self.receipt()["status"], "db_applied")
        self.actual["playlist-1"].reverse()
        with patch("requests.get", return_value=self.public_response()) as read:
            result = publish.verify_public_history(self.manifest, self.db, self.client, verifier=self.verifier)
        self.assertEqual(result["repair_status"], "surfaces_verified")
        self.assertFalse(read.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.receipt()["followups"], {"daily": "pending", "monday": "pending", "device": "pending"})

    def test_redirect_stale_public_file_pending_audit_and_unknown_playback_cannot_unlock(self):
        self.publish()
        publish.install_history(self.manifest, self.db)
        for response in (self.public_response(status=302), self.public_response(content=b"old history")):
            with patch("requests.get", return_value=response), self.assertRaises(PlaybackBlocked):
                publish.verify_public_history(self.manifest, self.db, self.client, verifier=self.verifier)
        self.verifier.unavailable.add(KEEP)
        with patch("requests.get", return_value=self.public_response()), self.assertRaises(PlaybackBlocked):
            publish.verify_public_history(self.manifest, self.db, self.client, verifier=self.verifier)
        self.verifier.unavailable.clear()
        self.pending()
        with patch("requests.get", side_effect=AssertionError("Must fail before HTTP")), self.assertRaises(PlaybackBlocked):
            publish.verify_public_history(self.manifest, self.db, self.client, verifier=self.verifier)
        self.assertEqual(verify_repair(self.conn, self.manifest)["repair_status"], "db_applied")


if __name__ == "__main__":
    unittest.main()
