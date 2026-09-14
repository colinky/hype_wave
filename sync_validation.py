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


def verifier_for(client: Any, *, fresh: bool = False):
    from ytmusic_playability import PlayabilityVerifier

    cached = vars(client).get("_hype_playability_verifier")
    if cached is not None and not fresh:
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


def require_search_budget(verifier, *, wait_seconds=0):
    """Stop search work before an API call or delay can outlive this run."""
    if verifier is None:
        return
    require_playable(verifier, [])
    if hasattr(verifier, "deadline") and verifier.deadline - verifier.clock() <= wait_seconds:
        raise PlaybackBlocked("YouTube Music search time budget exhausted")


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


def _guest_credits(title):
    """Keep each named guest paired with their explicitly asserted role."""
    from ytmusic_playlist_sync import ALIASES, normalize_text, split_artist_names

    marker = r"(?:featuring|feat|ft|narration|narr)"
    delimiter = r"(?:[.:]\s*|\s+)"
    pattern = rf"\b({marker}){delimiter}(.*?)(?=[\)\]]|\b{marker}{delimiter}|$)"
    credits = set()
    for match in re.finditer(pattern, title, re.I):
        role = "narr" if match.group(1).lower().startswith("narr") else "feat"
        names = split_artist_names(match.group(2).strip(" ,&:"))
        if not names:
            credits.add((role, ""))
        for name in names:
            variants = ALIASES.get_variants(name, "artist")
            person = min(normalize_text(value).replace(" ", "") for value in variants)
            credits.add((role, person))
    return credits


def _feature_names(title):
    return {person for _, person in _guest_credits(title)}


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


def _same_video_verified_player_artist(metadata, observed, left_name, right_name):
    """Corroborate literal player spelling using one verified artist of this video."""
    video_id = metadata.get("video_id")
    artist_ids = metadata.get("artist_ids")
    linked = metadata.get("artist_names_by_id")
    if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or observed.get("video_id") != video_id or observed.get("exact_id") is not True
            or metadata.get("artist_identity_complete") is not True
            or not isinstance(artist_ids, list) or len(artist_ids) != 1
            or not isinstance(artist_ids[0], str)
            or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", artist_ids[0])
            or not isinstance(linked, dict) or set(linked) != {artist_ids[0]}):
        return False
    names = linked[artist_ids[0]]
    return (isinstance(names, list) and all(isinstance(name, str) and name.strip() for name in names)
            and left_name in names and right_name in names
            and all(row[field] in names for row in (metadata, observed)
                    for field in ("artist", "artist_ko", "artist_en") if field in row))


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
    left_roles = assertions(left_rows, 0, lambda title: frozenset(_guest_credits(title)))
    right_roles = assertions(right_rows, 0, lambda title: frozenset(_guest_credits(title)))
    left_features = assertions(left_rows, 0, lambda title: frozenset(_feature_names(title)))
    right_features = assertions(right_rows, 0, lambda title: frozenset(_feature_names(title)))
    left_albums = {_album_version(row[2]) for row in left_rows if row[2]}
    right_albums = {_album_version(row[2]) for row in right_rows if row[2]}
    if any(len(values) > 1 for values in (left_versions, right_versions, left_ratings, right_ratings,
                                          left_features, right_features, left_roles, right_roles, left_albums, right_albums)):
        return False
    for lt, la, _ in left_rows:
        for rt, _, _ in right_rows:
            left_credits, right_credits = _guest_credits(lt), _guest_credits(rt)
            if any(not name for _, name in left_credits | right_credits):
                return False
            if left_credits == right_credits or (player and not right_credits):
                continue
            # Omitted feature labels still require that locale's source artist
            # credits. A narrator role must be explicit, except in the player.
            if not left_credits and right_credits and all(role == "feat" for role, _ in right_credits):
                declared = set()
                for name in artist_variants(la):
                    declared |= _feature_names("Song (feat. " + name + ")")
                if {person for _, person in right_credits} <= declared:
                    continue
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
                if not (player and _same_video_verified_player_artist(left, right, la, ra)):
                    continue
            return True
    return False


