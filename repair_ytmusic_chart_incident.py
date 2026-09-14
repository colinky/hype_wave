"""Evidence-scoped chart repair. No provider calls; plan/verify are read-only.

plan --spec collects before-images from an existing database. rehearse rolls
back; apply-db/resume commit only the supplied manifest. Publication/history
use the returned repair ID and remain separate, audited phases.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Mapping

from hype_db_common import utc_now_iso
from hype_db_schema import PostgresConnectionWrapper
from hype_db_store import (
    CanonicalDecisionError, _canonical_manual_rows, _canonical_write_scope,
    _invalidate_ytmusic_song_translation,
    _metadata_rows_equivalent, _rebuild_track_metadata_lookup,
    _sync_canonical_video_flags, apply_canonical_decision, ensure_track,
    find_track_by_service_song, validate_canonical_decision,
)

PROTECTED_TABLES = (
    "track_list", "playlist_order", "match_runs", "match_attempts",
    "match_candidates", "manual_overrides", "source_song_relations",
    "chart_source_audit", "album_metadata",
    "playlist_update_runs", "playlist_update_items",
)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return fingerprint({key: value for key, value in manifest.items() if key != "manifest_hash"})


def implementation_fingerprint() -> str:
    """Bind reviewed manifests to the storage/repair code that actually executes."""
    root = Path(__file__).resolve().parent
    return fingerprint({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                        for name in ("hype_db_store.py", "hype_db_common.py", "sync_validation.py",
                                     "matching_alias.json", "heal_split_tracks.py", Path(__file__).name)})


def _scope_uids(case):
    uids = {case["decision"]["expected_track_uid"], case["decision"].get("expected_target_uid")}
    for row in [*case.get("bindings", []), *case.get("aliases", []), *case.get("alias_splits", [])]:
        uids.update((row.get("expected_track_uid"), row.get("target_track_uid")))
    uids.update(row["expected_track_uid"] for row in case.get("metadata_decisions", []))
    return {uid for uid in uids if uid}


def _case_decisions(case):
    return [case["decision"], *case.get("metadata_decisions", []),
            *[item["decision"] for item in case.get("alias_splits", [])]]


def _rows(conn: Any, query: str, params: tuple = ()) -> list[dict]:
    return _plain([dict(row) for row in conn.execute(query, params).fetchall()])


def _has_translations(conn: Any) -> bool:
    if type(conn).__name__ == "PostgresConnectionWrapper":
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=ANY(current_schemas(false)) "
                                 "AND table_name='ytmusic_song_translations'").fetchone())
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ytmusic_song_translations'").fetchone())


def _case_state(conn: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    decision = case["decision"]
    uids = _scope_uids(case)
    if decision.get("expected_target_uid"):
        uids.add(str(decision["expected_target_uid"]))
    bindings = case.get("bindings") or [{"service": case["service"], "song_id": case["song_id"]}]
    for binding in bindings:
        uid = find_track_by_service_song(conn, binding["service"], binding["song_id"])
        if uid:
            uids.add(uid)
        if binding.get("expected_track_uid"):
            uids.add(binding["expected_track_uid"])
    for alias in case.get("aliases", []):
        if alias.get("expected_track_uid"):
            uids.add(alias["expected_track_uid"])
    videos = {video for item in _case_decisions(case)
              for video in (item.get("expected_video_id"), item["selected_video_id"]) if video}
    placeholders = ",".join("?" for _ in uids)
    video_marks = ",".join("?" for _ in videos)
    ids = tuple(sorted(uids))
    state = {
        "tracks": _rows(conn, f"SELECT * FROM tracks WHERE track_uid IN ({placeholders}) "
                        f"OR canonical_yt_video_id IN ({video_marks}) ORDER BY track_uid", (*ids, *sorted(videos))),
        "platform_song_ids": _rows(conn, f"SELECT * FROM platform_song_ids WHERE track_uid IN ({placeholders}) "
                                   "ORDER BY service,song_id", ids),
        "yt_video_ids": _rows(conn, f"SELECT * FROM yt_video_ids WHERE track_uid IN ({placeholders}) "
                              f"OR video_id IN ({video_marks}) ORDER BY video_id", (*ids, *sorted(videos))),
        "metadata_lookup_index": _rows(conn, f"SELECT * FROM metadata_lookup_index WHERE track_uid IN ({placeholders}) "
                                       "ORDER BY lookup_key", ids),
    }
    policies = {}
    for uid in ids:
        for video in videos:
            for policy in _canonical_manual_rows(conn, uid, video):
                policies[(policy["service"], policy["song_id"])] = policy
    state["manual_overrides"] = _plain([policies[key] for key in sorted(policies)])
    source_rows = []
    for binding in state["platform_song_ids"]:
        source_rows.extend(_rows(conn, "SELECT * FROM track_list WHERE service=? AND song_id=?",
                                 (binding["service"], binding["song_id"])))
    state["track_list"] = source_rows
    for repair in case.get("source_metadata_repairs", []):
        for row in _rows(conn, "SELECT * FROM track_list WHERE service=? AND song_id=?", (repair["service"], repair["song_id"])):
            if row not in state["track_list"]:
                state["track_list"].append(row)
    state["track_list"].sort(key=lambda row: (row["service"], row["song_id"]))
    state["ytmusic_song_translations"] = []
    if _has_translations(conn):
        for repair in case.get("invalidate_translations", []):
            state["ytmusic_song_translations"].extend(_rows(
                conn, "SELECT * FROM ytmusic_song_translations WHERE video_id=?", (repair["video_id"],),
            ))
    return state


def _protected(conn: Any, cases: list[dict]) -> dict[str, str]:
    corrected_sources = {(item["service"], item["song_id"])
                         for case in cases for item in case.get("source_metadata_repairs", [])}
    return {
        table: fingerprint(sorted(
            (row for row in _rows(conn, f"SELECT * FROM {table}")
             if table != "track_list" or (row["service"], row["song_id"]) not in corrected_sources),
            key=lambda row: json.dumps(row, sort_keys=True),
        ))
        for table in PROTECTED_TABLES
    }


def _outside_scope(conn: Any, cases: list[dict]) -> dict[str, str]:
    uids = set()
    for case in cases:
        uids.update(_scope_uids(case))
    result = {table: fingerprint(sorted(
        (row for row in _rows(conn, f"SELECT * FROM {table}") if row["track_uid"] not in uids),
        key=lambda row: json.dumps(row, sort_keys=True),
    )) for table in ("tracks", "platform_song_ids", "yt_video_ids", "metadata_lookup_index")}
    if _has_translations(conn):
        videos = {item["video_id"] for case in cases for item in case.get("invalidate_translations", [])}
        result["ytmusic_song_translations"] = fingerprint(sorted(
            (row for row in _rows(conn, "SELECT * FROM ytmusic_song_translations") if row["video_id"] not in videos),
            key=lambda row: json.dumps(row, sort_keys=True),
        ))
    return result


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("version") != 1 or manifest.get("manifest_hash") != manifest_hash(manifest):
        raise ValueError("Repair manifest hash or version is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(manifest.get("repair_id") or "")):
        raise ValueError("Repair ID must be a stable, non-empty identifier")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Repair needs explicit cases")
    seen, targets = set(), set()
    for case in cases:
        if not case.get("case_id") or case["case_id"] in seen:
            raise ValueError("Repair case IDs must be unique")
        seen.add(case["case_id"])
        decision = case["decision"]
        uid = decision["expected_track_uid"]
        if uid in targets:
            raise ValueError("Overlapping repair tracks must be grouped into one case")
        targets.add(uid)
        if decision.get("reason") == "restore_unintended_chart_switch":
            incident = decision.get("incident") or {}
            if incident.get("repair_id") != manifest["repair_id"] or incident.get("case_id") != case["case_id"]:
                raise ValueError("Incident decision belongs to another repair or case")
        if case.get("action", "restore_canonical") not in {"restore_canonical", "split_binding", "repair_recording_identity"}:
            raise ValueError("Unsupported repair action")
        if any(item.get("metadata", {}).get("identity_basis") == "exact_native_uploader" for item in _case_decisions(case)) and case.get("action") != "split_binding":
            raise ValueError("Uploader evidence is only valid for a native source split")


def plan_repair(conn: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build a manifest from caller-provided identity/player evidence, without writes."""
    manifest = {"version": 1, "repair_id": spec["repair_id"],
                "implementation_revision": spec["implementation_revision"],
                "implementation_fingerprint": implementation_fingerprint(),
                "created_at": utc_now_iso(), "outputs": _plain(spec.get("outputs") or {}), "cases": []}
    if not manifest["implementation_revision"]:
        raise ValueError("Repair implementation revision must be explicit")
    for supplied in spec["cases"]:
        case = _plain(supplied)
        decision = case["decision"]
        current = conn.execute("SELECT * FROM tracks WHERE track_uid=?", (decision["expected_track_uid"],)).fetchone()
        current = dict(current) if current else {"track_uid": decision["expected_track_uid"]}
        validate_canonical_decision(current, decision)
        case["before"] = _case_state(conn, case)
        case["before_hash"] = fingerprint(case["before"])
        manifest["cases"].append(case)
    manifest["protected_before"] = _protected(conn, manifest["cases"])
    manifest["outside_scope_before"] = _outside_scope(conn, manifest["cases"])
    manifest["review_conflicts_before"] = _rows(conn, "SELECT * FROM review_conflicts ORDER BY conflict_id")
    manifest["manifest_hash"] = manifest_hash(manifest)
    _validate_manifest(manifest)
    return manifest


