from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import ytmusic_playlist_sync as playlist_sync
from playlist_playability_fixture import PlaylistVerifier


SUBSTITUTIONS = {
    18: ("to3sjq-CAvA", "i0zqeEAJEx8"),
    19: ("Gk8F8waA0tM", "4Lv-UEjc53g"),
    34: ("oZ61Le_JdVY", "nNgPxwyPDe0"),
    40: ("pHciG9_2xXM", "f7-z0FDBpZo"),
    46: ("TEbPKeeJnlA", "2Ib3TgRsyGA"),
    63: ("9XnsnNHj4Jk", "B-zN_Y8w7Us"),
    75: ("Yyfv2R97dHA", "Jit6zG2_Xho"),
}


def detail(title: str, author: str, seconds: int, kind: str = "ATV") -> dict:
    return {
        "videoDetails": {
            "title": title,
            "author": author,
            "lengthSeconds": str(seconds),
            "musicVideoType": kind,
        }
    }


def weekly_ids() -> tuple[list[str], list[str]]:
    expected = [f"same-{position:03d}" for position in range(1, 101)]
    actual = list(expected)
    for position, (expected_id, actual_id) in SUBSTITUTIONS.items():
        expected[position - 1] = expected_id
        actual[position - 1] = actual_id
    return expected, actual


def weekly_metadata() -> dict[str, dict]:
    rows = {
        "to3sjq-CAvA": detail("생각을 멈추다 보면", "최유리", 206, "MUSIC_VIDEO_TYPE_UGC"),
        "i0zqeEAJEx8": detail("생각을 멈추다 보면", "최유리", 200),
        "Gk8F8waA0tM": detail("행복한 나를 (2026ver.) MV", "하예일기장", 287, "MUSIC_VIDEO_TYPE_OMV"),
        "4Lv-UEjc53g": detail("Happy Me (2026 ver.)", "송하예", 276),
        "oZ61Le_JdVY": detail("마음의 숲", "조선힙합", 189, "MUSIC_VIDEO_TYPE_OMV"),
        "nNgPxwyPDe0": detail("마음의 숲", "조선힙합", 189),
        "pHciG9_2xXM": detail("Meet my GRLS, TUIDE", "HYBE LABELS", 192, "MUSIC_VIDEO_TYPE_UGC"),
        "f7-z0FDBpZo": detail("GRLS", "TUIDE", 169),
        "TEbPKeeJnlA": detail("GG EZ", "M.Sasuke", 168, "MUSIC_VIDEO_TYPE_OMV"),
        "2Ib3TgRsyGA": detail("GG EZ", "M.Sasuke", 168),
        "9XnsnNHj4Jk": detail("[MV] 주호(Juho) - 녹슨 가슴(Rusted Heart)", "주호", 290, "MUSIC_VIDEO_TYPE_OMV"),
        "B-zN_Y8w7Us": detail("녹슨 가슴", "주호", 284),
        "Yyfv2R97dHA": detail("남유정 '초대 (feat. 이민혁)' Performance Video", "유랄라Youlalla", 192, "MUSIC_VIDEO_TYPE_UGC"),
        "Jit6zG2_Xho": detail("초대", "남유정", 189),
    }
    for video_id, payload in rows.items():
        payload["videoDetails"]["videoId"] = video_id
    return rows


