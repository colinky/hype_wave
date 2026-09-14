"""Offline orchestration/export contracts; all temporary data stays in tests/."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import hype_db
import hype_db_reports as reports
import hype_moment
import sync_all
import sync_validation
import ytmusic_playlist_sync as playlist_sync


DAY = "2026-09-10"
ROW = {"video_id": "fixture-video", "title": "Song", "artist": "Artist",
       "hype_rank": 1, "hype_index": 10.0, "apple_rank": 1}


def observation(state="playable"):
    now = datetime.now(timezone.utc)
    return {"state": state, "environment": "fixture", "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat()}


class SyncPhaseReadinessTests(unittest.TestCase):
    def run_sync(self, *, source_return=0, source_ready=True, heal_return=0,
                 publication_error=None, disk_payload=None, anchor_periods=None,
                 health="healthy", unknown_video=False, unknown_target=False,
                 drift_at=None, freeze_error=None, unknown_after_publish=False):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            for name in ("apple_music_to_ytmusic_crawl.py", "ytmusic_to_ytmusic_crawl.py", "heal_split_tracks.py"):
                (root / name).touch()
            tasks = [
                {"service": "apple", "job_name": "KR-Top-100", "target_id": "apple-target",
                 "include_in_hype": True, "hype_group": "apple"},
                {"service": "ytmusic", "job_name": "Weekly-Hot-100", "target_id": "weekly-target",
                 "include_in_hype": True, "hype_group": "ytmusic"},
                {"service": "hypex", "job_name": "Hype-Wave-Daily", "target_id": "hype-target"},
            ]
            if anchor_periods:
                tasks.insert(1, {**tasks[0], "job_name": "Other-Apple"})
            (root / "sync_config.json").write_text(json.dumps(tasks))
            output = root / "github-output"
            conn = MagicMock()
            conn.__enter__.return_value = conn
            conn.execute.side_effect = lambda query, params=(): MagicMock(**{
                "fetchall.return_value": [],
                "fetchone.return_value": {"reference_period": (anchor_periods or {}).get(params[1] if params else "", "2026-09-09")}})
            events = []
            snapshot = {"history_date": DAY, "report": [ROW.copy()], "fingerprint": "frozen", "outputs": [
                {"job_name": t["job_name"], "service": t["service"], "playlist_id": t["target_id"],
                 "playlist_name": t["job_name"], "video_ids": [ROW["video_id"]]} for t in tasks]}
            checks = 0

            class Verifier:
                environment = "fixture"
                def check_health(self, **kwargs):
                    events.append("health")
                    return {"run_health": health, "auth_state": "authenticated"}

                def verify(self, video_id, *, availability=None, **kwargs):
                    events.append("verify")
                    unknown = unknown_video or availability is False or (
                        unknown_after_publish and any(e.startswith("published:") for e in events))
                    return observation("unknown" if unknown else "playable")

            def readiness(_conn, **kwargs):
                events.append("ready:" + kwargs["job_name"])
                if kwargs["service"] == "ytmusic":
                    if isinstance(source_ready, Exception):
                        raise source_ready
                    return source_ready
                return True

            def run(cmd, **kwargs):
                script = Path(cmd[1]).name
                events.append("run:" + script)
                if script == "heal_split_tracks.py":
                    code = heal_return
                else:
                    self.assertIn("--defer-publish", cmd)
                    self.assertNotIn("--db-only", cmd)
                    self.assertEqual(kwargs["env"]["HYPE_DEFER_HISTORY_EXPORT"], "1")
                    self.assertTrue(kwargs["env"]["HYPE_MATCH_STARTED_AT"])
                    code = source_return if script == "ytmusic_to_ytmusic_crawl.py" else 0
                if code:
                    raise subprocess.CalledProcessError(code, cmd)

            def freeze(_conn, _tasks, **kwargs):
                events.append("freeze")
                self.assertEqual(kwargs["history_date"], DAY)
                if freeze_error:
                    raise freeze_error
                return snapshot

            def guard(*args):
                nonlocal checks
                checks += 1
                events.append("guard")
                if checks == drift_at:
                    raise sync_validation.PlaybackBlocked("snapshot changed")

            def read_playlist(_client, playlist_id):
                events.append("read:" + playlist_id)
                return [{"videoId": ROW["video_id"], "isAvailable": not (
                    unknown_target and playlist_id == "hype-target")}]

            def publish(_client, playlist_id, video_ids, **kwargs):
                self.assertEqual(video_ids, [ROW["video_id"]])
                self.assertIsNotNone(kwargs["playability_verifier"])
                kwargs["before_mutation"]()
                if publication_error and playlist_id == "apple-target":
                    events.append("publish-error")
                    raise publication_error
                events.append("published:" + playlist_id)

            def export(_db, path, **kwargs):
                events.append("export")
                self.assertEqual(kwargs["reports_by_date"], {DAY: snapshot["report"]})
                payload = reports.compact_frontend_history(kwargs["reports_by_date"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload if disk_payload is None else disk_payload))
                return payload

            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": "", "GITHUB_OUTPUT": str(output)}),
                patch.object(sync_all, "__file__", str(root / "sync_all.py")),
                patch.object(sync_all, "load_env_file"),
                patch.object(sync_all, "data_snapshot_ready", side_effect=readiness),
                patch.object(hype_db, "connect", return_value=conn),
                patch.object(hype_db, "export_frontend_history", side_effect=export) as exporter,
                patch.object(sync_all.subprocess, "run", side_effect=run) as runner,
                patch.object(playlist_sync, "make_ytmusic", return_value=object()) as maker,
                patch.object(playlist_sync, "get_existing_playlist_items", side_effect=read_playlist),
                patch.object(playlist_sync, "update_ytmusic_playlist", side_effect=publish) as publisher,
                patch.object(sync_validation, "verifier_for", side_effect=lambda _client: Verifier()),
                patch.object(sync_validation, "freeze_outputs", side_effect=freeze),
                patch.object(sync_validation, "assert_frozen", side_effect=guard),
                patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
            ):
                try:
                    sync_all.main()
                    exitcode = 0
                except SystemExit as exc:
                    exitcode = exc.code
            return {"exitcode": exitcode, "calls": runner.call_args_list,
                    "exports": exporter.call_args_list, "publishes": publisher.call_args_list,
                    "events": events, "clients": maker.call_count,
                    "ready": output.read_text() if output.exists() else ""}

    def test_sources_then_heal_then_freeze_then_all_publish_then_exact_report_export(self):
        result = self.run_sync()
        self.assertEqual(result["exitcode"], 0)
        self.assertEqual(result["clients"], 6)
        self.assertEqual(len({id(call.kwargs["playability_verifier"])
                              for call in result["publishes"]}), 3)
        events = result["events"]
        self.assertEqual(events[0], "health")
        sequence = [e for e in events if e.startswith(("run:", "ready:", "published:")) or e in {"freeze", "export"}]
        self.assertEqual(sequence, ["run:apple_music_to_ytmusic_crawl.py", "ready:KR-Top-100",
            "run:ytmusic_to_ytmusic_crawl.py", "ready:Weekly-Hot-100", "run:heal_split_tracks.py", "freeze",
            "published:apple-target", "published:weekly-target", "published:hype-target", "export"])
        self.assertLess(events.index("read:hype-target"), events.index("published:apple-target"))
        self.assertEqual(result["ready"], "history_ready=true\n")

    def test_source_failure_cannot_reuse_an_older_ready_snapshot(self):
        result = self.run_sync(source_return=1, source_ready=True)
        self.assertEqual(result["exitcode"], 1)
        self.assertNotIn("freeze", result["events"])
        self.assertFalse(result["publishes"] or result["exports"] or result["ready"])
        self.assertNotIn("run:heal_split_tracks.py", result["events"])

    def test_missing_snapshot_or_lookup_error_blocks_even_exit_zero(self):
        for ready in (False, RuntimeError("database unavailable")):
            with self.subTest(ready=ready):
                result = self.run_sync(source_ready=ready)
                self.assertEqual(result["exitcode"], 1)
                self.assertFalse(result["publishes"] or result["exports"] or result["ready"])

    def test_heal_failure_is_fatal_before_freeze_and_publication(self):
        result = self.run_sync(heal_return=1)
        self.assertEqual(result["exitcode"], 1)
        self.assertNotIn("freeze", result["events"])
        self.assertFalse(result["publishes"] or result["exports"])

    def test_publication_transport_failure_keeps_valid_frozen_history_and_nonzero_exit(self):
        result = self.run_sync(publication_error=RuntimeError("pending playlist recovery"))
        self.assertEqual(result["exitcode"], 1)
        self.assertEqual(result["ready"], "history_ready=true\n")
        self.assertEqual(len(result["publishes"]), 3)
        self.assertEqual(len(result["exports"]), 1)

    def test_playback_unknown_aborts_later_publications_and_history(self):
        result = self.run_sync(publication_error=sync_validation.PlaybackBlocked("unknown"))
        self.assertEqual(result["exitcode"], 1)
        self.assertEqual(len(result["publishes"]), 1)
        self.assertFalse(result["exports"] or result["ready"])

    def test_uncertain_mutation_aborts_later_publications_and_history(self):
        result = self.run_sync(publication_error=playlist_sync.PlaylistMutationUncertain("unacknowledged move"))
        self.assertEqual(result["exitcode"], 1)
        self.assertEqual(len(result["publishes"]), 1)
        self.assertFalse(result["exports"] or result["ready"])

    def test_preflight_unknown_has_no_source_or_output_writes(self):
        result = self.run_sync(health="unknown")
        self.assertFalse(result["calls"] or result["publishes"] or result["exports"])
        self.assertEqual(result["exitcode"], 1)

    def test_unknown_final_candidate_or_later_target_blocks_every_publication(self):
        for kwargs in ({"unknown_video": True}, {"unknown_target": True}):
            with self.subTest(kwargs=kwargs):
                result = self.run_sync(**kwargs)
                self.assertEqual(result["exitcode"], 1)
                self.assertFalse(result["publishes"] or result["exports"])

    def test_snapshot_changes_before_first_publish_or_later_chunk_block_history(self):
        for check in (1, 2, 3, 7):
            with self.subTest(check=check):
                result = self.run_sync(drift_at=check)
                self.assertEqual(result["exitcode"], 1)
                self.assertFalse(result["exports"] or result["ready"])
                if check <= 2:
                    self.assertFalse(any(e.startswith("published:") for e in result["events"]))

    def test_unknown_at_history_boundary_preserves_previous_history(self):
        result = self.run_sync(unknown_after_publish=True)
        self.assertEqual(result["exitcode"], 1)
        self.assertFalse(result["exports"] or result["ready"])

    def test_freeze_calculation_failure_blocks_all_outputs(self):
        result = self.run_sync(freeze_error=ValueError("ambiguous raw input"))
        self.assertEqual(result["exitcode"], 1)
        self.assertFalse(result["publishes"] or result["exports"])

    def test_stale_disk_history_cannot_mark_ready(self):
        result = self.run_sync(disk_payload=reports.compact_frontend_history({"2026-09-09": [ROW]}))
        self.assertEqual(result["exitcode"], 1)
        self.assertFalse(result["ready"])

    def test_disagreeing_current_apple_anchors_block_hype_and_export(self):
        result = self.run_sync(anchor_periods={"KR-Top-100": "2026-09-09", "Other-Apple": "2026-09-08"})
        self.assertEqual(result["exitcode"], 1)
        self.assertFalse(result["publishes"] or result["exports"])


class HistoryValidationTests(unittest.TestCase):
    def test_validation_rejects_bad_intended_date_metadata_and_ranks(self):
        valid = reports.compact_frontend_history({DAY: [ROW]})
        reports.validate_frontend_history(valid, DAY)
        variants = []
        for field, value in (("hype_rank", 2), ("hype_index", float("nan")), ("apple_rank", -1)):
            bad = copy.deepcopy(valid)
            bad["rankings"][DAY][ROW["video_id"]][field] = value
            variants.append(bad)
        bad = copy.deepcopy(valid)
        bad["tracks"][ROW["video_id"]]["artist"] = ""
        variants.extend([bad, reports.compact_frontend_history({DAY: []}),
                         reports.compact_frontend_history({"2026-09-09": [ROW]})])
        for bad in variants:
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                reports.validate_frontend_history(bad, DAY)

    def test_export_validation_and_atomic_failure_preserve_existing_bytes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            db = root / "fixture.db"
            db.touch()
            output = root / "history.json"
            before = json.dumps(reports.compact_frontend_history({"2026-09-09": [ROW]})).encode()
            conn = MagicMock()
            conn.__enter__.return_value = conn
            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                patch.object(reports, "init_db"),
                patch.object(reports, "connect", return_value=conn),
                patch.object(reports, "fetch_hype_rows_for_dates", return_value={DAY: []}),
                patch.object(reports, "previous_apple_videos_for_history", return_value=set()),
            ):
                for case in ("no_anchor", "wrong_day", "empty_report", "atomic_failure", "success"):
                    with self.subTest(case=case), ExitStack() as stack:
                        output.write_bytes(before)
                        conn.execute.return_value.fetchall.return_value = (
                            [] if case == "no_anchor" else [{"chart_date": "2026-09-09"}])
                        stack.enter_context(patch.object(reports, "build_hype_report_from_rows",
                                                         return_value=[] if case == "empty_report" else [ROW]))
                        if case == "atomic_failure":
                            stack.enter_context(patch.object(reports.os, "replace", side_effect=OSError("replace failed")))
                        if case == "success":
                            payload = reports.export_frontend_history(db, output, expected_date=DAY)
                            self.assertEqual(json.loads(output.read_text()), payload)
                            self.assertEqual(payload["days"], 31)
                            reports.validate_frontend_history(payload, DAY)
                        else:
                            with self.assertRaises((ValueError, RuntimeError, OSError)):
                                reports.export_frontend_history(db, output,
                                    expected_date="2026-09-11" if case == "wrong_day" else DAY)
                            self.assertEqual(output.read_bytes(), before)
                        self.assertFalse(list(root.glob(".history.json.*.tmp")))


class HypeFailurePhaseTests(unittest.TestCase):
    def test_explicit_failure_codes_and_frozen_history_after_publication(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db = Path(directory) / "fixture.db"
            db.touch()
            conn = MagicMock()
            conn.__enter__.return_value = conn
            conn.execute.side_effect = lambda query, params=(): MagicMock(**{
                "fetchall.return_value": [] if "migration_reports" in query else [{"job_name": "KR-Top-100"}]})
            argv = ["hype_moment.py", "--db-path", str(db), "--yt-playlist-id", "target", "--history-date", DAY]
            cases = [("calc", 2), ("export", 3), ("auth", 4), ("publish", 4), ("uncertain", 4), ("unknown", 4), ("deferred", 0)]
            for case, expected in cases:
                with self.subTest(case=case), ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, {"SUPABASE_DB_URL": "",
                        "HYPE_DEFER_HISTORY_EXPORT": "1" if case == "deferred" else "0"}))
                    stack.enter_context(patch.object(sys, "argv", argv))
                    stack.enter_context(patch.object(hype_db, "init_db"))
                    stack.enter_context(patch.object(hype_db, "connect", return_value=conn))
                    stack.enter_context(patch.object(hype_db, "hype_inputs", return_value={"KR-Top-100": {"hype_group": "apple"}}))
                    snapshot = {"report": [] if case == "calc" else [ROW], "history_date": DAY,
                                "outputs": [{"video_ids": [ROW["video_id"]]}]}
                    stack.enter_context(patch.object(sync_validation, "freeze_outputs", return_value=snapshot))
                    stack.enter_context(patch.object(sync_validation, "assert_frozen"))
                    verifier = MagicMock()
                    verifier.environment = "fixture"
                    verifier.check_health.return_value = {"run_health": "healthy", "auth_state": "authenticated"}
                    verifier.verify.side_effect = lambda *a, **kw: observation("unknown" if case == "unknown" else "playable")
                    stack.enter_context(patch.object(sync_validation, "verifier_for", return_value=verifier))
                    events = []
                    def export(*args, **kwargs):
                        events.append("export")
                        self.assertEqual(kwargs["reports_by_date"], {DAY: [ROW]})
                        self.assertNotIn("full_rebuild", kwargs)
                        if case == "export":
                            raise RuntimeError("export failed")
                    exporter = stack.enter_context(patch.object(hype_db, "export_frontend_history", side_effect=export))
                    maker = stack.enter_context(patch.object(hype_moment, "make_ytmusic",
                        side_effect=RuntimeError("auth failed") if case == "auth" else None))
                    def publish(*args, **kwargs):
                        events.append("publish")
                        kwargs["before_mutation"]()
                        if case == "publish":
                            raise RuntimeError("pending run")
                        if case == "uncertain":
                            raise playlist_sync.PlaylistMutationUncertain("unacknowledged move")
                    publisher = stack.enter_context(patch.object(hype_moment, "update_ytmusic_playlist", side_effect=publish))
                    stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("network forbidden")))
                    self.assertEqual(hype_moment.main(), expected)
                    if case in {"calc", "auth", "uncertain", "unknown", "deferred"}:
                        exporter.assert_not_called()
                    if case in {"calc", "auth", "unknown"}:
                        publisher.assert_not_called()
                    if case in {"export", "publish"}:
                        self.assertEqual(events, ["publish", "export"])


if __name__ == "__main__":
    unittest.main()