def _apply_source_metadata_repairs(conn: Any, case: Mapping[str, Any]) -> None:
    """Correct only source fields with separate, explicit upstream evidence."""
    from sync_validation import source_recording_matches
    allowed = {"album_id", "title_ko", "artist_ko", "album_ko", "title_en", "artist_en", "album_en", "artwork_url"}
    uids = {case["decision"]["expected_track_uid"], case["decision"].get("expected_target_uid")}
    uids.update(item.get("expected_track_uid") for item in case.get("bindings", []))
    for repair in case.get("source_metadata_repairs", []):
        values = repair.get("values") or {}
        service, song_id = repair["service"], repair["song_id"]
        row = conn.execute("SELECT * FROM track_list WHERE service=? AND song_id=?", (service, song_id)).fetchone()
        if (not repair.get("evidence_ref") or repair.get("source_metadata_verified") is not True
                or not values or set(values) - allowed or not row
                or find_track_by_service_song(conn, service, song_id) not in uids):
            raise CanonicalDecisionError("Source metadata correction lacks exact upstream proof")
        corrected = {**dict(row), **values}
        metadata = case["decision"]["metadata"]
        native_source_correction = (
            case.get("action") == "split_binding" and service == case.get("service") == "ytmusic"
            and song_id == case.get("song_id") == case["decision"]["selected_video_id"]
        )
        if native_source_correction:
            if not set(values) <= {"album_ko", "album_en"} or any(value != metadata.get(key) for key, value in values.items()):
                raise CanonicalDecisionError("Native source correction must preserve its caption and use exact observed albums")
        elif not source_recording_matches(corrected, metadata, service=service):
            raise CanonicalDecisionError("Corrected source metadata describes another recording")
        columns = sorted(values)
        conn.execute(f"UPDATE track_list SET {','.join(key + '=?' for key in columns)} WHERE service=? AND song_id=?",
                     (*[values[key] for key in columns], service, song_id))
    videos = {case["decision"].get("expected_video_id"), case["decision"]["selected_video_id"]}
    videos.update(item["video_id"] for item in case.get("aliases", []))
    if case.get("service") == "ytmusic":
        videos.add(case["song_id"])
    for repair in case.get("invalidate_translations", []):
        if not repair.get("evidence_ref") or repair["video_id"] not in videos:
            raise CanonicalDecisionError("Translation invalidation is outside the reviewed recording scope")
        _invalidate_ytmusic_song_translation(conn, repair["video_id"])


