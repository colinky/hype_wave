from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from heal_split_tracks import find_canonical_uid, heal
from hype_db import connect, init_db
from hype_db_common import compact_metadata_key
from hype_db_store import ensure_track, upsert_track_list_metadata


class SplitTrackHealingSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_live_title_cannot_resolve_to_studio_metadata_candidate(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "live.db"
            init_db(db_path)
            with connect(db_path) as conn:
                ensure_track(
                    conn,
                    track_uid="uid-studio",
                    video_id="studioVid01",
                    yt_title="Song",
                    yt_artist="Artist",
                    yt_album="Release",
                    status="matched",
                    score=1.0,
                )
                conn.execute(
                    "INSERT INTO metadata_lookup_index VALUES (?, 'uid-studio', 'fixture', 1.0)",
                    (compact_metadata_key("Song", "Artist"),),
                )
                conn.commit()

                self.assertIsNone(
                    find_canonical_uid(conn, "Song (Live)", "Artist", "Release")
                )

    def test_polluted_exact_video_mapping_does_not_bypass_metadata_gate(self) -> None:
        source_video = "pHciG9_2xXM"
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "polluted-video.db"
            init_db(db_path)
            with connect(db_path) as conn:
                ensure_track(
                    conn,
                    track_uid="uid-polluted",
                    video_id=source_video,
                    yt_title="Meet my GRLS",
                    yt_artist="FKA twigs",
                    status="matched",
                    score=0.668,
                )
                upsert_track_list_metadata(
                    conn,
                    service="ytmusic",
                    song_id=source_video,
                    track_uid="uid-polluted",
                    row={
                        "title_en": "GRLS",
                        "artist_en": "TUIDE",
                        "album_en": "TUNE & PLAY",
                    },
                    bind_source_id=False,
                )
                conn.execute(
                    """
                    INSERT INTO playlist_order(
                        service, job_name, source_variant, reference_period,
                        song_id, rank_order
                    ) VALUES ('ytmusic', 'Fixture-Weekly', 'default',
                              '2026-W36', ?, 40)
                    """,
                    (source_video,),
                )
                conn.commit()

            self.assertEqual(heal(db_path, dry_run=False), 0)
            with connect(db_path) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM platform_song_ids "
                    "WHERE service='ytmusic' AND song_id=?",
                    (source_video,),
                ).fetchone())


if __name__ == "__main__":
    unittest.main()
