"""Real SQLite joins and compact exports; no production DB, API or history access."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hype_db import connect, init_db, persist_crawled_tracks, persist_crawl_run
from hype_db_reports import export_frontend_history, inflate_frontend_history
from sync_all import data_snapshot_ready
from sync_validation import PlaybackBlocked, assert_frozen, freeze_outputs
from ytmusic_playlist_sync import SourceTrack


SONGS = (
    ("REDRED", "CORTIS", "5sOgJQ3N03Q"),
    ("Pop Off Pop Off", "KiiiKiii", "QbsbqekMkCU"),
    ("RUDE!", "Hearts2Hearts", "Q4AE3ub4nBM"),
    ("404 (New Era)", "KiiiKiii", "MUny_GDYIDM"),
    ("Less than a Lover", "JENNIE", "z9ifbheDGFM"),
)
DAY = "2026-09-14"
START = "2026-09-14T12:00:00+00:00"


class FrozenPublicationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("external access forbidden")))
        self.enterContext(patch("hype_db_store.get_expected_track_count", return_value=len(SONGS)))
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).parent))
        self.path = Path(directory) / "fixture.db"
        init_db(self.path)
        self.conn = self.enterContext(connect(self.path))
        self.tasks = [
            {"service": "apple", "job_name": "KR-Top-100", "target_id": "apple-target"},
            {"service": "hypex", "job_name": "Hype-Wave-Daily", "target_id": "hype-target", "entity_limit": 5},
        ]
        self.seed("apple", "KR-Top-100", "2026-09-13")
        self.seed("ytmusic", "Weekly-Hot-100", "2026-W37")

    def seed(self, service, job_name, period):
        raw = [SourceTrack(rank, title, artist, service=service, song_id=f"{service}-{rank}", album="Album")
               for rank, (title, artist, video) in enumerate(SONGS, 1)]
        matches = [{**vars(track), "video_id": video, "yt_title": track.title, "yt_artist": track.artist,
                    "yt_album": "Album", "status": "matched", "score": 1.0}
                   for track, (_, _, video) in zip(raw, SONGS)]
        persist_crawled_tracks(self.path, service=service, job_name=job_name, chart_date="2026-09-13",
                               reference_period=period, tracks=raw, conn=self.conn, commit=False)
        persist_crawl_run(self.path, service=service, job_name=job_name, chart_date="2026-09-13",
                          reference_period=period, started_at=START, tracks=raw, matches=matches,
                          conn=self.conn, commit=False, skip_playlist_order=True)
        self.conn.commit()

    def test_same_five_verified_ids_feed_source_hype_and_export_without_requery(self):
        self.assertTrue(data_snapshot_ready(self.conn, service="apple", job_name="KR-Top-100", started_at=START))
        snapshot = freeze_outputs(self.conn, self.tasks, history_date=DAY)
        expected = [item[2] for item in SONGS]
        for output in snapshot["outputs"]:
            self.assertEqual(output["video_ids"], expected)
        self.assertEqual([row["video_id"] for row in snapshot["report"]], expected)
        assert_frozen(self.conn, snapshot, self.tasks)
        with patch("hype_db_reports.connect", side_effect=AssertionError("frozen report must not requery")):
            payload = export_frontend_history(self.path, self.path.with_name("history.json"), expected_date=DAY,
                                              reports_by_date={DAY: snapshot["report"]})
        self.assertEqual(inflate_frontend_history(payload)[DAY], snapshot["report"])

    def test_canonical_source_mapping_raw_and_manual_changes_invalidate_frozen_output(self):
        snapshot = freeze_outputs(self.conn, self.tasks, history_date=DAY)
        changes = (
            ("UPDATE tracks SET canonical_yt_video_id=? WHERE canonical_yt_video_id=?", ("DBOyG7y_FQg", SONGS[0][2])),
            ("UPDATE playlist_order SET rank_order=rank_order+20 WHERE service='apple'", ()),
            ("UPDATE platform_song_ids SET track_uid=(SELECT track_uid FROM tracks WHERE canonical_yt_video_id=?) "
             "WHERE service='apple' AND song_id='apple-1'", (SONGS[1][2],)),
            ("INSERT INTO manual_overrides(service,song_id,action,reason,updated_at) VALUES('apple','apple-1','block','fixture',?)", (START,)),
        )
        for sql, params in changes:
            with self.subTest(sql=sql):
                self.conn.execute("SAVEPOINT fixture_change")
                self.conn.execute(sql, params)
                with self.assertRaises(PlaybackBlocked):
                    assert_frozen(self.conn, snapshot, self.tasks)
                self.conn.execute("ROLLBACK TO fixture_change")
                self.conn.execute("RELEASE fixture_change")
                assert_frozen(self.conn, snapshot, self.tasks)

    def test_skipped_weekly_source_is_still_guarded_as_hype_input(self):
        snapshot = freeze_outputs(self.conn, self.tasks, history_date=DAY)
        self.assertFalse(any(row["job_name"] == "Weekly-Hot-100" for row in snapshot["inputs"]))
        self.assertTrue(any(row["job_name"] == "Weekly-Hot-100" for row in snapshot["hype_rows"]))
        self.conn.execute("UPDATE playlist_order SET rank_order=rank_order+10 WHERE service='ytmusic'")
        with self.assertRaises(PlaybackBlocked):
            assert_frozen(self.conn, snapshot, self.tasks)


if __name__ == "__main__":
    unittest.main()