def _native_uploader_metadata_matches(case: Mapping[str, Any]) -> bool:
    """A native UGC creator channel proves the exact entity, not a musical artist."""
    try:
        decision, proof = case["decision"], case["native_watch_evidence"]
        metadata, player = decision["metadata"], proof["player_details"]
        selected, channel = decision["selected_video_id"], player["channelId"]
        observed = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
        length = int(player["lengthSeconds"])
        if (not proof["evidence_ref"] or not observed.tzinfo
                or not isinstance(channel, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel) or length <= 0
                or not timedelta(0) <= datetime.now(timezone.utc) - observed <= timedelta(minutes=30)
                or player["videoId"] != selected or player["musicVideoType"] != "MUSIC_VIDEO_TYPE_UGC"
                or decision["candidate_evidence"]["music_video_type"] != "MUSIC_VIDEO_TYPE_UGC"
                or metadata["music_video_type"] != "MUSIC_VIDEO_TYPE_UGC"
                or metadata["artist_identity_complete"] is not False
                or metadata["artist_ids"] != [] or metadata["artist_names_by_id"] != {}
                or metadata["creator_channel_id"] != channel or metadata["length_seconds"] != length
                or metadata["album"] != ""):
            return False
        for field, player_field in (("title", "title"), ("artist", "author")):
            value = player[player_field]
            if not isinstance(value, str) or not value.strip() or decision["candidate_evidence"][field] != value or metadata[field] != value:
                return False
            if any(metadata[field + "_" + locale] != value for locale in ("ko", "en")):
                return False
        for locale in ("ko", "en"):
            rows = proof["watch"][locale]
            # The watch parser places numeric view/like/year labels in artists.
            # Match the strict metadata reader's display-only filter; keep every creator.
            artists = [artist for artist in rows[0]["artists"] if not (
                artist.get("id") is None and isinstance(artist.get("name"), str)
                and re.fullmatch(r"(?:(?:조회수|좋아요)\s*\d[\d.,]*\s*[천만억]?\s*[회개]"
                                 r"|(?:19|20)\d{2}년?|\d[\d.,]*\s*[KMB]?\s*(?:views?|likes?))",
                                 artist["name"].strip(), re.IGNORECASE))] if len(rows) == 1 else []
            if (len(rows) != 1 or rows[0]["videoId"] != selected or rows[0]["title"] != player["title"]
                    or artists != [{"name": player["author"], "id": channel}]
                    or rows[0].get("isAvailable") is False or rows[0].get("album") is not None
                    or rows[0]["videoType"] != "MUSIC_VIDEO_TYPE_UGC" or metadata["album_" + locale] != ""):
                return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def _apply_split(conn: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    """Separate only explicitly evidenced source bindings/aliases; never infer an inverse merge."""
    from sync_validation import recording_identity_matches
    from ytmusic_playlist_sync import normalize_text, split_artist_names

    decision, bindings, aliases = case["decision"], case.get("bindings"), case.get("aliases")
    destination = decision["expected_track_uid"]
    if (not case.get("evidence_ref") or not bindings or not aliases
            or conn.execute("SELECT 1 FROM tracks WHERE track_uid=?", (destination,)).fetchone()):
        raise CanonicalDecisionError("Split needs explicit evidence, bindings, aliases, and a new destination UID")
    if decision.get("expected_video_id"):
        raise CanonicalDecisionError("A new split destination cannot already have a canonical")
    metadata = decision["metadata"]
    selected = decision["selected_video_id"]
    validate_canonical_decision({"track_uid": destination}, decision)
    native = case.get("service") == "ytmusic" and case.get("song_id") == selected
    uploader = metadata.get("identity_basis") == "exact_native_uploader"
    if uploader and (not native or not _native_uploader_metadata_matches(case)):
        raise CanonicalDecisionError("Native uploader identity needs fresh exact watch and player evidence")
    if native:
        names_by_id = metadata.get("artist_names_by_id") or {}
        artist_ids = metadata.get("artist_ids")
        if not uploader and (not isinstance(artist_ids, list) or not artist_ids or not isinstance(names_by_id, dict)
                or not all(isinstance(artist, str) and artist.strip()
                           and isinstance(names_by_id.get(artist), list) and names_by_id[artist]
                           and all(isinstance(name, str) and name.strip() for name in names_by_id[artist])
                           for artist in artist_ids)):
            raise CanonicalDecisionError("Native source split needs complete exact artist identities")
        known_names = {normalize_text(name).replace(" ", "")
                       for artist in artist_ids for name in names_by_id[artist]}
        if (len(bindings) != 1 or len(aliases) != 1
                or (not uploader and metadata.get("artist_identity_complete") is not True)
                or any(metadata.get(field) not in (metadata.get(field + "_ko"), metadata.get(field + "_en"))
                       for field in ("title", "artist", "album"))
                or not all(metadata.get(field + suffix) for field in ("title", "artist") for suffix in ("_ko", "_en"))
                or (not uploader and not all(normalize_text(name).replace(" ", "") in known_names
                                             for suffix in ("_ko", "_en") for name in split_artist_names(metadata["artist" + suffix])))
                or type(metadata.get("length_seconds")) not in (int, float)
                or not math.isfinite(metadata["length_seconds"]) or metadata["length_seconds"] <= 0
                or not recording_identity_matches(metadata, decision["candidate_evidence"], player=True)):
            raise CanonicalDecisionError("Native source split needs strict exact recording metadata and playback")
    if selected not in {item["video_id"] for item in aliases}:
        raise CanonicalDecisionError("Split aliases must include the selected video")
    old_uids = set()
    for binding in bindings:
        old_uid = binding["expected_track_uid"]
        if find_track_by_service_song(conn, binding["service"], binding["song_id"]) != old_uid:
            raise CanonicalDecisionError("Split source ownership changed")
        raw = conn.execute("SELECT * FROM track_list WHERE service=? AND song_id=?",
                           (binding["service"], binding["song_id"])).fetchone()
        if (not raw or _canonical_manual_rows(conn, old_uid, selected)
                or (native and (binding["service"] != "ytmusic" or binding["song_id"] != selected
                                or binding.get("source_metadata_verified") is not True or not binding.get("evidence_ref")))
                or (not native and not _metadata_rows_equivalent(dict(raw), metadata))):
            raise CanonicalDecisionError("Split source identity or manual policy requires review")
        old_uids.add(old_uid)
    for alias in aliases:
        owner = conn.execute("SELECT track_uid,is_canonical FROM yt_video_ids WHERE video_id=?", (alias["video_id"],)).fetchone()
        if conn.execute("SELECT 1 FROM tracks WHERE canonical_yt_video_id=?", (alias["video_id"],)).fetchone():
            raise CanonicalDecisionError("Moving another track's canonical needs a separate reviewed repair")
        if alias.get("expected_absent") is True:
            if (owner or "expected_track_uid" not in alias or alias["expected_track_uid"] is not None
                    or len(aliases) != 1 or not native
                    or alias["video_id"] != selected or not alias.get("evidence_ref")):
                raise CanonicalDecisionError("An absent alias needs exact native source and strict recording proof")
        elif not owner or owner[0] != alias["expected_track_uid"] or owner[0] not in old_uids or (native and owner[1]):
            raise CanonicalDecisionError("Split alias ownership is not proved")
    ensure_track(conn, track_uid=destination)
    for alias in aliases:
        if alias.get("expected_absent") is True:
            conn.execute("INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES (?,?,0)",
                         (alias["video_id"], destination))
        else:
            conn.execute("UPDATE yt_video_ids SET track_uid=?,is_canonical=0 WHERE video_id=? AND track_uid=?",
                         (destination, alias["video_id"], alias["expected_track_uid"]))
    for binding in bindings:
        conn.execute("UPDATE platform_song_ids SET track_uid=? WHERE service=? AND song_id=? AND track_uid=?",
                     (destination, binding["service"], binding["song_id"], binding["expected_track_uid"]))
    for uid in old_uids:
        _rebuild_track_metadata_lookup(conn, winner_uid=uid)
        _sync_canonical_video_flags(conn, [uid])
    return apply_canonical_decision(conn, track_uid=destination, decision=decision,
                                    source_row=None if native else case.get("source_row"))


def _apply_identity_repair(conn: Any, case: Mapping[str, Any]) -> dict[str, Any]:
    """Repair evidenced source/alias ownership while preserving existing canonicals.

    Alias-only splits need no invented platform source. Each split carries its
    own fresh exact metadata/player evidence; a different recording is never
    represented as an unavailable or interchangeable canonical.
    """
    from sync_validation import source_recording_matches
    decision = case["decision"]
    destination = decision["expected_track_uid"]
    if not case.get("evidence_ref") or decision.get("expected_video_id") != decision["selected_video_id"]:
        raise CanonicalDecisionError("Identity cleanup must preserve the reviewed canonical")
    current = conn.execute("SELECT * FROM tracks WHERE track_uid=?", (destination,)).fetchone()
    validate_canonical_decision(dict(current) if current else {}, decision)
    metadata = decision["metadata"]
    touched = {destination}
    for binding in case.get("bindings", []):
        old_uid, target = binding["expected_track_uid"], binding.get("target_track_uid")
        raw = conn.execute("SELECT * FROM track_list WHERE service=? AND song_id=?",
                           (binding["service"], binding["song_id"])).fetchone()
        if (target != destination or old_uid == target or not binding.get("evidence_ref")
                or binding.get("source_metadata_verified") is not True
                or find_track_by_service_song(conn, binding["service"], binding["song_id"]) != old_uid
                or not raw or not source_recording_matches(dict(raw), metadata, service=binding["service"])
                or _canonical_manual_rows(conn, old_uid, decision["selected_video_id"])):
            raise CanonicalDecisionError("Source rebinding lacks exact identity/ownership proof")
        conn.execute("UPDATE platform_song_ids SET track_uid=? WHERE service=? AND song_id=? AND track_uid=?",
                     (target, binding["service"], binding["song_id"], old_uid))
        touched.add(old_uid)
    for split in case.get("alias_splits", []):
        video, old_uid, target = split["video_id"], split["expected_track_uid"], split["target_track_uid"]
        selected = split["decision"]
        owner = conn.execute("SELECT track_uid,is_canonical FROM yt_video_ids WHERE video_id=?", (video,)).fetchone()
        if (old_uid != destination or not split.get("evidence_ref") or not owner or owner[0] != old_uid
                or owner[1] or selected.get("expected_track_uid") != target or selected.get("expected_video_id")
                or selected.get("selected_video_id") != video
                or conn.execute("SELECT 1 FROM tracks WHERE track_uid=? OR canonical_yt_video_id=?", (target, video)).fetchone()
                or _canonical_manual_rows(conn, old_uid, video)
                or _metadata_rows_equivalent(metadata, selected.get("metadata") or {})):
            raise CanonicalDecisionError("Alias separation needs a proved distinct recording and an unused destination")
        validate_canonical_decision({"track_uid": target}, selected)
        ensure_track(conn, track_uid=target)
        conn.execute("UPDATE yt_video_ids SET track_uid=?,is_canonical=0 WHERE video_id=? AND track_uid=?",
                     (target, video, old_uid))
        apply_canonical_decision(conn, track_uid=target, decision=selected)
        touched.add(target)
    for correction in case.get("metadata_decisions", []):
        if (correction.get("expected_track_uid") not in touched
                or correction.get("expected_video_id") != correction.get("selected_video_id")):
            raise CanonicalDecisionError("Related metadata correction may not change a canonical")
        apply_canonical_decision(conn, track_uid=correction["expected_track_uid"], decision=correction)
    result = apply_canonical_decision(conn, track_uid=destination, decision=decision, source_row=case.get("source_row"))
    for uid in touched:
        _rebuild_track_metadata_lookup(conn, winner_uid=uid)
    _sync_canonical_video_flags(conn, touched)
    return result


def _receipt(conn: Any, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload_json FROM migration_reports WHERE report_id=?",
                       ("chart-repair:" + manifest["repair_id"],)).fetchone()
    return json.loads(row[0]) if row else None


def verify_repair(conn: Any, manifest: Mapping[str, Any]) -> dict[str, Any]:
    _validate_manifest(manifest)
    receipt = _receipt(conn, manifest)
    if not receipt or receipt.get("manifest_hash") != manifest["manifest_hash"]:
        raise ValueError("No matching committed DB repair receipt")
    for case in manifest["cases"]:
        current = _case_state(conn, case)
        if fingerprint(current) != receipt["after"][case["case_id"]]["fingerprint"]:
            raise CanonicalDecisionError("Database changed after repair; resume requires review")
        for decision in _case_decisions(case):
            uid, selected = decision["expected_track_uid"], decision["selected_video_id"]
            track = next((row for row in current["tracks"] if row["track_uid"] == uid), None)
            owned = [row for row in current["yt_video_ids"] if row["track_uid"] == uid]
            metadata = decision["metadata"]
            expected_metadata = [metadata.get(field) or metadata.get("yt_" + field)
                                 or metadata.get(field + "_en") or metadata.get(field + "_ko") or ""
                                 for field in ("title", "artist", "album")]
            if (not track or track["canonical_yt_video_id"] != selected
                    or [row["video_id"] for row in owned if row["is_canonical"]] != [selected]
                    or [str(track.get("yt_" + field) or "") for field in ("title", "artist", "album")] != expected_metadata):
                raise CanonicalDecisionError("Repair canonical/alias/metadata invariant failed")
        for binding in case.get("bindings", []):
            target = binding.get("target_track_uid") or case["decision"]["expected_track_uid"]
            if find_track_by_service_song(conn, binding["service"], binding["song_id"]) != target:
                raise CanonicalDecisionError("Repaired source did not reach its reviewed recording")
        for correction in case.get("source_metadata_repairs", []):
            source = conn.execute("SELECT * FROM track_list WHERE service=? AND song_id=?",
                                  (correction["service"], correction["song_id"])).fetchone()
            if not source or any(source[key] != value for key, value in correction["values"].items()):
                raise CanonicalDecisionError("Source metadata correction was not stored exactly")
        for invalidation in case.get("invalidate_translations", []):
            if _has_translations(conn) and conn.execute("SELECT 1 FROM ytmusic_song_translations WHERE video_id=?",
                                                       (invalidation["video_id"],)).fetchone():
                raise CanonicalDecisionError("Invalidated translation was retained")
    stages = receipt.get("stages") or {}
    return {"repair_id": manifest["repair_id"], "manifest_hash": manifest["manifest_hash"],
            "status": "db_verified", "cases": len(manifest["cases"]),
            "publication_status": stages.get("publication", "not_verified"),
            "history_status": stages.get("history", "not_verified"),
            "repair_status": receipt.get("status"), "followups": receipt.get("followups") or {}}


def _validate_stage_readback(observation: Mapping[str, Any], applied_at: str) -> None:
    try:
        observed = datetime.fromisoformat(str(observation["observed_at"]).replace("Z", "+00:00"))
        applied = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        valid = observed.tzinfo and applied.tzinfo and applied <= observed <= now and now - observed <= timedelta(minutes=30)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid or observation.get("readback_verified") is not True:
        raise CanonicalDecisionError("Stage needs a fresh verified read after DB application")


def mark_repair_stage(conn: Any, manifest: Mapping[str, Any], stage: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Persist verified external readbacks atomically; perform no external writes."""
    _validate_manifest(manifest)
    if stage not in {"publication", "history"} or not evidence.get("evidence_ref"):
        raise ValueError("Unknown repair stage or missing durable evidence reference")
    with _canonical_write_scope(conn):
        if type(conn).__name__ == "PostgresConnectionWrapper":
            conn.execute("SELECT report_id FROM migration_reports WHERE report_id=? FOR UPDATE",
                         ("chart-repair:" + manifest["repair_id"],)).fetchone()
        verify_repair(conn, manifest)
        receipt = _receipt(conn, manifest)
        if not receipt or receipt.get("stages", {}).get("db") != "applied":
            raise CanonicalDecisionError("External repair stages require an applied DB receipt")
        outputs = manifest.get("outputs") or {}
        if stage == "publication":
            expected, observed = outputs.get("playlists"), evidence.get("playlists")
            if not isinstance(expected, list) or not isinstance(observed, list):
                raise CanonicalDecisionError("Publication scope and exact playlist readbacks must be explicit")
            expected_by_id = {row["playlist_id"]: row for row in expected}
            observed_by_id = {row["playlist_id"]: row for row in observed}
            if (len(expected_by_id) != len(expected) or len(observed_by_id) != len(observed)
                    or expected_by_id.keys() != observed_by_id.keys()):
                raise CanonicalDecisionError("Publication readback does not cover the manifest playlists")
            for playlist_id, target in expected_by_id.items():
                readback = observed_by_id[playlist_id]
                _validate_stage_readback(readback, receipt["created_at"])
                items = readback.get("items")
                if not isinstance(items, list):
                    raise CanonicalDecisionError("Playlist readback needs full item ownership")
                ids = [item.get("video_id") for item in items]
                tokens = [item.get("set_video_id") for item in items]
                if (readback.get("video_ids") != target["video_ids"] or ids != target["video_ids"]
                        or any(not isinstance(token, str) or not token for token in tokens)
                        or len(set(tokens)) != len(tokens)):
                    raise CanonicalDecisionError("Published playlist differs from the exact ordered manifest")
                actual_slots = set(zip(ids, tokens))
                if any((item["video_id"], item["set_video_id"]) not in actual_slots
                       for item in target.get("preserved_items", [])):
                    raise CanonicalDecisionError("A preserved playlist item was deleted and re-added")
        else:
            expected, observed = outputs.get("history"), evidence.get("files")
            if not isinstance(expected, list) or not isinstance(observed, list):
                raise CanonicalDecisionError("History scope and public file readbacks must be explicit")
            expected_by_path = {row["path"]: row for row in expected}
            observed_by_path = {row["path"]: row for row in observed}
            if (len(expected_by_path) != len(expected) or len(observed_by_path) != len(observed)
                    or expected_by_path.keys() != observed_by_path.keys()):
                raise CanonicalDecisionError("History readback does not cover the manifest files")
            for path, target in expected_by_path.items():
                readback = observed_by_path[path]
                _validate_stage_readback(readback, receipt["created_at"])
                if (not re.fullmatch(r"[0-9a-f]{64}", str(target.get("sha256") or ""))
                        or not str(target.get("public_url") or "").startswith("https://")
                        or readback.get("sha256") != target["sha256"]
                        or readback.get("public_sha256") != target["sha256"]
                        or readback.get("public_url") != target["public_url"]):
                    raise CanonicalDecisionError("Local and public history hashes must match the manifest")
        if receipt["stages"].get(stage) == "verified":
            return {"repair_id": manifest["repair_id"], "stage": stage, "status": "already_verified",
                    "repair_status": receipt["status"], "followups": receipt.get("followups") or {}}
        receipt["stages"][stage] = "verified"
        receipt.setdefault("stage_evidence", {})[stage] = _plain(evidence)
        receipt.setdefault("followups", {"daily": "pending", "monday": "pending", "device": "pending"})
        if all(receipt["stages"].get(key) == "verified" for key in ("publication", "history")):
            receipt["status"] = "surfaces_verified"
            receipt["stages"]["verification"] = "surfaces_verified"
        conn.execute("UPDATE migration_reports SET payload_json=? WHERE report_id=?",
                     (json.dumps(receipt, ensure_ascii=False), "chart-repair:" + manifest["repair_id"]))
        return {"repair_id": manifest["repair_id"], "stage": stage, "status": "verified",
                "repair_status": receipt["status"], "followups": receipt["followups"]}


def apply_repair(conn: Any, manifest: Mapping[str, Any], *, evidence_refresh: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Atomic caller-owned DB apply; existing receipt makes retries verification-only."""
    _validate_manifest(manifest)
    if manifest.get("implementation_fingerprint") != implementation_fingerprint():
        raise CanonicalDecisionError("Repair implementation changed since planning")
    with _canonical_write_scope(conn):
        if type(conn).__name__ == "PostgresConnectionWrapper":
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", ("chart-incident-repair",))
        existing = _receipt(conn, manifest)
        if existing:
            result = verify_repair(conn, manifest)
            result["status"] = "already_applied"
            return result
        protected = _protected(conn, manifest["cases"])
        if protected != manifest["protected_before"]:
            raise CanonicalDecisionError("Protected source/audit state changed since planning")
        outside_scope = _outside_scope(conn, manifest["cases"])
        if outside_scope != manifest["outside_scope_before"]:
            raise CanonicalDecisionError("Unrelated storage state changed since planning")
        for case in manifest["cases"]:
            if fingerprint(_case_state(conn, case)) != case["before_hash"]:
                raise CanonicalDecisionError("Repair before-image is stale")
        after = {}
        for original_case in manifest["cases"]:
            case = _plain(original_case)
            if evidence_refresh:
                if evidence_refresh.get("manifest_hash") != manifest["manifest_hash"]:
                    raise CanonicalDecisionError("Evidence refresh belongs to another manifest")
                evidence = evidence_refresh.get("evidence") or {}
                for decision in _case_decisions(case):
                    fields = [("candidate_evidence", "selected_video_id")]
                    if decision.get("expected_video_id") and decision["expected_video_id"] != decision["selected_video_id"]:
                        fields.append(("current_evidence", "expected_video_id"))
                    for field, video_key in fields:
                        previous = decision.get(field) or {}
                        refreshed = evidence.get(decision.get(video_key))
                        if not refreshed or refreshed.get("environment") != previous.get("environment"):
                            raise CanonicalDecisionError("Evidence refresh changed the environment or omitted a recording")
                        decision[field] = refreshed
            _apply_source_metadata_repairs(conn, case)
            if case.get("action") == "repair_recording_identity":
                result = _apply_identity_repair(conn, case)
            elif case.get("action") == "split_binding":
                result = _apply_split(conn, case)
            else:
                if find_track_by_service_song(conn, case["service"], case["song_id"]) != case["decision"]["expected_track_uid"]:
                    raise CanonicalDecisionError("Repair source binding changed")
                result = apply_canonical_decision(
                    conn, track_uid=case["decision"]["expected_track_uid"],
                    decision=case["decision"], source_row=case.get("source_row"),
                )
            state = _case_state(conn, case)
            after[case["case_id"]] = {"selection": result, "fingerprint": fingerprint(state), "state": state}
        if _protected(conn, manifest["cases"]) != protected:
            raise CanonicalDecisionError("Repair modified protected source/audit/manual data")
        if _outside_scope(conn, manifest["cases"]) != outside_scope:
            raise CanonicalDecisionError("Repair modified storage outside the reviewed scope")
        conflicts = {row["conflict_id"]: row for row in _rows(conn, "SELECT * FROM review_conflicts")}
        if any(conflicts.get(row["conflict_id"]) != row for row in manifest["review_conflicts_before"]):
            raise CanonicalDecisionError("Repair modified existing review evidence")
        receipt = {"repair_id": manifest["repair_id"], "manifest_hash": manifest["manifest_hash"],
                   "status": "db_applied", "created_at": utc_now_iso(), "after": after,
                   "before": {case["case_id"]: {"fingerprint": case["before_hash"], "state": case["before"]}
                              for case in manifest["cases"]},
                   "implementation_revision": manifest["implementation_revision"],
                   "implementation_fingerprint": manifest["implementation_fingerprint"],
                   "evidence_refresh": _plain(evidence_refresh),
                   "stages": {"db": "applied", "publication": "pending", "history": "pending", "verification": "pending"},
                   "followups": {"daily": "pending", "monday": "pending", "device": "pending"},
                   "outputs": manifest.get("outputs") or {}}
        conn.execute(
            "INSERT INTO migration_reports(report_id,source,rows_read,tracks_seen,conflicts_seen,created_at,payload_json) "
            "VALUES (?,?,?, ?,0,?,?)",
            ("chart-repair:" + manifest["repair_id"], "ytmusic_chart_incident", len(manifest["cases"]),
             len(manifest["cases"]), utc_now_iso(), json.dumps(receipt, ensure_ascii=False)),
        )
        return verify_repair(conn, manifest)


@contextmanager
def incident_connection(db_path: Path, *, read_only: bool, schema: str = "public"):
    """Open an existing DB without schema initialization, migrations, or index writes."""
    if os.environ.get("SUPABASE_DB_URL"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("Invalid PostgreSQL schema identifier")
        import psycopg2
        from psycopg2 import sql
        raw = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=20)
        raw.set_session(readonly=read_only, isolation_level="SERIALIZABLE")
        with raw.cursor() as cursor:
            cursor.execute(sql.SQL("SET search_path TO {},pg_catalog").format(sql.Identifier(schema)))
            cursor.execute("SET LOCAL lock_timeout='5s'")
            cursor.execute("SET LOCAL statement_timeout='120s'")
        conn = PostgresConnectionWrapper(raw)
    else:
        raw = sqlite3.connect(f"file:{db_path.resolve()}?mode={'ro' if read_only else 'rw'}", uri=True)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys=ON")
        if read_only:
            raw.execute("PRAGMA query_only=ON")
        conn = raw
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "rehearse", "apply-db", "verify", "resume"))
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--evidence-refresh", type=Path, help="New exact-ID evidence bound to the unchanged manifest hash")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.spec if args.phase == "plan" else args.manifest
    if not source:
        parser.error("plan needs --spec; other phases need --manifest")
    if args.output.resolve() in {args.db_path.resolve(), source.resolve()}:
        parser.error("Output must not overwrite the DB, input specification, or manifest")
    payload = json.loads(source.read_text(encoding="utf-8"))
    refreshed = json.loads(args.evidence_refresh.read_text(encoding="utf-8")) if args.evidence_refresh else None
    guard = nullcontext()
    if args.phase in {"rehearse", "apply-db", "resume"}:
        from sync_validation import sync_run_lock
        guard = sync_run_lock(args.db_path)
    with guard, incident_connection(args.db_path, read_only=args.phase in {"plan", "verify"}, schema=args.schema) as conn:
        if args.phase == "plan":
            result = plan_repair(conn, payload)
        elif args.phase == "verify":
            result = verify_repair(conn, payload)
        else:
            result = apply_repair(conn, payload, evidence_refresh=refreshed)
            if args.phase == "rehearse":
                conn.rollback()
                result.update(status="rehearsed", committed=False)
            else:
                conn.commit()
                result["committed"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    os.replace(temporary, args.output)
    print(json.dumps({key: value for key, value in result.items() if key not in {"cases", "outputs", "protected_before"}},
                     ensure_ascii=False) if args.phase != "plan" else json.dumps({"status": "planned", "repair_id": result["repair_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
