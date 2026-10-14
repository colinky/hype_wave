"""Offline publication and durable recovery checks for incremental playlist edits."""
from copy import deepcopy
from itertools import combinations, permutations
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hype_db
import ytmusic_playlist_sync as sync
from reconcile_playlist_update import reconcile_playlist_update


class Playlist:
    def __init__(self, videos):
        self.items = [{"videoId": video, "setVideoId": f"original-{i}"} for i, video in enumerate(videos)]
        self.original = deepcopy(self.items)
        self.aliases, self.canonical = {}, {}
        self.artist_ids = {}
        self.adds, self.removes, self.moves, self.timeline = [], [], [], []
        self.move_batches = []
        self.partial_batch = False
        self.intermediate_read = False
        self.pending_reads = []
        self.lag, self.zero_count, self.lose_move, self.lose_add, self.foreign_after_add = 0, False, False, False, False
        self.lose_remove = False
        self.next_slot = 0
        self.language = "en"

    def get_playlist(self, *args, **kwargs):
        items = self.pending_reads.pop(0) if self.pending_reads else self.items
        return {"tracks": deepcopy(items), "trackCount": 0 if self.zero_count and self.adds else len(items)}

    def delay(self, before):
        self.pending_reads = [deepcopy(before) for _ in range(self.lag)]

    def get_song(self, video_id):
        return {"videoDetails": {"videoId": video_id, "title": self.canonical.get(video_id, video_id),
                "author": "Fixture Artist", "lengthSeconds": "200", "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"}}

    def get_watch_playlist(self, videoId):
        return {"tracks": [{"videoId": videoId, "title": self.canonical.get(videoId, videoId),
                "artists": [{"name": "Fixture Artist", "id": self.artist_ids.get(videoId, "UC-fixture")}]}]}

    def get_artist(self, *args):
        return {"name": "Fixture Artist"}

    def add_playlist_items(self, playlist_id, values, duplicates=False):
        before, receipts = deepcopy(self.items), []
        self.adds.append(list(values))
        self.timeline.append("add")
        for value in values:
            self.next_slot += 1
            slot = f"added-{self.next_slot}"
            self.items.append({"videoId": self.aliases.get(value, value), "setVideoId": slot})
            receipts.append({"videoId": value, "setVideoId": slot})
        if self.foreign_after_add:
            self.items[0]["setVideoId"] = "FOREIGN"
        self.delay(before)
        if self.lose_add:
            raise TimeoutError("add applied but response lost")
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": receipts}

    def remove_playlist_items(self, playlist_id, items):
        before = deepcopy(self.items)
        self.removes.append(deepcopy(items))
        self.timeline.append("remove")
        slots = {item["setVideoId"] for item in items}
        self.items = [item for item in self.items if item["setVideoId"] not in slots]
        self.delay(before)
        if self.lose_remove:
            raise TimeoutError("remove applied but response lost")
        return {"status": "STATUS_SUCCEEDED"}

    def edit_playlist(self, playlist_id, **kwargs):
        if "moveItem" in kwargs:
            before = deepcopy(self.items)
            move = kwargs["moveItem"]
            slot, successor = move if isinstance(move, tuple) else (move, "")
            item = next(item for item in self.items if item["setVideoId"] == slot)
            self.items.remove(item)
            position = next((i for i, item in enumerate(self.items) if item["setVideoId"] == successor), len(self.items))
            self.items.insert(position, item)
            self.moves.append((slot, successor))
            self.timeline.append("move")
            self.delay(before)
            if self.lose_move:
                raise TimeoutError("move applied but response lost")
        return {"status": "STATUS_SUCCEEDED"}

    def _check_auth(self):
        pass

    def _send_request(self, endpoint, body):
        assert endpoint == "browse/edit_playlist" and body["playlistId"] == "fixture"
        before = deepcopy(self.items)
        self.move_batches.append(deepcopy(body["actions"]))
        for index, action in enumerate(body["actions"]):
            assert action["action"] == "ACTION_MOVE_VIDEO_BEFORE"
            self.edit_playlist("fixture", moveItem=(action["setVideoId"], action.get("movedSetVideoIdSuccessor", "")))
            if self.partial_batch:
                break  # An overall success cannot authorize a partially applied batch.
            if index == 0 and self.intermediate_read:
                before = deepcopy(self.items)
        self.delay(before)
        return {"status": "STATUS_SUCCEEDED"}


class PlaylistDeltaTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "audit.db"
        self.enterContext(patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("psycopg2.connect", side_effect=AssertionError("production DB forbidden")))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.enterContext(sync.bilingual_cache_read_only())

    def publish(self, client, target):
        with patch.object(sync, "make_ytmusic", return_value=client):
            sync.update_ytmusic_playlist(client, "fixture", target, dry_run=False,
                db_path=self.db, service="apple", job_name="Delta-Fixture")

    def latest(self):
        with hype_db.connect(self.db, read_only=True) as conn:
            run_id = conn.execute("SELECT update_run_id FROM playlist_update_runs ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()[0]
        return hype_db.get_playlist_update_run(self.db, run_id, read_only=True)

    def test_one_changed_song_retains_199_original_slots_and_uses_one_move(self):
        videos = [f"song-{i}" for i in range(200)]
        client = Playlist(videos)
        self.publish(client, ["new", *videos[1:]])
        self.assertEqual(client.items[1:], client.original[1:])
        self.assertEqual(client.adds, [["new"]])
        self.assertEqual(client.removes, [[client.original[0]]])
        self.assertEqual(len(client.moves), 1)
        self.assertEqual(len(client.move_batches), 1)
        self.assertEqual(client.timeline, ["add", "remove", "move"])
        self.assertEqual(self.latest()["status"], "published")

    def test_rotation_uses_one_move_without_add_or_remove(self):
        videos = [f"song-{i}" for i in range(100)]
        client = Playlist(videos)
        self.publish(client, [*videos[1:], videos[0]])
        self.assertEqual(client.items, [*client.original[1:], client.original[0]])
        self.assertEqual(len(client.moves), 1)
        self.assertEqual((client.adds, client.removes), ([], []))
        self.assertEqual(sync._recoverable_playlist_items(self.latest()), client.items)

    def test_current_order_performs_no_item_mutation(self):
        client = Playlist(["a", "b", "c"])
        self.publish(client, ["a", "b", "c"])
        self.assertEqual(client.timeline, [])
        self.assertEqual(self.latest()["status"], "skipped_current")

    def test_reverse_100_items_uses_two_requests_and_retains_every_original(self):
        videos = [f"song-{i}" for i in range(100)]
        client = Playlist(videos)
        self.publish(client, list(reversed(videos)))
        self.assertEqual(client.items, list(reversed(client.original)))
        self.assertEqual(len(client.moves), 99)
        self.assertEqual([len(batch) for batch in client.move_batches], [50, 49])
        self.assertEqual((client.adds, client.removes), ([], []))
        self.assertEqual(sync._recoverable_playlist_items(self.latest()), client.items)

    def test_known_intermediate_batch_read_settles_without_replay(self):
        client = Playlist(["a", "b", "c", "d"])
        client.lag, client.intermediate_read = 2, True
        self.publish(client, ["d", "c", "b", "a"])
        self.assertEqual(client.items, list(reversed(client.original)))
        self.assertEqual(len(client.move_batches), 1)

    def test_partial_batch_success_cannot_acknowledge_or_replay(self):
        client = Playlist(["a", "b", "c", "d"])
        client.partial_batch = True
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client, ["d", "c", "b", "a"])
        self.assertEqual(len(client.move_batches), 1)
        self.assertEqual(len(client.moves), 1)
        with self.assertRaises(RuntimeError):
            sync._recoverable_playlist_items(self.latest())
        with self.assertRaises(sync.PlaylistPublicationBlocked):
            self.publish(client, ["d", "c", "b", "a"])
        self.assertEqual(len(client.move_batches), 1)

    def test_equivalent_alias_is_retained_and_moved_with_strict_artist_proof(self):
        client = Playlist(["alias", "b", "obsolete"])
        client.canonical["alias"] = "a"
        self.publish(client, ["b", "a"])
        self.assertEqual(client.items, [client.original[1], client.original[0]])
        self.assertEqual(client.adds, [])
        self.assertEqual(client.removes, [[client.original[2]]])
        self.assertEqual(len(client.moves), 1)

    def test_same_title_from_different_artist_is_replaced_instead_of_reused(self):
        client = Playlist(["alias", "b"])
        client.canonical["alias"] = "a"
        client.artist_ids["alias"] = "UC-other"
        self.publish(client, ["a", "b"])
        self.assertEqual(client.adds, [["a"]])
        self.assertEqual(client.removes, [[client.original[0]]])
        self.assertEqual(client.items[1], client.original[1])

    def test_known_old_reads_after_add_remove_and_move_settle_without_replaying(self):
        client = Playlist(["a", "b", "obsolete"])
        client.lag = 2
        self.publish(client, ["new", "b", "a"])
        self.assertEqual([item["videoId"] for item in client.items], ["new", "b", "a"])
        self.assertEqual(client.adds, [["new"]])
        self.assertEqual(len(client.removes), 1)
        self.assertEqual(len(client.moves), 2)

    def test_stale_zero_count_preserves_items_and_delta_publication(self):
        client = Playlist(["a", "obsolete"])
        client.zero_count = True
        self.publish(client, ["new", "a"])
        self.assertEqual(client.items[-1], client.original[0])
        self.assertEqual(client.adds, [["new"]])
        self.assertEqual(client.removes, [[client.original[1]]])
        self.assertEqual(self.latest()["status"], "published")

    def test_rejected_only_new_song_restores_originals_without_readding_them(self):
        client = Playlist(["old"])
        client.aliases["new"] = "unrelated"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.publish(client, ["new"])
        self.assertEqual(client.items, client.original)
        self.assertEqual(client.adds, [["new"]])
        self.assertEqual(len(client.removes), 1)
        self.assertTrue(all(item["setVideoId"].startswith("added-") for item in client.removes[0]))
        self.assertEqual(self.latest()["status"], "recovery_required")
        self.assertTrue(self.latest()["identity_review_required"])

    def test_partial_policy_omits_bad_addition_and_keeps_unchanged_original(self):
        client = Playlist(["keep", "obsolete"])
        client.aliases["bad"] = "unrelated"
        self.publish(client, ["keep", "good", "bad"])
        self.assertEqual(client.items[0], client.original[0])
        self.assertEqual([item["videoId"] for item in client.items], ["keep", "good"])
        run = self.latest()
        self.assertEqual((run["status"], run["publication_mode"]), ("published", "partial"))
        self.assertEqual(run["effective_video_ids"], ["keep", "good"])
        self.assertEqual(run["excluded_items"][0]["position"], 3)
        self.assertFalse(hype_db.get_pending_playlist_recovery(self.db, "fixture"))

    def test_lost_add_response_leaves_originals_untouched_and_blocks_next_run(self):
        client = Playlist(["a", "old"])
        client.lose_add = True
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client, ["a", "new"])
        self.assertEqual(client.items[:2], client.original)
        self.assertEqual(client.removes, [])
        self.assertEqual(client.moves, [])
        with self.assertRaises(sync.PlaylistPublicationBlocked):
            self.publish(client, ["a", "new"])
        self.assertEqual(client.adds, [["new"]])

    def test_lost_move_response_or_ack_commit_never_replays_or_rolls_back(self):
        append = hype_db.append_playlist_update_evidence
        for failure in ("response", "ack_commit"):
            with self.subTest(failure=failure):
                self.db = self.db.with_name(f"{failure}.db")
                client = Playlist(["a", "b", "c"])
                client.lose_move = failure == "response"
                def evidence(*args, **kwargs):
                    if failure == "ack_commit" and args[2]["operation"] == "move_batch" and args[2]["state"] == "ack":
                        raise RuntimeError("ACK commit failed")
                    return append(*args, **kwargs)
                with patch("hype_db.append_playlist_update_evidence", side_effect=evidence), \
                        self.assertRaises(sync.PlaylistMutationUncertain):
                    self.publish(client, ["b", "a", "c"])
                self.assertEqual(len(client.moves), 1)
                self.assertEqual((client.adds, client.removes), ([], []))
                self.assertEqual(self.latest()["status"], "recovery_required")
                with self.assertRaises(RuntimeError):
                    sync._recoverable_playlist_items(self.latest())

    def test_lost_remove_response_keeps_retained_slot_and_blocks_reordering(self):
        client = Playlist(["keep", "obsolete"])
        client.lose_remove = True
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.publish(client, ["new", "keep"])
        self.assertEqual(client.items[0], client.original[0])
        self.assertEqual(client.adds, [["new"]])
        self.assertEqual(len(client.removes), 1)
        self.assertEqual(client.moves, [])
        self.assertEqual(self.latest()["status"], "recovery_required")

    def test_intent_commit_failure_prevents_move_request(self):
        client = Playlist(["a", "b"])
        append = hype_db.append_playlist_update_evidence
        def fail_intent(*args, **kwargs):
            if args[2]["operation"] == "move_batch" and args[2]["state"] == "intent":
                raise RuntimeError("intent commit failed")
            return append(*args, **kwargs)
        with patch("hype_db.append_playlist_update_evidence", side_effect=fail_intent), self.assertRaises(RuntimeError):
            self.publish(client, ["b", "a"])
        self.assertEqual(client.items, client.original)
        self.assertEqual(client.timeline, [])
        self.assertEqual(self.latest()["status"], "restored")

    def test_crash_after_move_ack_reconciles_by_moving_original_slots_only(self):
        videos = [f"song-{i}" for i in range(60)]
        client = Playlist(videos)
        append = hype_db.append_playlist_update_evidence
        def crash(*args, **kwargs):
            event = append(*args, **kwargs)
            if args[2]["operation"] == "move_batch" and args[2]["state"] == "ack":
                raise SystemExit("crash after durable ACK")
            return event
        with patch("hype_db.append_playlist_update_evidence", side_effect=crash), self.assertRaises(SystemExit):
            self.publish(client, list(reversed(videos)))
        run = self.latest()
        self.assertEqual(sync._recoverable_playlist_items(run), client.items)
        plan = reconcile_playlist_update(client, self.db, run["update_run_id"], "fixture")
        self.assertEqual(plan["action"], "restore_owned_items")
        with self.assertRaisesRegex(RuntimeError, "workers"):
            reconcile_playlist_update(client, self.db, run["update_run_id"], "fixture", apply=True)
        result = reconcile_playlist_update(client, self.db, run["update_run_id"], "fixture",
            apply=True, workers_quiescent=True)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(client.items, client.original)
        self.assertEqual((client.adds, client.removes), ([], []))

    def test_foreign_slot_after_add_stops_before_removal_or_reorder(self):
        client = Playlist(["a", "old"])
        client.foreign_after_add = True
        with self.assertRaisesRegex(RuntimeError, "slots"):
            self.publish(client, ["a", "new"])
        self.assertEqual(client.removes, [])
        self.assertEqual(client.moves, [])
        self.assertEqual(self.latest()["status"], "recovery_required")

    def test_unacknowledged_move_cannot_finalize_matching_old_snapshot(self):
        client = Playlist(["a", "b"])
        run_id = hype_db.record_playlist_update(self.db, playlist_id="fixture", service="apple", job_name="Delta-Fixture",
            requested_video_ids=["b", "a"], existing_items=client.items, claim_token="owner")
        hype_db.append_playlist_update_evidence(self.db, run_id, {"phase": "publish", "operation": "move",
            "state": "intent", "items": [client.items[0]], "before_items": client.items,
            "successor_set_video_id": "", "attempt": 1}, claim_token="owner")
        hype_db.finish_playlist_update(self.db, run_id, status="recovery_required", actual_items=client.items, claim_token="owner")
        plan = reconcile_playlist_update(client, self.db, run_id, "fixture")
        self.assertEqual(plan["action"], "blocked")
        self.assertEqual(client.timeline, [])

    def test_move_receipt_rejects_wrong_successor_incomplete_or_wrong_order(self):
        client = Playlist(["a", "b", "c"])
        run_id = hype_db.record_playlist_update(self.db, playlist_id="fixture", service="apple", job_name="Delta-Fixture",
            requested_video_ids=["b", "a", "c"],
            existing_items=client.items, claim_token="owner")
        intent = hype_db.append_playlist_update_evidence(self.db, run_id, {"phase": "publish", "operation": "move",
            "state": "intent", "items": [client.items[0]], "before_items": client.items,
            "successor_set_video_id": "original-2"}, claim_token="owner")
        correct = [client.items[1], client.items[0], client.items[2]]
        for after, successor in ((correct[:-1], "original-2"), (client.items, "original-2"), (correct, "")):
            with self.assertRaises(ValueError):
                hype_db.append_playlist_update_evidence(self.db, run_id, {"phase": "publish", "operation": "move",
                    "state": "ack", "items": [client.items[0]], "after_items": after,
                    "successor_set_video_id": successor, "intent_seq": intent["seq"]}, claim_token="owner")

    def test_batch_receipt_requires_exact_plan_items_and_complete_order(self):
        client = Playlist(["a", "b", "c"])
        run_id = hype_db.record_playlist_update(self.db, playlist_id="fixture", service="apple", job_name="Delta-Fixture",
            requested_video_ids=["c", "b", "a"], existing_items=client.items, claim_token="owner")
        moves = [{"set_video_id": "original-1", "successor_set_video_id": "original-0"},
                 {"set_video_id": "original-2", "successor_set_video_id": "original-1"}]
        event = {"phase": "publish", "operation": "move_batch", "items": client.items[1:], "moves": moves}
        intent = hype_db.append_playlist_update_evidence(self.db, run_id,
            {**event, "state": "intent", "before_items": client.items}, claim_token="owner")
        correct = list(reversed(client.items))
        wrong_plan = [moves[0], {**moves[1], "successor_set_video_id": ""}]
        for changes in ({"after_items": correct[:-1]}, {"after_items": client.items}, {"moves": wrong_plan},
                        {"items": list(reversed(client.items[1:]))}):
            with self.assertRaises(ValueError):
                hype_db.append_playlist_update_evidence(self.db, run_id,
                    {**event, "state": "ack", "intent_seq": intent["seq"], "after_items": correct, **changes}, claim_token="owner")
        hype_db.append_playlist_update_evidence(self.db, run_id,
            {**event, "state": "ack", "intent_seq": intent["seq"], "after_items": correct}, claim_token="owner")
        self.assertEqual(sync._recoverable_playlist_items(hype_db.get_playlist_update_run(self.db, run_id)), correct)

    def test_minimum_moves_reach_every_six_item_permutation(self):
        baseline = [str(i) for i in range(6)]
        items = [{"videoId": slot, "setVideoId": slot} for slot in baseline]
        for target in permutations(baseline):
            positions = {slot: i for i, slot in enumerate(target)}
            longest = max(len(subset) for size in range(7) for subset in combinations(baseline, size)
                          if [positions[slot] for slot in subset] == sorted(positions[slot] for slot in subset))
            stationary = sync._stationary_playlist_slots(baseline, list(target))
            self.assertEqual(len(stationary), longest)
            after, successor = items, ""
            for slot in reversed(target):
                if slot not in stationary:
                    after = sync._moved_playlist_items(after, slot, successor)
                successor = slot
            self.assertEqual(sync._playlist_slots(after), list(target))


if __name__ == "__main__":
    unittest.main()
