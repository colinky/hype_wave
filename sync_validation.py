"""Run-scoped playback checks and immutable inputs shared by every publisher."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class PlaybackBlocked(RuntimeError):
    """An uncertain/unavailable selection must not become a completed snapshot."""


@contextmanager
def sync_run_lock(db_path):
    """One cooperative writer across scheduled runs, local runs and incident repair."""
    if os.environ.get("HYPE_SYNC_PARENT_PID") == str(os.getppid()):
        yield
        return
    if os.environ.get("SUPABASE_DB_URL"):
        from hype_db_schema import connect
        with connect(db_path, read_only=True) as conn:
            # The hosted URL is a transaction pooler: a session lock could be
            # detached from this client at commit. Pin only this dedicated lock
            # transaction until the command exits; normal DB work stays short.
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '0'")
            acquired = conn.execute("SELECT pg_try_advisory_xact_lock(1847391027)").fetchone()[0]
            if not acquired:
                raise PlaybackBlocked("Another synchronization or incident repair owns the write lock")
            yield
    else:
        import fcntl
        lock_path = Path(str(db_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlaybackBlocked("Another synchronization or repair owns the write lock") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def run_locked_cli(main):
    """Lock standalone writers while an actual sync_all child shares its parent's lock."""
    import argparse
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db-path", default=str(Path(__file__).with_name("hype_wave_data.db")))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()
    load_dotenv(args.env_file)
    if args.dry_run:
        return main()
    with sync_run_lock(args.db_path):
        return main()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def assert_no_active_repair(conn):
    """A partially applied incident must be resumed before ordinary synchronization."""
    rows = conn.execute("SELECT payload_json FROM migration_reports WHERE source='ytmusic_chart_incident'").fetchall()
    for row in rows:
        receipt = json.loads(row["payload_json"])
        if receipt.get("status") not in {"surfaces_verified", "completed"}:
            raise PlaybackBlocked("An incident repair has unfinished playlist or history publication")


def _approved_canonical_changes(conn):
    changes = {}
    for row in conn.execute("SELECT payload_json FROM migration_reports WHERE source='canonical_decision'").fetchall():
        receipt = json.loads(row["payload_json"])
        decision = receipt.get("decision") or {}
        uid, old, selected = (receipt.get(key) for key in ("track_uid", "expected_video_id", "selected_video_id"))
        if (uid and old and selected and decision.get("expected_track_uid") == uid
                and decision.get("expected_video_id") == old and decision.get("selected_video_id") == selected):
            changes.setdefault((uid, old), set()).add(selected)
    return changes


def _has_approved_path(changes, uid, old, selected):
    pending, seen = [old], set()
    while pending:
        current = pending.pop()
        if current == selected:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(changes.get((uid, current), ()))
    return False


def verifier_for(client: Any):
    from ytmusic_playability import PlayabilityVerifier

    cached = vars(client).get("_hype_playability_verifier")
    if cached is not None:
        return cached
    config_path = Path(__file__).with_name("ytmusic_validation_config.json")
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    path = os.environ.get("YTMUSIC_EXPECTED_ACCOUNT_FILE")
    expected = json.loads(Path(path).read_text()) if path else config.get("expected_account")
    controls = os.environ.get("YTMUSIC_CONTROL_VIDEO_IDS", ",".join(config.get("control_video_ids", [])))
    verifier = PlayabilityVerifier(
        client, control_video_ids=[v.strip() for v in controls.split(",") if v.strip()],
        expected_account=expected, environment="actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
    )
    client._hype_playability_verifier = verifier
    return verifier


def require_playable(verifier, video_ids, *, items=(), force=False):
    from ytmusic_playability import evidence_is_fresh

    health = verifier.check_health()
    if health.get("run_health") != "healthy" or health.get("auth_state") != "authenticated":
        raise PlaybackBlocked("Authentication or playback controls are inconclusive")
    availability = {}
    for item in items:
        if isinstance(item.get("isAvailable"), bool):
            vid = item["videoId"]
            availability[vid] = availability.get(vid, True) and item["isAvailable"]
    evidence = {}
    for video_id in dict.fromkeys(video_ids):
        result = verifier.verify(video_id, availability=availability.get(video_id), force=force)
        evidence[video_id] = result
        if result.get("state") != "playable":
            raise PlaybackBlocked(f"Playback verification blocked {video_id}: {result.get('reason_code', 'unknown')}")
    health = verifier.check_health()
    if health.get("run_health") != "healthy" or health.get("auth_state") != "authenticated" or any(
        not evidence_is_fresh(value, environment=verifier.environment) for value in evidence.values()
    ):
        raise PlaybackBlocked("Playback evidence expired or the execution environment changed")
    return evidence


