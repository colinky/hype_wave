"""Offline regressions for delayed playlist counts after acknowledged edits."""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ytmusic_playlist_sync as sync


def playlist_items(prefix, count):
    return [{"videoId": f"{prefix}-{index}", "setVideoId": f"slot-{prefix}-{index}"}
            for index in range(count)]


class PlaylistCountRetryTests(unittest.TestCase):
    def setUp(self):
        context = ExitStack()
        self.addCleanup(context.close)
        self.sleep = context.enter_context(patch.object(sync.time, "sleep"))
        context.enter_context(patch("socket.socket.connect", side_effect=AssertionError("offline test")))

    def test_zero_count_after_fifty_added_tracks_retries_complete_read(self):
        items = playlist_items("new", 50)
        for reported_count in (0, "0"):
            with self.subTest(reported_count=reported_count):
                client = Mock()
                client.get_playlist.side_effect = [
                    {"tracks": items, "trackCount": reported_count},
                    {"tracks": items, "trackCount": 50},
                ]
                self.assertEqual(sync.get_existing_playlist_items(client, "fixture"), items)
                self.assertEqual(client.get_playlist.call_args_list,
                                 [call("fixture", limit=None), call("fixture", limit=None)])
                client.add_playlist_items.assert_not_called()
                client.remove_playlist_items.assert_not_called()

    def test_permanent_low_or_high_count_stops_after_three_reads(self):
        for reported_count in (0, 100):
            with self.subTest(reported_count=reported_count):
                self.sleep.reset_mock()
                client = Mock()
                client.get_playlist.return_value = {
                    "tracks": playlist_items("new", 50), "trackCount": reported_count,
                }
                with self.assertRaisesRegex(RuntimeError, f"trackCount={reported_count}"):
                    sync.get_existing_playlist_items(client, "fixture")
                self.assertEqual(client.get_playlist.call_count, 3)
                self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_malformed_response_is_not_hidden_by_count_retry(self):
        responses = [None, {}, {"tracks": {}}, {"tracks": [None]},
                     {"tracks": [{"videoId": "new"}]},
                     {"tracks": [{"videoId": " ", "setVideoId": "slot"}]},
                     {"tracks": [{"videoId": "new", "setVideoId": 42}]},
                     {"tracks": playlist_items("new", 1) * 2}]
        for response in responses:
            with self.subTest(response=response):
                if isinstance(response, dict):
                    response["trackCount"] = 999
                client = Mock()
                client.get_playlist.side_effect = [response, {"tracks": [], "trackCount": 0}]
                with self.assertRaises(RuntimeError):
                    sync.get_existing_playlist_items(client, "fixture")
                self.assertEqual(client.get_playlist.call_count, 1)
        self.sleep.assert_not_called()

    def test_read_exception_is_not_retried(self):
        for error in (PermissionError("403 denied"), TimeoutError("read timed out")):
            with self.subTest(error=error):
                client = Mock()
                client.get_playlist.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "Failed to fetch"):
                    sync.get_existing_playlist_items(client, "fixture")
                self.assertEqual(client.get_playlist.call_count, 1)
        self.sleep.assert_not_called()

    def replace_after_stale_count(self, *, foreign_slot=False, persistent=False):
        original = playlist_items("old", 100)
        target = [item["videoId"] for item in playlist_items("new", 100)]
        state = deepcopy(original)
        client = Mock()
        events = []
        stale_reads = []

        def read(*args, **kwargs):
            observed = deepcopy(state)
            count = len(observed)
            if client.add_playlist_items.call_count == 1:
                if persistent or not stale_reads:
                    stale_reads.append(len(observed))
                    count = 0
                elif foreign_slot:
                    observed[0]["setVideoId"] = "foreign-slot"
            return {"tracks": observed, "trackCount": count}

        def remove(_playlist_id, removed):
            slots = {item["setVideoId"] for item in removed}
            state[:] = [item for item in state if item["setVideoId"] not in slots]
            return {"status": "STATUS_SUCCEEDED"}

        def add(_playlist_id, requested, **kwargs):
            added = [{"videoId": value, "setVideoId": f"slot-{value}"} for value in requested]
            state.extend(added)
            return {"status": "STATUS_SUCCEEDED", "playlistEditResults": deepcopy(added)}

        def evidence(event):
            saved = {**deepcopy(event), "seq": len(events) + 1}
            events.append(saved)
            return saved

        client.get_playlist.side_effect = read
        client.remove_playlist_items.side_effect = remove
        client.add_playlist_items.side_effect = add
        if foreign_slot:
            with self.assertRaisesRegex(RuntimeError, "slots changed"):
                sync._replace_playlist_contents(client, "fixture", original, target,
                                                evidence=evidence, phase="publish")
        else:
            actual = sync._replace_playlist_contents(client, "fixture", original, target,
                                                     evidence=evidence, phase="publish")
            self.assertEqual(actual, state)
            self.assertEqual([item["videoId"] for item in actual], target)
        self.assertEqual(client.remove_playlist_items.call_args_list,
                         [call("fixture", original[:50]), call("fixture", original[50:])])
        expected_adds = [call("fixture", target[:50], duplicates=False)]
        if not foreign_slot:
            expected_adds.append(call("fixture", target[50:], duplicates=False))
        self.assertEqual(client.add_playlist_items.call_args_list, expected_adds)
        self.assertEqual([row["state"] for row in events],
                         ["intent", "ack"] * (len(expected_adds) + 2))
        self.assertEqual(stale_reads, [50] * (3 if persistent else 1))

    def test_stale_count_between_add_chunks_never_replays_mutation(self):
        self.replace_after_stale_count()
        self.sleep.assert_called_once_with(1)

    def test_foreign_slot_after_retry_still_blocks_next_mutation(self):
        self.replace_after_stale_count(foreign_slot=True)
        self.sleep.assert_called_once_with(1)

    def test_persistent_count_between_chunks_uses_complete_receipts_without_replaying_edits(self):
        self.replace_after_stale_count(persistent=True)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_durable_recovery_restores_two_hundred_items_after_incomplete_first_chunk_read(self):
        import hype_db
        from reconcile_playlist_update import reconcile_playlist_update

        original = playlist_items("old", 200)
        ids = [item["videoId"] for item in original]
        state = deepcopy(original)
        client = Mock()
        stale = True

        def read(*args, **kwargs):
            incomplete = stale and len(state) == 50
            return {"tracks": deepcopy(state[:-1] if incomplete else state), "trackCount": 0 if incomplete else len(state)}

        def remove(_playlist, items):
            slots = {item["setVideoId"] for item in items}
            state[:] = [item for item in state if item["setVideoId"] not in slots]
            return {"status": "STATUS_SUCCEEDED"}

        def add(_playlist, values, **kwargs):
            added = [{"videoId": value, "setVideoId": f"new-{client.add_playlist_items.call_count}-{value}"}
                     for value in values]
            state.extend(added)
            return {"status": "STATUS_SUCCEEDED", "playlistEditResults": deepcopy(added)}

        client.get_playlist.side_effect = read
        client.remove_playlist_items.side_effect = remove
        client.add_playlist_items.side_effect = add
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}):
            db = Path(directory) / "recovery.db"
            run_id = hype_db.record_playlist_update(
                db, playlist_id="fixture", service="apple", job_name="KR-Top-Songs",
                existing_items=original, requested_video_ids=ids, claim_token="publisher",
            )

            def evidence(event):
                return hype_db.append_playlist_update_evidence(db, run_id, event, claim_token="publisher")

            with self.assertRaisesRegex(RuntimeError, "trackCount=0"):
                sync._replace_playlist_contents(client, "fixture", original, ids, evidence=evidence, phase="publish")
            hype_db.finish_playlist_update(db, run_id, status="recovery_required", claim_token="publisher")
            self.assertEqual(len(state), 50)
            stale = False
            plan = reconcile_playlist_update(client, db, run_id, "fixture")
            self.assertEqual(plan["action"], "restore_owned_items")
            result = reconcile_playlist_update(client, db, run_id, "fixture", apply=True, workers_quiescent=True)
            self.assertTrue(result["applied"])
            self.assertEqual([item["videoId"] for item in state], ids)
            self.assertEqual(hype_db.get_playlist_update_run(db, run_id)["status"], "restored")

    def test_count_fallback_requires_complete_owned_slots_in_order(self):
        expected = playlist_items("new", 50)
        for case in ("exact", "missing", "extra", "foreign_slot", "changed_video", "reordered"):
            actual = deepcopy(expected)
            if case == "missing": actual.pop()
            elif case == "extra": actual += playlist_items("other", 1)
            elif case == "foreign_slot": actual[0]["setVideoId"] = "foreign-slot"
            elif case == "changed_video": actual[0]["videoId"] = "different-video"
            elif case == "reordered": actual.reverse()
            client = Mock()
            client.get_playlist.return_value = {"tracks": actual, "trackCount": 0}
            with self.subTest(case=case):
                if case in {"exact", "changed_video"}:
                    self.assertEqual(sync.get_existing_playlist_items(client, "fixture", expected_items=expected), actual)
                else:
                    with self.assertRaisesRegex(RuntimeError, "trackCount=0"):
                        sync.get_existing_playlist_items(client, "fixture", expected_items=expected)
            self.assertEqual(client.get_playlist.call_count, 3)
            client.add_playlist_items.assert_not_called()
            client.remove_playlist_items.assert_not_called()

    def test_stale_count_with_substitutions_still_requires_strict_publication_identity(self):
        import hype_db

        requested = [f"new-{index}" for index in range(200)]
        aliases = {"new-8": "alias-8", "new-41": "alias-41"}
        canonical = {alias: video for video, alias in aliases.items()}
        for case in ("equivalent", "wrong_title", "wrong_artist", "missing_metadata"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}):
                state = playlist_items("old", 200)
                client = Mock()
                client.language = "en"

                def read(*args, **kwargs):
                    return {"tracks": deepcopy(state),
                            "trackCount": 0 if client.add_playlist_items.called else len(state)}

                def remove(_playlist, items):
                    slots = {item["setVideoId"] for item in items}
                    state[:] = [item for item in state if item["setVideoId"] not in slots]
                    return {"status": "STATUS_SUCCEEDED"}

                def add(_playlist, values, **kwargs):
                    receipts = [{"videoId": value, "setVideoId": f"added-{value}"} for value in values]
                    state.extend({**item, "videoId": aliases.get(item["videoId"], item["videoId"])}
                                 for item in receipts)
                    return {"status": "STATUS_SUCCEEDED", "playlistEditResults": receipts}

                def song(video_id):
                    if case == "missing_metadata" and video_id == "alias-8":
                        return {}
                    title = canonical.get(video_id, video_id)
                    if case == "wrong_title" and video_id == "alias-8":
                        title = "Unrelated song"
                    return {"videoDetails": {"videoId": video_id, "title": title,
                            "author": "Fixture Artist", "lengthSeconds": "200",
                            "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"}}

                def watch(videoId, *args, **kwargs):
                    artist_id = "UC-other" if case == "wrong_artist" and videoId == "alias-8" else "UC-fixture"
                    return {"tracks": [{"videoId": videoId, "title": canonical.get(videoId, videoId),
                            "artists": [{"name": "Fixture Artist", "id": artist_id}]}]}

                client.get_playlist.side_effect = read
                client.remove_playlist_items.side_effect = remove
                client.add_playlist_items.side_effect = add
                client.get_song.side_effect = song
                client.get_watch_playlist.side_effect = watch
                client.get_artist.return_value = {"name": "Fixture Artist"}
                db = Path(directory) / "publication.db"
                with patch.object(sync, "make_ytmusic", return_value=client):
                    sync.update_ytmusic_playlist(client, "fixture", requested, dry_run=False,
                                                db_path=db, service="apple", job_name="KR-Top-Songs")
                with hype_db.connect(db, read_only=True) as conn:
                    run_id = conn.execute("SELECT update_run_id FROM playlist_update_runs").fetchone()[0]
                run = hype_db.get_playlist_update_run(db, run_id, read_only=True)
                self.assertEqual(run["status"], "published")
                self.assertFalse(hype_db.get_pending_playlist_recovery(db, "fixture"))
                expected = [aliases.get(video, video) for video in requested
                            if case == "equivalent" or video != "new-8"]
                self.assertEqual([item["videoId"] for item in state], expected)
                self.assertEqual(len(state), 200 if case == "equivalent" else 199)
                self.assertEqual(client.add_playlist_items.call_count, 4)
                self.assertEqual(client.remove_playlist_items.call_count, 4 if case == "equivalent" else 5)
                if case != "equivalent":
                    reason = {"wrong_title": "title_mismatch", "wrong_artist": "author_mismatch",
                              "missing_metadata": "metadata_error"}[case]
                    self.assertEqual(run["publication_mode"], "partial")
                    self.assertEqual(run["excluded_items"][0]["reason"], reason)


if __name__ == "__main__":
    unittest.main()
