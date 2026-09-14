"""Release acceptance counterexamples: isolated SQLite and no external access."""
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hype_db import connect, init_db, persist_crawled_tracks, persist_crawl_run
from hype_db_reports import fetch_hype_rows_for_dates, build_hype_report_from_rows
import hype_db_store as store
from sync_validation import PlaybackBlocked, freeze_outputs, sync_run_lock
from heal_split_tracks import heal
from hype_db_schema import init_schema, PostgresRow


DAY, PERIOD, START = "2026-09-14", "2026-09-13", "2026-09-14T12:00:00+00:00"
GOOD, WEEKLY, OTHER = "5sOgJQ3N03Q", "QbsbqekMkCU", "cQPXraDIvS4"


class ReleaseAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1"}))
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("External access forbidden")))
        self.enterContext(patch("hype_db_store.get_expected_track_count", return_value=None))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "release.db"
        init_db(self.path)
        self.conn = self.enterContext(connect(self.path))
        self.tasks = [
            {"service": "apple", "job_name": "KR-Top-100", "target_id": "apple-output", "match_started_at": START},
            {"service": "hypex", "job_name": "Hype-Wave-Daily", "target_id": "hype-output"},
        ]
        self.seed("apple", "KR-Top-100", PERIOD, [{"song_id": "apple-1", "video_id": GOOD, "title": "REDRED", "artist": "CORTIS"}])

    def seed(self, service, job, period, rows):
        raw = [{"service": service, "rank": rank, "album": "Album", **row} for rank, row in enumerate(rows, 1)]
        matches = [{**row, "yt_title": row["title"], "yt_artist": row["artist"], "yt_album": row["album"],
                    "status": row.get("status", "matched"), "score": 1.0} for row in raw]
        persist_crawled_tracks(self.path, service=service, job_name=job, chart_date=PERIOD,
                               reference_period=period, tracks=raw, conn=self.conn, commit=False)
        persist_crawl_run(self.path, service=service, job_name=job, chart_date=PERIOD,
                          reference_period=period, tracks=raw, matches=matches, started_at=START,
                          conn=self.conn, commit=False, skip_playlist_order=True)
        self.conn.commit()

    def weekly(self, *, status="matched", video_id=WEEKLY):
        self.seed("ytmusic", "Weekly-Hot-100", "2026-W37", [{
            "song_id": WEEKLY, "video_id": video_id, "title": "Pop Off Pop Off", "artist": "KiiiKiii", "status": status}])

    def test_failed_chart_attempt_cannot_contribute_to_hype(self):
        self.weekly(status="failed", video_id="")
        rows = fetch_hype_rows_for_dates(self.conn, [DAY])[DAY]
        self.assertFalse(any(row["song_id"] == WEEKLY for row in rows))

    def test_skipped_weekly_unapproved_canonical_change_blocks_frozen_hype(self):
        self.weekly()
        self.conn.execute("UPDATE tracks SET canonical_yt_video_id=? WHERE canonical_yt_video_id=?", (OTHER, WEEKLY))
        self.conn.commit()
        with self.assertRaises(PlaybackBlocked):
            freeze_outputs(self.conn, self.tasks, history_date=DAY)

    def test_skipped_weekly_missing_attempt_evidence_cannot_silently_drop_its_weight(self):
        self.weekly()
        self.conn.execute("DELETE FROM match_attempts WHERE service='ytmusic'")
        self.conn.commit()
        with self.assertRaises(PlaybackBlocked):
            freeze_outputs(self.conn, self.tasks, history_date=DAY)

    def test_new_manual_block_on_skipped_weekly_cannot_enter_hype(self):
        self.weekly()
        self.conn.execute("INSERT INTO manual_overrides(service,song_id,action,updated_at) VALUES ('ytmusic',?,'block',?)",
                          (WEEKLY, START))
        self.conn.commit()
        try:
            snapshot = freeze_outputs(self.conn, self.tasks, history_date=DAY)
        except PlaybackBlocked:
            return
        self.assertNotIn(WEEKLY, [row["video_id"] for row in snapshot["report"]])

    def test_hype_only_freeze_still_rejects_unapproved_selection_change(self):
        self.conn.execute("UPDATE tracks SET canonical_yt_video_id=? WHERE canonical_yt_video_id=?", (OTHER, GOOD))
        self.conn.commit()
        with self.assertRaises(PlaybackBlocked):
            freeze_outputs(self.conn, self.tasks[1:], history_date=DAY)

    def test_unweighted_enrichment_can_select_a_different_hype_recording(self):
        weighted = dict(fetch_hype_rows_for_dates(self.conn, [DAY])[DAY][0])
        weighted['rank_order'] = 10
        enrichment = {**weighted, 'service': 'spotify', 'job_name': 'Hot-Hits-Korea',
                      'song_id': 'spotify-source', 'video_id': OTHER, 'rank_order': 1}
        baseline = build_hype_report_from_rows([weighted])
        enriched = build_hype_report_from_rows([weighted, enrichment])
        self.assertEqual(baseline[0]['video_id'], GOOD)
        self.assertEqual(enriched[0]['video_id'], OTHER)
        self.assertEqual(baseline[0]['hype_index'], enriched[0]['hype_index'])

    def test_disconnected_unweighted_row_is_not_consumed_and_does_not_block_freeze(self):
        baseline = build_hype_report_from_rows(fetch_hype_rows_for_dates(self.conn, [DAY])[DAY])
        self.seed('spotify', 'Hot-Hits-Korea', PERIOD, [{
            'song_id': 'unrelated-source', 'video_id': WEEKLY,
            'title': 'Entirely Unrelated', 'artist': 'Different Artist'}])
        self.conn.execute('UPDATE tracks SET canonical_yt_video_id=? WHERE canonical_yt_video_id=?',
                          (OTHER, WEEKLY))
        self.conn.commit()
        self.assertEqual(build_hype_report_from_rows(fetch_hype_rows_for_dates(self.conn, [DAY])[DAY]), baseline)
        snapshot = freeze_outputs(self.conn, self.tasks[1:], history_date=DAY)
        self.assertEqual(snapshot['report'], baseline)
        self.assertNotIn('unrelated-source', [row['song_id'] for row in snapshot['hype_rows']])

    def test_unweighted_enrichment_of_a_weighted_group_still_requires_approved_selection(self):
        self.seed('spotify', 'Hot-Hits-Korea', PERIOD, [{
            'song_id': 'related-source', 'video_id': GOOD, 'title': 'REDRED', 'artist': 'CORTIS'}])
        self.conn.execute("UPDATE match_attempts SET video_id=? WHERE service='spotify'", (OTHER,))
        self.conn.commit()
        with self.assertRaises(PlaybackBlocked):
            freeze_outputs(self.conn, self.tasks[1:], history_date=DAY)

    def test_standalone_initialization_cannot_repair_before_incident_guard(self):
        owner = store.find_track_by_video(self.conn, GOOD)
        store.upsert_metadata_lookup(self.conn, track_uid=owner, row={"title": "REDRED", "artist": "CORTIS", "album": "Album"}, source="fixture", score=1)
        store.ensure_track(self.conn, track_uid="failed", status="failed")
        store.upsert_track_list_metadata(self.conn, service="ytmusic", song_id="failed-source", track_uid="failed",
                                         row={"title": "REDRED", "artist": "CORTIS", "album": "Album"})
        self.conn.execute("INSERT INTO migration_reports(report_id,source,rows_read,tracks_seen,conflicts_seen,payload_json,created_at) "
                          "VALUES ('incident','ytmusic_chart_incident',1,0,1,?,?)", (json.dumps({"status": "db_applied"}), START))
        self.conn.commit()
        with self.assertRaises(PlaybackBlocked):
            persist_crawled_tracks(self.path, service="apple", job_name="KR-Top-100", chart_date=PERIOD,
                                   reference_period=PERIOD, tracks=[{"rank": 1, "song_id": "apple-1", "title": "REDRED", "artist": "CORTIS"}])
        actual = self.conn.execute("SELECT track_uid FROM platform_song_ids WHERE service='ytmusic' AND song_id='failed-source'").fetchone()[0]
        self.assertEqual(actual, "failed")

    def test_standalone_heal_cannot_mutate_under_another_global_run_lock(self):
        owner = store.find_track_by_video(self.conn, GOOD)
        store.upsert_metadata_lookup(self.conn, track_uid=owner, row={"title": "REDRED", "artist": "CORTIS", "album": "Album"}, source="fixture", score=1)
        store.upsert_track_list_metadata(self.conn, service="ytmusic", song_id="unbound-song", track_uid=owner,
                                         row={"title": "REDRED", "artist": "CORTIS", "album": "Album"}, bind_source_id=False)
        self.conn.execute("INSERT INTO playlist_order(service,job_name,source_variant,reference_period,song_id,rank_order) "
                          "VALUES ('ytmusic','Weekly-Hot-100','default','2026-W37','unbound-song',1)")
        self.conn.commit()
        with sync_run_lock(self.path):
            with self.assertRaises(PlaybackBlocked):
                heal(self.path, dry_run=False)

    def test_pending_recovery_read_does_not_initialize_a_missing_database(self):
        path = self.path.with_name("missing.db")
        before = set(path.parent.iterdir())
        self.assertIsNone(store.get_pending_playlist_recovery(path, "playlist"))
        self.assertEqual(set(path.parent.iterdir()), before)

    def test_pending_recovery_read_preserves_delete_journal_database_bytes(self):
        path = self.path.with_name("delete-journal.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        init_schema(conn, repair_source_bindings=False)
        conn.commit()
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()
        before, files = path.read_bytes(), set(path.parent.iterdir())
        self.assertIsNone(store.get_pending_playlist_recovery(path, "playlist"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(path.parent.iterdir()), files)

    def test_only_the_actual_parent_pid_can_share_the_run_lock(self):
        with sync_run_lock(self.path):
            with patch.dict(os.environ, {"HYPE_SYNC_PARENT_PID": str(os.getpid())}):
                with self.assertRaises(PlaybackBlocked):
                    with sync_run_lock(self.path):
                        pass
            with patch.dict(os.environ, {"HYPE_SYNC_PARENT_PID": str(os.getppid())}):
                with sync_run_lock(self.path):
                    pass

    def test_postgres_lock_uses_scalar_value_not_row_truthiness(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = PostgresRow([("pg_try_advisory_xact_lock",)], (False,))
        @contextmanager
        def fake_connect(*args, **kwargs):
            self.assertTrue(kwargs["read_only"])
            yield conn
        with patch.dict(os.environ, {"SUPABASE_DB_URL": "offline-no-network", "HYPE_SYNC_PARENT_PID": ""}), patch(
                "hype_db_schema.connect", side_effect=fake_connect):
            with self.assertRaises(PlaybackBlocked):
                with sync_run_lock(self.path):
                    pass

    def test_postgres_pooler_lock_keeps_one_transaction_until_command_exit(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = PostgresRow([("pg_try_advisory_xact_lock",)], (True,))
        exits = []
        @contextmanager
        def fake_connect(*args, **kwargs):
            yield conn
            exits.append("transaction-end")
        with patch.dict(os.environ, {"SUPABASE_DB_URL": "offline-no-network", "HYPE_SYNC_PARENT_PID": ""}), patch(
                "hype_db_schema.connect", side_effect=fake_connect):
            with sync_run_lock(self.path):
                self.assertEqual(exits, [])
                conn.commit.assert_not_called()
                conn.rollback.assert_not_called()
        self.assertEqual(exits, ["transaction-end"])
        self.assertEqual([call.args[0] for call in conn.execute.call_args_list], [
            "SET LOCAL idle_in_transaction_session_timeout = '0'",
            "SELECT pg_try_advisory_xact_lock(1847391027)",
        ])


if __name__ == "__main__":
    unittest.main()