def playable_cache(cache, verifier):
    """Exclude only confirmed unavailable cache candidates, without changing the DB."""
    selected = {}
    for song_id, match in cache.items():
        match = dict(match)
        video_id = match.get("video_id")
        if video_id and match.get("status") != "manual_blocked":
            observed = verifier.verify(video_id)
            if observed.get("state") == "unknown":
                raise PlaybackBlocked(f"Cached recording is uncertain: {video_id}")
            if observed.get("state") == "unavailable":
                if match.get("status") == "manual_override" or match.get("manual_action"):
                    raise PlaybackBlocked(f"Manual recording is unavailable: {video_id}")
                match = {"status": "unavailable", "excluded_video_ids": [video_id]}
        selected[song_id] = match
    return selected


def _feature_names(title):
    """Compare credited people, including reviewed name translations, not title tokens."""
    from ytmusic_playlist_sync import ALIASES, normalize_text, split_artist_names

    match = re.search(r"\b(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)", title, re.I)
    if not match:
        return set()
    names = set()
    for name in split_artist_names(match.group(1)):
        variants = ALIASES.get_variants(name, "artist")
        names.add(min(normalize_text(value).replace(" ", "") for value in variants))
    return names


def _rating_version(title):
    match = re.search(r"\b(explicit|clean|censored|uncensored)\b", title, re.I)
    return match.group(1).lower() if match else ""


def _recording_version(title):
    from hype_db_common import version_signature

    # A trailing featured-person group is not part of an Explicit/Clean label.
    title = re.sub(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]", "", title, flags=re.I)
    return version_signature(title.strip())


def _album_version(album):
    from hype_db_common import version_signature

    # Remixing/Remixed are recording markers too; a different album spelling
    # must not hide the known Remapping/Remixing collision.
    if re.search(r"\bremix(?:ing|ed|es)?\b", album, re.I):
        return "remix"
    album = re.sub(r"\b(?:deluxe|apple music)\s+edition\b", "", album, flags=re.I)
    return version_signature(album)


def _identity_variants(row):
    # Preserve every literal credit/version assertion; search normalization
    # would deduplicate differing featured people before checking them.
    return list(dict.fromkeys(tuple(str(row.get(field + suffix) or "").strip()
                                    for field in ("title", "artist", "album"))
                             for suffix in ("", "_ko", "_en")
                             if row.get("title" + suffix) and row.get("artist" + suffix)))


