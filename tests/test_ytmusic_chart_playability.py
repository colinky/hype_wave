"""Charts may expose an exact official audio ID that cannot be played."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from playlist_playability_fixture import PlaylistVerifier

from hype_db import connect, init_db
import ytmusic_playlist_sync as sync
import ytmusic_to_ytmusic_crawl as chart
import hype_db_store as store


SOURCE = "UsbRoaH6y-Q"
ATV = "cQPXraDIvS4"
EXISTING = "QbsbqekMkCU"


class ChartPlayabilityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1",
        }))
        self.verifier = PlaylistVerifier()
        self.enterContext(patch("sync_validation.verifier_for", side_effect=lambda _client: self.verifier))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live network forbidden")))

    def test_reported_five_songs_keep_their_recording_on_repeated_chart_runs(self):
        # Authenticated player results captured on 2026-09-14. No live requests.
        cases = (
            ("REDRED", "CORTIS", "U6BDbXIah-Y", "5sOgJQ3N03Q", "DBOyG7y_FQg", "OK"),
            ("Pop Off Pop Off", "KiiiKiii", SOURCE, EXISTING, ATV, "UNPLAYABLE"),
            ("RUDE!", "Hearts2Hearts", "F7sGJVUrkjQ", "Q4AE3ub4nBM", "EsU7ng0KIy0", "OK"),
            ("404 (New Era)", "KiiiKiii", "zhHB4dZTChw", "MUny_GDYIDM", "KL7km3PxilQ", "UNPLAYABLE"),
            ("Less than a Lover", "JENNIE", "_IT83Y_HcAw", "z9ifbheDGFM", "1PVUTUFxq8g", "UNPLAYABLE"),
        )
        for title, artist, source_id, old_id, new_id, status in cases:
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "replay.db"
                init_db(db_path)
                metadata = {"title_en": title, "artist_en": artist}
                with connect(db_path) as conn:
                    store.ensure_track(conn, track_uid="existing", video_id=old_id,
                                       yt_title=title, yt_artist=artist, status="matched", score=1.0)
                    for service, song_id in (("ytmusic", source_id), ("melon", "123"), ("apple", "456")):
                        store.upsert_track_list_metadata(conn, service=service, song_id=song_id,
                                                         track_uid="existing", row=metadata)
                entry = {
                    **metadata, "source": "youtube_charts_weekly_browse_api",
                    "song_id": source_id, "original_video_id": source_id,
                    "atv_external_video_id": new_id, "title": title, "artist": artist,
                }
                client = Mock()
                self.verifier = PlaylistVerifier(states={new_id: "playable" if status == "OK" else "unavailable"})
                client.get_song.return_value = {
                    "playabilityStatus": {"status": status},
                    "videoDetails": {"videoId": new_id, "musicVideoType": "MUSIC_VIDEO_TYPE_ATV",
                                     "title": title, "author": artist},
                }
                for run in range(2):
                    with connect(db_path) as conn, patch.object(chart, "resolve_bilingual_song", return_value=metadata):
                        result = chart.preflight_chart_relations(conn, [entry], client, "2026-W37")
                        self.assertEqual(result[source_id]["video_id"], old_id)
                        self.assertNotIn(new_id, [call[0] for call in self.verifier.calls])
                        self.assertEqual(conn.execute(
                            "SELECT canonical_yt_video_id FROM tracks WHERE track_uid='existing'"
                        ).fetchone()[0], old_id)
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 1)
                    if run == 0:
                        first_run = db_path.read_bytes()
                    else:
                        self.assertEqual(db_path.read_bytes(), first_run)

    def test_confirmed_unavailable_is_excluded_but_ambiguity_blocks_the_run(self):
        for status, state in (
            ({"status": "UNPLAYABLE", "reason": "동영상을 재생할 수 없음"}, "unavailable"),
            ({"status": "UNPLAYABLE", "reason": "This video is available with Music Premium"}, "unknown"),
            ({"status": "LOGIN_REQUIRED"}, "unknown"),
            ({}, "unknown"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "chart.db"
                init_db(db_path)
                self.verifier = PlaylistVerifier(states={ATV: state})
                client = Mock()
                client.get_song.return_value = {
                    "videoDetails": {
                        "videoId": ATV, "musicVideoType": "MUSIC_VIDEO_TYPE_ATV",
                        "title": "Pop Off Pop Off", "author": "KiiiKiii",
                    },
                    "playabilityStatus": status,
                }
                with connect(db_path) as conn, patch.object(chart, "resolve_bilingual_song") as localize:
                    entries = [{
                        "source": "youtube_charts_weekly_browse_api",
                        "original_video_id": SOURCE, "atv_external_video_id": ATV,
                        "title": "Pop Off Pop Off", "artist": "KiiiKiii",
                    }]
                    if state == "unknown":
                        with self.assertRaisesRegex(RuntimeError, "playback is uncertain"):
                            chart.preflight_chart_relations(conn, entries, client, "2026-W37")
                    else:
                        result = chart.preflight_chart_relations(conn, entries, client, "2026-W37")
                        self.assertEqual(result[SOURCE]["excluded_video_ids"], [ATV])
                        self.assertNotIn("video_id", result[SOURCE])
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM source_song_relations"
                    ).fetchone()[0], 0)
                    localize.assert_not_called()

    def test_search_cannot_reselect_rejected_atv_or_keep_it_as_fallback(self):
        results = [{
            "videoId": video_id, "title": "Pop Off Pop Off", "resultType": "song",
            "artists": [{"name": "KiiiKiii"}], "album": {"name": "WhyKiiiKiii"},
        } for video_id in (ATV, EXISTING)]
        with patch.object(sync, "search_ytmusic_songs", return_value=results), patch.object(
            sync, "resolve_bilingual_song", return_value={}
        ):
            resolved = sync.resolve_video_to_song(
                Mock(), video_id=SOURCE, title="Pop Off Pop Off", artist="KiiiKiii",
                excluded_video_ids={ATV},
            )
        self.assertEqual(resolved["resolved_video_id"], EXISTING)
        for title in ("Pop Off Pop Off", ""):
            with self.subTest(title=title), patch.object(sync, "search_ytmusic_songs", return_value=[]):
                resolved = sync.resolve_video_to_song(
                    Mock(), video_id=ATV, title=title, artist="KiiiKiii",
                    excluded_video_ids={ATV},
                )
                self.assertEqual(resolved["resolved_video_id"], "")
                self.assertEqual(resolved["mapping_status"], "failed")

    def test_main_preserves_good_cache_replaces_unavailable_and_keeps_manual_policy(self):
        for cached_id, manual_status in (
            (EXISTING, "cached_match"), (ATV, "cached_match"),
            (ATV, "manual_override"), ("", "manual_blocked"),
        ):
            with self.subTest(cached_id=cached_id, manual_status=manual_status), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "chart-main.db"
                init_db(db_path)
                identity = {"title": "Pop Off Pop Off", "artist": "KiiiKiii", "album": "WhyKiiiKiii"}
                with connect(db_path) as conn:
                    if cached_id:
                        store.ensure_track(conn, track_uid="existing", video_id=cached_id,
                                           yt_title=identity["title"], yt_artist=identity["artist"],
                                           yt_album=identity["album"], status="matched", score=1)
                        for service, song_id in (("ytmusic", SOURCE), ("apple", "123")):
                            store.upsert_track_list_metadata(conn, service=service, song_id=song_id,
                                                             track_uid="existing", row=identity)
                    if manual_status in {"manual_override", "manual_blocked"}:
                        conn.execute("INSERT INTO manual_overrides(service,song_id,action,canonical_yt_video_id,updated_at) "
                                     "VALUES ('ytmusic',?,?,?, '2026-09-14T00:00:00Z')",
                                     (SOURCE, "block" if manual_status == "manual_blocked" else "set_canonical", cached_id or None))
                self.verifier = PlaylistVerifier(states={ATV: "unavailable"},
                    metadata={video_id: {**identity, "confirmed_unavailable": video_id == ATV} for video_id in (ATV, EXISTING)})
                entries = [{"source": "youtube_charts_weekly_browse_api", "rank": 1,
                            "video_id": SOURCE, "atv_external_video_id": ATV, **identity,
                            "chart_period_start": "2026-09-04", "chart_period_end": "2026-09-10"}]
                with (
                    patch("sys.argv", ["chart", "--youtube-charts-csv", "fixture.csv", "--db-path", str(db_path),
                          "--yt-auth", "unused", "--yt-playlist-id", "target", "--chart-period-end", "2026-09-10",
                          "--job-name", "Fixture-Chart-Playability"]),
                    patch.object(chart, "load_dotenv"),
                    patch.object(chart, "make_ytmusic", return_value=Mock()),
                    patch.object(chart, "extract_chart_entries_from_csv", return_value=entries),
                    patch.object(chart, "record_chart_source_audit"),
                    patch.object(chart, "preflight_chart_relations", return_value={SOURCE: {
                        "status": "unavailable", "excluded_video_ids": [ATV]}}),
                    patch.object(chart, "resolve_video_to_song", return_value={
                        "resolved_video_id": EXISTING, "mapping_status": "resolved_to_song",
                        "resolved_title": identity["title"], "resolved_artist": identity["artist"],
                        "resolved_album": identity["album"], "score": 1.0}) as resolve,
                    patch.object(sync, "get_verified_video_metadata", side_effect=lambda _client, video_id, **kwargs:
                                 {"video_id": video_id, "music_video_type": "MUSIC_VIDEO_TYPE_ATV", **identity}),
                    patch.object(chart, "update_ytmusic_playlist") as publish,
                    patch.object(chart.time, "sleep"),
                ):
                    if manual_status == "manual_override":
                        with self.assertRaisesRegex(RuntimeError, "Manual recording is unavailable"):
                            chart.main()
                        publish.assert_not_called()
                        resolve.assert_not_called()
                        continue
                    self.assertEqual(chart.main(), 0)
                expected = [] if manual_status == "manual_blocked" else [EXISTING]
                self.assertEqual(publish.call_args.args[2], expected)
                if cached_id == ATV:
                    self.assertEqual(resolve.call_args.kwargs["excluded_video_ids"], {ATV})
                else:
                    resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