class StatefulPlaylist:
    def __init__(
        self,
        video_ids: list[str],
        *,
        fail_requested: bool = False,
        fail_restore: bool = False,
        substitute_requested: list[str] | None = None,
        requested_values: list[str] | None = None,
    ) -> None:
        self._hype_playability_verifier = PlaylistVerifier()
        self._next_slot = 0
        self._items = []
        self.video_ids = list(video_ids)
        self.original = list(video_ids)
        self.fail_requested = fail_requested
        self.fail_restore = fail_restore
        self.substitute_requested = substitute_requested
        self.requested_values = requested_values or ["new"]
        self.edit_calls = 0
        self.remove_calls = 0
        self.add_calls: list[list[str]] = []

    @property
    def video_ids(self):
        return [item["videoId"] for item in self._items]

    @video_ids.setter
    def video_ids(self, values):
        self._items = []
        for value in values:
            self._next_slot += 1
            self._items.append({"videoId": value, "setVideoId": f"set-{self._next_slot}"})

    def get_playlist(self, _playlist_id: str, limit=None) -> dict:
        return {
            "tracks": deepcopy(self._items)
        }

    def edit_playlist(self, _playlist_id: str, **kwargs) -> dict:
        self.edit_calls += 1
        move = kwargs.get("moveItem")
        if move is not None:
            slot, successor = move if isinstance(move, tuple) else (move, "")
            item = next(row for row in self._items if row["setVideoId"] == slot)
            self._items.remove(item)
            position = next((i for i, row in enumerate(self._items) if row["setVideoId"] == successor), len(self._items))
            self._items.insert(position, item)
        return {"status": "STATUS_SUCCEEDED"}

    def get_song(self, video_id: str) -> dict:
        payload = detail(video_id, "Fixture Artist", 200)
        payload["videoDetails"]["videoId"] = video_id
        return payload

    def remove_playlist_items(self, _playlist_id: str, _items: list[dict]) -> dict:
        self.remove_calls += 1
        removed = {item["setVideoId"] for item in _items}
        self._items = [item for item in self._items if item["setVideoId"] not in removed]
        return {"status": "STATUS_SUCCEEDED"}

    def add_playlist_items(
        self, _playlist_id: str, video_ids: list[str], duplicates: bool = False
    ) -> dict:
        values = list(video_ids)
        self.add_calls.append(values)
        if values == self.requested_values and self.fail_requested:
            raise RuntimeError("new addition failed")
        if values == self.original and self.fail_restore:
            raise RuntimeError("restore failed")
        if values == self.requested_values and self.substitute_requested is not None:
            actual = self.substitute_requested
        else:
            actual = values
        receipts = []
        for requested, video_id in zip(values, actual):
            self._next_slot += 1
            slot = f"set-{self._next_slot}"
            self._items.append({"videoId": video_id, "setVideoId": slot})
            receipts.append({"videoId": requested, "setVideoId": slot})
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": receipts}


class PlaylistComparisonRegressionTests(unittest.TestCase):
    def test_weekly_fixture_reports_all_seven_differences(self) -> None:
        expected, actual = weekly_ids()
        metadata = weekly_metadata()
        ytmusic = Mock()
        ytmusic.get_song.side_effect = lambda video_id: metadata[video_id]
        ytmusic.get_watch_playlist.side_effect = lambda videoId, **kwargs: {"tracks": [{
            "videoId": videoId, "title": metadata[videoId]["videoDetails"]["title"],
            "artists": [{"id": metadata[videoId]["videoDetails"]["author"],
                         "name": metadata[videoId]["videoDetails"]["author"]}],
        }]}
        ytmusic.get_artist.side_effect = lambda artist_id: {"name": artist_id}

        compare = getattr(playlist_sync, "_compare_playlist_video_ids", None)
        self.assertIsNotNone(compare, "structured playlist comparator is required")
        with patch.object(playlist_sync, "make_ytmusic", return_value=ytmusic):
            result = compare(ytmusic, expected, actual)

        self.assertFalse(result["matches"])
        self.assertEqual((result["expected_count"], result["actual_count"]), (100, 100))
        self.assertEqual(
            [row["position"] for row in result["differences"]],
            [18, 19, 34, 40, 46, 63, 75],
        )
        accepted = {
            row["position"]: row["accepted"] for row in result["differences"]
        }
        self.assertEqual(
            accepted,
            {18: True, 19: False, 34: True, 40: False, 46: True, 63: False, 75: False},
        )
        self.assertEqual(
            next(row for row in result["differences"] if not row["accepted"])[
                "position"
            ],
            19,
        )
        self.assertTrue(all(row["reason"] for row in result["differences"]))
        self.assertEqual(ytmusic.get_song.call_count, 14)

    def test_live_remix_and_language_versions_are_not_interchangeable(self) -> None:
        cases = [
            ("Song (Live)", "Song"),
            ("Song (Remix)", "Song"),
            ("Song (Korean Ver.)", "Song (Japanese Ver.)"),
        ]
        for expected_title, actual_title in cases:
            with self.subTest(expected_title=expected_title, actual_title=actual_title):
                ytmusic = Mock()
                expected = detail(expected_title, "Artist", 200)
                expected["videoDetails"]["videoId"] = "expected"
                actual = detail(actual_title, "Artist", 200)
                actual["videoDetails"]["videoId"] = "actual"
                ytmusic.get_song.side_effect = [expected, actual]
                self.assertFalse(
                    playlist_sync._playlist_video_ids_match(
                        ytmusic, ["expected"], ["actual"]
                    )
                )