def recording_identity_matches(left, right, *, player=False):
    """Check source→exact watch metadata, or exact watch→sparser player metadata.

    Only the player may omit a feature/version assertion. A source's missing
    feature must be present in its artist credits or separately proven.
    """
    from ytmusic_playlist_sync import artist_variants, similarity, split_artist_names, title_variants

    if left.get("length_seconds") is not None and right.get("length_seconds") is not None:
        try:
            lengths = [float(row["length_seconds"]) for row in (left, right)]
        except (TypeError, ValueError):
            return False
        if any(not math.isfinite(value) or value <= 0 for value in lengths) or abs(lengths[0] - lengths[1]) > 2:
            return False
    left_rows, right_rows = _identity_variants(left), _identity_variants(right)
    def assertions(rows, field, extractor):
        return {value for row in rows if (value := extractor(row[field]))}
    left_versions = assertions(left_rows, 0, _recording_version)
    right_versions = assertions(right_rows, 0, _recording_version)
    left_ratings = assertions(left_rows, 0, _rating_version)
    right_ratings = assertions(right_rows, 0, _rating_version)
    left_features = assertions(left_rows, 0, lambda title: frozenset(_feature_names(title)))
    right_features = assertions(right_rows, 0, lambda title: frozenset(_feature_names(title)))
    left_albums = {_album_version(row[2]) for row in left_rows if row[2]}
    right_albums = {_album_version(row[2]) for row in right_rows if row[2]}
    if any(len(values) > 1 for values in (left_versions, right_versions, left_ratings, right_ratings,
                                          left_features, right_features, left_albums, right_albums)):
        return False
    if left_versions != right_versions and not (player and not right_versions):
        return False
    if left_ratings and right_ratings and left_ratings != right_ratings:
        return False
    if left_albums and right_albums and left_albums != right_albums:
        versioned_albums = [row[2] for row in left_rows + right_rows if _album_version(row[2])]
        if not (left_ratings and left_ratings == right_ratings
                and left_albums | right_albums == {"", "version"}
                and all(re.search(r"\(Weekday Ver\.?\)$", album, re.I) for album in versioned_albums)):
            return False
    for lt, la, _ in left_rows:
        for rt, ra, _ in right_rows:
            lf = next(iter(left_features), frozenset())
            rf = next(iter(right_features), frozenset())
            if lf != rf:
                if player and not rf:
                    pass
                elif not lf and rf:
                    credits = set()
                    for name in artist_variants(la):
                        credits |= _feature_names("Song (feat. " + name + ")")
                    if not rf <= credits:
                        continue
                else:
                    continue
            # Parentheses containing a translated title or an OST attribution
            # may be omitted only after their explicit version/feature checks.
            def titles(value):
                value = re.sub(r'\s*\(From\s+["“].*?\)\s*', ' ', value, flags=re.I)
                return title_variants(value)
            if max((similarity(a, b, is_title=True) for a in titles(lt) for b in titles(rt)), default=0) < 0.8:
                continue
            left_lead = artist_variants(split_artist_names(la)[0])
            right_lead = artist_variants(split_artist_names(ra)[0])
            if max((similarity(a, b) for a in left_lead for b in right_lead), default=0) < 0.8:
                continue
            return True
    return False


