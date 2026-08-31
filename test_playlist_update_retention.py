import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hype_db import connect, init_db, record_playlist_update


class PlaylistUpdateRetentionTest(unittest.TestCase):
    def test_record_keeps_only_latest_31_days_of_items(self):
        with tempfile.TemporaryDirectory() as directory:
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


if __name__ == "__main__":
    unittest.main()