class PlaylistPublicationSafetyTests(unittest.TestCase):
    def run_update(self, ytmusic, db_path: Path, video_ids: list[str]) -> None:
        playlist_sync.update_ytmusic_playlist(
            ytmusic,
            "playlist",
            video_ids,
            description="description",
            dry_run=False,
            db_path=db_path,
            service="ytmusic",
            job_name="Weekly-Hot-100",
            playlist_name="Weekly Hot 100",
            playability_verifier=PlaylistVerifier(),
        )

    def audit_patches(self):
        run = {"evidence_version": 1, "recovery_payload": {"snapshot": {}, "events": []}}

        def record(*args, **kwargs):
            run["recovery_payload"]["snapshot"]["existing_items"] = deepcopy(kwargs["existing_items"])
            return "run-1"

        def append(*args, **kwargs):
            events = run["recovery_payload"]["events"]
            event = {**deepcopy(args[2]), "seq": len(events) + 1}
            events.append(event)
            return event

        @contextmanager
        def support():
            with (
                patch("hype_db.append_playlist_update_evidence", side_effect=append, create=True),
                patch("hype_db.get_playlist_update_run", side_effect=lambda *a, **kw: deepcopy(run), create=True),
                patch("ytmusic_playlist_sync.time.sleep"),
            ):
                yield
        return (
            patch("hype_db.get_pending_playlist_recovery", return_value=None, create=True),
            patch("hype_db.record_playlist_update", side_effect=record),
            patch("hype_db.finish_playlist_update", create=True),
            support(),
        )

    def test_audit_failure_blocks_every_playlist_mutation(self) -> None:
        ytmusic = StatefulPlaylist(["old"])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "audit.db"
            with (
                patch(
                    "hype_db.get_pending_playlist_recovery",
                    return_value=None,
                    create=True,
                ),
                patch(
                    "hype_db.record_playlist_update",
                    side_effect=RuntimeError("audit unavailable"),
                ),
                patch("hype_db.finish_playlist_update", create=True),
                patch("ytmusic_playlist_sync.time.sleep"),
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, db_path, ["new"])

        self.assertEqual(ytmusic.video_ids, ["old"])
        self.assertEqual((ytmusic.edit_calls, ytmusic.remove_calls, ytmusic.add_calls), (0, 0, []))

    def test_malformed_initial_playlist_response_blocks_mutation(self) -> None:
        ytmusic = Mock()
        ytmusic.get_playlist.return_value = {}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, self.assertRaises(RuntimeError):
            self.run_update(ytmusic, Path(directory) / "malformed.db", ["new"])
        ytmusic.edit_playlist.assert_not_called()
        ytmusic.remove_playlist_items.assert_not_called()
        ytmusic.add_playlist_items.assert_not_called()

    def test_pending_recovery_blocks_a_fresh_mutation(self) -> None:
        ytmusic = StatefulPlaylist(["old"])
        record = Mock(return_value="run-1")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "pending.db"
            with (
                patch(
                    "hype_db.get_pending_playlist_recovery",
                    return_value={"update_run_id": "pending", "status": "recovery_required"},
                    create=True,
                ),
                patch("hype_db.record_playlist_update", record),
                patch("hype_db.finish_playlist_update", create=True),
                patch("ytmusic_playlist_sync.time.sleep"),
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, db_path, ["new"])

        self.assertEqual(ytmusic.video_ids, ["old"])
        self.assertEqual((ytmusic.edit_calls, ytmusic.remove_calls, ytmusic.add_calls), (0, 0, []))
        record.assert_not_called()

    def test_pre_mutation_reread_failure_finishes_the_audit(self) -> None:
        class SecondReadFails(StatefulPlaylist):
            def __init__(self) -> None:
                super().__init__(["old"])
                self.reads = 0

            def get_playlist(self, playlist_id: str, limit=None) -> dict:
                self.reads += 1
                if self.reads == 2:
                    raise RuntimeError("second read failed")
                return super().get_playlist(playlist_id, limit=limit)

        ytmusic = SecondReadFails()
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, Path(directory) / "reread.db", ["new"])

        self.assertEqual(ytmusic.video_ids, ["old"])
        self.assertEqual((ytmusic.edit_calls, ytmusic.remove_calls, ytmusic.add_calls), (0, 0, []))
        self.assertEqual(finish.call_count, 1)
        self.assertIn(
            finish.call_args.kwargs["status"],
            {"verification_failed", "recovery_required"},
        )

    def test_current_and_published_playlists_finish_with_terminal_status(self) -> None:
        cases = [
            (["new"], "skipped_current", 0),
            (["old"], "published", 1),
        ]
        for initial, status, expected_removals in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                ytmusic = StatefulPlaylist(initial)
                finish = Mock()
                pending, record, _unused_finish, sleep = self.audit_patches()
                with (
                    pending,
                    record,
                    patch("hype_db.finish_playlist_update", finish, create=True),
                    sleep,
                ):
                    self.run_update(ytmusic, Path(directory) / "status.db", ["new"])

                self.assertEqual(ytmusic.video_ids, ["new"])
                self.assertEqual(ytmusic.remove_calls, expected_removals)
                self.assertEqual(finish.call_args.kwargs["status"], status)
                self.assertEqual(finish.call_args.kwargs["actual_video_ids"], ["new"])

    def test_ambiguous_add_failure_never_retries_or_restores(self) -> None:
        ytmusic = StatefulPlaylist(["old"], fail_requested=True)
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, Path(directory) / "restore.db", ["new"])

        self.assertEqual(ytmusic.video_ids, [])
        self.assertEqual(ytmusic.add_calls, [["new"]])
        self.assertEqual(
            [call.kwargs["status"] for call in finish.call_args_list],
            ["recovery_required"],
        )

    def test_verification_failure_restores_the_previous_order(self) -> None:
        ytmusic = StatefulPlaylist(
            ["old"],
            requested_values=["new-a", "new-b"],
            # Unknown song IDs in acknowledged, owned slots can be removed
            # without approving the substituted songs' identity.
            substitute_requested=["different-a", "different-b"],
        )
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(
                    ytmusic, Path(directory) / "verify.db", ["new-a", "new-b"]
                )

        self.assertEqual(ytmusic.video_ids, ["old"])
        self.assertEqual(
            [call.kwargs["status"] for call in finish.call_args_list],
            ["recovery_required"],
        )

    def test_unknown_external_item_is_not_deleted_during_recovery(self) -> None:
        class ExternalInsert(StatefulPlaylist):
            def add_playlist_items(self, *args, **kwargs):
                result = super().add_playlist_items(*args, **kwargs)
                self.video_ids = ["external-item"]  # new, unacknowledged setVideoId
                return result
        ytmusic = ExternalInsert(["old"])
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, Path(directory) / "external.db", ["new"])

        self.assertEqual(ytmusic.video_ids, ["external-item"])
        self.assertEqual(ytmusic.remove_calls, 1)
        self.assertEqual(
            [call.kwargs["status"] for call in finish.call_args_list],
            ["recovery_required"],
        )

    def test_verification_read_errors_never_leave_a_running_audit(self) -> None:
        class ReadFailureAfterMutation(StatefulPlaylist):
            def get_playlist(self, playlist_id: str, limit=None) -> dict:
                if self.add_calls:
                    raise RuntimeError("verification read failed")
                return super().get_playlist(playlist_id, limit=limit)

        ytmusic = ReadFailureAfterMutation(["old"])
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, Path(directory) / "read-error.db", ["new"])

        statuses = [call.kwargs["status"] for call in finish.call_args_list]
        self.assertEqual(statuses, ["recovery_required"])
        self.assertNotIn("running", statuses)

    def test_restore_failure_leaves_recovery_required(self) -> None:
        ytmusic = StatefulPlaylist(
            ["old"], substitute_requested=["unverified-new"], fail_restore=True
        )
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(ytmusic, Path(directory) / "recovery.db", ["new"])

        self.assertEqual(
            [call.kwargs["status"] for call in finish.call_args_list],
            ["recovery_required"],
        )

    def test_restore_failure_audit_rereads_the_latest_playlist_state(self) -> None:
        class PartialRestoreFailure(StatefulPlaylist):
            def add_playlist_items(
                self,
                playlist_id: str,
                video_ids: list[str],
                duplicates: bool = False,
            ) -> dict:
                values = list(video_ids)
                if values == self.original:
                    self.add_calls.append(values)
                    self.video_ids = ["partial-state"]
                    raise RuntimeError("restore stopped after a partial provider update")
                return super().add_playlist_items(
                    playlist_id, video_ids, duplicates=duplicates
                )

        ytmusic = PartialRestoreFailure(["old"], substitute_requested=["unverified-new"])
        finish = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            pending, record, _unused_finish, sleep = self.audit_patches()
            with (
                pending,
                record,
                patch("hype_db.finish_playlist_update", finish, create=True),
                sleep,
                self.assertRaises(RuntimeError),
            ):
                self.run_update(
                    ytmusic, Path(directory) / "partial-restore.db", ["new"]
                )

        self.assertEqual(ytmusic.video_ids, ["partial-state"])
        self.assertEqual(
            [call.kwargs["status"] for call in finish.call_args_list],
            ["recovery_required"],
        )
        self.assertEqual(
            finish.call_args.kwargs["actual_video_ids"], ["partial-state"]
        )


if __name__ == "__main__":
    unittest.main()