def proven_source_identity(source, service, policy):
    """Apply an explicitly reviewed source-credit omission, bound to exact metadata.

    This supplies identity evidence only. It neither selects a YouTube ID nor
    changes source rows, attempts, playback status, or canonical ownership.
    """
    proof = policy.get("source_identity_evidence", {}).get(f"{service}:{source.get('song_id')}")
    if not proof or proof.get("kind") not in {"official_cross_service_credit", "official_cross_service_title_translation"}:
        return source
    def exact_text(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    expected = {tuple(exact_text(value) for value in variant)
                for variant in proof.get("source_variants", []) if len(variant) == 3}
    actual = _identity_variants(source)
    if (not actual or not all(tuple(exact_text(value) for value in row) in expected for row in actual)
            or not proof.get("references") or not proof.get("recording")):
        return source
    if source.get("length_seconds") is not None:
        try:
            length = float(source["length_seconds"])
            if not math.isfinite(length) or abs(length - float(proof["recording"]["length_seconds"])) > 2:
                return source
        except (TypeError, ValueError, KeyError):
            return source
    return {**source, **proof["recording"]}


def source_recording_matches(source, metadata, *, service=None, policy=None):
    """Shared identity boundary for a fresh source and an exact recording."""
    from hype_db_common import normalized_service

    if policy is None:
        policy = json.loads(Path(__file__).with_name("matching_alias.json").read_text())
    service = normalized_service(service or source.get("service") or "")
    proven = proven_source_identity(source, service, policy)
    proof = policy.get("source_identity_evidence", {}).get(f"{service}:{source.get('song_id')}") or {}
    if proven != source and (proof.get("kind") == "official_cross_service_title_translation" or "candidate" in proof):
        expected = proof.get("candidate") or {}
        # Any proof carrying a candidate is scoped to that exact recording.
        # Existing credit proofs without a candidate keep their prior contract.
        try:
            length = float(metadata["length_seconds"])
            duration_matches = math.isfinite(length) and abs(length - float(expected["length_seconds"])) <= 2
        except (KeyError, TypeError, ValueError):
            duration_matches = False
        if (not expected.get("video_id")
                or not duration_matches
                or any(metadata.get(key) != value for key, value in expected.items() if key != "length_seconds")):
            proven = source
    if recording_identity_matches(proven, metadata):
        return True
    # An album may contain the original alongside separately named remixes.
    # Only reviewed exact source/recording pairs may ignore that album label;
    # every title, performer, feature, rating and duration check still applies.
    def exact_variants(row):
        return {tuple(" ".join(unicodedata.normalize("NFKC", value).casefold().split())
                      for value in variant) for variant in _identity_variants(row)}
    def without_album(row):
        return {**row, **{field: "" for field in ("album", "album_ko", "album_en")}}
    for proof in policy.get("recording_release_evidence", []):
        if (proof.get("kind") != "official_recording_release_equivalence"
                or str(source.get("song_id")) not in proof.get("sources", {}).get(service, [])
                or metadata.get("video_id") != proof.get("recording", {}).get("video_id")):
            continue
        expected = proof["recording"]
        allowed = {tuple(" ".join(unicodedata.normalize("NFKC", value).casefold().split())
                         for value in variant) for variant in proof.get("source_variants", [])}
        references = proof.get("references", [])
        isrc = proof.get("catalog_isrc")
        if (not exact_variants(source) or not exact_variants(source) <= allowed
                or any(metadata.get(key) != value for key, value in expected.items()
                       if key.startswith(("title", "artist", "album")) and key not in {"title", "artist", "album"})
                # The authenticated client's language chooses the default
                # display fields. Both exact locale observations stay fixed.
                or any(not all(field + suffix in expected for suffix in ("_ko", "_en"))
                       or metadata.get(field) not in (expected[field + "_ko"], expected[field + "_en"])
                       for field in ("title", "artist", "album"))
                or metadata.get("length_seconds") != expected.get("length_seconds")
                or not isinstance(expected.get("length_seconds"), (float, int))
                or not isrc or len(references) < 2
                or any(ref.get("isrc") != isrc or not ref.get("url") for ref in references)):
            continue
        if ({_rating_version(row[0]) for row in _identity_variants(proven)}
                != {_rating_version(row[0]) for row in _identity_variants(metadata)}):
            continue
        comparison_source = proven
        source_references = [ref for ref in references
                             if ref.get("catalog_song_id") == str(source.get("song_id"))]
        if source_references:
            # Do not treat a known catalog duration as missing just because the
            # chart collector does not expose duration on its source row.
            durations = [ref.get("duration_ms") for ref in source_references]
            if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    for value in durations) or len(set(durations)) != 1):
                continue
            if source.get("length_seconds") is None:
                comparison_source = {**proven, "length_seconds": durations[0] / 1000}
        pair = proof.get("catalog_duration_pair")
        if pair is not None:
            # An explicit reviewed catalog pair may differ in reported duration.
            # Only that pair bypasses source→watch duration; all other identity
            # assertions and the reference→watch duration bound remain active.
            try:
                source_ms = pair["source_duration_ms"]
                reference_ms = pair["recording_duration_ms"]
                candidate_seconds = float(metadata["length_seconds"])
                if (pair.get("source_service") != service
                        or pair.get("source_song_id") != str(source.get("song_id"))
                        or pair.get("recording_video_id") != metadata.get("video_id")
                        or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                               for value in (source_ms, reference_ms))
                        or not math.isfinite(candidate_seconds)
                        or abs(reference_ms / 1000 - candidate_seconds) > 2
                        or (source.get("length_seconds") is not None
                            and float(source["length_seconds"]) != source_ms / 1000)
                        or not any(ref.get("url") == pair.get("source_url")
                                   and ref.get("duration_ms") == source_ms for ref in source_references)
                        or not any(ref.get("url") == pair.get("recording_reference_url")
                                   and ref.get("catalog_song_id") == pair.get("recording_song_id")
                                   and ref.get("duration_ms") == reference_ms for ref in references)):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            comparison_source = {key: value for key, value in comparison_source.items() if key != "length_seconds"}
        if recording_identity_matches(without_album(comparison_source), without_album(metadata)):
            return True
    return False


