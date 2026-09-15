"""Publish the exact outputs of an applied, reviewed chart repair manifest.

The manifest contains a post-repair business fingerprint, full playlist before
items, preserved slots and a validated history preview. Provider operations use
the normal durable publisher; a pending operation must be reconciled explicitly.
Installing history and confirming its public deployment are separate phases.
Refresh playback evidence before planning: different decision evidence changes
the immutable canonical receipt and requires a new projected fingerprint/manifest.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from repair_ytmusic_chart_incident import (
    _followup_replacements, _receipt, _validate_manifest, _validate_stage_readback, fingerprint, incident_connection,
    mark_repair_stage, verify_repair,
)
from sync_validation import PlaybackBlocked, require_playable, sync_run_lock, verifier_for
from hype_db_store import _canonical_write_scope
from ytmusic_playlist_sync import get_existing_playlist_items, make_ytmusic, update_ytmusic_playlist

BUSINESS_TABLES = (
    "tracks", "platform_song_ids", "yt_video_ids", "metadata_lookup_index",
    "manual_overrides", "track_list", "playlist_order", "match_runs", "match_attempts",
    "source_song_relations", "chart_source_audit", "ytmusic_song_translations", "migration_reports",
    "playlist_update_runs", "playlist_update_items",
)


def business_fingerprint(conn, *, repair_id=None):
    """One round trip on PostgreSQL; audit writes do not invalidate this snapshot.

    Database-generated creation/update times can differ between a rolled-back
    rehearsal and the actual commit. All selection, source and policy fields
    remain part of the comparison.
    """
    if repair_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", repair_id):
        raise ValueError("Invalid repair audit scope")
    prefix = "repair:" + repair_id + ":" if repair_id else None
    def selection(table):
        if not prefix:
            return "", []
        if table == "playlist_update_runs":
            return " WHERE substr(COALESCE(r.job_name,''),1,?) <> ?", [len(prefix), prefix]
        if table == "playlist_update_items":
            return (" WHERE NOT EXISTS (SELECT 1 FROM playlist_update_runs p "
                    "WHERE p.update_run_id=r.update_run_id AND substr(COALESCE(p.job_name,''),1,?)=?)",
                    [len(prefix), prefix])
        return "", []
    if type(conn).__name__ == "PostgresConnectionWrapper":
        queries, values = [], []
        for table in BUSINESS_TABLES:
            where, params = selection(table)
            queries.append(f"SELECT '{table}' AS table_name, row_to_json(r)::text AS payload FROM {table} r" + where)
            values.extend(params)
        rows = conn.execute(" UNION ALL ".join(queries), values).fetchall()
        records = [(row["table_name"], json.loads(row["payload"])) for row in rows]
    else:
        records = []
        for table in BUSINESS_TABLES:
            where, params = selection(table)
            records.extend((table, dict(row)) for row in conn.execute(f"SELECT r.* FROM {table} r" + where, params))
    repair_timestamp_tables = {"tracks", "platform_song_ids", "yt_video_ids", "metadata_lookup_index"}
    def stable_value(table, key, value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if table == "ytmusic_song_translations" and key == "updated_at" and value is not None:
            observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("Translation timestamp must include its timezone")
            return observed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return value
    normalized = [(table, {key: stable_value(table, key, value) for key, value in row.items()
                           if not ((table in repair_timestamp_tables
                                    or table == "migration_reports" and row.get("source") == "canonical_decision")
                                   and key in {"created_at", "updated_at", "yt_metadata_verified_at"})})
                  for table, row in records
                  if table != "migration_reports" or row.get("source") != "ytmusic_chart_incident"]
    return fingerprint(sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, default=str)))


def _slots(items):
    return [(row.get("video_id") or row.get("videoId"),
             row.get("set_video_id") or row.get("setVideoId")) for row in items]


def _assert_preserved(target, actual):
    slots = _slots(actual)
    if any(not video or not token for video, token in slots) or len({token for _, token in slots}) != len(slots):
        raise PlaybackBlocked("Playlist readback lacks complete item ownership")
    if any(slot not in slots for slot in _slots(target["preserved_items"])):
        raise PlaybackBlocked("A preserved item was removed or re-added")


def _scope(manifest):
    _validate_manifest(manifest)
    outputs = manifest["outputs"]
    if not outputs.get("business_fingerprint") or not outputs.get("history") or not outputs.get("playlists"):
        raise ValueError("Publication needs reviewed DB, playlist and history outputs")
    if len({target["playlist_id"] for target in outputs["playlists"]}) != len(outputs["playlists"]):
        raise ValueError("Playlist scope contains duplicate targets")
    for target in outputs["playlists"]:
        if ("before_items" not in target or "preserved_items" not in target or not target.get("video_ids")
                or any(not isinstance(target.get(key), str) or not target[key]
                       for key in ("playlist_id", "service", "job_name"))):
            raise ValueError("Playlist target needs exact before state and preserved slots")
        if (any(not isinstance(video, str) or not video for video in target["video_ids"])
                or len(target["video_ids"]) != len(set(target["video_ids"]))):
            raise ValueError("Playlist target contains duplicate recordings")
        _assert_preserved(target, target["before_items"])
        common = [slot for slot in _slots(target["before_items"]) if slot[0] in target["video_ids"]]
        if _slots(target["preserved_items"]) != common:
            raise ValueError("All shared playlist slots must be preserved")
    if len({target["path"] for target in outputs["history"]}) != len(outputs["history"]):
        raise ValueError("History scope contains duplicate targets")
    for target in outputs["history"]:
        if (any(not isinstance(target.get(key), str) or not target[key]
                for key in ("path", "preview_path", "expected_date", "public_url"))
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(target.get(key) or ""))
                       for key in ("sha256", "before_sha256"))):
            raise ValueError("History needs exact reviewed paths, date and hashes")
        url = urlsplit(target["public_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("Public history needs an exact HTTPS URL")
    return outputs


def _guard(db_path, manifest, *, check_pending=True, publication_required=False):
    with incident_connection(Path(db_path), read_only=True) as conn:
        return _guard_connection(conn, manifest, check_pending=check_pending, publication_required=publication_required)


def _guard_connection(conn, manifest, *, check_pending=True, publication_required=False):
    from repair_ytmusic_chart_incident import implementation_fingerprint
    if manifest["implementation_fingerprint"] != implementation_fingerprint():
        raise PlaybackBlocked("Repair implementation or identity policy changed since review")
    receipt = _receipt(conn, manifest)
    if (not receipt or receipt.get("manifest_hash") != manifest["manifest_hash"]
            or receipt.get("stages", {}).get("db") != "applied"):
        raise PlaybackBlocked("Publication requires the matching applied DB receipt")
    if publication_required and receipt.get("stages", {}).get("publication") != "verified":
        raise PlaybackBlocked("History requires verified playlist publication")
    if check_pending:
        ids = [target["playlist_id"] for target in manifest["outputs"]["playlists"]]
        pending = conn.execute(
            f"SELECT update_run_id FROM playlist_update_runs WHERE playlist_id IN ({','.join('?' for _ in ids)}) "
            "AND status IN ('running','mutation_failed','recovery_required') LIMIT 1", ids,
        ).fetchone()
        if pending:
            raise PlaybackBlocked("Playlist has a pending durable operation; reconcile it before resuming")
    if business_fingerprint(conn, repair_id=manifest["repair_id"]) != manifest["outputs"]["business_fingerprint"]:
        raise PlaybackBlocked("DB source, selection, policy or original publication audit changed after repair review")
    return receipt


def _mark_stage(db_path, manifest, stage, evidence):
    with incident_connection(Path(db_path), read_only=False) as conn:
        with _canonical_write_scope(conn):
            _guard_connection(conn, manifest, publication_required=stage == "history")
            result = mark_repair_stage(conn, manifest, stage, evidence)
        conn.commit()
    return {**result, "evidence": evidence}


def _read_previews(outputs, *, manifest=None):
    from hype_db_reports import inflate_frontend_history, validate_frontend_history
    videos, previews = [], []
    for target in outputs["history"]:
        payload = json.loads(Path(target["preview_path"]).read_text())
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode()
        if hashlib.sha256(serialized).hexdigest() != target["sha256"]:
            raise PlaybackBlocked("History preview differs from the reviewed manifest")
        validate_frontend_history(payload, target["expected_date"], days=payload.get("days", 31))
        for date in payload["dates"]:
            validate_frontend_history(payload, date, days=payload.get("days", 31))
        history = inflate_frontend_history(payload)
        videos.extend(row["video_id"] for rows in history.values() for row in rows)
        previews.append((target, payload))
    if manifest and manifest.get("supersedes"):
        parent, _ = _read_previews(manifest["supersedes"]["manifest"]["outputs"])
        old_by_path = {target["path"]: payload for target, payload in parent}
        replacements = _followup_replacements(manifest)
        # Reuse the existing history representation. A merge needing new scores
        # is outside this canonical-only follow-up and must be reviewed separately.
        display = {"video_id", "title", "artist", "album", "yt_title", "yt_artist", "yt_album", "artwork_url", "identity_key"}
        for target, payload in previews:
            original = old_by_path[target["path"]]
            if (payload.get("dates") != original.get("dates") or payload.get("days") != original.get("days")
                    or payload.get("identity_exclusions", []) != original.get("identity_exclusions", [])):
                raise PlaybackBlocked("Follow-up history must preserve the full window and identity exclusions")
            old, new = inflate_frontend_history(original), inflate_frontend_history(payload)
            for date, rows in old.items():
                expected_ids = [replacements.get(row["video_id"], row["video_id"]) for row in rows]
                if len(set(expected_ids)) != len(expected_ids) or expected_ids != [row["video_id"] for row in new[date]]:
                    raise PlaybackBlocked("Follow-up history would omit, reorder or combine recordings")
                for before, after in zip(rows, new[date]):
                    if before["video_id"] == after["video_id"]:
                        valid = before == after
                    else:
                        valid = ({key: value for key, value in before.items() if key not in display}
                                 == {key: value for key, value in after.items() if key not in display}
                                 and after.get("identity_key", after["video_id"])
                                 == before.get("identity_key", before["video_id"]))
                    if not valid:
                        raise PlaybackBlocked("Follow-up changed retained history fields, scores or recording identity")
    return previews, videos


def _recheck_lists(outputs, client, verifier):
    observations = []
    for target in outputs["playlists"]:
        actual = get_existing_playlist_items(client, target["playlist_id"])
        observed_at = datetime.now(timezone.utc).isoformat()
        if [video for video, _ in _slots(actual)] != target["video_ids"]:
            raise PlaybackBlocked("Published playlist differs from the full ordered target")
        _assert_preserved(target, actual)
        require_playable(verifier, target["video_ids"], items=actual, force=True)
        observations.append({"playlist_id": target["playlist_id"], "video_ids": target["video_ids"],
                             "items": [{"video_id": video, "set_video_id": token} for video, token in _slots(actual)],
                             "readback_verified": True, "observed_at": observed_at})
    return observations


def publish(manifest, db_path, client, *, verifier=None):
    outputs = _scope(manifest)
    managed_verifier = verifier is None
    verifier = verifier if verifier is not None else verifier_for(client, fresh=True)
    _, history_videos = _read_previews(outputs, manifest=manifest)
    video_ids = list(dict.fromkeys(history_videos + [video for target in outputs["playlists"]
                                                   for video in target["video_ids"]]))
    require_playable(verifier, video_ids)
    with incident_connection(Path(db_path), read_only=True) as conn:
        verify_repair(conn, manifest)
    _guard(db_path, manifest)
    for target in outputs["playlists"]:
        _guard(db_path, manifest)
        if managed_verifier:
            # Each playlist is a separate bounded observation phase. Never
            # carry successes or extend a deadline from the preceding phase.
            verifier = verifier_for(client, fresh=True)
        require_playable(verifier, target["video_ids"])
        actual = get_existing_playlist_items(client, target["playlist_id"])
        actual_ids = [video for video, _ in _slots(actual)]
        if actual_ids == target["video_ids"]:
            _assert_preserved(target, actual)
            require_playable(verifier, actual_ids, items=actual)
            continue
        if _slots(actual) != _slots(target["before_items"]):
            raise PlaybackBlocked("Playlist changed; reconcile the specific durable operation before resuming")
        update_ytmusic_playlist(
            client, target["playlist_id"], target["video_ids"], dry_run=False,
            db_path=db_path, service=target["service"],
            job_name="repair:" + manifest["repair_id"] + ":" + target["job_name"],
            playability_verifier=verifier,
            before_mutation=lambda: _guard(db_path, manifest, check_pending=False),
            expected_before_items=target["before_items"],
        )
    if managed_verifier:
        verifier = verifier_for(client, fresh=True)
    require_playable(verifier, video_ids)
    observations = _recheck_lists(outputs, client, verifier)
    _guard(db_path, manifest)
    evidence = {"evidence_ref": "manifest:" + manifest["manifest_hash"], "playlists": observations}
    return _mark_stage(db_path, manifest, "publication", evidence)


def install_history(manifest, db_path, *, client=None, verifier=None):
    from hype_db_reports import write_frontend_history
    outputs = _scope(manifest)
    previews, _ = _read_previews(outputs, manifest=manifest)
    with incident_connection(Path(db_path), read_only=True) as conn:
        state = verify_repair(conn, manifest)
    if state["publication_status"] != "verified":
        raise PlaybackBlocked("History installation requires verified playlist publication")
    receipt = _guard(db_path, manifest, publication_required=True)
    reads = receipt.get("stage_evidence", {}).get("publication", {}).get("playlists", [])
    try:
        if {row["playlist_id"] for row in reads} != {target["playlist_id"] for target in outputs["playlists"]}:
            raise ValueError("Publication readback scope is missing")
        for readback in reads:
            _validate_stage_readback(readback, receipt["created_at"])
    except (ValueError, RuntimeError):
        if client is None:
            raise PlaybackBlocked("Publication proof expired; fresh playlist reads are required")
        _recheck_lists(outputs, client, verifier or verifier_for(client))
    for target, payload in previews:
        _guard(db_path, manifest, publication_required=True)
        path = Path(target["path"])
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if current_hash == target["sha256"]:
            continue
        write_frontend_history(payload, path, expected_date=target["expected_date"],
                               expected_file_sha256=target["before_sha256"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != target["sha256"]:
            raise PlaybackBlocked("Installed history has an unexpected serialization")
    return {"status": "installed", "public_verification": "pending"}


def verify_public_history(manifest, db_path, client, *, verifier=None):
    import requests
    outputs = _scope(manifest)
    with incident_connection(Path(db_path), read_only=True) as conn:
        verify_repair(conn, manifest)
    _guard(db_path, manifest, publication_required=True)
    _, history_videos = _read_previews(outputs, manifest=manifest)
    verifier = verifier or verifier_for(client)
    require_playable(verifier, history_videos)
    evidence = {"evidence_ref": "manifest:" + manifest["manifest_hash"], "files": []}
    for target in outputs["history"]:
        if not target["public_url"].startswith("https://"):
            raise ValueError("Public history must use HTTPS")
        response = requests.get(target["public_url"], params={"repair": manifest["manifest_hash"]},
                                headers={"Cache-Control": "no-cache"}, allow_redirects=False, timeout=30)
        response.raise_for_status()
        if response.status_code != 200:
            raise PlaybackBlocked("Public history did not return the reviewed resource directly")
        local = hashlib.sha256(Path(target["path"]).read_bytes()).hexdigest()
        public = hashlib.sha256(response.content).hexdigest()
        if local != target["sha256"] or public != local:
            raise PlaybackBlocked("Public history has not deployed the reviewed file")
        evidence["files"].append({"path": target["path"], "sha256": local, "public_sha256": public,
                                  "public_url": target["public_url"], "readback_verified": True,
                                  "observed_at": datetime.now(timezone.utc).isoformat()})
    evidence["playlists"] = _recheck_lists(outputs, client, verifier)
    _guard(db_path, manifest, publication_required=True)
    return _mark_stage(db_path, manifest, "history", evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("publish", "install-history", "verify-public-history"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--auth", default=".secrets/browser.json")
    parser.add_argument("--env-file", default=".secrets/.env")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from sync_all import load_env_file
    load_env_file(Path(args.env_file))
    manifest = json.loads(args.manifest.read_text())
    with sync_run_lock(args.db_path):
        if args.phase == "publish":
            result = publish(manifest, args.db_path, make_ytmusic(args.auth))
        elif args.phase == "install-history":
            result = install_history(manifest, args.db_path, client=make_ytmusic(args.auth))
        else:
            result = verify_public_history(manifest, args.db_path, make_ytmusic(args.auth))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({key: value for key, value in result.items() if key != "evidence"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
