"""Pending recovery is a publication failure even before a new audit exists."""
import sqlite3
import subprocess
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hype_db
import sync_all
import sync_validation
import ytmusic_playlist_sync as playlist_sync


class PublicationBlockedPhaseTests(unittest.TestCase):
    def test_pending_recovery_blocks_before_network_or_new_audit(self):
        for status in ("running", "mutation_failed", "recovery_required"):
            with self.subTest(status=status):
                client = Mock()
                pending = {"update_run_id": "older-run", "status": status,
                           "started_at": "2026-09-27T13:00:21+00:00"}
                with (
                    patch.object(hype_db, "get_pending_playlist_recovery", return_value=pending),
                    patch.object(hype_db, "record_playlist_update") as record,
                    self.assertRaises(playlist_sync.PlaylistPublicationBlocked) as raised,
                ):
                    playlist_sync.update_ytmusic_playlist(
                        client, "target", ["matched-video"], dry_run=False,
                        db_path="unused.db", service="apple", job_name="Fixture-Daily",
                    )
                self.assertIn("run_id=older-run", str(raised.exception))
                self.assertIn("2026-09-27T13:00:21+00:00", str(raised.exception))
                self.assertEqual(client.mock_calls, [])
                record.assert_not_called()

    def test_cli_maps_only_publication_block_and_keeps_flush_errors_visible(self):
        for case in ("blocked", "unknown", "flush_failed"):
            with (
                self.subTest(case=case),
                patch.object(sys, "argv", ["crawler.py"]),
                patch.object(playlist_sync, "load_dotenv"),
                patch.object(sync_validation, "sync_run_lock", return_value=nullcontext()),
                patch.object(playlist_sync.BILINGUAL_CACHE, "flush") as flush,
            ):
                error = (RuntimeError("unrelated failure") if case == "unknown"
                         else playlist_sync.PlaylistPublicationBlocked("pending recovery"))
                main = Mock(side_effect=error)
                if case == "blocked":
                    self.assertEqual(sync_validation.run_locked_cli(main),
                                     sync_validation.EXIT_PUBLICATION_BLOCKED)
                    flush.assert_called_once_with()
                else:
                    if case == "flush_failed":
                        flush.side_effect = OSError("cache save failed")
                    with self.assertRaisesRegex(
                        RuntimeError if case == "unknown" else OSError,
                        "unrelated failure" if case == "unknown" else "cache save failed",
                    ):
                        sync_validation.run_locked_cli(main)
                    if case == "unknown":
                        flush.assert_not_called()

    def test_old_pending_audit_does_not_reclassify_unrelated_failures(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("""CREATE TABLE playlist_update_runs(
            job_name TEXT, playlist_id TEXT, match_started_at TEXT,
            started_at TEXT, status TEXT)""")
        conn.execute("INSERT INTO playlist_update_runs VALUES (?, ?, ?, ?, ?)",
                     ("Fixture-Daily", "target", "older-run", "2026-09-27", "recovery_required"))
        task = {"service": "apple", "job_name": "Fixture-Daily", "target_id": "target"}
        self.assertEqual(sync_all.classify_task_failure(conn, task, "current-run", returncode=1),
                         "task_or_unknown")
        self.assertEqual(sync_all.classify_task_failure(
            conn, task, "current-run", returncode=sync_validation.EXIT_PUBLICATION_BLOCKED), "publish")
        conn.execute("INSERT INTO playlist_update_runs VALUES (?, ?, ?, ?, ?)",
                     ("Fixture-Daily", "target", "current-run", "2026-10-11", "verification_failed"))
        self.assertEqual(sync_all.classify_task_failure(conn, task, "current-run", returncode=1),
                         "publish")

    def test_parent_keeps_completed_data_eligible_after_blocked_publication(self):
        task = {"service": "apple", "job_name": "Fixture-Daily", "target_id": "target",
                "source_urls": ["https://music.apple.com/fixture"]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = None
        with (
            patch.object(sync_all, "load_env_file"),
            patch.object(sync_all, "postgres_url", return_value=""),
            patch.object(sync_all.json, "load", return_value=[task]),
            patch.object(sync_all.subprocess, "run", side_effect=subprocess.CalledProcessError(
                sync_validation.EXIT_PUBLICATION_BLOCKED, ["crawler.py"])),
            patch.object(sync_all, "data_snapshot_ready", return_value=True),
            patch.object(hype_db, "connect", return_value=conn),
            patch.object(hype_db, "latest_hype_history_date", return_value="2026-10-11"),
            patch.object(hype_db, "export_frontend_history", return_value={}) as export,
            patch.object(hype_db, "validate_frontend_history"),
            patch.object(Path, "read_text", return_value="{}"),
            patch.object(sync_all, "mark_history_ready") as ready,
            self.assertLogs("sync_all", level="INFO") as logs,
            self.assertRaises(SystemExit) as exited,
        ):
            sync_all.main()
        self.assertEqual(exited.exception.code, 1)
        self.assertIn("publication failed; committed chart data is ready", "\n".join(logs.output))
        self.assertIn("Publication failures requiring retry: Fixture-Daily", "\n".join(logs.output))
        self.assertNotIn("Data/task failures", "\n".join(logs.output))
        export.assert_called_once()
        ready.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
