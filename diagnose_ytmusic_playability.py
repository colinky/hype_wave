#!/usr/bin/env python3
"""Opt-in read-only player/account/playlist diagnostics; writes one safe artifact.

No database, synchronization, publishing, OAuth refresh, or media-download code is
imported. The output is observation evidence, not a recording-selection command.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time

from ytmusic_playability import PlayabilityVerifier, SENSITIVE, VIDEO_ID, safe_text


def collect_probe(verifier: PlayabilityVerifier, video_ids, playlist_ids=()) -> dict:
    verifier.check_health()
    playlists = [verifier.read_playlist(playlist_id) for playlist_id in dict.fromkeys(playlist_ids)]
    availability: dict[str, set[bool]] = {}
    for playlist in playlists:
        for row in playlist["tracks"]:
            if isinstance(row["is_available"], bool):
                availability.setdefault(row["video_id"], set()).add(row["is_available"])
    videos = []
    for video_id in dict.fromkeys(video_ids):
        flags = availability.get(video_id, set())
        row = verifier.verify(video_id, availability=next(iter(flags)) if len(flags) == 1 else None)
        if len(flags) > 1:
            row.update(state="unknown", reason_code="availability_conflict", confirmed_unavailable=False)
        videos.append(row)
    # A later circuit breaker must invalidate previously collected successes too.
    if verifier.health["run_health"] != "healthy":
        for row in videos:
            if row["state"] != "unknown":
                row["reason_code"] = "run_invalidated"
            row.update(state="unknown", run_health=verifier.health["run_health"],
                       auth_state=verifier.health["auth_state"], confirmed_unavailable=False)
    return {"health": verifier.health, "videos": videos, "playlists": playlists,
            "api_call_count": verifier.request_count}


def assert_safe_artifact(report: dict) -> None:
    """Fail closed before writing if a future change accidentally adds raw data."""
    allowed = {
        "schema_version", "read_only", "captured_at", "environment", "language", "python_version",
        "ytmusicapi_version", "code_revision", "run_id", "observations", "authenticated", "public",
        "health", "videos", "playlists", "api_call_count", "auth_state", "run_health", "reason_code",
        "controls", "player_path_healthy", "observed_at", "expires_at", "video_id", "state", "exact_id",
        "player_status", "has_audio", "player_reason_code", "is_available", "confirmed_unavailable",
        "title", "artist", "album", "music_video_type", "playlist_id", "tracks", "track_count", "complete",
    }

    def inspect(value):
        if isinstance(value, dict):
            if set(value) - allowed:
                raise ValueError("Unsafe artifact fields")
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, str) and SENSITIVE.search(value):
            raise ValueError("Unsafe artifact content")

    inspect(report)


def write_artifact(path: Path, report: dict) -> None:
    assert_safe_artifact(report)
    # Exclusive creation cannot overwrite an auth file, database, or prior evidence.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")


def make_probe_client(auth_file: Path | None, *, language: str, timeout: float, deadline: float):
    import requests
    from ytmusicapi import YTMusic

    class ProbeSession(requests.Session):
        def request(self, method, url, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("Probe time budget exhausted")
            kwargs["timeout"] = min(timeout, remaining)
            return super().request(method, url, **kwargs)

    auth = None
    if auth_file:
        auth = json.loads(auth_file.read_text(encoding="utf-8"))
        # Browser auth supplied as an in-memory dict cannot update a token file.
        if not isinstance(auth, dict) or not any(key.lower() == "cookie" for key in auth):
            raise ValueError("A browser authentication JSON file is required")
        if any(key in auth for key in ("access_token", "refresh_token", "client_secret")):
            raise ValueError("OAuth credentials are not supported by the read-only probe")
    client = YTMusic(auth, language=language, requests_session=ProbeSession())
    client.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en-US,en;q=0.8" if language == "ko" else "en-US,en;q=0.9"})
    return client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, help="Existing browser auth JSON; never copied to output")
    parser.add_argument("--expected-account-file", type=Path, help="Expected profile/digest JSON, or config containing expected_account")
    parser.add_argument("--controls", nargs=3, required=True, help="Three recently verified IDs from different artists")
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--playlist-id", action="append", default=[])
    parser.add_argument("--compare-public", action="store_true")
    parser.add_argument("--environment", default="actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local")
    parser.add_argument("--language", choices=("ko", "en"), default="ko")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--budget-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file; existing paths are never overwritten")
    args = parser.parse_args(argv)
    if any(not VIDEO_ID.fullmatch(video_id) for video_id in args.controls + args.ids):
        parser.error("Every song ID must be exactly eleven YouTube ID characters")
    if len(set(args.controls)) != 3 or args.timeout <= 0 or args.budget_seconds <= 0:
        parser.error("Three distinct controls and positive request/time limits are required")
    if args.output.exists() or args.output.is_symlink():
        parser.error("Output must be a new file")
    expected = None
    deadline = time.monotonic() + args.budget_seconds
    report = {
        "schema_version": 1, "read_only": True, "captured_at": datetime.now(timezone.utc).isoformat(),
        "environment": safe_text(args.environment, 80), "language": args.language,
        "python_version": platform.python_version(), "ytmusicapi_version": importlib.metadata.version("ytmusicapi"),
        "code_revision": safe_text(os.environ.get("GITHUB_SHA", ""), 64),
        "run_id": safe_text(os.environ.get("GITHUB_RUN_ID", ""), 64), "observations": {},
    }
    try:
        if args.expected_account_file:
            expected = json.loads(args.expected_account_file.read_text(encoding="utf-8"))
            if isinstance(expected, dict) and "expected_account" in expected:
                expected = expected["expected_account"]
        modes = [("authenticated" if args.auth else "public", args.auth)]
        if args.compare_public and args.auth:
            modes.append(("public", None))
        for label, auth_file in modes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["observations"][label] = {"reason_code": "budget_exhausted"}
                continue
            client = make_probe_client(auth_file, language=args.language, timeout=args.timeout, deadline=deadline)
            verifier = PlayabilityVerifier(client, control_video_ids=args.controls,
                                          expected_account=expected if auth_file else None,
                                          environment=args.environment, timeout=args.timeout,
                                          budget_seconds=max(0.001, deadline - time.monotonic()))
            # The public comparison is a player/auth negative control. A private
            # or personalized playlist is only observable in its authenticated context.
            playlists = args.playlist_id if auth_file or not args.auth else ()
            report["observations"][label] = collect_probe(verifier, args.ids, playlists)
        write_artifact(args.output, report)
    except Exception:
        # Exceptions can contain request headers, URLs, paths, or profile details.
        print("Read-only probe failed; no credentials or raw responses were recorded.", file=sys.stderr)
        return 2
    primary = report["observations"].get("authenticated", report["observations"].get("public", {}))
    healthy = primary.get("health", {}).get("run_health") == "healthy"
    print(json.dumps({"read_only": True, "run_health": primary.get("health", {}).get("run_health", "unknown"),
                      "counts": {state: sum(row["state"] == state for row in primary.get("videos", []))
                                 for state in ("playable", "unavailable", "unknown")}}))
    complete = ("health" in primary and len(primary.get("videos", [])) == len(set(args.ids))
                and all(row["player_status"] != "UNKNOWN" for row in primary.get("videos", []))
                and all(row.get("complete") for row in primary.get("playlists", [])))
    if args.auth and args.compare_public:
        # Public access is an authentication negative control. Its circuit
        # breaker can legitimately leave player metadata unobservable; that
        # does not invalidate complete authenticated observations.
        public = report["observations"].get("public", {})
        complete = (complete and "health" in public
                    and public["health"].get("auth_state") != "authenticated"
                    and len(public.get("videos", [])) == len(set(args.ids))
                    and all(row["state"] == "unknown" for row in public.get("videos", [])))
    return 0 if healthy and complete and all(row["state"] != "unknown" for row in primary.get("videos", [])) else 2


if __name__ == "__main__":
    raise SystemExit(main())
