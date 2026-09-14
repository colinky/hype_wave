import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hype_moment
from hype_db_common import dedupe_source_tracks, hype_identity_key
from hype_db_reports import export_frontend_history
from hype_db import (
    connect,
    finish_playlist_update,
    persist_crawled_tracks,
    persist_crawl_run,
    record_playlist_update,
)
from melon_gen_to_ytmusic_crawl import merge_melon_generation_tracks
from sync_all import (
    classify_task_failure,
    data_snapshot_ready,
    failed_hype_inputs,
    mark_history_ready,
)
from ytmusic_playlist_sync import SourceTrack
from ytmusic_to_ytmusic_crawl import latest_ytmusic_reference_period


class SnapshotContractTests(unittest.TestCase):
    @staticmethod
    def seed_ready_snapshot(path: Path, started_at: str) -> None:
        raw = [
            SourceTrack(
                rank=1,
                title="Matched",
                artist="Artist",
                service="apple",
                song_id="matched-source",
            ),
            SourceTrack(
                rank=2,
                title="Allowed Unmatched",
                artist="Artist",
                service="apple",
                song_id="failed-source",
            ),
        ]
        matches = [
            {
                **vars(raw[0]),
                "video_id": "matched-video",
                "yt_title": "Matched",
                "yt_artist": "Artist",
                "status": "matched",
                "score": 1.0,
            },
            {
                **vars(raw[1]),
                "video_id": "",
                "status": "failed",
                "score": 0.1,
            },
        ]
        persist_crawled_tracks(
            path,
            service="apple",
            job_name="Fixture-Daily",
            chart_date="2026-09-07",
            reference_period="2026-09-07",
            tracks=raw,
        )
        persist_crawl_run(
            path,
            service="apple",
            job_name="Fixture-Daily",
            chart_date="2026-09-07",
            reference_period="2026-09-07",
            started_at=started_at,
            tracks=raw,
            matches=matches,
            skip_playlist_order=True,
        )

    def test_hype_main_does_not_migrate_existing_sqlite_before_validation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "legacy.db"
            db_path.touch()
            before = db_path.read_bytes()
            argv = [
                "hype_moment.py",
                "--db-path",
                str(db_path),
                "--yt-playlist-id",
                "unused",
            ]
            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                patch.object(sys, "argv", argv),
            ):
                self.assertEqual(hype_moment.main(), hype_moment.EXIT_CALCULATION_FAILED)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(match_runs)")
                }
            self.assertFalse(columns)
            self.assertEqual(db_path.read_bytes(), before)

    def test_hype_dry_run_missing_schema_fails_closed_without_writing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "legacy.db"
            db_path.touch()
            before = db_path.read_bytes()
            argv = [
                "hype_moment.py",
                "--db-path",
                str(db_path),
                "--yt-playlist-id",
                "unused",
                "--dry-run",
            ]
            with (
                patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                patch.object(sys, "argv", argv),
            ):
                self.assertEqual(hype_moment.main(), hype_moment.EXIT_CALCULATION_FAILED)

            self.assertEqual(db_path.read_bytes(), before)

    def test_source_dedup_keeps_first_raw_rank(self):
        raw = [
            SourceTrack(rank=15, title="Less than a Lover", artist="JENNIE", service="apple", song_id="6804046965"),
            SourceTrack(rank=92, title="Less than a Lover", artist="JENNIE", service="apple", song_id="6804046965"),
        ]
        effective = dedupe_source_tracks(raw, "apple")
        self.assertEqual([track.rank for track in effective], [15])
        self.assertEqual([track.rank for track in raw], [15, 92])

    def test_album_version_is_part_of_hype_identity(self):
        base = {
            "title": "Stuck",
            "artist": "ENHYPEN",
            "album": "THE SIN : BLISS",
            "yt_title": "",
            "yt_artist": "",
            "yt_album": "",
            "video_id": "video",
        }
        korean = {**base, "album": "THE SIN : BLISS (Korean Ver.)"}
        self.assertNotEqual(hype_identity_key(base), hype_identity_key(korean))

    def test_genz_duplicate_uses_best_source_rank(self):
        gen10 = [
            SourceTrack(rank=1, title="A", artist="Artist", service="melon", song_id="1"),
            SourceTrack(rank=90, title="A", artist="Artist", service="melon", song_id="1"),
            SourceTrack(rank=2, title="B", artist="Artist", service="melon", song_id="2"),
        ]
        _, _, merged, _ = merge_melon_generation_tracks(gen10, [], top_k=2)
        self.assertEqual([track.song_id for track in merged], ["1", "2"])

    def test_hype_gate_reports_only_failed_inputs(self):
        tasks = [
            {"job_name": "KR-Top-100", "include_in_hype": True},
            {"job_name": "KR-Top-Songs"},
        ]
        self.assertEqual(failed_hype_inputs(tasks, ["KR-Top-Songs"]), [])
        self.assertEqual(failed_hype_inputs(tasks, ["KR-Top-100"]), ["KR-Top-100"])

    def test_data_readiness_requires_this_run_and_every_source_attempt(self) -> None:
        started_at = "2026-09-07T12:00:00.123456+00:00"
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "ready.db"
            with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                self.seed_ready_snapshot(db_path, started_at)
                with connect(db_path) as conn:
                    self.assertTrue(
                        data_snapshot_ready(
                            conn,
                            service="apple",
                            job_name="Fixture-Daily",
                            started_at=started_at,
                        )
                    )
                    self.assertFalse(
                        data_snapshot_ready(
                            conn,
                            service="apple",
                            job_name="Fixture-Daily",
                            started_at="older-run-token",
                        )
                    )
                    run_id = conn.execute(
                        "SELECT run_id FROM match_runs WHERE started_at = ?",
                        (started_at,),
                    ).fetchone()[0]
                    conn.execute(
                        "DELETE FROM match_attempts WHERE run_id = ? AND song_id = ?",
                        (run_id, "failed-source"),
                    )
                    conn.commit()
                    self.assertFalse(
                        data_snapshot_ready(
                            conn,
                            service="apple",
                            job_name="Fixture-Daily",
                            started_at=started_at,
                        )
                    )

    def test_publish_failure_phase_is_independent_of_same_run_data_readiness(self) -> None:
        started_at = "2026-09-07T12:00:00.654321+00:00"
        task = {
            "service": "apple",
            "job_name": "Fixture-Daily",
            "target_id": "target-playlist",
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "classification.db"
            with patch.dict(
                os.environ,
                {"SUPABASE_DB_URL": "", "HYPE_MATCH_STARTED_AT": started_at},
            ):
                self.seed_ready_snapshot(db_path, started_at)
                run_id = record_playlist_update(
                    db_path,
                    playlist_id="target-playlist",
                    service="apple",
                    job_name="Fixture-Daily",
                    requested_video_ids=["new"],
                    existing_video_ids=["old"],
                    dry_run=False,
                )
                finish_playlist_update(
                    db_path,
                    run_id,
                    status="verification_failed",
                    actual_video_ids=["unexpected"],
                    error="different item",
                    differences=[{"position": 1}],
                )
                with connect(db_path) as conn:
                    self.assertEqual(
                        classify_task_failure(conn, task, started_at), "publish"
                    )
                    conn.execute(
                        "DELETE FROM match_attempts WHERE song_id = 'failed-source'"
                    )
                    conn.commit()
                    self.assertEqual(
                        classify_task_failure(conn, task, started_at), "publish"
                    )
                    self.assertFalse(
                        data_snapshot_ready(conn, service="apple", job_name="Fixture-Daily",
                                            started_at=started_at)
                    )

    def test_only_one_active_update_run_can_claim_a_playlist(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "active-run.db"
            with patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
                first = record_playlist_update(
                    db_path,
                    playlist_id="target-playlist",
                    service="apple",
                    job_name="Fixture-Daily",
                    requested_video_ids=["new"],
                    existing_video_ids=["old"],
                )
                with self.assertRaises(Exception):
                    record_playlist_update(
                        db_path,
                        playlist_id="target-playlist",
                        service="apple",
                        job_name="Fixture-Daily",
                        requested_video_ids=["other"],
                        existing_video_ids=["old"],
                    )
                with connect(db_path) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM playlist_update_runs "
                            "WHERE playlist_id='target-playlist' "
                            "AND status IN ('running', 'mutation_failed', 'recovery_required')"
                        ).fetchone()[0],
                        1,
                    )

                other_playlist = record_playlist_update(
                    db_path,
                    playlist_id="other-playlist",
                    service="apple",
                    job_name="Fixture-Daily",
                    requested_video_ids=["new"],
                    existing_video_ids=["old"],
                )
                self.assertNotEqual(other_playlist, first)

                finish_playlist_update(db_path, first, status="verification_failed")
                second = record_playlist_update(
                    db_path,
                    playlist_id="target-playlist",
                    service="apple",
                    job_name="Fixture-Daily",
                    requested_video_ids=["other"],
                    existing_video_ids=["old"],
                )
                self.assertNotEqual(second, first)

    def test_history_ready_output_is_written_only_when_called(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            output = Path(directory) / "github-output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                mark_history_ready()
            self.assertEqual(output.read_text(encoding="utf-8"), "history_ready=true\n")

    def test_raw_slots_and_effective_run_have_separate_counts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "contract.db"
            raw = [
                SourceTrack(
                    rank=rank,
                    title=f"Song {rank}",
                    artist="Artist",
                    service="apple",
                    song_id="duplicate" if rank in {15, 92} else f"song-{rank}",
                )
                for rank in range(1, 101)
            ]
            effective = dedupe_source_tracks(raw, "apple")
            matches = [
                {
                    "rank": track.rank,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "service": "apple",
                    "song_id": track.song_id,
                    "video_id": f"video-{track.song_id}",
                    "yt_title": track.title,
                    "yt_artist": track.artist,
                    "status": "matched",
                    "score": 1.0,
                }
                for track in effective
            ]
            persist_crawled_tracks(
                db_path,
                service="apple",
                job_name="KR-Top-100",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                tracks=raw,
            )
            persist_crawl_run(
                db_path,
                service="apple",
                job_name="KR-Top-100",
                chart_date="2026-08-30",
                reference_period="2026-08-30",
                started_at="20260830T000000Z",
                tracks=effective,
                matches=matches,
                skip_playlist_order=True,
            )
            with connect(db_path) as conn:
                slots = conn.execute(
                    "SELECT COUNT(*) AS count, COUNT(DISTINCT song_id) AS unique_count FROM playlist_order"
                ).fetchone()
                run = conn.execute(
                    "SELECT status, total_tracks, completed_at FROM match_runs WHERE source = 'crawler'"
                ).fetchone()
            self.assertEqual((slots["count"], slots["unique_count"]), (100, 99))
            self.assertEqual((run["status"], run["total_tracks"]), ("completed", 99))
            self.assertTrue(run["completed_at"])

    def test_history_and_latest_week_ignore_running_snapshot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "snapshot.db"
            output_path = Path(directory) / "history.json"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE playlist_order (
                    service TEXT, job_name TEXT, source_variant TEXT,
                    reference_period TEXT, song_id TEXT, rank_order INTEGER
                );
                CREATE TABLE match_runs (
                    service TEXT, job_name TEXT, source_variant TEXT,
                    reference_period TEXT, status TEXT, completed_at TEXT
                );
                CREATE TABLE platform_song_ids (service TEXT, song_id TEXT, track_uid TEXT);
                CREATE TABLE tracks (
                    track_uid TEXT, canonical_yt_video_id TEXT,
                    yt_title TEXT, yt_artist TEXT, yt_album TEXT,
                    match_status TEXT DEFAULT 'unmatched', best_score REAL DEFAULT 0,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE track_list (
                    service TEXT, song_id TEXT, album_id TEXT,
                    title_ko TEXT, title_en TEXT, artist_ko TEXT, artist_en TEXT,
                    album_ko TEXT, album_en TEXT, artwork_url TEXT
                );

                INSERT INTO playlist_order VALUES
                    ('apple', 'KR-Top-100', 'default', '2026-08-28', 'old', 1),
                    ('apple', 'KR-Top-100', 'default', '2026-08-29', 'staged', 1),
                    ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W34', 'yt-old', 1),
                    ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W35', 'yt-staged', 1);
                INSERT INTO match_runs VALUES
                    ('apple', 'KR-Top-100', 'default', '2026-08-28', 'completed', '2026-08-29T00:00:00Z'),
                    ('apple', 'KR-Top-100', 'default', '2026-08-29', 'running', NULL),
                    ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W34', 'completed', '2026-08-24T00:00:00Z'),
                    ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W35', 'running', NULL);
                INSERT INTO platform_song_ids VALUES ('apple', 'old', 'track-old');
                INSERT INTO tracks(
                    track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
                    match_status, best_score, created_at, updated_at
                ) VALUES (
                    'track-old', 'video-old', 'Old', 'Artist', 'Album',
                    'matched', 1.0, '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z'
                );
                INSERT INTO track_list VALUES
                    ('apple', 'old', '', 'Old', '', 'Artist', '', 'Album', '', '');
                ALTER TABLE match_runs ADD COLUMN run_id TEXT;
                ALTER TABLE match_runs ADD COLUMN created_at TEXT;
                UPDATE match_runs SET run_id=service || ':' || reference_period,
                    created_at=COALESCE(completed_at, '2026-08-30T00:00:00Z');
                CREATE TABLE match_attempts (
                    run_id TEXT, service TEXT, song_id TEXT, status TEXT, video_id TEXT
                );
                INSERT INTO match_attempts VALUES
                    ('apple:2026-08-28', 'apple', 'old', 'matched', 'video-old');
                CREATE TABLE manual_overrides (service TEXT, song_id TEXT, action TEXT);
                """
            )
            conn.commit()
            self.assertEqual(latest_ytmusic_reference_period(conn, "Weekly-Hot-100"), "2026-W34")
            conn.close()

            payload = export_frontend_history(db_path, output_path, full_rebuild=True)
            self.assertEqual(payload["dates"], ["2026-08-29"])
            self.assertIn("video-old", payload["rankings"]["2026-08-29"])


if __name__ == "__main__":
    unittest.main()