def validate_matches(conn, *, service, sources, matches, client, verifier=None):
    """Finish all network checks before raw data or matching results are written."""
    from hype_db_common import normalized_service, row_dict
    from hype_db_store import get_bulk_cached_matches, manual_override, find_track_by_video
    from ytmusic_playlist_sync import ALIASES, get_verified_video_metadata, normalize_text, _watch_playlist_for_metadata

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
    native_identity = {}

    def partial_credits_match(source, metadata):
        if metadata.get("artist_identity_complete") is not False:
            return True
        from ytmusic_playlist_sync import split_artist_names
        def names(value):
            return {normalize_text(name).replace(" ", "") for name in ALIASES.get_variants(value, "artist")}
        required = [names(name) for name in metadata.get("unlinked_artist_names") or ()]
        linked = metadata.get("artist_names_by_id") or {}
        for artist_id in metadata.get("artist_ids") or ():
            variants = linked.get(artist_id) or ()
            if not variants:
                return False
            required.append(set().union(*(names(name) for name in variants)))
        if not required or any(not group for group in required):
            return False
        # Every observed performer, linked or unlinked, needs an explicit
        # source credit in each locale. Same-ID locale names may corroborate it.
        for title, artist, _ in _identity_variants(source):
            credits = set().union(*(names(name) for name in split_artist_names(artist)))
            credits |= _feature_names(title)
            if any(not group & credits for group in required):
                return False
        return bool(_identity_variants(source))
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
        keeping_native = (not manual and service == "ytmusic" and song_id == old == selected
                          and str(prior.get("yt_title") or "").strip()
                          and str(prior.get("yt_artist") or "").strip()
                          and old_evidence.get("state") == "playable")
        trusted = cached.get(song_id, {})
        if old and selected != old and not manual:
            if old_evidence.get("state") == "unknown":
                raise PlaybackBlocked(f"Existing recording is uncertain: {old}")
            # Direct native identity authorizes preservation, not a claim that
            # another selected video contains the same recording.
            if trusted.get("video_id") != old or trusted.get("cache_origin") == "native_source_id":
                old_metadata = get_verified_video_metadata(
                    client, old, metadata_cache=metadata_cache,
                    allow_partial_artist_ids=old_evidence.get("state") == "playable")
                if (not old_metadata or old_metadata.get("video_id") != old
                        or not partial_credits_match(source, old_metadata)
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
        if keeping_native:
            match.pop("canonical_decision", None)
            # A native source ID already identifies this exact existing video.
            # Charts may shorten upload titles or label a performance "Live".
            # Preserve its stored identity; this cannot approve another ID.
            with verifier._bounded_session():
                watch = _watch_playlist_for_metadata(client, selected)
            rows = watch.get("tracks") if isinstance(watch, dict) else None
            exact = [row for row in rows if isinstance(row, dict) and row.get("videoId") == selected] if isinstance(rows, list) else []
            if len(exact) != 1 or exact[0].get("isAvailable") is False:
                raise PlaybackBlocked(f"Native recording availability is inconclusive: {selected}")
            match.update(yt_title=prior["yt_title"], yt_artist=prior["yt_artist"], yt_album=prior["yt_album"],
                         status="cached_match", query="preserved_exact_native_id")
            native_identity[selected] = {key: selected_evidence.get(key) for key in ("title", "artist", "music_video_type")}
        elif not manual:
            # get_song can omit credits that its exact watch metadata exposes.
            # A trusted cache must not hide that live contradiction (Dirty Work).
            keeping_healthy = selected == old and old_evidence.get("state") == "playable"
            metadata = get_verified_video_metadata(
                client, selected, metadata_cache=metadata_cache,
                allow_partial_artist_ids=keeping_healthy)
            if (not metadata or metadata.get("video_id") != selected
                    or (metadata.get("artist_identity_complete") is False and not keeping_healthy)
                    or not partial_credits_match(source, metadata)
                    or not source_recording_matches(source, metadata, service=service, policy=identity_policy)
                    or not recording_identity_matches(metadata, selected_evidence, player=True)):
                raise PlaybackBlocked(f"Selected recording does not match the source identity: {selected}")
            match.update(yt_title=metadata.get("title", ""), yt_artist=metadata.get("artist", ""),
                         yt_album=metadata.get("album", ""))
        if not manual and not keeping_native:
            validated_identity[selected] = metadata or selected_evidence
        if old and old != selected and not manual:
            metadata = metadata or get_verified_video_metadata(client, selected, metadata_cache=metadata_cache)
            if (not metadata or metadata.get("video_id") != selected
                    or metadata.get("artist_identity_complete") is False):
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
    for video_id, identity in native_identity.items():
        final_evidence[video_id] = require_playable(verifier, [video_id], force=True)[video_id]
        if any(final_evidence[video_id].get(key) != value for key, value in identity.items()):
            raise PlaybackBlocked(f"Native recording changed during validation: {video_id}")
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