def proven_source_identity(source, service, policy):
    """Apply an explicitly reviewed source-credit omission, bound to exact metadata.

    This supplies identity evidence only. It neither selects a YouTube ID nor
    changes source rows, attempts, playback status, or canonical ownership.
    """
    proof = policy.get("source_identity_evidence", {}).get(f"{service}:{source.get('song_id')}")
    if not proof or proof.get("kind") != "official_cross_service_credit":
        return source
    def exact_text(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    expected = {tuple(exact_text(value) for value in variant)
                for variant in proof.get("source_variants", []) if len(variant) == 3}
    actual = _identity_variants(source)
    if (not actual or not all(tuple(exact_text(value) for value in row) in expected for row in actual)
            or not proof.get("references") or not proof.get("recording")):
        return source
    return {**source, **proof["recording"]}


def source_recording_matches(source, metadata, *, service=None, policy=None):
    """Shared identity boundary for a fresh source and an exact recording."""
    from hype_db_common import normalized_service

    if policy is None:
        policy = json.loads(Path(__file__).with_name("matching_alias.json").read_text())
    service = normalized_service(service or source.get("service") or "")
    return recording_identity_matches(proven_source_identity(source, service, policy), metadata)


def validate_matches(conn, *, service, sources, matches, client, verifier=None):
    """Finish all network checks before raw data or matching results are written."""
    from hype_db_common import normalized_service, row_dict
    from hype_db_store import get_bulk_cached_matches, manual_override, find_track_by_video
    from ytmusic_playlist_sync import ALIASES, get_verified_video_metadata, normalize_text

    verifier = verifier if verifier is not None else verifier_for(client)
    require_playable(verifier, [])
    service = normalized_service(service)
    source_rows = [row_dict(row) for row in sources]
    result = [row_dict(row).copy() for row in matches]
    by_source = {str(row.get("song_id") or ""): row for row in source_rows}
    alias_path = Path(__file__).with_name("matching_alias.json")
    alias_file = alias_path.read_bytes() if alias_path.is_file() else None
    identity_policy = json.loads(alias_file) if alias_file else {}
    aliases = dict(ALIASES.overrides)
    artist_alias_fingerprint = fingerprint(ALIASES.artist_map)

    def alias_video(source):
        for suffix in ("", "_ko", "_en"):
            key = "|".join(normalize_text(source.get(field + suffix) or "") for field in ("title", "artist"))
            if aliases.get(key):
                return str(aliases[key])
        return ""

    def read_policy(song_id):
        row = manual_override(conn, service, song_id)
        policy = dict(row) if row else {}
        if policy.get("target_track_uid"):
            target = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?",
                                  (policy["target_track_uid"],)).fetchone()
            policy["target_exists"] = target is not None
            policy["target_video_id"] = target["canonical_yt_video_id"] if target else None
        if policy:
            target_video = policy.get("canonical_yt_video_id") or policy.get("target_video_id")
            policy["video_owner"] = find_track_by_video(conn, target_video) if target_video else None
        return policy

    policies = {song_id: read_policy(song_id) for song_id in by_source}
    existing = {}
    for row in source_rows:
        song_id = str(row.get("song_id") or "")
        prior = conn.execute(
            "SELECT t.* FROM platform_song_ids p JOIN tracks t ON t.track_uid=p.track_uid "
            "WHERE p.service=? AND p.song_id=?", (service, song_id),
        ).fetchone()
        if prior:
            existing[song_id] = dict(prior)
    # These are reads. End the snapshot before bounded network observations.
    conn.commit()
    cached = get_bulk_cached_matches(conn, service=service, tracks=source_rows, read_only=True)
    conn.commit()
    metadata_cache = {}
    validated_identity = {}
    for match in result:
        song_id = str(match.get("song_id") or "")
        source = by_source[song_id]
        policy = policies[song_id]
        alias = alias_video(source) if not policy else ""
        manual = bool(policy or alias)
        if policy and str(policy["action"]).lower() in {"block", "manual_blocked"}:
            if match.get("status") != "manual_blocked" or match.get("video_id"):
                raise PlaybackBlocked(f"Manual policy changed for {service}:{song_id}")
            continue
        selected = str(match.get("video_id") or "")
        if policy:
            target_video = policy.get("canonical_yt_video_id") or policy.get("target_video_id")
            target_uid = policy.get("target_track_uid")
            if (not target_video or selected != target_video or match.get("status") != "manual_override"
                    or (target_uid and (not policy.get("target_exists")
                                        or policy.get("video_owner") not in {None, target_uid}))):
                raise PlaybackBlocked(f"Manual target requires a fresh decision for {service}:{song_id}")
        elif alias:
            if selected != alias:
                raise PlaybackBlocked(f"Alias manual target was not selected for {service}:{song_id}")
            match["status"] = "manual_override"
        elif match.get("status") in {"manual_override", "manual_blocked"}:
            raise PlaybackBlocked(f"Manual policy no longer authorizes {service}:{song_id}")
        prior = existing.get(song_id, {})
        old = str(prior.get("canonical_yt_video_id") or "")
        if not selected:
            if old and match.get("status") != "manual_blocked":
                raise PlaybackBlocked(f"Existing selection would disappear for {service}:{song_id}")
            continue
        old_evidence = verifier.verify(old) if old else None
        trusted = cached.get(song_id, {})
        if old and selected != old and not manual:
            if old_evidence.get("state") == "unknown":
                raise PlaybackBlocked(f"Existing recording is uncertain: {old}")
            if trusted.get("video_id") != old:
                old_metadata = get_verified_video_metadata(client, old, metadata_cache=metadata_cache)
                if (not old_metadata or old_metadata.get("video_id") != old
                        or not source_recording_matches(source, old_metadata, service=service, policy=identity_policy)
                        or not recording_identity_matches(old_metadata, old_evidence, player=True)):
                    raise PlaybackBlocked(f"Existing recording identity needs review for {service}:{song_id}")
            if old_evidence.get("state") == "playable":
                match.update(video_id=old, yt_title=prior["yt_title"], yt_artist=prior["yt_artist"],
                             yt_album=prior["yt_album"], status="cached_match", query="preserved_playable_canonical")
                selected = old
        selected_evidence = require_playable(verifier, [selected])[selected]
        match["playability_evidence"] = selected_evidence
        metadata = None
        if not manual:
            # get_song can omit credits that its exact watch metadata exposes.
            # A trusted cache must not hide that live contradiction (Dirty Work).
            metadata = get_verified_video_metadata(client, selected, metadata_cache=metadata_cache)
            if (not metadata or metadata.get("video_id") != selected
                    or not source_recording_matches(source, metadata, service=service, policy=identity_policy)
                    or not recording_identity_matches(metadata, selected_evidence, player=True)):
                raise PlaybackBlocked(f"Selected recording does not match the source identity: {selected}")
            match.update(yt_title=metadata.get("title", ""), yt_artist=metadata.get("artist", ""),
                         yt_album=metadata.get("album", ""))
        if not manual:
            validated_identity[selected] = metadata or selected_evidence
        if old and old != selected and not manual:
            metadata = metadata or get_verified_video_metadata(client, selected, metadata_cache=metadata_cache)
            if not metadata or metadata.get("video_id") != selected:
                raise PlaybackBlocked(f"Replacement identity is uncertain: {selected}")
            metadata = {**metadata, "verified": True}
            match["canonical_decision"] = {
                "expected_track_uid": prior["track_uid"], "expected_video_id": old,
                "selected_video_id": selected, "reason": "replace_unavailable_recording",
                "same_recording": True, "current_evidence": old_evidence,
                "candidate_evidence": selected_evidence, "metadata": metadata,
                "expected_target_uid": find_track_by_video(conn, selected),
                "expected_identity_policy_hash": hashlib.sha256(alias_file or b"").hexdigest(),
            }
            conn.commit()
    final_evidence = require_playable(verifier, [row["video_id"] for row in result if row.get("video_id")])
    for video_id, identity in validated_identity.items():
        if not recording_identity_matches(identity, final_evidence[video_id], player=True):
            raise PlaybackBlocked(f"Live recording identity changed during validation: {video_id}")
    conn.commit()
    fresh_policies = {song_id: read_policy(song_id) for song_id in by_source}
    conn.commit()
    if (fresh_policies != policies or ALIASES.overrides != aliases
            or fingerprint(ALIASES.artist_map) != artist_alias_fingerprint
            or (alias_path.read_bytes() if alias_path.is_file() else None) != alias_file):
        raise PlaybackBlocked("Manual matching policy changed during playback validation")
    for match in result:
        if match.get("video_id"):
            match["playability_evidence"] = final_evidence[match["video_id"]]
            if match.get("canonical_decision"):
                match["canonical_decision"]["candidate_evidence"] = match["playability_evidence"]
    return result


