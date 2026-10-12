"""CSV rows without an official video URL retain their rank without a fabricated match."""
import csv
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hype_db import connect, init_db
from hype_db_common import build_track_url
from sync_all import data_snapshot_ready
import ytmusic_to_ytmusic_crawl as crawl


def csv_text(rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Rank", "Track Name", "Artist Names", "YouTube URL"])
    writer.writerows(rows)
    return stream.getvalue()


class MissingCsvUrlTests(unittest.TestCase):
    def test_valid_csv_entry_keeps_existing_fields(self):
        entries = crawl.extract_chart_entries_from_csv_text(csv_text([
            [7, "Song", "Artist", "https://www.youtube.com/watch?v=valid000001"],
        ]))
        self.assertEqual(entries, [{
            "rank": 7, "video_id": "valid000001", "title": "Song", "artist": "Artist",
            "source": "youtube_charts_weekly_csv", "views": "", "previous_rank": "",
        }])

    def test_missing_url_identity_is_stable_and_keeps_version_text(self):
        def parse(rank, title="One Love"):
            return crawl.extract_chart_entries_from_csv_text(csv_text([
                [rank, title, "M.C the MAX", "n/a"],
            ]))[0]

        first = parse(100)
        self.assertEqual(first["video_id"], "")
        self.assertTrue(first["song_id"].startswith("fallback:ytmusic:csv:"))
        self.assertEqual(first["song_id"], parse(75)["song_id"])
        self.assertNotEqual(first["song_id"], parse(100, "One Love (Live)")["song_id"])
        self.assertEqual(build_track_url("ytmusic", first["song_id"]), "")
        with self.assertRaisesRegex(ValueError, "requires a title and artist"):
            crawl.extract_chart_entries_from_csv_text(csv_text([[100, "", "Artist", "n/a"]]))

    def test_source_playlist_raw_rows_keep_their_video_ids(self):
        entries = [{"rank": rank, "original_video_id": f"vid{rank:08d}",
                    "original_title": f"Song {rank}", "original_artist_or_channel": "Artist"}
                   for rank in range(1, 101)]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}):
            db = Path(directory) / "fixture.db"
            init_db(db)
            with (
                patch.object(sys, "argv", ["crawler.py", "--use-source-playlist", "--db-only",
                                            "--db-path", str(db), "--yt-auth", "unused.json"]),
                patch.object(crawl, "load_dotenv"),
                patch.object(crawl, "make_ytmusic"),
                patch.object(crawl, "fetch_ytmusic_playlist_entries", return_value=entries),
                patch("hype_db.persist_crawled_tracks", side_effect=RuntimeError("raw-write-stop")) as persist,
                patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
                self.assertLogs("ytmusic_to_ytmusic_crawl", level="INFO"),
                self.assertRaisesRegex(RuntimeError, "raw-write-stop"),
            ):
                crawl.main()
            self.assertEqual([row["song_id"] for row in persist.call_args.kwargs["tracks"]],
                             [row["original_video_id"] for row in entries])

    def test_db_only_import_keeps_all_ranks_and_missing_url_unresolved(self):
        rows = [[rank, f"Song {rank}", "Artist", f"https://www.youtube.com/watch?v=vid{rank:08d}"]
                for rank in range(1, 100)]
        rows.append([100, "One Love", "M.C the MAX", "n/a"])
        source = csv_text(rows)
        parsed = crawl.extract_chart_entries_from_csv_text(source)
        self.assertEqual(len(parsed), 100)
        missing_id = parsed[-1]["song_id"]
        client = Mock()

        def resolve(_client, **kwargs):
            self.assertIs(_client, client)
            self.assertTrue(kwargs["video_id"].startswith("vid"))
            return {"mapping_status": "already_song", "mapping_reason": "fixture",
                    "mapping_score": 1.0, "resolved_video_id": kwargs["video_id"],
                    "resolved_title": kwargs["title"], "resolved_artist": kwargs["artist"]}

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "HYPE_DB_BACKEND": "sqlite", "HYPE_DEFER_HISTORY_EXPORT": "1",
            "HYPE_MATCH_STARTED_AT": "csv-missing-url-fixture", "BYPASS_TRACK_COUNT_VAL": "false",
        }):
            folder = Path(directory)
            db = folder / "fixture.db"
            path = folder / "youtube-charts-top-songs-kr-weekly-20261001.csv"
            path.write_text(source, encoding="utf-8")
            init_db(db)
            argv = ["ytmusic_to_ytmusic_crawl.py", "--youtube-charts-csv", str(path),
                    "--db-path", str(db), "--yt-auth", "unused.json", "--db-only"]
            with (
                patch.object(sys, "argv", argv),
                patch.object(crawl, "load_dotenv"),
                patch.object(crawl, "make_ytmusic", return_value=client),
                patch.object(crawl, "load_verified_matching_cache", return_value={}) as cache,
                patch.object(crawl, "resolve_video_to_song", side_effect=resolve) as resolver,
                patch.object(crawl, "update_ytmusic_playlist") as publish,
                patch.object(crawl.time, "sleep"),
                patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
                self.assertLogs("ytmusic_to_ytmusic_crawl", level="INFO"),
            ):
                self.assertEqual(crawl.main(), 0)

            publish.assert_not_called()
            self.assertEqual(resolver.call_count, 99)
            cached_rows = cache.call_args.kwargs["tracks"]
            self.assertEqual(len(cached_rows), 99)
            self.assertTrue(all(row["original_video_id"] for row in cached_rows))
            self.assertNotIn(missing_id, [row["song_id"] for row in cached_rows])
            with connect(db, read_only=True) as conn:
                raw = conn.execute("SELECT rank_order, song_id FROM playlist_order ORDER BY rank_order").fetchall()
                self.assertEqual([row["rank_order"] for row in raw], list(range(1, 101)))
                self.assertEqual(raw[-1]["song_id"], missing_id)
                attempt = conn.execute("SELECT * FROM match_attempts WHERE song_id = ?", (missing_id,)).fetchone()
                self.assertEqual((attempt["status"], attempt["video_id"], attempt["query"]),
                                 ("failed", "", "missing_original_video_id"))
                track = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
                                     (attempt["track_uid"],)).fetchone()
                self.assertIsNone(track["canonical_yt_video_id"])
                self.assertIsNone(conn.execute("SELECT 1 FROM yt_video_ids WHERE video_id = ?", (missing_id,)).fetchone())
                run = conn.execute("SELECT * FROM match_runs").fetchone()
                self.assertEqual((run["reference_period"], run["total_tracks"], run["matched_tracks"], run["failed_tracks"]),
                                 ("2026-W40", 100, 99, 1))
                self.assertTrue(data_snapshot_ready(conn, service="ytmusic", job_name="Weekly-Hot-100",
                                                    started_at="csv-missing-url-fixture"))


if __name__ == "__main__":
    unittest.main()
