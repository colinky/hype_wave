"""Read-only probe acceptance: metadata-only output and no database/publisher path."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import diagnose_ytmusic_playability as probe
from ytmusic_playability import PlayabilityVerifier
from test_ytmusic_playability import CONTROLS, EXPECTED, TARGET, FakeClient, FakeClock


PLAYLIST = "PLtawHGpcUVZWJV93R_C0xb8rnW1wx79Yx"


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Offline test attempted network")))
        self.client, self.clock = FakeClient(), FakeClock()

    def verifier(self):
        return PlayabilityVerifier(self.client, control_video_ids=CONTROLS, expected_account=EXPECTED,
                                   clock=self.clock, now=self.clock.now, sleep=self.clock.sleep)

    def test_only_allowlisted_playlist_and_player_fields_survive(self):
        self.client.playlists[PLAYLIST] = {"trackCount": 1, "tracks": [{
            "videoId": TARGET, "setVideoId": "PRIVATE OWNERSHIP HANDLE", "title": "https://private.invalid?token=secret",
            "artists": [{"name": "An artist", "private": "secret"}], "album": {"name": "An album"},
            "isAvailable": False, "headers": {"Cookie": "cookie-secret"},
        }], "owner": "PRIVATE ACCOUNT NAME"}
        report = probe.collect_probe(self.verifier(), [TARGET, TARGET], [PLAYLIST, PLAYLIST])
        probe.assert_safe_artifact(report)
        encoded = json.dumps(report)
        for private in ("PRIVATE", "cookie-secret", "private.invalid", EXPECTED["channelHandle"], "setVideoId"):
            self.assertNotIn(private, encoded)
        self.assertEqual(len(report["videos"]), 1)
        self.assertEqual(report["videos"][0]["state"], "unknown")
        self.assertEqual(report["videos"][0]["reason_code"], "availability_conflict")
        self.assertEqual(self.client.calls[PLAYLIST], 1)

    def test_conflicting_playlist_observations_remain_unknown(self):
        other = PLAYLIST + "A"
        for playlist, available in ((PLAYLIST, True), (other, False)):
            self.client.playlists[playlist] = {"trackCount": 1, "tracks": [
                {"videoId": TARGET, "title": "Song", "artists": None, "isAvailable": available}]}
        report = probe.collect_probe(self.verifier(), [TARGET], [PLAYLIST, other])
        self.assertEqual(report["videos"][0]["state"], "unknown")

    def test_artifact_refuses_secrets_and_overwrite_and_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            for data in ({"headers": {}}, {"accountName": "private"}, {"url": "anything"},
                         {"title": "Bearer sensitive"}, {"artist": "https://signed.invalid/url"}):
                with self.assertRaises(ValueError):
                    probe.write_artifact(output, data)
                self.assertFalse(output.exists())
            probe.write_artifact(output, {"read_only": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                probe.write_artifact(output, {"read_only": False})
            self.assertEqual(json.loads(output.read_text()), {"read_only": True})

    def test_browser_credentials_are_read_into_memory_and_oauth_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "browser.json"
            auth.write_text(json.dumps({"cookie": "PRIVATE_COOKIE", "origin": "https://music.youtube.com"}))
            before = auth.read_bytes()
            fake = Mock(headers={})
            with patch("ytmusicapi.YTMusic", return_value=fake) as factory:
                probe.make_probe_client(auth, language="ko", timeout=20, deadline=probe.time.monotonic() + 30)
            self.assertIsInstance(factory.call_args.args[0], dict)
            self.assertEqual(auth.read_bytes(), before)
            auth.write_text(json.dumps({"access_token": "private", "refresh_token": "private"}))
            with patch("ytmusicapi.YTMusic") as factory, self.assertRaises(ValueError):
                probe.make_probe_client(auth, language="ko", timeout=20, deadline=probe.time.monotonic() + 30)
            factory.assert_not_called()

    def test_cli_writes_safe_evidence_without_db_or_publishing_imports(self):
        import builtins
        original_import = builtins.__import__

        def read_only_import(name, *args, **kwargs):
            if name.startswith(("hype_db", "ytmusic_playlist_sync", "sync_all", "crawler_common")):
                raise AssertionError("Probe must not import database or publishing code")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "expected.json"
            expected.write_text(json.dumps(EXPECTED))
            output = Path(directory) / "report.json"
            args = ["--auth", str(Path(directory) / "browser.json"), "--expected-account-file", str(expected),
                    "--controls", *CONTROLS, "--ids", TARGET, "--output", str(output)]
            with patch.object(probe, "make_probe_client", return_value=self.client), patch("builtins.__import__", side_effect=read_only_import), redirect_stdout(io.StringIO()) as stdout:
                code = probe.main(args)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text())["observations"]["authenticated"]["videos"][0]["state"], "playable")
            for value in (EXPECTED["channelHandle"], "PRIVATE ACCOUNT NAME", "private.invalid"):
                self.assertNotIn(value, output.read_text() + stdout.getvalue())

    def test_cli_unknown_auth_is_not_success_and_errors_never_print_raw_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["--controls", *CONTROLS, "--ids", TARGET, "--output", str(Path(directory) / "report.json")]
            with patch.object(probe, "make_probe_client", return_value=self.client), redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(args), 2)
            args[-1] = str(Path(directory) / "failed.json")
            with patch.object(probe, "make_probe_client", side_effect=ValueError("Cookie: PRIVATE_CREDENTIAL https://secret.invalid")), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(probe.main(args), 2)
            self.assertNotIn("PRIVATE_CREDENTIAL", stderr.getvalue())
            self.assertFalse(Path(args[-1]).exists())

    def test_public_comparison_does_not_require_authenticated_playlist_access(self):
        public = FakeClient()
        public.get_account_info = Mock(side_effect=RuntimeError("authentication required"))
        self.client.playlists[PLAYLIST] = {"trackCount": 1, "tracks": [
            {"videoId": TARGET, "title": "Song", "artists": [], "isAvailable": True}]}
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "profile.json"
            expected.write_text(json.dumps(EXPECTED))
            output = Path(directory) / "probe.json"
            args = ["--auth", "private-browser.json", "--expected-account-file", str(expected),
                    "--controls", *CONTROLS, "--ids", TARGET, "--playlist-id", PLAYLIST,
                    "--compare-public", "--output", str(output)]
            with patch.object(probe, "make_probe_client", side_effect=[self.client, public]), redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(args), 0)
            self.assertEqual(json.loads(output.read_text())["observations"]["public"]["playlists"], [])
            self.assertEqual(public.calls.get(PLAYLIST, 0), 0)


if __name__ == "__main__":
    unittest.main()
