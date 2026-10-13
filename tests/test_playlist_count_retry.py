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
        if foreign_slot or persistent:
            message = "slots changed" if foreign_slot else "trackCount=0"
            with self.assertRaisesRegex(RuntimeError, message):
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
        if not (foreign_slot or persistent):
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

    def test_persistent_count_between_chunks_preserves_ack_and_stops(self):
        self.replace_after_stale_count(persistent=True)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_durable_recovery_restores_two_hundred_items_after_first_chunk_count_failure(self):
        import hype_db
        from reconcile_playlist_update import reconcile_playlist_update

        original = playlist_items("old", 200)
        ids = [item["videoId"] for item in original]
        state = deepcopy(original)
        client = Mock()
        stale = True

        def read(*args, **kwargs):
            return {"tracks": deepcopy(state), "trackCount": 0 if stale and len(state) == 50 else len(state)}

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


if __name__ == "__main__":
    unittest.main()
