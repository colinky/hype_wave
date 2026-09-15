"""Read-only, run-scoped YouTube Music player evidence; never a playback guarantee.

This module has no database, publisher, or credential-file dependencies. A verifier
belongs to one account/client/environment and must not be shared between threads.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import math
import re
import time
from typing import Any

import requests


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
SENSITIVE = re.compile(r"https?://|SAPISID|__Secure-|authorization\s*[:=]|cookie\s*[:=]|Bearer\s", re.I)
CONTENT_UNAVAILABLE = {
    "동영상을 재생할 수 없음", "동영상을 사용할 수 없습니다.",
    "이 동영상을 볼 수 없습니다.", "이 동영상은 해당 국가에서 볼 수 없습니다.",
    "비공개 동영상입니다.", "video unavailable", "this video is unavailable",
    "this video is not available", "video cannot be played",
    "this video is not available in your country",
    "this video is not available in your country or region", "this video is private",
    "this video has been removed",
}
ENVIRONMENT_REASONS = {"authentication_required", "challenge", "http_401", "http_403",
                       "http_429", "network_error", "request_timeout", "server_error"}


def safe_text(value: Any, limit: int = 250) -> str:
    """Metadata is untrusted; credentials and media URLs never belong in reports."""
    if not isinstance(value, str):
        return ""
    if SENSITIVE.search(value):
        return "[redacted]"
    return " ".join(value.split())[:limit]


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _reason_category(reason: Any) -> str:
    text = reason.casefold().strip() if isinstance(reason, str) else ""
    if any(word in text for word in ("not a bot", "captcha", "로봇", "봇이")):
        return "challenge"
    if any(word in text for word in ("premium", "프리미엄", "members-only", "회원만", "confirm your age", "연령")):
        return "entitlement_required"
    if any(word in text for word in ("sign in", "log in", "로그인", "authentication")):
        return "authentication_required"
    if text.rstrip(".") in {item.rstrip(".") for item in CONTENT_UNAVAILABLE}:
        return "content_unavailable"
    return "unrecognized_player_status"


def classify_playability(video_id: str, payload: Any, *, run_health: str = "unknown",
                         availability: bool | None = None,
                         confirmed_unavailable: bool = False) -> dict[str, Any]:
    """Pure classification. Public/Premium and transport failures remain unknown.

    ``run_health='healthy'`` is an explicit caller assertion that authentication
    and control checks passed. Only the live verifier should assert it in use.
    """
    payload = _dict(payload)
    details = _dict(payload.get("videoDetails"))
    playability = _dict(payload.get("playabilityStatus"))
    streams = _dict(payload.get("streamingData"))
    formats = [item for key in ("adaptiveFormats", "formats")
               for item in (streams.get(key) if isinstance(streams.get(key), list) else [])
               if isinstance(item, dict)]
    has_audio = any(str(item.get("mimeType", "")).startswith("audio/")
                    or item.get("audioQuality") in ("AUDIO_QUALITY_LOW", "AUDIO_QUALITY_MEDIUM", "AUDIO_QUALITY_HIGH")
                    or bool(re.fullmatch(r"[1-9][0-9]{0,6}", str(item.get("audioSampleRate", ""))))
                    for item in formats)
    raw_status = playability.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in {"OK", "UNPLAYABLE", "ERROR", "LOGIN_REQUIRED", "LIVE_STREAM_OFFLINE"} else "UNKNOWN"
    exact = details.get("videoId") == video_id
    result = {
        "video_id": video_id if isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id) else "",
        "state": "unknown", "reason_code": "run_not_healthy", "exact_id": exact,
        "player_status": status, "has_audio": has_audio,
        "player_reason_code": "player_ok" if status == "OK" else _reason_category(playability.get("reason")),
        "is_available": availability if isinstance(availability, bool) else None,
        "confirmed_unavailable": False, "title": safe_text(details.get("title")),
        "artist": safe_text(details.get("author")),
        "music_video_type": safe_text(details.get("musicVideoType"), 80),
    }
    if not result["video_id"]:
        result["reason_code"] = "invalid_video_id"
    elif details.get("videoId") and not exact:
        result["reason_code"] = "video_id_mismatch"
    elif run_health != "healthy":
        pass
    elif status == "OK":
        if availability is False:
            result["reason_code"] = "availability_conflict"
        elif not exact or not result["title"] or not result["artist"]:
            result["reason_code"] = "incomplete_metadata"
        elif details.get("isPrivate"):
            result["reason_code"] = "private_resource"
        elif not has_audio:
            result["reason_code"] = "missing_audio_information"
        else:
            result.update(state="playable", reason_code="player_ok")
    else:
        category = "authentication_required" if status == "LOGIN_REQUIRED" else _reason_category(playability.get("reason"))
        result["reason_code"] = category
        if status in {"UNPLAYABLE", "ERROR"} and category == "content_unavailable":
            if availability is True or has_audio:
                result["reason_code"] = "availability_conflict"
            elif confirmed_unavailable:
                result.update(state="unavailable", confirmed_unavailable=True)
            else:
                result["reason_code"] = "unavailable_needs_confirmation"
    return result


def classify_account(payload: Any, expected_account: dict | None) -> dict[str, str]:
    """Compare private expected profile fields without returning their values."""
    if not isinstance(expected_account, dict) or not expected_account:
        return {"auth_state": "unknown", "reason_code": "expected_account_missing"}
    if any(key not in {"channelHandle", "accountName", "channelHandleSha256", "accountNameSha256"} or not isinstance(value, str) or not value.strip()
           for key, value in expected_account.items()):
        return {"auth_state": "unknown", "reason_code": "invalid_expected_account"}
    if not isinstance(payload, dict) or not isinstance(payload.get("accountName"), str) or not payload["accountName"].strip():
        return {"auth_state": "unknown", "reason_code": "account_response_incomplete"}
    for key, expected in expected_account.items():
        actual = payload.get(key.removesuffix("Sha256"))
        if key.endswith("Sha256"):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                return {"auth_state": "unknown", "reason_code": "invalid_expected_account"}
            actual = hashlib.sha256(actual.encode("utf-8")).hexdigest() if isinstance(actual, str) else ""
            expected = expected.lower()
        if not isinstance(actual, str) or not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
            return {"auth_state": "unknown", "reason_code": "account_mismatch"}
    return {"auth_state": "authenticated", "reason_code": "account_matches"}


def evidence_is_fresh(evidence: dict, *, environment: str | None = None,
                      now: datetime | None = None) -> bool:
    try:
        observed = datetime.fromisoformat(evidence["observed_at"])
        expires = datetime.fromisoformat(evidence["expires_at"])
        current = now or datetime.now(timezone.utc)
        return (observed.tzinfo is not None and expires.tzinfo is not None
                and observed <= current < expires
                and (environment is None or evidence.get("environment") == environment))
    except (KeyError, TypeError, ValueError):
        return False


class _BudgetExceeded(Exception):
    pass


class PlayabilityVerifier:
    """One sequential verification run with bounded reads and expiring evidence.

    Call ``check_health()`` before selecting candidates and inspect ``health``
    again before publishing: later errors can invalidate the run. Results are
    observations, never a durable cache of universally playable recordings.
    """

    def __init__(self, client, *, control_video_ids, expected_account=None,
                 environment="local", timeout=20, budget_seconds=600,
                 evidence_ttl_seconds=1800, max_retries=2,
                 clock=time.monotonic, now=None, sleep=time.sleep):
        controls = tuple(dict.fromkeys(control_video_ids))
        if len(controls) != 3 or any(not isinstance(value, str) or not VIDEO_ID.fullmatch(value) for value in controls):
            raise ValueError("Exactly three different valid control IDs are required")
        if (any(not math.isfinite(value) or value <= 0 for value in (timeout, budget_seconds, evidence_ttl_seconds))
                or not isinstance(max_retries, int) or not 0 <= max_retries <= 2):
            raise ValueError("Invalid verification limits")
        self.client, self.controls = client, controls
        self.expected_account = dict(expected_account) if isinstance(expected_account, dict) else expected_account
        self.environment = safe_text(environment, 80)
        if not self.environment or self.environment == "[redacted]":
            raise ValueError("A non-sensitive environment label is required")
        self.timeout, self.ttl, self.max_retries = timeout, evidence_ttl_seconds, max_retries
        self.clock, self.now, self.sleep = clock, now or (lambda: datetime.now(timezone.utc)), sleep
        self.deadline = clock() + budget_seconds
        self.health = {"auth_state": "unknown", "run_health": "unknown", "reason_code": "not_checked", "controls": []}
        self._memo: dict[str, dict] = {}
        self._environment_errors: set[str] = set()
        self._halted = False
        self.request_count = 0

    @contextmanager
    def _bounded_session(self, client=None):
        session = getattr(self.client if client is None else client, "_session", None)
        original = getattr(session, "request", None)
        if not callable(original):
            yield  # Injected offline clients have no HTTP session.
            return

        def request(*args, **kwargs):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise _BudgetExceeded()
            limit = min(self.timeout, remaining)
            requested = kwargs.get("timeout")
            # Nested scopes must preserve the strictest caller timeout. Requests
            # accepts a scalar or separate connect/read values, including None.
            if isinstance(requested, tuple):
                kwargs["timeout"] = tuple(limit if value is None else min(value, limit) for value in requested)
            else:
                kwargs["timeout"] = limit if requested is None else min(requested, limit)
            response = original(*args, **kwargs)
            response.raise_for_status()
            return response

        session.request = request
        try:
            yield
        finally:
            session.request = original

    def _call(self, method: str, *args, **kwargs):
        if method not in {"get_song", "get_account_info", "get_playlist"}:
            raise ValueError("Only read-only probe operations are allowed")
        for attempt in range(self.max_retries + 1):
            if self.clock() >= self.deadline:
                return None, "budget_exhausted"
            try:
                self.request_count += 1
                with self._bounded_session():
                    result = getattr(self.client, method)(*args, **kwargs)
                if self.clock() >= self.deadline:
                    return None, "budget_exhausted"
                return result, ""
            except Exception as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
                if isinstance(exc, _BudgetExceeded):
                    reason = "budget_exhausted"
                elif isinstance(exc, requests.Timeout):
                    reason = "request_timeout"
                elif isinstance(exc, requests.ConnectionError):
                    reason = "network_error"
                elif status in (401, 403, 429):
                    reason = f"http_{status}"
                elif status is not None and 500 <= status <= 599:
                    reason = "server_error"
                elif "provide authentication" in str(exc).lower():
                    reason = "authentication_required"
                else:
                    reason = "response_error"
                retryable = reason in {"request_timeout", "network_error", "http_429", "server_error"}
                if not retryable or attempt == self.max_retries:
                    return None, reason
                delay = 2 ** attempt
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (ValueError, TypeError):
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - self.now()).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if delay >= self.deadline - self.clock():
                    return None, "budget_exhausted"
                if delay > 60:
                    return None, "retry_deferred"
                self.sleep(delay)
        return None, "response_error"

    def _stamp(self, evidence: dict) -> dict:
        now = self.now()
        expires = now + timedelta(seconds=self.ttl)
        if self.health.get("expires_at"):
            expires = min(expires, datetime.fromisoformat(self.health["expires_at"]))
        return {**evidence, "observed_at": now.isoformat(), "expires_at": expires.isoformat(),
                "environment": self.environment, "auth_state": self.health["auth_state"],
                "run_health": self.health["run_health"]}

    def check_health(self, *, force=False) -> dict:
        if self.clock() >= self.deadline:
            self.health.update(run_health="unknown", reason_code="budget_exhausted")
            return {**self.health, "controls": [dict(item) for item in self.health["controls"]]}
        if not force and evidence_is_fresh(self.health, environment=self.environment, now=self.now()):
            return {**self.health, "controls": [dict(item) for item in self.health["controls"]]}
        account, error = self._call("get_account_info")
        auth = classify_account(account, self.expected_account) if not error else {
            "auth_state": "unauthenticated" if error in {"authentication_required", "http_401"} else "unknown",
            "reason_code": error,
        }
        controls, observations = [], {}
        for video_id in self.controls:
            payload, error = self._call("get_song", video_id)
            observation = classify_playability(video_id, payload, run_health="healthy")
            if error:
                observation.update(state="unknown", reason_code=error)
            cached = self._memo.get(video_id)
            if (cached and evidence_is_fresh(cached, environment=self.environment, now=self.now())
                    and cached["is_available"] is False and observation["state"] == "playable"):
                observation.update(state="unknown", reason_code="availability_conflict", is_available=False)
            observations[video_id] = observation
            controls.append({key: observation[key] for key in
                             ("video_id", "exact_id", "player_status", "has_audio", "reason_code")})
        player_ok = sum(item["state"] == "playable" for item in observations.values()) >= 2
        environment_error = any(item["reason_code"] in ENVIRONMENT_REASONS for item in controls)
        healthy = auth["auth_state"] == "authenticated" and player_ok and not environment_error and not self._halted
        now = self.now()
        self.health = {
            **auth, "run_health": "healthy" if healthy else "unknown", "controls": controls,
            "player_path_healthy": player_ok, "environment": self.environment,
            "observed_at": now.isoformat(), "expires_at": (now + timedelta(seconds=self.ttl)).isoformat(),
        }
        if healthy:
            self.health["reason_code"] = "auth_and_controls_ok"
        elif auth["auth_state"] == "unauthenticated":
            self.health["run_health"] = "unhealthy"
        elif auth["auth_state"] == "authenticated":
            self.health["reason_code"] = "controls_not_healthy"
        for video_id, observation in observations.items():
            if not healthy:
                observation.update(state="unknown", reason_code="run_not_healthy", confirmed_unavailable=False)
            stamped = self._stamp(observation)
            previous = self._memo.get(video_id)
            if previous and observation["is_available"] is not None:
                # A fresh player response cannot renew an older playlist observation.
                stamped["expires_at"] = min(stamped["expires_at"], previous["expires_at"])
            self._memo[video_id] = stamped
        return {**self.health, "controls": [dict(item) for item in controls]}

    def verify(self, video_id: str, *, availability: bool | None = None, force=False) -> dict:
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            return self._stamp(classify_playability(video_id, {}))
        self.check_health()
        cached = self._memo.get(video_id)
        if cached and availability is None and evidence_is_fresh(cached, environment=self.environment, now=self.now()):
            availability = cached["is_available"]
        observation = None
        if (not force and cached and cached["run_health"] == self.health["run_health"]
                and cached["auth_state"] == self.health["auth_state"]
                and evidence_is_fresh(cached, environment=self.environment, now=self.now())):
            result = dict(cached)
            if availability is not None:
                result["is_available"] = availability
                if (availability is False and result["state"] == "playable") or (availability is True and result["state"] == "unavailable"):
                    result.update(state="unknown", reason_code="availability_conflict", confirmed_unavailable=False)
            self._memo[video_id] = dict(result)
            if result["reason_code"] != "unavailable_needs_confirmation":
                return result
            observation = result
        payload, error = None, ""
        if observation is None:
            payload, error = (None, "candidate_environment_errors") if self._halted else self._call("get_song", video_id)
            observation = classify_playability(video_id, payload, run_health=self.health["run_health"], availability=availability)
            if error:
                observation.update(state="unknown", reason_code=error)
        if observation["reason_code"] == "unavailable_needs_confirmation":
            # A burst of player requests can return a transient generic failure.
            # Separate the confirmation from that burst and refresh the controls.
            if self.deadline - self.clock() <= 1:
                observation.update(state="unknown", reason_code="budget_exhausted")
                result = self._stamp(observation)
                self._memo[video_id] = dict(result)
                return result
            self.sleep(1)
            self.check_health(force=True)
            repeated, error = self._call("get_song", video_id)
            second = classify_playability(video_id, repeated, run_health=self.health["run_health"],
                                          availability=availability, confirmed_unavailable=True)
            if error:
                observation.update(state="unknown", reason_code=error)
            elif second["state"] == "unavailable":
                observation = second
            elif second["state"] == "playable" and self.deadline - self.clock() > 1:
                # A recovered response needs a second exact-ID audio response;
                # a single positive in a contradictory pair remains inconclusive.
                self.sleep(1)
                recovered, error = self._call("get_song", video_id)
                third = classify_playability(video_id, recovered, run_health=self.health["run_health"],
                                             availability=availability)
                if not error and third["state"] == "playable":
                    observation = third
                else:
                    observation.update(state="unknown", reason_code=error or "inconsistent_player_responses")
            else:
                observation.update(state="unknown", reason_code="inconsistent_player_responses")
        # Count distinct successive failing IDs; a successful observation resets it.
        raw_category = _reason_category(_dict(_dict(payload).get("playabilityStatus")).get("reason"))
        environment_reason = error or ("authentication_required" if observation["player_status"] == "LOGIN_REQUIRED" else raw_category)
        if environment_reason in ENVIRONMENT_REASONS:
            self._environment_errors.add(video_id)
            if len(self._environment_errors) >= 3:
                self._halted = True
                self.check_health(force=True)
                self.health.update(run_health="unknown", reason_code="candidate_environment_errors")
                observation.update(state="unknown", reason_code="candidate_environment_errors", confirmed_unavailable=False)
        else:
            self._environment_errors.clear()
        result = self._stamp(observation)
        self._memo[video_id] = dict(result)
        return result

    def read_playlist(self, playlist_id: str) -> dict:
        """Return only non-sensitive exact-ID availability/metadata observations."""
        if not isinstance(playlist_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist_id):
            raise ValueError("Invalid playlist ID")
        payload, error = self._call("get_playlist", playlist_id, limit=None)
        tracks = _dict(payload).get("tracks")
        if error or not isinstance(tracks, list):
            return {"playlist_id": playlist_id, "reason_code": error or "playlist_response_incomplete", "tracks": []}
        clean = []
        for row in tracks:
            if not isinstance(row, dict) or not isinstance(row.get("videoId"), str) or not VIDEO_ID.fullmatch(row["videoId"]):
                continue
            artists = row.get("artists") if isinstance(row.get("artists"), list) else []
            clean.append({"video_id": row["videoId"], "title": safe_text(row.get("title")),
                          "artist": safe_text(", ".join(str(item.get("name", "")) for item in artists if isinstance(item, dict))),
                          "album": safe_text(_dict(row.get("album")).get("name")),
                          "is_available": row.get("isAvailable") if isinstance(row.get("isAvailable"), bool) else None})
        count = _dict(payload).get("trackCount")
        complete = isinstance(count, int) and count == len(tracks) == len(clean)
        return {"playlist_id": playlist_id, "track_count": len(clean), "complete": complete,
                "reason_code": "playlist_observed" if complete else "playlist_count_unconfirmed", "tracks": clean}
