"""Metadata transport failures cannot authorize removal of existing playlist slots."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import hype_db
import ytmusic_playlist_sync as sync
from test_playlist_delta import Playlist


class MetadataOutageTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(self.enterContext(tempfile.TemporaryDirectory())) / "audit.db"
        self.enterContext(patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("psycopg2.connect", side_effect=AssertionError("production DB forbidden")))
        self.enterContext(patch.object(sync.time, "sleep"))
        self.enterContext(sync.bilingual_cache_read_only())

    def publish(self, client, target):
        with patch.object(sync, "make_ytmusic", return_value=client):
            return sync.update_ytmusic_playlist(client, "fixture", target, dry_run=False,
                                               db_path=self.db, service="apple", job_name="Outage-Fixture")

    def test_transient_decode_error_retries_read_and_caches_only_success(self):
        client = Playlist(["song"])
        payload = client.get_song("song")
        client.get_song = Mock(side_effect=[json.JSONDecodeError("empty", "", 0), requests.Timeout(), payload])
        memo = {}
        self.assertFalse(sync._playlist_video_details(client, "song", memo)["error"])
        self.assertFalse(sync._playlist_video_details(client, "song", memo)["error"])
        self.assertEqual(client.get_song.call_count, 3)

    def test_one_failed_initial_substitution_preserves_every_original(self):
        client = Playlist(["keep", "alias", "old"])
        client.canonical["alias"] = "song"
        client.get_song = Mock(side_effect=json.JSONDecodeError("empty", "", 0))
        with self.assertRaisesRegex(RuntimeError, "preserving"):
            self.publish(client, ["keep", "song", "new"])
        self.assertEqual(client.items, client.original)
        self.assertEqual(client.timeline, [])

    def test_failed_alias_lookup_before_add_preserves_original_slots(self):
        client = Playlist(["alias", "keep"])
        client.canonical["alias"] = "song"
        client.get_song = Mock(side_effect=json.JSONDecodeError("empty", "", 0))
        # Known IDs in other positions bypass initial positional metadata reads.
        with self.assertRaisesRegex(RuntimeError, "preserving"):
            self.publish(client, ["keep", "song"])
        self.assertEqual(client.items, client.original)
        self.assertEqual(client.timeline, [])

    def test_failure_after_add_restores_originals_and_removes_only_new_slots(self):
        client = Playlist(["keep", "obsolete"])
        client.aliases["new"] = "alias"
        client.canonical["alias"] = "new"
        original_get = client.get_song
        def get_song(video_id):
            if client.adds and video_id == "alias":
                raise json.JSONDecodeError("empty", "", 0)
            return original_get(video_id)
        client.get_song = get_song
        with self.assertRaisesRegex(RuntimeError, "restored"):
            self.publish(client, ["keep", "new"])
        self.assertEqual(client.items, client.original)
        self.assertEqual(client.adds, [["new"]])
        self.assertTrue(client.removes)
        self.assertTrue(all(item["setVideoId"].startswith("added-") for batch in client.removes for item in batch))
        with hype_db.connect(self.db, read_only=True) as conn:
            row = conn.execute("SELECT update_run_id,status FROM playlist_update_runs").fetchone()
        run = hype_db.get_playlist_update_run(self.db, row["update_run_id"], read_only=True)
        self.assertEqual(run["status"], "restored")
        self.assertEqual(run["publication_mode"], "full")

    def test_final_partial_fallback_cannot_omit_missing_metadata(self):
        client = Playlist(["keep", "alias"])
        evidence = Mock()
        result = sync._publish_without_unverified_substitutions(client, "fixture", ["keep", "song"],
            deepcopy(client.items), {"matches": False, "differences": [{"accepted": False, "reason": "metadata_error"}]},
            evidence=evidence)
        self.assertIsNone(result)
        self.assertEqual(client.timeline, [])
        evidence.assert_not_called()

    def test_response_diagnostic_keeps_status_without_sensitive_content(self):
        response = requests.Response()
        response.status_code = 403
        response.url = "https://music.youtube.com/youtubei/v1/player?key=SECRET"
        response.headers["Content-Type"] = "text/html"
        response.headers["Set-Cookie"] = "SECRET"
        response._content = b"<html>SECRET</html>"
        with self.assertLogs(sync.LOG, level="WARNING") as logs:
            sync._log_ytmusic_response(response)
        output = " ".join(logs.output)
        self.assertIn("http=403", output)
        self.assertIn("json=False", output)
        self.assertNotIn("SECRET", output)

    def test_mutation_rejection_reports_status_and_reason_without_full_response(self):
        result = {"status": "STATUS_FAILED", "private": "SECRET", "actions": [{"addToToastAction": {
            "item": {"notificationActionRenderer": {"responseText": {"runs": [{"text": "Already in playlist"}]}}}}}]}
        with self.assertRaises(RuntimeError) as error:
            sync._require_playlist_mutation_success(result, "addition")
        self.assertIn("STATUS_FAILED", str(error.exception))
        self.assertIn("Already in playlist", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
