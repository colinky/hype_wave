"""A rejected new tail must leave every original playlist item untouched."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import ytmusic_playlist_sync as sync
from test_playlist_playability_safety import MovingPlaylist


class AddBeforeRemoveTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("external access forbidden")))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.events = []

    def evidence(self, event):
        saved = {**deepcopy(event), "seq": len(self.events) + 1}
        self.events.append(saved)
        return saved

    def preserve(self, client, target, **kwargs):
        return sync._preserve_playlist_slots(client, "fixture", deepcopy(client._items),
            target, evidence=self.evidence, **kwargs)

    def assert_original_untouched(self, client, before):
        self.assertEqual(client._items[:len(before)], before)
        self.assertEqual(client.remove_calls, 0)
        self.assertEqual(client.moves, [])

    def test_gx_maps_to_existing_jjx_with_distinct_tokens_and_blocks_next_chunk(self):
        first = ["GxChUrrY4bc", *[f"new-{i}" for i in range(49)]]
        client = MovingPlaylist(["JJx_WQXOeK0", "old-unavailable", "keep"],
            requested_values=first, substitute_requested=["JJx_WQXOeK0", *first[1:]])
        client._hype_playability_verifier.states["old-unavailable"] = "unavailable"
        before = deepcopy(client._items)
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.preserve(client, [*first, "next-chunk", "keep"])
        self.assert_original_untouched(client, before)
        self.assertEqual(client.add_calls, [first])
        twins = [row for row in client._items if row["videoId"] == "JJx_WQXOeK0"]
        self.assertEqual(len(twins), 2)
        self.assertNotEqual(twins[0]["setVideoId"], twins[1]["setVideoId"])
        ack = next(e for e in self.events if e["state"] == "ack")
        self.assertEqual(ack["items"][0]["videoId"], "GxChUrrY4bc")
        self.assertEqual(ack["items"][0]["setVideoId"], twins[1]["setVideoId"])
        rejection = self.events[-1]
        self.assertTrue(rejection["identity_review_required"])
        self.assertFalse(rejection["verification_matches"])
        self.assertEqual([(d["expected_id"], d["actual_id"]) for d in rejection["differences"]],
                         [("GxChUrrY4bc", "JJx_WQXOeK0")])

    def test_japanese_source_replaced_by_korean_id_keeps_originals_in_publish_and_restore(self):
        for phase in ("publish", "restore"):
            with self.subTest(phase=phase):
                self.events.clear()
                client = MovingPlaylist(["before", "keep"], requested_values=["0iiW__6izcs"],
                    substitute_requested=["korean-recording"])
                before = deepcopy(client._items)
                with self.assertRaises(sync.PlaylistMutationUncertain):
                    self.preserve(client, ["0iiW__6izcs", "keep"], phase=phase)
                self.assert_original_untouched(client, before)
                self.assertEqual(client.add_calls, [["0iiW__6izcs"]])
                self.assertEqual(self.events[-1]["phase"], phase)
                self.assertTrue(self.events[-1]["identity_review_required"])

    def test_unknown_or_false_availability_after_add_prevents_remove_and_next_add(self):
        for mode in ("health", "state", "availability"):
            with self.subTest(mode=mode):
                class BlockedAfterAdd(MovingPlaylist):
                    def add_playlist_items(self, *args, **kwargs):
                        result = super().add_playlist_items(*args, **kwargs)
                        if mode == "health":
                            self._hype_playability_verifier.health = "unknown"
                        elif mode == "state":
                            self._hype_playability_verifier.states["new-0"] = "unknown"
                        else:
                            self._items[-50]["isAvailable"] = False
                        return result
                client = BlockedAfterAdd(["old"])
                before = deepcopy(client._items)
                with self.assertRaises(sync.PlaybackBlocked):
                    self.preserve(client, [f"new-{i}" for i in range(51)])
                self.assert_original_untouched(client, before)
                self.assertEqual(len(client.add_calls), 1)
                self.assertEqual(self.events[-1]["state"], "ack")

    def test_lost_response_or_ack_commit_stops_without_replay_or_removal(self):
        for mode in ("response", "ack"):
            with self.subTest(mode=mode):
                self.events.clear()
                class LostResponse(MovingPlaylist):
                    def add_playlist_items(self, *args, **kwargs):
                        result = super().add_playlist_items(*args, **kwargs)
                        if mode == "response":
                            raise TimeoutError("response lost after application")
                        return result
                client = LostResponse(["old"])
                before = deepcopy(client._items)
                def evidence(event):
                    if mode == "ack" and event["state"] == "ack":
                        raise RuntimeError("ACK commit failed")
                    return self.evidence(event)
                with self.assertRaises(sync.PlaylistMutationUncertain):
                    sync._preserve_playlist_slots(client, "fixture", before,
                        [f"new-{i}" for i in range(51)], evidence=evidence)
                self.assert_original_untouched(client, before)
                self.assertEqual(len(client.add_calls), 1)
                self.assertFalse(any(e.get("identity_review_required") for e in self.events))

    def test_foreign_token_or_reordered_prefix_is_uncertainty_not_invented_identity_rejection(self):
        for mode in ("token", "order"):
            with self.subTest(mode=mode):
                self.events.clear()
                class BadRead(MovingPlaylist):
                    def get_playlist(self, *args, **kwargs):
                        result = super().get_playlist(*args, **kwargs)
                        if self.add_calls:
                            if mode == "token":
                                result["tracks"][-1]["setVideoId"] = "FOREIGN"
                            else:
                                result["tracks"][:2] = reversed(result["tracks"][:2])
                        return result
                client = BadRead(["old", "keep"])
                before = deepcopy(client._items)
                with self.assertRaises(sync.PlaylistMutationUncertain):
                    self.preserve(client, ["new", "keep"])
                self.assert_original_untouched(client, before)
                self.assertFalse(any(e.get("identity_review_required") for e in self.events))

    def test_same_token_original_id_drift_before_add_is_rejected_without_mutation(self):
        class ChangedBeforeAdd(MovingPlaylist):
            def get_playlist(self, *args, **kwargs):
                result = super().get_playlist(*args, **kwargs)
                result["tracks"][0]["videoId"] = "external-change"
                return result
        client = ChangedBeforeAdd(["old", "keep"])
        before = deepcopy(client._items)
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.preserve(client, ["new", "keep"])
        self.assert_original_untouched(client, before)
        self.assertEqual(client.add_calls, [])
        self.assertEqual(self.events, [])

    def test_count_inconsistent_alias_after_add_holds_without_replaying_or_invented_approval(self):
        class CountAndAlias(MovingPlaylist):
            def get_playlist(self, *args, **kwargs):
                result = super().get_playlist(*args, **kwargs)
                if self.add_calls:
                    result["trackCount"] = 1
                return result
        client = CountAndAlias(["old"], substitute_requested=["alias"])
        before = deepcopy(client._items)
        with self.assertRaises(sync.PlaylistMutationUncertain):
            self.preserve(client, ["new"])
        self.assert_original_untouched(client, before)
        self.assertEqual(client.add_calls, [["new"]])
        self.assertEqual([e["state"] for e in self.events], ["intent", "ack"])

    def test_successful_publish_and_restore_add_then_remove_then_move(self):
        for phase in ("publish", "restore"):
            with self.subTest(phase=phase):
                self.events.clear()
                client = MovingPlaylist(["old", "keep"])
                keep = deepcopy(client._items[1])
                actual = self.preserve(client, ["new", "keep"], phase=phase)
                self.assertEqual([i["videoId"] for i in actual], ["new", "keep"])
                self.assertEqual(actual[1], keep)
                self.assertEqual([e["operation"] for e in self.events if e["state"] == "ack"],
                                 ["add", "remove", "move"])
                self.assertTrue(all(e["phase"] == phase for e in self.events))


if __name__ == "__main__":
    unittest.main()