def freeze_outputs(conn, tasks, *, history_date=None):
    """Read current completed source slots once; never use a crawler's stale ID list."""
    from hype_db_common import MATCHED_STATUSES, normalized_service
    from hype_db_reports import latest_hype_history_date, fetch_hype_rows_for_dates, build_hype_report_from_rows, contributing_hype_rows
    from ytmusic_playlist_sync import normalize_text

    outputs, inputs = [], []
    approved_changes = _approved_canonical_changes(conn)
    manual = [dict(row) for row in conn.execute("SELECT * FROM manual_overrides ORDER BY service,song_id").fetchall()]
    policies = {(row["service"], row["song_id"]): row for row in manual}
    alias_data = json.loads(Path(__file__).with_name("matching_alias.json").read_text())
    aliases = {"|".join(normalize_text(part) for part in key.split("|")): str(value)
               for key, value in alias_data.get("overrides", {}).items()}

    def verify_selection(row, service, job_name):
        if row["status"] is None:
            raise PlaybackBlocked(f"Completed snapshot lacks selection evidence: {job_name}:{row['song_id']}")
        if row["status"] not in MATCHED_STATUSES:
            return
        if not _has_approved_path(approved_changes, row["track_uid"], row["matched_video_id"], row["video_id"]):
            raise PlaybackBlocked(f"A stored selection changed without an approved decision: {job_name}:{row['song_id']}")
        policy = policies.get((service, row["song_id"]))
        if policy:
            selected = policy.get("canonical_yt_video_id")
            uid = policy.get("target_track_uid")
            if uid and not selected:
                target = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (uid,)).fetchone()
                selected = target[0] if target else None
            if (str(policy.get("action") or "").lower() in {"block", "manual_blocked"}
                    or (selected and selected != row["video_id"])
                    or (uid and uid != row["track_uid"])):
                raise PlaybackBlocked(f"Stored selection conflicts with current manual policy: {job_name}:{row['song_id']}")
        else:
            for suffix in ("", "_ko", "_en"):
                key = "|".join(normalize_text(row.get(field + suffix) or "") for field in ("title", "artist"))
                if aliases.get(key) and aliases[key] != row["video_id"]:
                    raise PlaybackBlocked(f"Stored selection conflicts with current alias policy: {job_name}:{row['song_id']}")
    for task in tasks:
        service = normalized_service(task.get("service") or task.get("type") or "")
        if service == "hypex":
            continue
        variant = "combined" if task.get("service") == "melon_gen" else "default"
        started_at = task.get("match_started_at")
        run_filter = " AND started_at=?" if started_at else ""
        run = conn.execute(
            "SELECT * FROM match_runs WHERE service=? AND job_name=? AND source_variant=? "
            "AND status='completed'" + run_filter + " ORDER BY reference_period DESC, created_at DESC LIMIT 1",
            (service, task["job_name"], variant, *([started_at] if started_at else [])),
        ).fetchone()
        if not run:
            raise PlaybackBlocked(f"No completed input for {task['job_name']}")
        rows = [dict(row) for row in conn.execute(
            "SELECT po.song_id,po.rank_order,p.track_uid,t.canonical_yt_video_id AS video_id,"
            "t.yt_title,t.yt_artist,t.yt_album,t.yt_metadata_verified_key,ma.status,ma.video_id AS matched_video_id "
            "FROM playlist_order po LEFT JOIN platform_song_ids p "
            "ON p.service=po.service AND p.song_id=po.song_id LEFT JOIN tracks t ON t.track_uid=p.track_uid "
            "LEFT JOIN match_attempts ma ON ma.run_id=? AND ma.service=po.service AND ma.song_id=po.song_id "
            "WHERE po.service=? AND po.job_name=? AND po.source_variant=? AND po.reference_period=? "
            "ORDER BY po.rank_order",
            (run["run_id"], service, task["job_name"], variant, run["reference_period"]),
        ).fetchall()]
        if not rows:
            raise PlaybackBlocked(f"Completed input has no raw slots: {task['job_name']}")
        for row in rows:
            verify_selection(row, service, task["job_name"])
        videos = list(dict.fromkeys(row["video_id"] for row in rows
                                   if row["video_id"] and row["status"] in MATCHED_STATUSES))
        if not videos:
            raise PlaybackBlocked(f"No verified matches for {task['job_name']}")
        if task.get("shuffle"):
            # Stable per-source shuffle keeps retries from changing the requested order.
            videos.sort(key=lambda vid: fingerprint([task["job_name"], run["reference_period"], vid]))
        inputs.append({"job_name": task["job_name"], "run_id": run["run_id"],
                       "reference_period": run["reference_period"], "rows": rows})
        outputs.append({"job_name": task["job_name"], "service": service,
                        "playlist_id": task["target_id"], "playlist_name": task.get("playlist_name", task["job_name"]),
                        "video_ids": videos})
    date = history_date or latest_hype_history_date(conn)
    if not date:
        raise PlaybackBlocked("No completed history anchor")
    hype_rows = contributing_hype_rows(
        [dict(row) for row in fetch_hype_rows_for_dates(conn, [date], include_unmatched=True).get(date, [])])
    for row in hype_rows:
        verify_selection(row, row["service"], row["job_name"])
    report = build_hype_report_from_rows(row for row in hype_rows if row["status"] in MATCHED_STATUSES)
    for task in tasks:
        if (task.get("service") or task.get("type")) == "hypex":
            outputs.append({"job_name": task["job_name"], "service": "hypex", "playlist_id": task["target_id"],
                            "playlist_name": task.get("playlist_name", task["job_name"]),
                            "video_ids": [row["video_id"] for row in report[:int(task.get("entity_limit") or 100)]]})
    policy_files = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                    for name in ("matching_alias.json", "ytmusic_validation_config.json")}
    snapshot = {"inputs": inputs, "outputs": outputs, "history_date": date, "report": report,
                "hype_rows": hype_rows, "manual_policy": manual, "policy_files": policy_files}
    return {**snapshot, "fingerprint": fingerprint(snapshot)}


def assert_frozen(conn, snapshot, tasks):
    fresh = freeze_outputs(conn, tasks, history_date=snapshot["history_date"])
    if fresh["fingerprint"] != snapshot["fingerprint"]:
        raise PlaybackBlocked("Source, representative recording or manual policy changed after snapshot validation")
