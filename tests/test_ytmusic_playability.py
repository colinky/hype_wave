"""Offline acceptance of account-scoped player evidence and bounded failures."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

import requests

from ytmusic_playability import (
    PlayabilityVerifier, classify_account, classify_playability, evidence_is_fresh,
)


CONTROLS = ("5sOgJQ3N03Q", "Q4AE3ub4nBM", "z9ifbheDGFM")
TARGET = "cQPXraDIvS4"
EXPECTED = {"channelHandle": "@expected-private-profile"}


def player(video_id, status="OK", reason=None, audio=True):
    return {
        "videoDetails": {"videoId": video_id, "title": "A song", "author": "An artist",
                         "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"},
        "playabilityStatus": {"status": status, "reason": reason},
        "streamingData": {"adaptiveFormats": [{"mimeType": "audio/mp4", "url": "https://private.invalid/stream?token=secret"}] if audio else []},
    }


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def __call__(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds

    def now(self):
        return datetime(2026, 9, 14, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed)


class FakeClient:
    def __init__(self):
        self.calls = Counter()
        self.account = {"accountName": "PRIVATE ACCOUNT NAME", **EXPECTED, "accountPhotoUrl": "https://private.invalid/photo"}
        self.players = {}
        self.playlists = {}

    def get_account_info(self):
        self.calls["account"] += 1
        if isinstance(self.account, Exception):
            raise self.account
        return deepcopy(self.account)

    def get_song(self, video_id):
        self.calls[video_id] += 1
        value = self.players.get(video_id, player(video_id))
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def get_playlist(self, playlist_id, limit=None):
        self.calls[playlist_id] += 1
        return deepcopy(self.playlists[playlist_id])


class ClassificationTests(unittest.TestCase):
    def test_decision_table_including_public_and_ambiguous_responses(self):
        cases = [
            (player(TARGET), {}, "unknown", "run_not_healthy"),
            (player(TARGET), {"run_health": "healthy"}, "playable", "player_ok"),
            (player(TARGET, audio=False), {"run_health": "healthy"}, "unknown", "missing_audio_information"),
            (player(TARGET), {"run_health": "healthy", "availability": False}, "unknown", "availability_conflict"),
            (player(TARGET, "UNPLAYABLE", "Music Premium 회원만 시청할 수 있는 동영상입니다.", False),
             {"run_health": "healthy", "confirmed_unavailable": True}, "unknown", "entitlement_required"),
            (player(TARGET, "LOGIN_REQUIRED", audio=False), {"run_health": "healthy"}, "unknown", "authentication_required"),
            (player(TARGET, "UNPLAYABLE", "Sign in to confirm you're not a bot", False),
             {"run_health": "healthy"}, "unknown", "challenge"),
            (player(TARGET, "UNPLAYABLE", "동영상을 재생할 수 없음", False),
             {"run_health": "healthy"}, "unknown", "unavailable_needs_confirmation"),
            (player(TARGET, "UNPLAYABLE", "동영상을 재생할 수 없음", False),
             {"run_health": "healthy", "confirmed_unavailable": True}, "unavailable", "content_unavailable"),
            (player(TARGET, "UNPLAYABLE", "동영상을 재생할 수 없음", False),
             {"run_health": "healthy", "confirmed_unavailable": True, "availability": True}, "unknown", "availability_conflict"),
            (player(CONTROLS[0]), {"run_health": "healthy"}, "unknown", "video_id_mismatch"),
            ({"playabilityStatus": {"status": "OK"}}, {"run_health": "healthy"}, "unknown", "incomplete_metadata"),
            ({"playabilityStatus": {"status": ["OK"]}, "videoDetails": []}, {"run_health": "healthy"}, "unknown", "unrecognized_player_status"),
        ]
        for payload, options, state, reason in cases:
            with self.subTest(state=state, reason=reason, options=options):
                result = classify_playability(TARGET, payload, **options)
                self.assertEqual((result["state"], result["reason_code"]), (state, reason))
                self.assertNotIn("streamingData", result)
                self.assertNotIn("private.invalid", str(result))

    def test_unrecognized_formats_and_private_metadata_do_not_pass(self):
        payload = player(TARGET)
        payload["streamingData"]["adaptiveFormats"] = [{"url": "https://example.invalid", "audioQuality": "something"}]
        self.assertEqual(classify_playability(TARGET, payload, run_health="healthy")["state"], "unknown")
        payload = player(TARGET)
        payload["videoDetails"]["isPrivate"] = True
        self.assertEqual(classify_playability(TARGET, payload, run_health="healthy")["reason_code"], "private_resource")

    def test_account_response_is_not_an_identity_or_subscription_guess(self):
        account = FakeClient().account
        for expected, value in ((None, account), ({"channelHandle": "@someone-else"}, account),
                                ({"accountPhotoUrl": "private"}, account), (EXPECTED, {})):
            self.assertEqual(classify_account(value, expected)["auth_state"], "unknown")
        self.assertEqual(classify_account(account, EXPECTED)["auth_state"], "authenticated")
        self.assertNotIn("PRIVATE", str(classify_account(account, EXPECTED)))

    def test_expected_profile_digest_matches_without_disclosing_it(self):
        account = FakeClient().account
        expected = {"channelHandleSha256": hashlib.sha256(account["channelHandle"].encode()).hexdigest()}
        result = classify_account(account, expected)
        self.assertEqual(result["auth_state"], "authenticated")
        self.assertNotIn(expected["channelHandleSha256"], str(result))
        self.assertNotIn(account["channelHandle"], str(result))
        for changed in ({"channelHandleSha256": "0" * 64}, {"channelHandleSha256": "invalid"}):
            self.assertEqual(classify_account(account, changed)["auth_state"], "unknown")


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("No network in offline tests")))
        self.client, self.clock = FakeClient(), FakeClock()

    def verifier(self, **kwargs):
        return PlayabilityVerifier(self.client, control_video_ids=CONTROLS,
                                   expected_account=kwargs.pop("expected_account", EXPECTED),
                                   clock=self.clock, now=self.clock.now, sleep=self.clock.sleep, **kwargs)

    def test_auth_and_control_health_are_both_required(self):
        verifier = self.verifier(expected_account=None)
        health = verifier.check_health()
        self.assertTrue(health["player_path_healthy"])
        self.assertEqual(health["run_health"], "unknown")
        self.assertEqual(verifier.verify(TARGET)["state"], "unknown")
        self.assertEqual(verifier.verify(CONTROLS[0])["state"], "unknown")
        self.assertEqual(self.client.calls[CONTROLS[0]], 1)
        self.client.account = ValueError("Please provide authentication before using this function")
        verifier = self.verifier()
        self.assertEqual(verifier.check_health()["auth_state"], "unauthenticated")
        self.assertEqual(verifier.verify(TARGET)["state"], "unknown")

    def test_two_controls_suffice_but_an_auth_error_does_not(self):
        self.client.players[CONTROLS[0]] = player(CONTROLS[0], "UNPLAYABLE", "Video unavailable", False)
        verifier = self.verifier()
        self.assertEqual(verifier.check_health()["run_health"], "healthy")
        self.assertEqual(verifier.verify(CONTROLS[0])["state"], "unavailable")
        self.assertEqual(self.client.calls[CONTROLS[0]], 2)
        self.client.players[CONTROLS[0]] = player(CONTROLS[0], "LOGIN_REQUIRED", audio=False)
        self.assertEqual(self.verifier().check_health()["run_health"], "unknown")

    def test_five_reported_pairs_are_contextual_not_a_permanent_blocklist(self):
        pairs = [(CONTROLS[0], "DBOyG7y_FQg", "OK"), ("QbsbqekMkCU", TARGET, "UNPLAYABLE"),
                 (CONTROLS[1], "EsU7ng0KIy0", "OK"), ("MUny_GDYIDM", "KL7km3PxilQ", "UNPLAYABLE"),
                 (CONTROLS[2], "1PVUTUFxq8g", "UNPLAYABLE")]
        for old, new, status in pairs:
            self.client.players[new] = player(new, status, "동영상을 재생할 수 없음" if status != "OK" else None, status == "OK")
        verifier = self.verifier()
        for old, new, status in pairs:
            self.assertEqual(verifier.verify(old)["state"], "playable")
            result = verifier.verify(new)
            self.assertEqual(result["state"], "playable" if status == "OK" else "unavailable")
            self.assertEqual(self.client.calls[new], 1 if status == "OK" else 2)
        self.client.players[TARGET] = player(TARGET)
        self.assertEqual(self.verifier().verify(TARGET)["state"], "playable")

    def test_confirmation_disagreement_is_unknown(self):
        self.client.players[TARGET] = [player(TARGET, "UNPLAYABLE", "Video unavailable", False), player(TARGET)]
        result = self.verifier().verify(TARGET)
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["confirmed_unavailable"])

    def test_memo_reuses_controls_and_preserves_later_availability_conflict(self):
        verifier = self.verifier()
        first = verifier.verify(CONTROLS[0])
        first["state"] = "corrupted caller copy"
        self.assertEqual(verifier.verify(CONTROLS[0])["state"], "playable")
        self.assertEqual(self.client.calls[CONTROLS[0]], 1)
        self.assertEqual(verifier.verify(CONTROLS[0], availability=False)["state"], "unknown")
        verifier.check_health(force=True)
        self.assertEqual(verifier.verify(CONTROLS[0])["state"], "unknown")

    def test_expiry_requires_new_observation_and_account_check(self):
        verifier = self.verifier(evidence_ttl_seconds=30)
        old = verifier.verify(TARGET)
        self.assertTrue(evidence_is_fresh(old, environment="local", now=self.clock.now()))
        self.clock.sleep(31)
        self.assertFalse(evidence_is_fresh(old, now=self.clock.now()))
        new = verifier.verify(TARGET)
        self.assertNotEqual(old["observed_at"], new["observed_at"])
        self.assertEqual(self.client.calls[TARGET], 2)
        self.assertEqual(self.client.calls["account"], 2)
        self.assertFalse(evidence_is_fresh(new, environment="actions", now=self.clock.now()))

    def test_run_budget_exhaustion_cannot_reuse_a_playable_memo(self):
        verifier = self.verifier(budget_seconds=10)
        self.assertEqual(verifier.verify(TARGET)["state"], "playable")
        self.clock.sleep(11)
        self.assertEqual(verifier.verify(TARGET)["state"], "unknown")
        self.assertEqual(verifier.health["reason_code"], "budget_exhausted")
        self.assertEqual(self.client.calls[TARGET], 1)

    def test_account_refresh_does_not_extend_old_playlist_evidence(self):
        verifier = self.verifier(evidence_ttl_seconds=30)
        evidence = verifier.verify(CONTROLS[0], availability=False)
        self.clock.sleep(10)
        verifier.check_health(force=True)
        repeated = verifier.verify(CONTROLS[0])
        self.assertEqual(repeated["state"], "unknown")
        self.assertEqual(repeated["expires_at"], evidence["expires_at"])

    def test_session_timeout_is_bounded_and_original_request_restored(self):
        from types import SimpleNamespace
        observed_timeouts = []

        def request(*args, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            response = requests.Response()
            response.status_code = 200
            return response

        self.client._session = SimpleNamespace(request=request)
        get_song = self.client.get_song

        def song(video_id):
            self.client._session.request("POST", "https://example.invalid", timeout=999)
            return get_song(video_id)

        self.client.get_song = song
        self.assertEqual(self.verifier(timeout=2).verify(TARGET)["state"], "playable")
        self.assertEqual(observed_timeouts, [2, 2, 2, 2])
        self.assertIs(self.client._session.request, request)

    def test_temporary_failure_retries_are_bounded_and_budgeted(self):
        self.client.players[TARGET] = requests.Timeout("https://secret.invalid/request")
        result = self.verifier().verify(TARGET)
        self.assertEqual((result["state"], result["reason_code"]), ("unknown", "request_timeout"))
        self.assertEqual(self.client.calls[TARGET], 3)
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "1000"
        self.client.players[TARGET] = requests.HTTPError(response=response)
        result = self.verifier(budget_seconds=10).verify(TARGET)
        self.assertEqual(result["reason_code"], "budget_exhausted")

    def test_three_different_environment_failures_halt_remaining_candidates(self):
        verifier = self.verifier(max_retries=0)
        verifier.verify(CONTROLS[0])
        for video_id in (TARGET, "KL7km3PxilQ", "1PVUTUFxq8g"):
            self.client.players[video_id] = requests.Timeout("private error details")
            verifier.verify(video_id)
        self.assertEqual(verifier.health["run_health"], "unknown")
        self.assertEqual(verifier.verify("DBOyG7y_FQg")["state"], "unknown")
        self.assertEqual(self.client.calls["DBOyG7y_FQg"], 0)
        self.assertEqual(verifier.verify(CONTROLS[0])["state"], "unknown")

    def test_read_api_allowlist_rejects_mutations(self):
        with self.assertRaises(ValueError):
            self.verifier()._call("add_playlist_items", "playlist", [TARGET])


if __name__ == "__main__":
    unittest.main()
