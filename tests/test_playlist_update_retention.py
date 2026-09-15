import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hype_db import connect, init_db, record_playlist_update
from sync_validation import PlaybackBlocked


class PlaylistUpdateRetentionTest(unittest.TestCase):
    def test_record_keeps_only_latest_31_days_of_items(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "retention.db"
            now = datetime(2026, 8, 30, tzinfo=timezone.utc)
            cutoff = now - timedelta(days=31)
            expired = (cutoff - timedelta(seconds=1)).isoformat()
            boundary = cutoff.isoformat()

            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                patch("hype_db_store.utc_now_iso", return_value=now.isoformat()),
            ):
                init_db(db_path)
                with connect(db_path) as conn:
                    conn.executemany(
                        """
                        INSERT INTO playlist_update_runs(
                            update_run_id, playlist_id, service, job_name, started_at,
                            dry_run, requested_count, existing_count, created_at
                        ) VALUES (?, 'playlist', 'apple', 'Retention-Test', ?, 0, 1, 0, ?)
                        """,
                        [
                            ("expired-run", expired, expired),
                            ("boundary-run", boundary, boundary),
                        ],
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_update_items(
                            update_run_id, action, video_id, item_order, created_at
                        ) VALUES (?, 'requested', ?, 1, ?)
                        """,
                        [
                            ("expired-run", "expired-video", expired),
                            ("boundary-run", "boundary-video", boundary),
                        ],
                    )

                record_playlist_update(
                    db_path,
                    playlist_id="playlist",
                    service="apple",
                    job_name="Retention-Test",
                    requested_video_ids=["new-video"],
                )

                with connect(db_path) as conn:
                    video_ids = {
                        row["video_id"]
                        for row in conn.execute(
                            "SELECT video_id FROM playlist_update_items"
                        ).fetchall()
                    }
                    expired_parent = conn.execute(
                        "SELECT 1 FROM playlist_update_runs WHERE update_run_id = ?",
                        ("expired-run",),
                    ).fetchone()

            self.assertEqual(video_ids, {"boundary-video", "new-video"})
            self.assertIsNotNone(expired_parent)


class ActiveRepairRetentionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for method in ("socket.socket.connect", "requests.sessions.Session.request", "psycopg2.connect"):
            self.enterContext(patch(method, side_effect=AssertionError("No external access")))
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.db"
        init_db(self.path)
        self.old = (datetime.now(timezone.utc) - timedelta(days=32)).isoformat()
        self.payload = json.dumps({"version": 1, "snapshot": {"complete": True, "existing_items": [],
                                   "requested_video_ids": ["old00000001"]}, "events": []})
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE ytmusic_song_translations(video_id TEXT PRIMARY KEY,updated_at TEXT)")
            conn.execute("INSERT INTO playlist_update_runs(update_run_id,playlist_id,service,job_name,status,evidence_version,"
                         "recovery_payload_json,started_at,completed_at,created_at) "
                         "VALUES ('old-terminal','other','apple','OldDaily','verification_failed',1,?,?,?,?)",
                         (self.payload, self.old, self.old, self.old))
            conn.execute("INSERT INTO playlist_update_items(update_run_id,action,video_id,item_order,created_at) "
                         "VALUES ('old-terminal','requested','old00000001',1,?)", (self.old,))

    def incident(self, *, status="db_applied", invalid=False):
        receipt = {"repair_id": "fixture", "status": status, "outputs": {"playlists": []}}
        if invalid:
            receipt["supersedes"] = {"repair_id": "missing-parent"}
        with connect(self.path) as conn:
            conn.execute("INSERT INTO migration_reports(report_id,source,created_at,payload_json) VALUES "
                         "('chart-repair:fixture','ytmusic_chart_incident',?,?)", (self.old, json.dumps(receipt)))

    def old_rows(self):
        with connect(self.path, read_only=True) as conn:
            return {table: [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE update_run_id='old-terminal'").fetchall()]
                for table in ("playlist_update_runs", "playlist_update_items")}

    def test_active_incident_preserves_all_old_audit_fields_for_every_caller(self):
        self.incident()
        before = self.old_rows()
        for job in ("repair:fixture:Apple200", "OrdinaryDaily"):
            with self.subTest(job=job):
                run = record_playlist_update(self.path, playlist_id=job, service="apple", job_name=job,
                                             requested_video_ids=["new00000001"])
                self.assertEqual(self.old_rows(), before)
                with connect(self.path, read_only=True) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM playlist_update_runs WHERE update_run_id=?", (run,)).fetchone()[0], "running")

    def test_invalid_child_coverage_retains_original_evidence_and_records_new_audit(self):
        self.incident(invalid=True)
        before = self.old_rows()
        run = record_playlist_update(self.path, playlist_id="new", job_name="OrdinaryDaily", requested_video_ids=["new00000001"])
        self.assertTrue(run)
        self.assertEqual(self.old_rows(), before)

    def test_completed_incident_allows_the_existing_retention_policy(self):
        self.incident(status="surfaces_verified")
        record_playlist_update(self.path, playlist_id="new", job_name="OrdinaryDaily", requested_video_ids=["new00000001"])
        after = self.old_rows()
        self.assertEqual(after["playlist_update_items"], [])
        self.assertEqual(json.loads(after["playlist_update_runs"][0]["recovery_payload_json"]), {"version": 1, "pruned": True})

    def test_unreadable_incident_fails_before_retention_or_new_audit(self):
        self.incident()
        with connect(self.path) as conn:
            conn.execute("UPDATE migration_reports SET payload_json='invalid JSON'")
        before = self.old_rows()
        with self.assertRaises(ValueError):
            record_playlist_update(self.path, playlist_id="new", job_name="OrdinaryDaily", requested_video_ids=["new00000001"])
        self.assertEqual(self.old_rows(), before)
        with connect(self.path, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_update_runs").fetchone()[0], 1)

    def run_shared_publisher(self, *, corrupt_original=False):
        import hype_db
        import publish_chart_repair as publication
        import ytmusic_playlist_sync as playlist
        from playlist_playability_fixture import PlaylistVerifier
        self.incident()
        with connect(self.path, read_only=True) as conn:
            baseline = publication.business_fingerprint(conn, repair_id="fixture")
        client, guards = Mock(), []
        actual = [{"videoId": "old00000001", "setVideoId": "original-owned-slot"}]
        real_record = hype_db.record_playlist_update

        def guard():
            with connect(self.path, read_only=True) as conn:
                unchanged = publication.business_fingerprint(conn, repair_id="fixture") == baseline
            guards.append(unchanged)
            if not unchanged:
                raise PlaybackBlocked("Original audit changed")

        def record(*args, **kwargs):
            run = real_record(*args, **kwargs)
            if corrupt_original:
                with connect(self.path) as conn:
                    conn.execute("UPDATE playlist_update_runs SET error='unreviewed change' WHERE update_run_id='old-terminal'")
            return run

        with patch.object(playlist, "get_existing_playlist_items", side_effect=lambda *_: list(actual)), \
                patch.object(hype_db, "record_playlist_update", side_effect=record):
            if corrupt_original:
                with self.assertRaisesRegex(PlaybackBlocked, "Original audit changed"):
                    playlist.update_ytmusic_playlist(client, "playlist", ["old00000001"], dry_run=False,
                        db_path=self.path, service="apple", job_name="repair:fixture:Apple200",
                        playability_verifier=PlaylistVerifier(), before_mutation=guard)
            else:
                playlist.update_ytmusic_playlist(client, "playlist", ["old00000001"], dry_run=False,
                    db_path=self.path, service="apple", job_name="repair:fixture:Apple200",
                    playability_verifier=PlaylistVerifier(), before_mutation=guard)
        self.assertEqual(client.mock_calls, [])
        with connect(self.path, read_only=True) as conn:
            status = conn.execute("SELECT status FROM playlist_update_runs WHERE job_name='repair:fixture:Apple200'").fetchone()[0]
        return guards, status

    def test_shared_publisher_noop_preserves_old_evidence_and_passes_post_record_guard(self):
        before = self.old_rows()
        guards, status = self.run_shared_publisher()
        self.assertEqual(guards, [True, True, True])
        self.assertEqual(status, "skipped_current")
        self.assertEqual(self.old_rows(), before)

    def test_retention_skip_does_not_hide_unrelated_audit_changes(self):
        guards, status = self.run_shared_publisher(corrupt_original=True)
        self.assertEqual(guards, [True, True, False])
        self.assertEqual(status, "recovery_required")


if __name__ == "__main__":
    unittest.main()
