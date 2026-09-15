from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from hype_db_common import (
    clean_track_title,
    compact_metadata_key,
    feature_signature,
    has_feature_mismatch,
    has_version_mismatch,
    infer_album_id,
    legacy_to_job_name,
    match_method_for_status,
    metadata_key,
    normalize_song_id,
    normalize_source_variant,
    normalize_text,
    normalized_service,
    reference_period_for_date,
    require_job_name,
    row_dict,
    stable_uid,
    strip_parens_from_title,
    utc_now_iso,
)
from hype_db_schema import connect, init_db, init_schema

LOG = logging.getLogger("hype_db")
__all__ = [
    "find_track_by_service_song",
    "find_track_by_video",
    "find_track_by_metadata",
    "manual_override",
    "ensure_track",
    "record_conflict",
    "upsert_track_list_metadata",
    "track_list_metadata_params",
    "metadata_lookup_params",
    "upsert_metadata_lookup",
    "resolve_track_uid",
    "upsert_track_match",
    "start_match_run",
    "record_match_attempt",
    "record_match_candidates",
    "cleanup_old_attempts_and_candidates",
    "get_expected_track_count",
    "persist_crawled_tracks",
    "persist_crawl_run",
    "repair_failed_source_bindings",
    "record_source_song_relation",
    "consolidate_source_song_relation",
    "repair_source_song_relations",
    "record_playlist_update",
    "append_playlist_update_evidence",
    "claim_playlist_update_recovery",
    "finish_playlist_update",
    "get_playlist_update_run",
    "get_pending_playlist_recovery",
    "get_bulk_cached_matches",
]

YOUTUBE_ATV_RELATION = "youtube_charts_atv_external_video_id"
PLAYLIST_UPDATE_STATUSES = {
    "running",
    "published",
    "skipped_current",
    "verification_failed",
    "mutation_failed",
    "restored",
    "recovery_required",
}
ACTIVE_PLAYLIST_UPDATE_STATUSES = {
    "running",
    "mutation_failed",
    "recovery_required",
}
PLAYLIST_EVIDENCE_PHASES = {"publish", "restore", "reconcile", "reconcile_tail"}
PLAYLIST_EVIDENCE_OPERATIONS = {"remove", "add", "observe", "finalize"}
PLAYLIST_EVIDENCE_STATES = {"intent", "ack", "ambiguous", "verified"}


def _yt_metadata_verified_key(video_id: str, title: str, artist: str, album: str) -> str:
    payload = json.dumps(
        [str(video_id or ""), str(title or ""), str(artist or ""), str(album or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def find_track_by_service_song(conn: sqlite3.Connection, service: str, song_id: str) -> str | None:
    if not service or not song_id:
        return None
    row = conn.execute(
        "SELECT track_uid FROM platform_song_ids WHERE service = ? AND song_id = ?",
        (normalized_service(service), song_id),
    ).fetchone()
    return row["track_uid"] if row else None


def find_track_by_video(conn: sqlite3.Connection, video_id: str | None) -> str | None:
    if not video_id:
        return None
    row = conn.execute("SELECT track_uid FROM yt_video_ids WHERE video_id = ?", (video_id,)).fetchone()
    return row["track_uid"] if row else None


def find_track_by_metadata(conn: sqlite3.Connection, title: str, artist: str, album: str = "") -> str | None:
    keys = [metadata_key(title, artist, album), compact_metadata_key(title, artist)]
    # Fallback 1: strip performance variant suffixes (Live, MV, Acoustic …)
    # e.g. '소문의 낙원 (Live)' → '소문의 낙원'
    cleaned = clean_track_title(title)
    if cleaned != title:
        keys.append(compact_metadata_key(cleaned, artist))
        if album:
            keys.append(metadata_key(cleaned, artist, album))
    # Fallback 2: strip ALL parenthetical content from the *query* title
    # e.g. 'KISS KISS KISS' matches index key for 'KISS KISS KISS (Prod. by Hukky Shibaseki)'
    stripped_title = strip_parens_from_title(title)
    if stripped_title != title and stripped_title != cleaned:
        keys.append(compact_metadata_key(stripped_title, artist))
        if album:
            keys.append(metadata_key(stripped_title, artist, album))
    # Fallback 3: strip parenthetical content from the *artist*
    # e.g. Melon stores 'LE SSERAFIM (르세라핌)' — strip → 'LE SSERAFIM'
    # which matches Apple's existing 'boompala|le sserafim' index key.
    stripped_artist = strip_parens_from_title(artist)
    if stripped_artist != artist:
        keys.append(compact_metadata_key(title, stripped_artist))
        keys.append(compact_metadata_key(cleaned, stripped_artist))
        if album:
            keys.append(metadata_key(title, stripped_artist, album))
    for key in keys:
        row = conn.execute(
            "SELECT track_uid FROM metadata_lookup_index WHERE lookup_key = ?",
            (key,),
        ).fetchone()
        if row:
            return row["track_uid"]
    return None



def manual_override(conn: sqlite3.Connection, service: str, song_id: str) -> sqlite3.Row | None:
    if not service or not song_id:
        return None
    return conn.execute(
        "SELECT * FROM manual_overrides WHERE service = ? AND song_id = ?",
        (normalized_service(service), song_id),
    ).fetchone()


def _canonical_track_values(
    existing: Any,
    *,
    video_id: str | None,
    yt_title: str = "",
    yt_artist: str = "",
    yt_album: str = "",
    allow_switch: bool = False,
) -> tuple[str | None, str, str, str, bool]:
    current = dict(existing) if existing else {}
    current_video = str(current.get("canonical_yt_video_id") or "")
    incoming_video = str(video_id or "")
    unsupported_change = bool(
        current_video and incoming_video and current_video != incoming_video and not allow_switch
    )
    if unsupported_change:
        return (
            current_video,
            str(current.get("yt_title") or ""),
            str(current.get("yt_artist") or ""),
            str(current.get("yt_album") or ""),
            False,
        )

    effective_video = incoming_video or current_video or None
    switching = bool(current_video and incoming_video and current_video != incoming_video)
    if switching:
        return effective_video, str(yt_title or ""), str(yt_artist or ""), str(yt_album or ""), True
    return (
        effective_video,
        str(yt_title or current.get("yt_title") or ""),
        str(yt_artist or current.get("yt_artist") or ""),
        str(yt_album or current.get("yt_album") or ""),
        True,
    )


def _sync_canonical_video_flags(conn: Any, track_uids: Iterable[str]) -> None:
    """Make each touched UID's video flags match its persisted canonical ID."""
    uids = list(dict.fromkeys(str(uid) for uid in track_uids if uid))
    if not uids:
        return

    canonical_by_uid: dict[str, str] = {}
    for start in range(0, len(uids), 500):
        chunk = uids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT track_uid, canonical_yt_video_id FROM tracks "
            f"WHERE track_uid IN ({placeholders})",
            chunk,
        ).fetchall()
        canonical_by_uid.update({
            str(row["track_uid"]): str(row["canonical_yt_video_id"] or "")
            for row in rows
        })

    claimed_by_uid: dict[str, str] = {}
    for uid, video_id in canonical_by_uid.items():
        if not video_id:
            continue
        other_uid = claimed_by_uid.setdefault(video_id, uid)
        if other_uid != uid:
            raise ValueError(
                f"Canonical video {video_id} is claimed by multiple tracks"
            )

    video_ids = list(claimed_by_uid)
    existing_owners: dict[str, str] = {}
    for start in range(0, len(video_ids), 500):
        chunk = video_ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT video_id, track_uid FROM yt_video_ids "
            f"WHERE video_id IN ({placeholders})",
            chunk,
        ).fetchall()
        existing_owners.update({
            str(row["video_id"]): str(row["track_uid"])
            for row in rows
        })
    for video_id, uid in claimed_by_uid.items():
        owner = existing_owners.get(video_id)
        if owner and owner != uid:
            raise ValueError(
                f"Canonical video {video_id} is already owned by another track"
            )

    if video_ids:
        conn.executemany(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                is_canonical = CASE
                    WHEN yt_video_ids.track_uid = excluded.track_uid THEN 1
                    ELSE yt_video_ids.is_canonical
                END
            """,
            [(video_id, uid) for video_id, uid in claimed_by_uid.items()],
        )
        # A concurrent insert can win between the owner check and this upsert.
        for start in range(0, len(video_ids), 500):
            chunk = video_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT video_id, track_uid FROM yt_video_ids "
                f"WHERE video_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                video_id = str(row["video_id"])
                if str(row["track_uid"]) != claimed_by_uid[video_id]:
                    raise ValueError(
                        f"Canonical video {video_id} is already owned by another track"
                    )

    for uid in uids:
        video_id = canonical_by_uid.get(uid, "")
        conn.execute(
            "UPDATE yt_video_ids "
            "SET is_canonical = CASE WHEN video_id = ? THEN 1 ELSE 0 END "
            "WHERE track_uid = ?",
            (video_id, uid),
        )


def ensure_track(
    conn: sqlite3.Connection,
    *,
    track_uid: str,
    video_id: str | None = None,
    yt_title: str = "",
    yt_artist: str = "",
    yt_album: str = "",
    status: str = "unmatched",
    score: float = 0.0,
) -> str:
    now = utc_now_iso()
    existing = conn.execute(
        """
        SELECT canonical_yt_video_id, yt_title, yt_artist, yt_album,
               match_status, best_score
        FROM tracks WHERE track_uid = ?
        """,
        (track_uid,),
    ).fetchone()
    effective_video, yt_title, yt_artist, yt_album, identity_supported = _canonical_track_values(
        existing,
        video_id=video_id,
        yt_title=yt_title,
        yt_artist=yt_artist,
        yt_album=yt_album,
    )
    persisted_status = status
    persisted_score = score
    if existing and not identity_supported:
        persisted_status = str(existing["match_status"] or status)
        persisted_score = float(existing["best_score"] or 0)
    conn.execute(
        """
        INSERT INTO tracks (
            track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
            match_status, best_score, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(track_uid) DO UPDATE SET
            canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
            yt_title = CASE
                WHEN tracks.canonical_yt_video_id IS NULL
                  OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                THEN COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title)
                ELSE tracks.yt_title
            END,
            yt_artist = CASE
                WHEN tracks.canonical_yt_video_id IS NULL
                  OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                THEN COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist)
                ELSE tracks.yt_artist
            END,
            yt_album = CASE
                WHEN tracks.canonical_yt_video_id IS NULL
                  OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                THEN COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album)
                ELSE tracks.yt_album
            END,
            match_status = CASE
                WHEN (tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id)
                 AND excluded.match_status != 'failed' THEN excluded.match_status
                ELSE tracks.match_status
            END,
            best_score = CASE
                WHEN (tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id)
                 AND COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0)
                THEN COALESCE(excluded.best_score, 0)
                ELSE COALESCE(tracks.best_score, 0)
            END,
            updated_at = CASE
                WHEN tracks.canonical_yt_video_id IS NULL
                  OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                THEN excluded.updated_at
                ELSE tracks.updated_at
            END
        """,
        (
            track_uid, effective_video, yt_title, yt_artist, yt_album,
            persisted_status, persisted_score, now, now,
        ),
    )
    _sync_canonical_video_flags(conn, [track_uid])
    return track_uid


def record_conflict(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    existing_track_uid: str | None,
    incoming_track_uid: str | None,
    existing_video_id: str | None,
    incoming_video_id: str | None,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> None:
    seed = "|".join(
        [
            normalized_service(service),
            song_id,
            existing_video_id or "",
            incoming_video_id or "",
            reason,
        ]
    )
    now = utc_now_iso()
    payload = payload or {}
    conn.execute(
        """
        INSERT INTO review_conflicts(
            conflict_id, service, song_id, job_name, source_variant, reference_period, title, artist,
            album, query, score, source_file, existing_track_uid, incoming_track_uid,
            existing_video_id, incoming_video_id, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (conflict_id) DO NOTHING
        """,
        (
            hashlib.sha1(seed.encode("utf-8")).hexdigest(),
            normalized_service(service),
            song_id,
            payload.get("job_name") or legacy_to_job_name(payload.get("playlist_name") or payload.get("chart_type") or ""),
            normalize_source_variant(payload.get("source_variant")),
            reference_period_for_date(
                payload.get("job_name") or legacy_to_job_name(payload.get("playlist_name") or payload.get("chart_type") or ""),
                payload.get("reference_period") or payload.get("chart_period") or payload.get("extracted_at") or payload.get("crawl_time") or "",
            ),
            payload.get("title") or payload.get("title_ko") or payload.get("title_en") or "",
            payload.get("artist") or payload.get("artist_ko") or payload.get("artist_en") or "",
            payload.get("album") or payload.get("album_ko") or payload.get("album_en") or "",
            payload.get("query") or "",
            float(payload.get("score") or 0),
            payload.get("source_file") or "",
            existing_track_uid,
            incoming_track_uid,
            existing_video_id,
            incoming_video_id,
            reason,
            now,
        ),
    )


def upsert_track_list_metadata(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    track_uid: str,
    row: dict[str, Any],
    locale: str = "",
    bind_source_id: bool = True,
) -> None:
    if bind_source_id:
        conn.execute(
            """
            INSERT INTO platform_song_ids(service, song_id, track_uid)
            VALUES (?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                track_uid = excluded.track_uid
            """,
            (normalized_service(service), song_id, track_uid),
        )
    conn.execute(
        """
        INSERT INTO track_list(
            service, song_id, album_id, title_ko, artist_ko, album_ko,
            title_en, artist_en, album_en, artwork_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service, song_id) DO UPDATE SET
            album_id = COALESCE(NULLIF(excluded.album_id, ''), track_list.album_id),
            title_ko = COALESCE(NULLIF(excluded.title_ko, ''), track_list.title_ko),
            artist_ko = COALESCE(NULLIF(excluded.artist_ko, ''), track_list.artist_ko),
            album_ko = COALESCE(NULLIF(excluded.album_ko, ''), track_list.album_ko),
            title_en = COALESCE(NULLIF(excluded.title_en, ''), track_list.title_en),
            artist_en = COALESCE(NULLIF(excluded.artist_en, ''), track_list.artist_en),
            album_en = COALESCE(NULLIF(excluded.album_en, ''), track_list.album_en),
            artwork_url = COALESCE(NULLIF(excluded.artwork_url, ''), track_list.artwork_url)
        """,
        track_list_metadata_params(service=service, song_id=song_id, row=row, locale=locale),
    )


def _localized_track_fields(
    *,
    service: str,
    row: dict[str, Any],
    locale: str = "",
) -> tuple[str, str, str, str, str, str]:
    service = normalized_service(service)
    source_locale = str(row.get("locale") or locale or "").strip().lower()
    if not source_locale:
        source_locale = "ko" if service == "melon" else "en"

    title = str(row.get("title") or "").strip()
    artist = str(row.get("artist") or "").strip()
    album = str(row.get("album") or "").strip()
    title_ko = str(row.get("title_ko") or "").strip()
    artist_ko = str(row.get("artist_ko") or "").strip()
    album_ko = str(row.get("album_ko") or "").strip()
    title_en = str(row.get("title_en") or "").strip()
    artist_en = str(row.get("artist_en") or "").strip()
    album_en = str(row.get("album_en") or "").strip()

    if source_locale.startswith("ko"):
        title_ko = title_ko or title
        artist_ko = artist_ko or artist
        album_ko = album_ko or album
    elif source_locale.startswith("en"):
        title_en = title_en or title
        artist_en = artist_en or artist
        album_en = album_en or album

    return title_ko, artist_ko, album_ko, title_en, artist_en, album_en


def track_list_metadata_params(
    *,
    service: str,
    song_id: str,
    row: dict[str, Any],
    locale: str = "",
) -> tuple[Any, ...]:
    service = normalized_service(service)
    title_ko, artist_ko, album_ko, title_en, artist_en, album_en = _localized_track_fields(
        service=service,
        row=row,
        locale=locale,
    )
    return (
        service,
        song_id,
        infer_album_id(service, row),
        title_ko,
        artist_ko,
        album_ko,
        title_en,
        artist_en,
        album_en,
        str(row.get("artwork_url") or "").strip(),
    )


def _metadata_variants(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    candidates = [
        (row.get("title"), row.get("artist"), row.get("album")),
        (row.get("title_en"), row.get("artist_en"), row.get("album_en")),
        (row.get("title_ko"), row.get("artist_ko"), row.get("album_ko")),
    ]
    variants: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for title, artist, album in candidates:
        if not title or not artist:
            continue
        variant = (str(title).strip(), str(artist).strip(), str(album or "").strip())
        normalized = tuple(normalize_text(value) for value in variant)
        if normalized in seen:
            continue
        seen.add(normalized)
        variants.append(variant)
    return variants


def _metadata_lookup_key_scores(row: dict[str, Any], score: float) -> list[tuple[str, float]]:
    key_scores: dict[str, float] = {}
    for title, artist, album in _metadata_variants(row):
        full_key = metadata_key(title, artist, album)
        compact_key = compact_metadata_key(title, artist)
        key_score_pairs = [(full_key, score)]
        if compact_key != full_key:
            key_score_pairs.append((compact_key, score * 0.8))
        stripped_title = strip_parens_from_title(title)
        if stripped_title != title and stripped_title:
            stripped_full_key = metadata_key(stripped_title, artist, album)
            stripped_compact_key = compact_metadata_key(stripped_title, artist)
            key_score_pairs.append((stripped_compact_key, score * 0.6))
            if stripped_full_key != stripped_compact_key:
                key_score_pairs.append((stripped_full_key, score * 0.6))
        stripped_artist = strip_parens_from_title(artist)
        if stripped_artist != artist and stripped_artist:
            key_score_pairs.append((compact_metadata_key(title, stripped_artist), score * 0.6))
            if stripped_title != title:
                key_score_pairs.append((compact_metadata_key(stripped_title, stripped_artist), score * 0.6))
        for key, effective_score in key_score_pairs:
            if key.strip("|"):
                key_scores[key] = max(key_scores.get(key, 0.0), effective_score)
    return list(key_scores.items())


def metadata_lookup_params(*, track_uid: str, row: dict[str, Any], source: str, score: float) -> list[tuple[Any, ...]]:
    return [
        (key, track_uid, source, effective_score)
        for key, effective_score in _metadata_lookup_key_scores(row, score)
    ]


def upsert_metadata_lookup(conn: sqlite3.Connection, *, track_uid: str, row: dict[str, Any], source: str, score: float) -> None:
    for params in metadata_lookup_params(track_uid=track_uid, row=row, source=source, score=score):
        conn.execute(
            """
            INSERT INTO metadata_lookup_index(lookup_key, track_uid, source, score)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                track_uid = CASE
                    WHEN excluded.score >= metadata_lookup_index.score THEN excluded.track_uid
                    ELSE metadata_lookup_index.track_uid
                END,
                source = CASE
                    WHEN excluded.score >= metadata_lookup_index.score THEN excluded.source
                    ELSE metadata_lookup_index.source
                END,
                score = CASE WHEN excluded.score > metadata_lookup_index.score THEN excluded.score ELSE metadata_lookup_index.score END
            """,
            params,
        )


def record_source_song_relation(
    conn: Any,
    *,
    service: str,
    source_song_id: str,
    relation_type: str,
    related_service: str,
    related_song_id: str,
    evidence_source: str,
    reference_period: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Store compact source evidence without silently changing an existing target."""
    service = normalized_service(service)
    related_service = normalized_service(related_service)
    source_song_id = str(source_song_id or "").strip()
    related_song_id = str(related_song_id or "").strip()
    relation_type = str(relation_type or "").strip()
    evidence_source = str(evidence_source or "").strip()
    reference_period = str(reference_period or "").strip()
    if not all((service, source_song_id, relation_type, related_service, related_song_id, evidence_source, reference_period)):
        raise ValueError("A source-song relation requires complete IDs, provenance, and reference period.")
    if relation_type == YOUTUBE_ATV_RELATION:
        video_id_pattern = r"[A-Za-z0-9_-]{11}"
        if not re.fullmatch(video_id_pattern, source_song_id) or not re.fullmatch(video_id_pattern, related_song_id):
            raise ValueError("YouTube Charts ATV relations require valid 11-character video IDs.")

    existing = conn.execute(
        """
        SELECT * FROM source_song_relations
        WHERE service = ? AND source_song_id = ? AND relation_type = ?
        """,
        (service, source_song_id, relation_type),
    ).fetchone()
    if existing and (
        existing["related_service"] != related_service
        or existing["related_song_id"] != related_song_id
    ):
        conflict_payload = dict(payload or {})
        conflict_payload.setdefault("reference_period", reference_period)
        conflict_payload.setdefault("query", evidence_source)
        record_conflict(
            conn,
            service=service,
            song_id=source_song_id,
            existing_track_uid=find_track_by_service_song(conn, service, source_song_id),
            incoming_track_uid=find_track_by_service_song(conn, related_service, related_song_id),
            existing_video_id=str(existing["related_song_id"] or ""),
            incoming_video_id=related_song_id,
            reason="source_song_relation_target_changed",
            payload=conflict_payload,
        )
        return "conflict"

    now = utc_now_iso()
    if existing:
        first_seen = min(
            value
            for value in (str(existing["first_seen_reference_period"] or ""), reference_period)
            if value
        )
        last_seen = max(
            value
            for value in (str(existing["last_seen_reference_period"] or ""), reference_period)
            if value
        )
        if (
            str(existing["evidence_source"] or "") != evidence_source
            or str(existing["first_seen_reference_period"] or "") != first_seen
            or str(existing["last_seen_reference_period"] or "") != last_seen
        ):
            conn.execute(
                """
                UPDATE source_song_relations
                SET evidence_source = ?, first_seen_reference_period = ?,
                    last_seen_reference_period = ?, updated_at = ?
                WHERE service = ? AND source_song_id = ? AND relation_type = ?
                """,
                (
                    evidence_source,
                    first_seen,
                    last_seen,
                    now,
                    service,
                    source_song_id,
                    relation_type,
                ),
            )
        return "confirmed"

    conn.execute(
        """
        INSERT INTO source_song_relations(
            service, source_song_id, relation_type, related_service,
            related_song_id, evidence_source, first_seen_reference_period,
            last_seen_reference_period, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            service,
            source_song_id,
            relation_type,
            related_service,
            related_song_id,
            evidence_source,
            reference_period,
            reference_period,
            now,
        ),
    )
    return "inserted"


def _track_metadata_rows(conn: Any, track_uid: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT tl.*
            FROM platform_song_ids ps
            JOIN track_list tl
              ON tl.service = ps.service
             AND tl.song_id = ps.song_id
            WHERE ps.track_uid = ?
            ORDER BY CASE tl.service WHEN 'apple' THEN 0 WHEN 'melon' THEN 1 ELSE 2 END,
                     tl.song_id
            """,
            (track_uid,),
        ).fetchall()
    ]


def _metadata_rows_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Require title, artist, and recording-version agreement across any locale pair."""
    from ytmusic_playlist_sync import similarity

    left_variants = _metadata_variants(left)
    right_variants = _metadata_variants(right)
    if not left_variants or not right_variants:
        return False
    for left_title, left_artist, left_album in left_variants:
        for right_title, right_artist, right_album in right_variants:
            if has_feature_mismatch(left_title, right_title):
                continue
            if has_version_mismatch(left_title, right_title):
                continue
            if left_album and right_album and has_version_mismatch(left_album, right_album):
                continue
            if similarity(left_title, right_title, is_title=True) < 0.55:
                continue
            if similarity(left_artist, right_artist) < 0.55:
                continue
            return True
    return False


def _relation_features_match(
    source_title: str,
    source_artist: str,
    related_title: str,
    related_artist: str,
) -> bool:
    """Accept a feature credit moved between title and artist for a proven relation."""
    source_feature = feature_signature(source_title)
    related_feature = feature_signature(related_title)
    if source_feature == related_feature:
        return True
    if source_feature and related_feature:
        return False

    feature = source_feature or related_feature
    artist = related_artist if source_feature else source_artist
    return bool(feature) and f" {feature} " in f" {normalize_text(artist)} "


def _relation_metadata_matches(
    conn: Any,
    source_uid: str | None,
    related_uid: str | None,
    source_row: dict[str, Any] | None,
    related_row: dict[str, Any] | None = None,
    *,
    source_binding: tuple[str, str] | None = None,
) -> bool:
    from ytmusic_playlist_sync import similarity

    source_rows = [source_row] if source_row else []
    if source_uid:
        source_rows.extend(_track_metadata_rows(conn, source_uid))
    related_rows = [related_row] if related_row else []
    if related_uid:
        related_track = conn.execute(
            "SELECT yt_title AS title, yt_artist AS artist, yt_album AS album FROM tracks WHERE track_uid = ?",
            (related_uid,),
        ).fetchone()
        if related_track:
            related_rows.append(dict(related_track))
        for row in _track_metadata_rows(conn, related_uid):
            if (
                source_uid == related_uid
                and source_binding
                and normalized_service(row.get("service")) == source_binding[0]
                and str(row.get("song_id") or "") == source_binding[1]
            ):
                continue
            related_rows.append(row)
    source_variants = [variant for row in source_rows for variant in _metadata_variants(row)]
    related_variants = [variant for row in related_rows for variant in _metadata_variants(row)]
    if not source_variants or not related_variants:
        return False
    for source_title, source_artist, source_album in source_variants:
        for related_title, related_artist, related_album in related_variants:
            if not _relation_features_match(
                source_title,
                source_artist,
                related_title,
                related_artist,
            ):
                continue
            if has_version_mismatch(source_title, related_title):
                continue
            if source_album and related_album and has_version_mismatch(source_album, related_album):
                continue
            if similarity(source_title, related_title, is_title=True) < 0.55:
                continue
            if source_artist and related_artist and similarity(source_artist, related_artist) < 0.55:
                continue
            return True
    return False


def _relation_manual_status(
    conn: Any,
    *,
    service: str,
    source_song_id: str,
    related_service: str,
    related_song_id: str,
    source_uid: str | None,
    related_uid: str | None,
) -> str:
    overrides = [
        row
        for row in (
            manual_override(conn, service, source_song_id),
            manual_override(conn, related_service, related_song_id),
        )
        if row
    ]
    if source_uid or related_uid:
        uids = [uid for uid in (source_uid, related_uid) if uid]
        placeholders = ",".join("?" for _ in uids)
        overrides.extend(
            conn.execute(
                f"SELECT * FROM manual_overrides WHERE target_track_uid IN ({placeholders})",
                tuple(uids),
            ).fetchall()
        )
    for override in overrides:
        action = str(override["action"] or "").lower()
        if action in {"block", "manual_blocked"}:
            return "manual_blocked"
        if action == "split":
            return "manual_split"
    for override in (
        manual_override(conn, service, source_song_id),
        manual_override(conn, related_service, related_song_id),
    ):
        if not override:
            continue
        override_video = str(override["canonical_yt_video_id"] or "")
        override_uid = str(override["target_track_uid"] or "")
        if override_video and override_video != related_song_id:
            return "manual_override_conflict"
        if override_uid and override_uid != str(related_uid or ""):
            return "manual_override_conflict"
    return ""


def _rebuild_track_metadata_lookup(
    conn: Any,
    *,
    winner_uid: str,
    removed_uids: Iterable[str] = (),
    anchor_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, int]:
    affected_uids = list(dict.fromkeys([winner_uid, *[uid for uid in removed_uids if uid]]))
    placeholders = ",".join("?" for _ in affected_uids)
    conn.execute(
        f"DELETE FROM metadata_lookup_index WHERE track_uid IN ({placeholders})",
        tuple(affected_uids),
    )

    anchors = [row for row in anchor_rows if _metadata_variants(row)]
    if not anchors:
        winner_track = conn.execute(
            "SELECT yt_title AS title, yt_artist AS artist, yt_album AS album FROM tracks WHERE track_uid = ?",
            (winner_uid,),
        ).fetchone()
        if winner_track and _metadata_variants(dict(winner_track)):
            anchors.append(dict(winner_track))
    if not anchors:
        anchors.extend(_track_metadata_rows(conn, winner_uid))

    generated: dict[str, tuple[str, float, dict[str, Any]]] = {}
    for row in _track_metadata_rows(conn, winner_uid):
        if anchors and not any(
            _metadata_rows_equivalent(row, anchor) for anchor in anchors
        ):
            record_conflict(
                conn,
                service=row.get("service") or "unknown",
                song_id=row.get("song_id") or "",
                existing_track_uid=winner_uid,
                incoming_track_uid=winner_uid,
                existing_video_id=None,
                incoming_video_id=None,
                reason="metadata_lookup_rebuild_row_mismatch",
                payload=row,
            )
            continue
        source = f"track_list:{normalized_service(row.get('service'))}"
        for key, score in _metadata_lookup_key_scores(row, 1.0):
            current = generated.get(key)
            if not current or score > current[1]:
                generated[key] = (source, score, row)

    inserted = 0
    conflicts = 0
    for key, (source, score, row) in generated.items():
        owner = conn.execute(
            "SELECT track_uid FROM metadata_lookup_index WHERE lookup_key = ?",
            (key,),
        ).fetchone()
        if owner and owner["track_uid"] != winner_uid:
            record_conflict(
                conn,
                service=row.get("service") or "unknown",
                song_id=row.get("song_id") or "",
                existing_track_uid=owner["track_uid"],
                incoming_track_uid=winner_uid,
                existing_video_id=None,
                incoming_video_id=None,
                reason="metadata_lookup_key_owned_by_unrelated_track",
                payload={
                    **row,
                    "query": key,
                    "score": score,
                },
            )
            conflicts += 1
            continue
        conn.execute(
            """
            INSERT INTO metadata_lookup_index(lookup_key, track_uid, source, score)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                track_uid = excluded.track_uid,
                source = excluded.source,
                score = excluded.score
            """,
            (key, winner_uid, source, score),
        )
        inserted += 1
    return {"lookup_keys": inserted, "lookup_conflicts": conflicts}


def _merge_track_uids(
    conn: Any,
    *,
    loser_uid: str,
    winner_uid: str,
    canonical_video: str,
    dry_run: bool = False,
    anchor_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    if loser_uid == winner_uid:
        return {"merged": 0, "winner_uid": winner_uid, "loser_uid": loser_uid}
    if dry_run:
        return {"merged": 1, "winner_uid": winner_uid, "loser_uid": loser_uid}

    now = utc_now_iso()
    conn.execute("UPDATE platform_song_ids SET track_uid = ? WHERE track_uid = ?", (winner_uid, loser_uid))
    conn.execute("UPDATE match_attempts SET track_uid = ? WHERE track_uid = ?", (winner_uid, loser_uid))
    conn.execute(
        """
        UPDATE manual_overrides
        SET target_track_uid = ?
        WHERE target_track_uid = ? AND action NOT IN ('block', 'split', 'manual_blocked')
        """,
        (winner_uid, loser_uid),
    )
    conn.execute("UPDATE yt_video_ids SET track_uid = ? WHERE track_uid = ?", (winner_uid, loser_uid))
    conn.execute(
        "UPDATE yt_video_ids SET is_canonical = CASE WHEN video_id = ? THEN 1 ELSE 0 END WHERE track_uid = ?",
        (canonical_video, winner_uid),
    )
    conn.execute(
        """
        INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
        VALUES (?, ?, 1)
        ON CONFLICT(video_id) DO UPDATE SET track_uid = excluded.track_uid, is_canonical = 1
        """,
        (canonical_video, winner_uid),
    )
    conn.execute(
        "UPDATE tracks SET canonical_yt_video_id = ?, updated_at = ? WHERE track_uid = ?",
        (canonical_video, now, winner_uid),
    )
    lookup_stats = _rebuild_track_metadata_lookup(
        conn,
        winner_uid=winner_uid,
        removed_uids=(loser_uid,),
        anchor_rows=anchor_rows,
    )
    conn.execute("DELETE FROM tracks WHERE track_uid = ?", (loser_uid,))
    return {
        "merged": 1,
        "winner_uid": winner_uid,
        "loser_uid": loser_uid,
        **lookup_stats,
    }


def _invalidate_ytmusic_song_translation(conn: Any, video_id: str) -> None:
    """Drop only the source-video localization that a verified ATV link supersedes."""
    if type(conn).__name__ == "PostgresConnectionWrapper":
        exists = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = ANY (current_schemas(false))
              AND table_name = 'ytmusic_song_translations'
            LIMIT 1
            """
        ).fetchone()
    else:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ytmusic_song_translations'"
        ).fetchone()
    if exists:
        conn.execute(
            "DELETE FROM ytmusic_song_translations WHERE video_id = ?",
            (video_id,),
        )


def _is_trusted_charts_relation_row(
    row: dict[str, Any] | None,
    source_song_id: str,
    related_song_id: str,
) -> bool:
    if not row or str(row.get("source") or "") != "youtube_charts_weekly_browse_api":
        return False
    row_source_id = str(row.get("song_id") or row.get("original_video_id") or "")
    return (
        row_source_id == source_song_id
        and str(row.get("atv_external_video_id") or "") == related_song_id
    )


def _refresh_trusted_chart_source_metadata(
    conn: Any,
    *,
    service: str,
    source_song_id: str,
    source_uid: str | None,
    source_row: dict[str, Any],
) -> bool:
    """Refresh one proven Charts source row, including an explicit empty album."""
    columns = (
        "album_id",
        "title_ko",
        "artist_ko",
        "album_ko",
        "title_en",
        "artist_en",
        "album_en",
        "artwork_url",
    )
    incoming = track_list_metadata_params(
        service=service,
        song_id=source_song_id,
        row=source_row,
    )[2:]
    current = conn.execute(
        f"SELECT {', '.join(columns)} FROM track_list WHERE service = ? AND song_id = ?",
        (service, source_song_id),
    ).fetchone()
    if not current:
        upsert_track_list_metadata(
            conn,
            service=service,
            song_id=source_song_id,
            track_uid=source_uid or stable_uid(f"{service}:{source_song_id}"),
            row=source_row,
            bind_source_id=False,
        )
        return True

    values = {
        column: str(current[column] or "")
        for column in columns
    }
    for column, value in zip(columns, incoming):
        value = str(value or "")
        if value:
            values[column] = value
    if not any(str(source_row.get(key) or "").strip() for key in ("album", "album_ko", "album_en")):
        values["album_ko"] = ""
        values["album_en"] = ""
        if not str(source_row.get("album_id") or "").strip():
            values["album_id"] = ""

    if all(values[column] == str(current[column] or "") for column in columns):
        return False
    conn.execute(
        f"UPDATE track_list SET {', '.join(f'{column} = ?' for column in columns)} "
        "WHERE service = ? AND song_id = ?",
        (*[values[column] for column in columns], service, source_song_id),
    )
    return True


def consolidate_source_song_relation(
    conn: Any,
    *,
    service: str,
    source_song_id: str,
    relation_type: str,
    related_service: str,
    related_song_id: str,
    source_row: dict[str, Any] | None = None,
    related_row: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    service = normalized_service(service)
    related_service = normalized_service(related_service)
    source_song_id = str(source_song_id or "").strip()
    related_song_id = str(related_song_id or "").strip()
    if relation_type != YOUTUBE_ATV_RELATION or service != "ytmusic" or related_service != "ytmusic":
        return {"status": "unsupported_relation"}
    video_id_pattern = r"[A-Za-z0-9_-]{11}"
    if not re.fullmatch(video_id_pattern, source_song_id) or not re.fullmatch(video_id_pattern, related_song_id):
        return {"status": "unsupported_relation"}

    evidence = conn.execute(
        """
        SELECT related_service, related_song_id, evidence_source
        FROM source_song_relations
        WHERE service = ? AND source_song_id = ? AND relation_type = ?
        """,
        (service, source_song_id, relation_type),
    ).fetchone()
    trusted_charts_source = _is_trusted_charts_relation_row(
        source_row,
        source_song_id,
        related_song_id,
    )
    trusted_dry_run_evidence = dry_run and trusted_charts_source
    if not trusted_dry_run_evidence and (
        not evidence
        or normalized_service(evidence["related_service"]) != related_service
        or str(evidence["related_song_id"] or "") != related_song_id
        or not str(evidence["evidence_source"] or "").strip()
    ):
        return {"status": "unsupported_relation"}

    def reject(
        status: str,
        reason: str,
        *,
        existing_uid: str | None,
        incoming_uid: str | None,
        existing_video: str | None,
    ) -> dict[str, Any]:
        if not dry_run:
            record_conflict(
                conn,
                service=service,
                song_id=source_song_id,
                existing_track_uid=existing_uid,
                incoming_track_uid=incoming_uid,
                existing_video_id=existing_video,
                incoming_video_id=related_song_id,
                reason=reason,
                payload=source_row,
            )
        return {"status": status, "winner_uid": incoming_uid, "loser_uid": existing_uid}

    source_uid = find_track_by_service_song(conn, service, source_song_id) or find_track_by_video(conn, source_song_id)
    related_by_song = find_track_by_service_song(conn, related_service, related_song_id)
    related_by_video = find_track_by_video(conn, related_song_id)
    if related_by_song and related_by_video and related_by_song != related_by_video:
        return reject(
            "metadata_conflict",
            "related_song_id_binding_conflict",
            existing_uid=related_by_song,
            incoming_uid=related_by_video,
            existing_video=related_song_id,
        )
    related_uid = related_by_video or related_by_song

    manual_status = _relation_manual_status(
        conn,
        service=service,
        source_song_id=source_song_id,
        related_service=related_service,
        related_song_id=related_song_id,
        source_uid=source_uid,
        related_uid=related_uid,
    )
    if manual_status:
        if manual_status == "manual_override_conflict":
            source_track = (
                conn.execute(
                    "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
                    (source_uid,),
                ).fetchone()
                if source_uid else None
            )
            return reject(
                manual_status,
                "source_relation_conflicts_with_manual_override",
                existing_uid=source_uid,
                incoming_uid=related_uid,
                existing_video=(source_track["canonical_yt_video_id"] if source_track else None),
            )
        return {"status": manual_status, "winner_uid": related_uid, "loser_uid": source_uid}

    related_track = None
    related_canonical = ""
    if related_uid:
        related_track = conn.execute(
            "SELECT * FROM tracks WHERE track_uid = ?",
            (related_uid,),
        ).fetchone()
        related_canonical = str(related_track["canonical_yt_video_id"] or "") if related_track else ""
        if related_canonical and related_canonical != related_song_id and related_uid != source_uid:
            return reject(
                "metadata_conflict",
                "source_relation_target_is_not_canonical",
                existing_uid=related_uid,
                incoming_uid=related_uid,
                existing_video=related_canonical,
            )

    if not _relation_metadata_matches(
        conn,
        source_uid,
        related_uid,
        source_row,
        related_row,
        source_binding=(service, source_song_id),
    ):
        return reject(
            "metadata_conflict",
            "source_song_relation_metadata_mismatch",
            existing_uid=source_uid,
            incoming_uid=related_uid,
            existing_video=source_song_id,
        )

    trusted_source_refresh = bool(
        not dry_run
        and trusted_charts_source
        and source_row
        and related_row
        and _relation_metadata_matches(
            conn,
            None,
            None,
            source_row,
            related_row,
        )
    )
    source_metadata_changed = bool(
        trusted_source_refresh
        and _refresh_trusted_chart_source_metadata(
            conn,
            service=service,
            source_song_id=source_song_id,
            source_uid=source_uid,
            source_row=source_row,
        )
    )

    source_binding = find_track_by_service_song(conn, service, source_song_id)
    related_binding = find_track_by_service_song(conn, related_service, related_song_id)
    source_video = conn.execute(
        "SELECT track_uid, is_canonical FROM yt_video_ids WHERE video_id = ?",
        (source_song_id,),
    ).fetchone()
    related_video = conn.execute(
        "SELECT track_uid, is_canonical FROM yt_video_ids WHERE video_id = ?",
        (related_song_id,),
    ).fetchone()
    already_consistent = bool(
        source_uid
        and source_uid == related_uid
        and source_binding == related_uid
        and related_binding == related_uid
        and source_video
        and source_video["track_uid"] == related_uid
        and int(source_video["is_canonical"] or 0) == (1 if source_song_id == related_song_id else 0)
        and related_video
        and related_video["track_uid"] == related_uid
        and int(related_video["is_canonical"] or 0) == 1
        and related_canonical == related_song_id
    )
    if already_consistent:
        if source_metadata_changed:
            _rebuild_track_metadata_lookup(
                conn,
                winner_uid=related_uid,
                anchor_rows=(related_row, source_row),
            )
        return {"status": "already_linked", "winner_uid": related_uid, "loser_uid": source_uid, "merged": 0}

    if dry_run:
        return {
            "status": "would_link",
            "winner_uid": related_uid or stable_uid(f"yt:{related_song_id}"),
            "loser_uid": source_uid,
        }

    if not related_uid:
        if not related_row:
            return {"status": "metadata_conflict", "winner_uid": None, "loser_uid": source_uid}
        related_uid = stable_uid(f"yt:{related_song_id}")
        metadata = related_row
        ensure_track(
            conn,
            track_uid=related_uid,
            video_id=related_song_id,
            yt_title=str(metadata.get("title_en") or metadata.get("title") or metadata.get("title_ko") or ""),
            yt_artist=str(metadata.get("artist_en") or metadata.get("artist") or metadata.get("artist_ko") or ""),
            yt_album=str(metadata.get("album_en") or metadata.get("album") or metadata.get("album_ko") or ""),
            status="matched",
            score=1.0,
        )
        related_track = conn.execute(
            "SELECT * FROM tracks WHERE track_uid = ?",
            (related_uid,),
        ).fetchone()

    anchor_rows = [related_row] if related_row and _metadata_variants(related_row) else []
    if trusted_source_refresh and source_row and _metadata_variants(source_row):
        anchor_rows.append(source_row)
    if not anchor_rows:
        exact_target = conn.execute(
            "SELECT * FROM track_list WHERE service = ? AND song_id = ?",
            (related_service, related_song_id),
        ).fetchone()
        if exact_target and _metadata_variants(dict(exact_target)):
            anchor_rows.append(dict(exact_target))
    if not anchor_rows and related_track:
        anchor_rows.append(
            {
                "title": related_track["yt_title"],
                "artist": related_track["yt_artist"],
                "album": related_track["yt_album"],
            }
        )
    if source_uid and source_uid != related_uid:
        merge_stats = _merge_track_uids(
            conn,
            loser_uid=source_uid,
            winner_uid=related_uid,
            canonical_video=related_song_id,
            anchor_rows=anchor_rows,
        )
        status = "linked"
    else:
        merge_stats = {"merged": 0, "winner_uid": related_uid, "loser_uid": source_uid}
        status = "already_linked" if source_uid else "linked"

    conn.execute(
        """
        INSERT INTO platform_song_ids(service, song_id, track_uid)
        VALUES (?, ?, ?)
        ON CONFLICT(service, song_id) DO UPDATE SET track_uid = excluded.track_uid
        """,
        (related_service, related_song_id, related_uid),
    )
    conn.execute(
        """
        INSERT INTO platform_song_ids(service, song_id, track_uid)
        VALUES (?, ?, ?)
        ON CONFLICT(service, song_id) DO UPDATE SET track_uid = excluded.track_uid
        """,
        (service, source_song_id, related_uid),
    )
    conn.execute(
        """
        INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
        VALUES (?, ?, 0)
        ON CONFLICT(video_id) DO UPDATE SET track_uid = excluded.track_uid, is_canonical = 0
        """,
        (source_song_id, related_uid),
    )
    conn.execute(
        """
        INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
        VALUES (?, ?, 1)
        ON CONFLICT(video_id) DO UPDATE SET track_uid = excluded.track_uid, is_canonical = 1
        """,
        (related_song_id, related_uid),
    )
    current = conn.execute(
        "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
        (related_uid,),
    ).fetchone()
    if not current or str(current["canonical_yt_video_id"] or "") != related_song_id:
        conn.execute(
            "UPDATE tracks SET canonical_yt_video_id = ?, updated_at = ? WHERE track_uid = ?",
            (related_song_id, utc_now_iso(), related_uid),
        )
    if source_song_id != related_song_id:
        _invalidate_ytmusic_song_translation(conn, source_song_id)
    if source_metadata_changed and not (source_uid and source_uid != related_uid):
        _rebuild_track_metadata_lookup(
            conn,
            winner_uid=related_uid,
            anchor_rows=anchor_rows,
        )
    return {"status": status, **merge_stats}


def repair_source_song_relations(
    conn: Any,
    *,
    service: str = "",
    source_song_id: str = "",
    dry_run: bool = False,
) -> dict[str, int]:
    clauses: list[str] = []
    params: list[str] = []
    if service:
        clauses.append("service = ?")
        params.append(normalized_service(service))
    if source_song_id:
        clauses.append("source_song_id = ?")
        params.append(source_song_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    relations = conn.execute(
        f"SELECT * FROM source_song_relations {where} ORDER BY service, source_song_id, relation_type",
        tuple(params),
    ).fetchall()
    stats: dict[str, int] = {"scanned": len(relations)}
    for relation in relations:
        result = consolidate_source_song_relation(
            conn,
            service=relation["service"],
            source_song_id=relation["source_song_id"],
            relation_type=relation["relation_type"],
            related_service=relation["related_service"],
            related_song_id=relation["related_song_id"],
            dry_run=dry_run,
        )
        status = str(result.get("status") or "unknown")
        stats[status] = stats.get(status, 0) + 1
    return stats


def _verify_metadata_merge(
    conn: sqlite3.Connection,
    track_uid: str,
    row: dict[str, Any],
    threshold: float = 0.5,
) -> bool:
    """기존 트랙의 소스 메타데이터와 입력 메타데이터의 유사도를 검증.
    
    metadata_lookup_index 키 충돌로 인한 오병합을 방지합니다.
    임계값(0.5)은 "거리에서" vs "Trip" (유사도≈0.0)은 거부하되,
    "뛰어(JUMP)" vs "뛰어" (유사도≈0.9)은 허용하는 수준입니다.
    """
    from ytmusic_playlist_sync import similarity

    existing_meta = conn.execute(
        """
        SELECT tl.title_ko, tl.title_en, tl.artist_ko, tl.artist_en
        FROM platform_song_ids ps
        JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
        WHERE ps.track_uid = ?
        ORDER BY
            CASE tl.service WHEN 'melon' THEN 0 WHEN 'apple' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (track_uid,),
    ).fetchone()
    if not existing_meta:
        return True  # 메타데이터가 없으면 기존 동작 유지

    src_title = row.get("title") or row.get("title_ko") or row.get("title_en") or ""
    if not src_title:
        return True

    # 기존 트랙의 한/영 제목 모두와 비교하여 최대 유사도를 사용
    existing_titles = [
        existing_meta["title_ko"] or "",
        existing_meta["title_en"] or "",
    ]
    best_sim = max(
        (similarity(src_title, t) for t in existing_titles if t),
        default=0.0,
    )
    if best_sim < threshold:
        LOG.warning(
            "Metadata merge rejected: input='%s' vs existing='%s'/'%s' (sim=%.2f < %.2f)",
            src_title,
            existing_meta["title_ko"],
            existing_meta["title_en"],
            best_sim,
            threshold,
        )
        return False
    return True


def resolve_track_uid(
    conn: sqlite3.Connection,
    *,
    service: str,
    song_id: str,
    row: dict[str, Any],
    video_id: str | None,
) -> str:
    service = normalized_service(service)
    override = manual_override(conn, service, song_id)
    if override and str(override["action"] or "").lower() in {"block", "manual_blocked"}:
        return stable_uid(f"blocked:{service}:{song_id}")
    if override and str(override["action"] or "").lower() == "split":
        return find_track_by_service_song(conn, service, song_id) or stable_uid(f"{service}:{song_id}")
    if override and override["target_track_uid"]:
        return override["target_track_uid"]
    if override and override["canonical_yt_video_id"]:
        existing = find_track_by_video(conn, override["canonical_yt_video_id"])
        if existing:
            return existing
        return stable_uid(f"yt:{override['canonical_yt_video_id']}")

    existing_by_song = find_track_by_service_song(conn, service, song_id)
    existing_by_video = find_track_by_video(conn, video_id)
    if existing_by_song:
        song_track = conn.execute(
            "SELECT canonical_yt_video_id, match_status FROM tracks WHERE track_uid = ?",
            (existing_by_song,),
        ).fetchone()
        song_video = song_track["canonical_yt_video_id"] if song_track else ""
        song_status = song_track["match_status"] if song_track else ""
        if existing_by_video and existing_by_video != existing_by_song:
            if not song_video or song_video == video_id or song_status in {"failed", "duplicate_skipped", "manual_blocked", "unmatched"}:
                return existing_by_video
            return existing_by_song
        if song_video:
            return existing_by_song
        if existing_by_video:
            return existing_by_video
    if existing_by_video:
        return existing_by_video
    existing_by_meta = find_track_by_metadata(
        conn,
        row.get("title") or row.get("title_en") or row.get("title_ko") or "",
        row.get("artist") or row.get("artist_en") or row.get("artist_ko") or "",
        row.get("album") or row.get("album_en") or row.get("album_ko") or "",
    )
    if existing_by_meta:
        if _verify_metadata_merge(conn, existing_by_meta, row):
            return existing_by_meta
    if video_id:
        return stable_uid(f"yt:{video_id}")
    if service and song_id:
        return stable_uid(f"{service}:{song_id}")
    return stable_uid(
        metadata_key(
            row.get("title") or row.get("title_en") or row.get("title_ko") or "",
            row.get("artist") or row.get("artist_en") or row.get("artist_ko") or "",
            row.get("album") or row.get("album_en") or row.get("album_ko") or "",
        )
    )


def upsert_track_match(
    conn: sqlite3.Connection,
    *,
    service: str,
    source_row: dict[str, Any],
    match_row: dict[str, Any] | None = None,
) -> str:
    service = normalized_service(service)
    merged = dict(source_row)
    if match_row:
        merged.update({k: v for k, v in match_row.items() if v not in (None, "")})
    song_id = normalize_song_id(service, merged)
    video_id = merged.get("video_id") or merged.get("canonical_yt_video_id")
    observed_video_id = video_id
    status = str(merged.get("status") or ("matched" if video_id else "failed"))
    score = float(merged.get("score") or 0)
    override = manual_override(conn, service, song_id)
    override_action = str(override["action"] or "").lower() if override else ""
    if override_action in {"block", "manual_blocked"} or status == "manual_blocked":
        track_uid = stable_uid(f"blocked:{service}:{song_id}")
        ensure_track(conn, track_uid=track_uid, status="manual_blocked", score=score)
        conn.execute(
            "UPDATE tracks SET canonical_yt_video_id = NULL, match_status = 'manual_blocked', updated_at = ? WHERE track_uid = ?",
            (utc_now_iso(), track_uid),
        )
        conn.execute("DELETE FROM yt_video_ids WHERE track_uid = ?", (track_uid,))
        if song_id:
            conn.execute(
                "DELETE FROM platform_song_ids WHERE service = ? AND song_id = ?",
                (service, song_id),
            )
            upsert_track_list_metadata(
                conn,
                service=service,
                song_id=song_id,
                track_uid=track_uid,
                row=merged,
                bind_source_id=False,
            )
        return track_uid
    if override_action == "split":
        track_uid = stable_uid(f"split:{service}:{song_id}")
        ensure_track(
            conn,
            track_uid=track_uid,
            video_id=video_id,
            yt_title=merged.get("yt_title", ""),
            yt_artist=merged.get("yt_artist", ""),
            yt_album=merged.get("yt_album", ""),
            status=status,
            score=score,
        )
        if song_id:
            upsert_track_list_metadata(
                conn,
                service=service,
                song_id=song_id,
                track_uid=track_uid,
                row=merged,
            )
        return track_uid
    if override and override_action == "set_canonical" and override["canonical_yt_video_id"]:
        video_id = override["canonical_yt_video_id"]
        merged["video_id"] = video_id
        merged["status"] = status = "manual_override"
    elif override and override["target_track_uid"]:
        target = conn.execute(
            "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
            (override["target_track_uid"],),
        ).fetchone()
        video_id = (target["canonical_yt_video_id"] if target else None) or video_id
        merged["video_id"] = video_id
        merged["status"] = status = "manual_override"
    if status in {"failed", "duplicate_skipped"} or (service == "spotify" and str(song_id).startswith("fallback:")):
        track_uid = stable_uid(f"unmatched:{service}:{song_id or metadata_key(merged.get('title'), merged.get('artist'), merged.get('album'))}")
        
        is_ytmusic_video = (service == "ytmusic" and song_id and not str(song_id).startswith("fallback:"))
        
        ensure_track(
            conn,
            track_uid=track_uid,
            status=status,
            score=score,
            video_id=song_id if is_ytmusic_video else None,
        )
        if song_id and not (service == "spotify" and str(song_id).startswith("fallback:")):
            upsert_track_list_metadata(
                conn,
                service=service,
                song_id=song_id,
                track_uid=track_uid,
                row=merged,
                bind_source_id=is_ytmusic_video,
            )
        return track_uid
    track_uid = resolve_track_uid(conn, service=service, song_id=song_id, row=merged, video_id=video_id)

    existing = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?", (track_uid,)).fetchone()
    existing_video = existing["canonical_yt_video_id"] if existing else None
    override_video = override["canonical_yt_video_id"] if override else None
    canonical_video = override_video or video_id

    identity_conflict = bool(
        existing_video and video_id and existing_video != video_id and not override_video
    )
    if identity_conflict:
        record_conflict(
            conn,
            service=service,
            song_id=song_id,
            existing_track_uid=track_uid,
            incoming_track_uid=find_track_by_video(conn, video_id),
            existing_video_id=existing_video,
            incoming_video_id=video_id,
            reason="same_track_uid_different_video",
            payload=merged,
        )
        canonical_video = existing_video

    ensure_track(
        conn,
        track_uid=track_uid,
        video_id=override_video or video_id,
        yt_title=(
            merged.get("yt_title", "")
            if not override_video or str(observed_video_id or "") == str(override_video)
            else ""
        ),
        yt_artist=(
            merged.get("yt_artist", "")
            if not override_video or str(observed_video_id or "") == str(override_video)
            else ""
        ),
        yt_album=(
            merged.get("yt_album", "")
            if not override_video or str(observed_video_id or "") == str(override_video)
            else ""
        ),
        status=status,
        score=score,
    )
    if override and override_action == "set_canonical" and override_video:
        owner = find_track_by_video(conn, override_video)
        if owner and owner != track_uid:
            raise ValueError(
                f"Manual canonical video {override_video} is already owned by another track"
            )
        conn.execute(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET is_canonical = 1
            """,
            (override_video, track_uid),
        )
        current = conn.execute(
            """
            SELECT canonical_yt_video_id, yt_title, yt_artist, yt_album
            FROM tracks WHERE track_uid = ?
            """,
            (track_uid,),
        ).fetchone()
        current = dict(current) if current else {}
        switching_video = bool(
            current.get("canonical_yt_video_id")
            and str(current["canonical_yt_video_id"]) != str(override_video)
        )
        metadata_matches_override = (
            str(observed_video_id or "") == str(override_video)
        )
        manual_metadata = []
        for key in ("yt_title", "yt_artist", "yt_album"):
            incoming_value = str(merged.get(key) or "")
            current_value = str(current.get(key) or "")
            if metadata_matches_override:
                manual_metadata.append(incoming_value or ("" if switching_video else current_value))
            else:
                manual_metadata.append("" if switching_video else current_value)
        conn.execute(
            """
            UPDATE tracks
            SET canonical_yt_video_id = ?,
                yt_title = ?, yt_artist = ?, yt_album = ?,
                match_status = 'manual_override', updated_at = ?
            WHERE track_uid = ?
            """,
            (
                override_video,
                *manual_metadata,
                utc_now_iso(), track_uid,
            ),
        )
        _sync_canonical_video_flags(conn, [track_uid])
    if song_id:
        bound_uid = find_track_by_service_song(conn, service, song_id)
        if bound_uid and bound_uid != track_uid:
            bound_video = conn.execute(
                "SELECT canonical_yt_video_id, match_status FROM tracks WHERE track_uid = ?",
                (bound_uid,),
            ).fetchone()
            bound_canonical = bound_video["canonical_yt_video_id"] if bound_video else None
            bound_status = bound_video["match_status"] if bound_video else ""
            can_rebind = (
                override_action == "set_canonical"
                or not bound_canonical
                or bound_canonical == canonical_video
                or bound_status in {"failed", "duplicate_skipped", "manual_blocked", "unmatched"}
            )
            if not can_rebind:
                record_conflict(
                    conn,
                    service=service,
                    song_id=song_id,
                    existing_track_uid=bound_uid,
                    incoming_track_uid=track_uid,
                    existing_video_id=bound_canonical,
                    incoming_video_id=video_id,
                    reason="service_song_id_already_bound",
                    payload=merged,
                )
                track_uid = bound_uid
        upsert_track_list_metadata(conn, service=service, song_id=song_id, track_uid=track_uid, row=merged)
    if canonical_video:
        conn.execute(
            "UPDATE tracks SET canonical_yt_video_id = COALESCE(canonical_yt_video_id, ?) WHERE track_uid = ?",
            (canonical_video, track_uid),
        )
    upsert_metadata_lookup(
        conn,
        track_uid=track_uid,
        row=source_row if identity_conflict else merged,
        source=status,
        score=score,
    )
    return track_uid


def start_match_run(
    conn: sqlite3.Connection,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    reference_period: str = "",
    started_at: str,
    source: str = "",
    total_tracks: int = 0,
) -> str:
    service = normalized_service(service)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    run_id = hashlib.sha1(
        f"{service}|{job_name}|{source_variant}|{reference_period}|{started_at}".encode("utf-8")
    ).hexdigest()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO match_runs(
            run_id, service, job_name, source_variant, reference_period, started_at,
            source, status, completed_at, total_tracks, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            job_name = excluded.job_name,
            source_variant = excluded.source_variant,
            reference_period = excluded.reference_period,
            total_tracks = excluded.total_tracks,
            source = excluded.source,
            status = 'running',
            completed_at = NULL
        """,
        (run_id, service, job_name, source_variant, reference_period, started_at, source, total_tracks, now),
    )
    return run_id


def record_match_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    service: str,
    song_id: str,
    track_uid: str,
    row: dict[str, Any],
) -> None:
    now = utc_now_iso()
    match_method, origin_method, _ = match_method_for_status(row.get("status"), row.get("query"))
    conn.execute(
        """
        INSERT INTO match_attempts(
            run_id, service, song_id, track_uid, rank_order,
            video_id, score, title_score, artist_score,
            album_score, yt_result_type, query, status, match_method, origin_method,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, service, song_id, rank_order) DO UPDATE SET
            track_uid = EXCLUDED.track_uid,
            rank_order = EXCLUDED.rank_order,
            video_id = EXCLUDED.video_id,
            score = EXCLUDED.score,
            title_score = EXCLUDED.title_score,
            artist_score = EXCLUDED.artist_score,
            album_score = EXCLUDED.album_score,
            yt_result_type = EXCLUDED.yt_result_type,
            query = EXCLUDED.query,
            status = EXCLUDED.status,
            match_method = EXCLUDED.match_method,
            origin_method = EXCLUDED.origin_method,
            created_at = EXCLUDED.created_at
        """,
        (
            run_id,
            normalized_service(service),
            song_id,
            track_uid,
            int(row.get("rank") or 0),
            row.get("video_id", ""),
            float(row.get("score") or 0),
            float(row.get("title_score") or 0),
            float(row.get("artist_score") or 0),
            float(row.get("album_score") or 0),
            row.get("yt_result_type", ""),
            row.get("query", ""),
            row.get("status", ""),
            row.get("match_method", match_method),
            row.get("origin_method", origin_method),
            now,
        ),
    )


def record_match_candidates(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    service: str,
    song_id: str,
    rank_order: int,
    candidates: Iterable[dict[str, Any]],
) -> None:
    now = utc_now_iso()
    for index, candidate in enumerate(candidates, 1):
        conn.execute(
            """
            INSERT INTO match_candidates(
                run_id, service, song_id, rank_order, candidate_order, video_id,
                yt_title, yt_artist, yt_album, score, title_score, artist_score,
                album_score, yt_result_type, query, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, service, song_id, rank_order, candidate_order) DO UPDATE SET
                video_id = EXCLUDED.video_id,
                yt_title = EXCLUDED.yt_title,
                yt_artist = EXCLUDED.yt_artist,
                yt_album = EXCLUDED.yt_album,
                score = EXCLUDED.score,
                title_score = EXCLUDED.title_score,
                artist_score = EXCLUDED.artist_score,
                album_score = EXCLUDED.album_score,
                yt_result_type = EXCLUDED.yt_result_type,
                query = EXCLUDED.query,
                created_at = EXCLUDED.created_at
            """,
            (
                run_id,
                normalized_service(service),
                song_id,
                int(rank_order or 0),
                index,
                candidate.get("video_id") or candidate.get("videoId", ""),
                candidate.get("yt_title") or candidate.get("title", ""),
                candidate.get("yt_artist") or candidate.get("artist", ""),
                candidate.get("yt_album") or candidate.get("album", ""),
                float(candidate.get("score") or 0),
                float(candidate.get("title_score") or 0),
                float(candidate.get("artist_score") or 0),
                float(candidate.get("album_score") or 0),
                candidate.get("yt_result_type") or candidate.get("resultType", ""),
                candidate.get("query", ""),
                now,
            ),
        )


def cleanup_old_attempts_and_candidates(conn: sqlite3.Connection, days: int = 15) -> None:
    """15일이 지난 매칭 시도 이력 및 검색 후보 데이터를 삭제하여 용량을 최적화합니다."""
    if type(conn).__name__ == "PostgresConnectionWrapper":
        conn.execute(
            "DELETE FROM match_attempts WHERE created_at::timestamptz < now() - make_interval(days => ?)",
            (int(days),),
        )
        conn.execute(
            "DELETE FROM match_candidates WHERE created_at::timestamptz < now() - make_interval(days => ?)",
            (int(days),),
        )
        return
    conn.execute("DELETE FROM match_attempts WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')", (days,))
    conn.execute("DELETE FROM match_candidates WHERE datetime(created_at) < datetime('now', '-' || ? || ' days')", (days,))


def get_expected_track_count(job_name: str) -> int | None:
    job = str(job_name).lower().strip()
    if "top-songs" in job:
        return 200
    if "top-100" in job or "hot-100" in job or "gen-z" in job:
        return 100
    if "top-25" in job:
        return 25
    return None


def _validate_track_count(job_name: str, actual: int) -> None:
    expected = get_expected_track_count(job_name)
    if expected is None:
        return

    is_dynamic_apple_chart = job_name.lower().strip() == "kr-top-songs"
    valid = (
        0.95 * expected <= actual <= expected
        if is_dynamic_apple_chart
        else actual == expected
    )
    if valid:
        return

    expectation = f"95%-100% of {expected}" if is_dynamic_apple_chart else f"exactly {expected}"
    if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
        LOG.warning(
            "Track count validation bypassed. Job '%s' has %d tracks, expected %s.",
            job_name,
            actual,
            expectation,
        )
        return

    raise ValueError(
        f"Validation Error: Job '{job_name}' has {actual} tracks, "
        f"but expected {expectation} tracks. Aborting database persistence to prevent corruption. "
        f"Set BYPASS_TRACK_COUNT_VAL=true to bypass."
    )


def _validated_rank(row: dict[str, Any], *, label: str) -> int:
    try:
        rank = int(row.get("rank") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} has an invalid rank: {row.get('rank')!r}") from exc
    if rank <= 0:
        raise ValueError(f"{label} must have a positive rank, found {rank}")
    return rank


def _validate_raw_tracks(service: str, job_name: str, rows: list[dict[str, Any]]) -> None:
    _validate_track_count(job_name, len(rows))
    if not rows:
        raise ValueError(f"Raw chart '{job_name}' is empty; refusing full replacement.")
    ranks: set[int] = set()
    for index, row in enumerate(rows, 1):
        song_id = normalize_song_id(service, row)
        if not song_id:
            raise ValueError(f"Raw chart '{job_name}' row {index} has no source song_id.")
        rank = _validated_rank(row, label=f"Raw chart '{job_name}' row {index}")
        if rank in ranks:
            raise ValueError(f"Raw chart '{job_name}' contains duplicate rank slot {rank}.")
        ranks.add(rank)


def _validate_effective_tracks(
    service: str,
    job_name: str,
    tracks: list[dict[str, Any]],
    matches: list[dict[str, Any]],
) -> None:
    if not tracks:
        raise ValueError(f"Effective chart '{job_name}' is empty.")
    if len(tracks) != len(matches):
        raise ValueError(
            f"Effective chart '{job_name}' has {len(tracks)} tracks but {len(matches)} match results."
        )
    track_keys: set[tuple[str, int]] = set()
    song_ids: set[str] = set()
    ranks: set[int] = set()
    for index, row in enumerate(tracks, 1):
        song_id = normalize_song_id(service, row)
        if not song_id:
            raise ValueError(f"Effective chart '{job_name}' row {index} has no source song_id.")
        rank = _validated_rank(row, label=f"Effective chart '{job_name}' row {index}")
        if song_id in song_ids:
            raise ValueError(f"Effective chart '{job_name}' contains duplicate song_id {song_id!r}.")
        if rank in ranks:
            raise ValueError(f"Effective chart '{job_name}' contains duplicate rank slot {rank}.")
        song_ids.add(song_id)
        ranks.add(rank)
        track_keys.add((song_id, rank))
    match_keys = {
        (normalize_song_id(service, row), _validated_rank(row, label=f"Match result for '{job_name}'"))
        for row in matches
    }
    if track_keys != match_keys:
        raise ValueError(f"Effective tracks and match results differ for chart '{job_name}'.")


def _validate_effective_snapshot(
    conn: Any,
    *,
    service: str,
    job_name: str,
    source_variant: str,
    reference_period: str,
    tracks: list[dict[str, Any]],
) -> None:
    expected = {
        (row["song_id"], int(row["rank_order"]))
        for row in conn.execute(
            """
            SELECT song_id, MIN(rank_order) AS rank_order
            FROM playlist_order
            WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
            GROUP BY song_id
            """,
            (service, job_name, source_variant, reference_period),
        ).fetchall()
    }
    actual = {(normalize_song_id(service, row), int(row.get("rank") or 0)) for row in tracks}
    if not expected:
        raise ValueError(f"No raw chart snapshot exists for {service}/{job_name}/{source_variant}/{reference_period}.")
    if actual != expected:
        raise ValueError(
            f"Effective chart does not match raw snapshot for {service}/{job_name}/{source_variant}/{reference_period}: "
            f"expected {len(expected)} unique songs, found {len(actual)}."
        )


def _persist_crawled_tracks_impl(
    conn: Any,
    service: str,
    job_name: str,
    source_variant: str,
    chart_date: str,
    reference_period: str | None,
    chart_period: str | None,
    tracks: Iterable[Any],
) -> None:
    track_rows = [row_dict(t) for t in tracks]
    service = normalized_service(service)
    ref_p = reference_period or chart_period
    resolved_period = reference_period_for_date(job_name, chart_date, ref_p)
    if not resolved_period:
        raise ValueError(f"Could not resolve reference period for raw chart '{job_name}'.")
    scope = (service, job_name, source_variant, resolved_period)
    if type(conn).__name__ == "PostgresConnectionWrapper":
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("|".join(scope),))
    conn.execute(
        """
        UPDATE match_runs
        SET status = 'stale', completed_at = NULL
        WHERE service = ? AND job_name = ? AND source_variant = ?
          AND reference_period = ? AND status = 'completed'
        """,
        scope,
    )
    LOG.info("Cleaning up existing playlist_order records for %s / %s / %s (period: %s)", *scope)
    conn.execute(
        """
        DELETE FROM playlist_order
        WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
        """,
        scope,
    )

    # 1. Fetch all existing UIDs in one select query
    song_ids = [normalize_song_id(service, t) for t in track_rows if normalize_song_id(service, t)]
    existing_uids = {}
    if song_ids:
        placeholders = ",".join("?" for _ in song_ids)
        rows = conn.execute(
            f"SELECT song_id, track_uid FROM platform_song_ids WHERE service = ? AND song_id IN ({placeholders})",
            (service, *song_ids)
        ).fetchall()
        existing_uids = {row["song_id"]: row["track_uid"] for row in rows}

    now = utc_now_iso()
    tracks_params = []
    platform_song_ids_params = []
    track_list_params = []
    playlist_order_params = []

    for track in track_rows:
        song_id = normalize_song_id(service, track)
        if not song_id:
            continue
        
        existing_uid = existing_uids.get(song_id)
        if existing_uid:
            track_uid = existing_uid
        else:
            track_uid = stable_uid(f"unmatched:{service}:{song_id}")
        
        # Collect tracks params
        tracks_params.append((track_uid, None, "", "", "", "unmatched", 0.0, now, now))
        
        # Collect platform_song_ids params
        platform_song_ids_params.append((service, song_id, track_uid))
        
        # Collect track_list metadata params
        track_list_params.append(track_list_metadata_params(service=service, song_id=song_id, row=track))
        
        # Collect playlist_order params
        playlist_order_params.append((
            normalized_service(service),
            job_name,
            source_variant,
            resolved_period,
            song_id,
            int(track.get("rank") or 0),
        ))

    if tracks_params:
        conn.executemany(
            """
            INSERT INTO tracks (
                track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
                match_status, best_score, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_uid) DO UPDATE SET
                canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
                updated_at = excluded.updated_at
            """,
            tracks_params
        )

    if platform_song_ids_params:
        conn.executemany(
            """
            INSERT INTO platform_song_ids(service, song_id, track_uid)
            VALUES (?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                track_uid = excluded.track_uid
            """,
            platform_song_ids_params
        )

    if track_list_params:
        conn.executemany(
            """
            INSERT INTO track_list(
                service, song_id, album_id, title_ko, artist_ko, album_ko,
                title_en, artist_en, album_en, artwork_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                album_id = COALESCE(NULLIF(excluded.album_id, ''), track_list.album_id),
                title_ko = COALESCE(NULLIF(excluded.title_ko, ''), track_list.title_ko),
                artist_ko = COALESCE(NULLIF(excluded.artist_ko, ''), track_list.artist_ko),
                album_ko = COALESCE(NULLIF(excluded.album_ko, ''), track_list.album_ko),
                title_en = COALESCE(NULLIF(excluded.title_en, ''), track_list.title_en),
                artist_en = COALESCE(NULLIF(excluded.artist_en, ''), track_list.artist_en),
                album_en = COALESCE(NULLIF(excluded.album_en, ''), track_list.album_en),
                artwork_url = COALESCE(NULLIF(excluded.artwork_url, ''), track_list.artwork_url)
            """,
            track_list_params
        )

    if playlist_order_params:
        conn.executemany(
            """
            INSERT INTO playlist_order(
                service, job_name, source_variant, reference_period,
                song_id, rank_order
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            playlist_order_params
        )


def persist_crawled_tracks(
    db_path: str | Path,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    tracks: Iterable[Any],
    conn: Any = None,
    commit: bool = True,
) -> None:
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    _validate_raw_tracks(service, job_name, track_rows)

    if conn is not None:
        if type(conn).__name__ != "PostgresConnectionWrapper":
            init_schema(conn)
        _persist_crawled_tracks_impl(conn, service, job_name, source_variant, chart_date, reference_period, chart_period, track_rows)
        if commit:
            conn.commit()
    else:
        init_db(db_path)
        with connect(db_path) as new_conn:
            _persist_crawled_tracks_impl(new_conn, service, job_name, source_variant, chart_date, reference_period, chart_period, track_rows)


def _rows_by_in(conn: Any, sql_prefix: str, values: list[str], params_prefix: tuple[Any, ...] = ()) -> list[Any]:
    if not values:
        return []
    values = list(dict.fromkeys(values))
    placeholders = ",".join("?" for _ in values)
    return conn.execute(f"{sql_prefix} ({placeholders})", (*params_prefix, *values)).fetchall()


def _persist_crawl_run_bulk_impl(
    conn: Any,
    service: str,
    job_name: str,
    source_variant: str,
    chart_date: str,
    reference_period: str | None,
    chart_period: str | None,
    started_at: str,
    tracks: Iterable[Any],
    matches: Iterable[Any],
    skip_playlist_order: bool = False,
) -> None:
    service = normalized_service(service)
    track_rows = [row_dict(t) for t in tracks]
    match_rows = [row_dict(m) for m in matches]
    ref_p = reference_period or chart_period
    resolved_period = reference_period_for_date(job_name, chart_date, ref_p)
    if not resolved_period:
        raise ValueError(f"Could not resolve reference period for match run '{job_name}'.")
    scope = (service, job_name, source_variant, resolved_period)
    if type(conn).__name__ == "PostgresConnectionWrapper":
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("|".join(scope),))
    elif not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    _validate_effective_snapshot(
        conn,
        service=service,
        job_name=job_name,
        source_variant=source_variant,
        reference_period=resolved_period,
        tracks=track_rows,
    )
    LOG.info("Skipping playlist_order rewrite for %s / %s / %s; raw chart order is persisted separately.", service, job_name, source_variant)

    run_id = start_match_run(
        conn,
        service=service,
        job_name=job_name,
        source_variant=source_variant,
        reference_period=resolved_period,
        started_at=started_at,
        source="crawler",
        total_tracks=len(track_rows),
    )
    track_by_song = {
        sid: track for track in track_rows
        if (sid := normalize_song_id(service, track))
    }
    song_ids = [sid for row in match_rows if (sid := normalize_song_id(service, row))]
    video_ids = [
        str(row.get("video_id") or row.get("canonical_yt_video_id") or "").strip()
        for row in match_rows
        if str(row.get("video_id") or row.get("canonical_yt_video_id") or "").strip()
    ]

    overrides = {
        row["song_id"]: dict(row)
        for row in _rows_by_in(
            conn,
            "SELECT * FROM manual_overrides WHERE service = ? AND song_id IN",
            song_ids,
            (service,),
        )
    }
    song_to_uid = {
        row["song_id"]: row["track_uid"]
        for row in _rows_by_in(
            conn,
            "SELECT song_id, track_uid FROM platform_song_ids WHERE service = ? AND song_id IN",
            song_ids,
            (service,),
        )
    }
    video_to_uid = {
        row["video_id"]: row["track_uid"]
        for row in _rows_by_in(
            conn,
            "SELECT video_id, track_uid FROM yt_video_ids WHERE video_id IN",
            video_ids,
        )
    }
    override_video_ids = [
        ov["canonical_yt_video_id"]
        for ov in overrides.values()
        if ov.get("action") == "set_canonical" and ov.get("canonical_yt_video_id")
    ]
    if override_video_ids:
        video_to_uid.update(
            {
                row["video_id"]: row["track_uid"]
                for row in _rows_by_in(
                    conn,
                    "SELECT video_id, track_uid FROM yt_video_ids WHERE video_id IN",
                    override_video_ids,
                )
            }
        )

    known_uids = set(song_to_uid.values()) | set(video_to_uid.values())
    known_uids.update(str(ov.get("target_track_uid") or "") for ov in overrides.values() if ov.get("target_track_uid"))
    tracks_by_uid = {
        row["track_uid"]: dict(row)
        for row in _rows_by_in(
            conn,
            "SELECT track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album, match_status, best_score FROM tracks WHERE track_uid IN",
            [uid for uid in known_uids if uid],
        )
    }

    now = utc_now_iso()
    tracks_params: list[tuple[Any, ...]] = []
    platform_song_ids_params: list[tuple[Any, ...]] = []
    track_list_params: list[tuple[Any, ...]] = []
    match_attempt_params: list[tuple[Any, ...]] = []
    match_candidate_params: list[tuple[Any, ...]] = []
    metadata_params: list[tuple[Any, ...]] = []
    blocked_song_ids: list[str] = []
    blocked_track_uids: list[str] = []
    manual_canonical_updates: list[tuple[str, str, str, str, str]] = []
    matched_count = 0
    failed_count = 0
    cache_hits = 0
    proxy_hits = 0

    failed_statuses = {"failed", "duplicate_skipped", "manual_blocked"}
    replaceable_statuses = failed_statuses | {"unmatched"}

    for match in match_rows:
        song_id = normalize_song_id(service, match)
        source_row = track_by_song.get(song_id, match)
        merged = dict(source_row)
        merged.update({k: v for k, v in match.items() if v not in (None, "")})
        video_id = str(merged.get("video_id") or merged.get("canonical_yt_video_id") or "").strip()
        observed_video_id = video_id
        status = str(merged.get("status") or ("matched" if video_id else "failed"))
        score = float(merged.get("score") or 0)
        override = overrides.get(song_id) if song_id else None

        override_action = str((override or {}).get("action") or "").lower()
        if override_action in {"block", "manual_blocked"} or status == "manual_blocked":
            track_uid = stable_uid(f"blocked:{service}:{song_id}")
            canonical_video = None
            status = "manual_blocked"
            if song_id:
                blocked_song_ids.append(song_id)
            blocked_track_uids.append(track_uid)
        elif override_action == "split":
            track_uid = stable_uid(f"split:{service}:{song_id}")
            canonical_video = video_id or None
        elif override and override_action == "set_canonical" and override.get("canonical_yt_video_id"):
            canonical_video = override["canonical_yt_video_id"]
            track_uid = video_to_uid.get(canonical_video) or stable_uid(f"yt:{canonical_video}")
            status = "manual_override"
        elif override and override.get("target_track_uid"):
            track_uid = override["target_track_uid"]
            target = tracks_by_uid.get(track_uid) or {}
            canonical_video = target.get("canonical_yt_video_id") or video_id
            status = "manual_override"
        elif status in failed_statuses or (service == "spotify" and str(song_id).startswith("fallback:")):
            track_uid = stable_uid(f"unmatched:{service}:{song_id or metadata_key(merged.get('title'), merged.get('artist'), merged.get('album'))}")
            canonical_video = song_id if (service == "ytmusic" and song_id and not str(song_id).startswith("fallback:")) else None
        else:
            existing_by_song = song_to_uid.get(song_id)
            existing_by_video = video_to_uid.get(video_id)
            song_track = tracks_by_uid.get(existing_by_song or "")
            song_video = song_track.get("canonical_yt_video_id") if song_track else ""
            song_status = song_track.get("match_status") if song_track else ""
            if existing_by_song and song_video:
                if existing_by_video and existing_by_video != existing_by_song and (
                    song_video == video_id or song_status in replaceable_statuses
                ):
                    track_uid = existing_by_video
                else:
                    track_uid = existing_by_song
            elif existing_by_video:
                track_uid = existing_by_video
            elif video_id:
                track_uid = stable_uid(f"yt:{video_id}")
            elif song_id:
                track_uid = stable_uid(f"{service}:{song_id}")
            else:
                track_uid = stable_uid(metadata_key(merged.get("title"), merged.get("artist"), merged.get("album")))
            canonical_video = video_id

        current_track = tracks_by_uid.get(track_uid) or {}
        metadata_matches_canonical = not observed_video_id or observed_video_id == canonical_video
        canonical_video, canonical_title, canonical_artist, canonical_album, identity_supported = (
            _canonical_track_values(
                current_track,
                video_id=canonical_video,
                yt_title=merged.get("yt_title", "") if metadata_matches_canonical else "",
                yt_artist=merged.get("yt_artist", "") if metadata_matches_canonical else "",
                yt_album=merged.get("yt_album", "") if metadata_matches_canonical else "",
                allow_switch=override_action in {"set_canonical", "split"},
            )
        )
        identity_conflict = bool(observed_video_id and not identity_supported)
        if identity_conflict:
            record_conflict(
                conn,
                service=service,
                song_id=song_id,
                existing_track_uid=track_uid,
                incoming_track_uid=video_to_uid.get(observed_video_id),
                existing_video_id=current_track.get("canonical_yt_video_id"),
                incoming_video_id=observed_video_id,
                reason="same_track_uid_different_video",
                payload=merged,
            )
        persisted_status = status
        persisted_score = score
        if identity_conflict:
            persisted_status = str(current_track.get("match_status") or status)
            persisted_score = float(current_track.get("best_score") or 0)
        tracks_by_uid[track_uid] = {
            **current_track,
            "track_uid": track_uid,
            "canonical_yt_video_id": canonical_video,
            "yt_title": canonical_title,
            "yt_artist": canonical_artist,
            "yt_album": canonical_album,
            "match_status": persisted_status,
            "best_score": persisted_score,
        }

        tracks_params.append((
            track_uid,
            canonical_video,
            canonical_title,
            canonical_artist,
            canonical_album,
            persisted_status,
            persisted_score,
            now,
            now,
        ))
        if override_action == "set_canonical" and canonical_video:
            manual_canonical_updates.append((
                canonical_video,
                track_uid,
                canonical_title,
                canonical_artist,
                canonical_album,
            ))
        if (
            song_id
            and (
                override_action == "split"
                or (canonical_video and status not in failed_statuses)
            )
            and not (service == "spotify" and str(song_id).startswith("fallback:"))
        ):
            platform_song_ids_params.append((service, song_id, track_uid))
        if song_id and not (service == "spotify" and str(song_id).startswith("fallback:")):
            track_list_params.append(track_list_metadata_params(service=service, song_id=song_id, row=merged))
        if canonical_video and status not in failed_statuses and override_action != "split":
            metadata_params.extend(metadata_lookup_params(
                track_uid=track_uid,
                row=source_row if identity_conflict else merged,
                source=status,
                score=score,
            ))
        rank_order = int(match.get("rank") or source_row.get("rank") or 0)
        if song_id:
            match_method, origin_method, _ = match_method_for_status(status, merged.get("query"))
            match_attempt_params.append((
                run_id,
                service,
                song_id,
                track_uid,
                rank_order,
                observed_video_id or canonical_video or "",
                float(match.get("score") or 0),
                float(match.get("title_score") or 0),
                float(match.get("artist_score") or 0),
                float(match.get("album_score") or 0),
                match.get("yt_result_type", ""),
                merged.get("query", ""),
                status,
                match.get("match_method", match_method),
                match.get("origin_method", origin_method),
                now,
            ))
            for index, candidate in enumerate(match.get("candidates") or [], 1):
                match_candidate_params.append((
                    run_id,
                    service,
                    song_id,
                    rank_order,
                    index,
                    candidate.get("video_id") or candidate.get("videoId", ""),
                    candidate.get("yt_title") or candidate.get("title", ""),
                    candidate.get("yt_artist") or candidate.get("artist", ""),
                    candidate.get("yt_album") or candidate.get("album", ""),
                    float(candidate.get("score") or 0),
                    float(candidate.get("title_score") or 0),
                    float(candidate.get("artist_score") or 0),
                    float(candidate.get("album_score") or 0),
                    candidate.get("yt_result_type") or candidate.get("resultType", ""),
                    candidate.get("query", ""),
                    now,
                ))
        if (observed_video_id or canonical_video) and status not in failed_statuses:
            matched_count += 1
        else:
            failed_count += 1
        if match.get("status") == "cached_match" or match.get("query") == "db_cache":
            cache_hits += 1
        if match.get("status") == "proxy_matched":
            proxy_hits += 1

    if tracks_params:
        conn.executemany(
            """
            INSERT INTO tracks (
                track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
                match_status, best_score, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_uid) DO UPDATE SET
                canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
                yt_title = CASE
                    WHEN tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                    THEN COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title)
                    ELSE tracks.yt_title
                END,
                yt_artist = CASE
                    WHEN tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                    THEN COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist)
                    ELSE tracks.yt_artist
                END,
                yt_album = CASE
                    WHEN tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                    THEN COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album)
                    ELSE tracks.yt_album
                END,
                match_status = CASE
                    WHEN (tracks.canonical_yt_video_id IS NULL
                          OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id)
                     AND excluded.match_status != 'failed'
                    THEN excluded.match_status
                    ELSE tracks.match_status
                END,
                best_score = CASE
                    WHEN (tracks.canonical_yt_video_id IS NULL
                          OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id)
                     AND COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0)
                    THEN COALESCE(excluded.best_score, 0)
                    ELSE COALESCE(tracks.best_score, 0)
                END,
                updated_at = CASE
                    WHEN tracks.canonical_yt_video_id IS NULL
                      OR tracks.canonical_yt_video_id = excluded.canonical_yt_video_id
                    THEN excluded.updated_at
                    ELSE tracks.updated_at
                END
            """,
            tracks_params,
        )
    if blocked_track_uids:
        placeholders = ",".join("?" for _ in blocked_track_uids)
        conn.execute(
            f"UPDATE tracks SET canonical_yt_video_id = NULL, match_status = 'manual_blocked', updated_at = ? "
            f"WHERE track_uid IN ({placeholders})",
            (now, *blocked_track_uids),
        )
        conn.execute(
            f"DELETE FROM yt_video_ids WHERE track_uid IN ({placeholders})",
            tuple(blocked_track_uids),
        )
    for canonical_video, track_uid, yt_title, yt_artist, yt_album in manual_canonical_updates:
        owner = find_track_by_video(conn, canonical_video)
        if owner and owner != track_uid:
            raise ValueError(
                f"Manual canonical video {canonical_video} is already owned by another track"
            )
        conn.execute(
            """
            UPDATE tracks
            SET canonical_yt_video_id = ?, yt_title = ?, yt_artist = ?, yt_album = ?,
                match_status = 'manual_override', updated_at = ?
            WHERE track_uid = ?
            """,
            (canonical_video, yt_title, yt_artist, yt_album, now, track_uid),
        )
    _sync_canonical_video_flags(
        conn,
        [params[0] for params in tracks_params],
    )
    if platform_song_ids_params:
        conn.executemany(
            """
            INSERT INTO platform_song_ids(service, song_id, track_uid)
            VALUES (?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET track_uid = excluded.track_uid
            """,
            platform_song_ids_params,
        )
    if blocked_song_ids:
        placeholders = ",".join("?" for _ in blocked_song_ids)
        conn.execute(
            f"DELETE FROM platform_song_ids WHERE service = ? AND song_id IN ({placeholders})",
            (service, *blocked_song_ids),
        )
    if track_list_params:
        conn.executemany(
            """
            INSERT INTO track_list(
                service, song_id, album_id, title_ko, artist_ko, album_ko,
                title_en, artist_en, album_en, artwork_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, song_id) DO UPDATE SET
                album_id = COALESCE(NULLIF(excluded.album_id, ''), track_list.album_id),
                title_ko = COALESCE(NULLIF(excluded.title_ko, ''), track_list.title_ko),
                artist_ko = COALESCE(NULLIF(excluded.artist_ko, ''), track_list.artist_ko),
                album_ko = COALESCE(NULLIF(excluded.album_ko, ''), track_list.album_ko),
                title_en = COALESCE(NULLIF(excluded.title_en, ''), track_list.title_en),
                artist_en = COALESCE(NULLIF(excluded.artist_en, ''), track_list.artist_en),
                album_en = COALESCE(NULLIF(excluded.album_en, ''), track_list.album_en),
                artwork_url = COALESCE(NULLIF(excluded.artwork_url, ''), track_list.artwork_url)
            """,
            track_list_params,
        )
    if metadata_params:
        conn.executemany(
            """
            INSERT INTO metadata_lookup_index(lookup_key, track_uid, source, score)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lookup_key) DO UPDATE SET
                track_uid = CASE WHEN excluded.score >= metadata_lookup_index.score THEN excluded.track_uid ELSE metadata_lookup_index.track_uid END,
                source = CASE WHEN excluded.score >= metadata_lookup_index.score THEN excluded.source ELSE metadata_lookup_index.source END,
                score = CASE WHEN excluded.score > metadata_lookup_index.score THEN excluded.score ELSE metadata_lookup_index.score END
            """,
            metadata_params,
        )
    if match_attempt_params:
        conn.executemany(
            """
            INSERT INTO match_attempts(
                run_id, service, song_id, track_uid, rank_order,
                video_id, score, title_score, artist_score,
                album_score, yt_result_type, query, status, match_method, origin_method,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, service, song_id, rank_order) DO UPDATE SET
                track_uid = EXCLUDED.track_uid,
                rank_order = EXCLUDED.rank_order,
                video_id = EXCLUDED.video_id,
                score = EXCLUDED.score,
                title_score = EXCLUDED.title_score,
                artist_score = EXCLUDED.artist_score,
                album_score = EXCLUDED.album_score,
                yt_result_type = EXCLUDED.yt_result_type,
                query = EXCLUDED.query,
                status = EXCLUDED.status,
                match_method = EXCLUDED.match_method,
                origin_method = EXCLUDED.origin_method,
                created_at = EXCLUDED.created_at
            """,
            match_attempt_params,
        )
    if match_candidate_params:
        conn.executemany(
            """
            INSERT INTO match_candidates(
                run_id, service, song_id, rank_order, candidate_order, video_id,
                yt_title, yt_artist, yt_album, score, title_score, artist_score,
                album_score, yt_result_type, query, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (run_id, service, song_id, rank_order, candidate_order) DO UPDATE SET
                video_id = EXCLUDED.video_id,
                yt_title = EXCLUDED.yt_title,
                yt_artist = EXCLUDED.yt_artist,
                yt_album = EXCLUDED.yt_album,
                score = EXCLUDED.score,
                title_score = EXCLUDED.title_score,
                artist_score = EXCLUDED.artist_score,
                album_score = EXCLUDED.album_score,
                yt_result_type = EXCLUDED.yt_result_type,
                query = EXCLUDED.query,
                created_at = EXCLUDED.created_at
            """,
            match_candidate_params,
        )
    cleanup_old_attempts_and_candidates(conn, days=15)
    conn.execute(
        """
        UPDATE match_runs
        SET matched_tracks = ?, failed_tracks = ?, cache_hits = ?, proxy_hits = ?,
            status = 'completed', completed_at = ?
        WHERE run_id = ?
        """,
        (matched_count, failed_count, cache_hits, proxy_hits, utc_now_iso(), run_id),
    )


def persist_crawl_run(
    db_path: str | Path,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    started_at: str,
    tracks: Iterable[Any],
    matches: Iterable[Any],
    conn: Any = None,
    skip_playlist_order: bool = False,
) -> None:
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    match_rows = [row_dict(m) for m in matches]
    if not skip_playlist_order:
        raise ValueError(
            "persist_crawl_run no longer writes playlist_order; persist raw tracks first "
            "and call with skip_playlist_order=True."
        )
    _validate_effective_tracks(service, job_name, track_rows, match_rows)

    if conn is not None:
        if type(conn).__name__ != "PostgresConnectionWrapper":
            init_schema(conn)
        _persist_crawl_run_bulk_impl(
            conn,
            service,
            job_name,
            source_variant,
            chart_date,
            reference_period,
            chart_period,
            started_at,
            track_rows,
            match_rows,
            skip_playlist_order=skip_playlist_order,
        )
        conn.commit()
    else:
        init_db(db_path)
        with connect(db_path) as new_conn:
            _persist_crawl_run_bulk_impl(
                new_conn,
                service,
                job_name,
                source_variant,
                chart_date,
                reference_period,
                chart_period,
                started_at,
                track_rows,
                match_rows,
                skip_playlist_order=skip_playlist_order,
            )


def repair_failed_source_bindings(conn: sqlite3.Connection) -> dict[str, int]:
    """Move failed exact source-id bindings back to known canonical tracks."""
    updated_bindings = 0
    merged_tracks = 0

    failed_bindings = conn.execute(
        """
        SELECT ps.service, ps.song_id, ps.track_uid, t.canonical_yt_video_id, t.match_status
        FROM platform_song_ids ps
        LEFT JOIN tracks t ON t.track_uid = ps.track_uid
        WHERE COALESCE(t.canonical_yt_video_id, '') = ''
           OR COALESCE(t.match_status, '') IN ('failed', 'duplicate_skipped', 'manual_blocked', 'unmatched')
        """
    ).fetchall()
    for row in failed_bindings:
        override = manual_override(conn, row["service"], row["song_id"])
        if override and str(override["action"] or "").lower() in {"block", "split", "manual_blocked"}:
            continue
        source_meta_row = conn.execute(
            """
            SELECT tl.service, tl.song_id, tl.title_ko, tl.artist_ko, tl.album_ko,
                   tl.title_en, tl.artist_en, tl.album_en
            FROM track_list tl
            WHERE tl.service = ? AND tl.song_id = ?
            """,
            (row["service"], row["song_id"]),
        ).fetchone()
        source_meta = dict(source_meta_row) if source_meta_row else {}

        def approved_target(target_uid: str) -> bool:
            candidates = [
                meta
                for meta in _track_metadata_rows(conn, target_uid)
                if not (
                    normalized_service(meta.get("service")) == normalized_service(row["service"])
                    and str(meta.get("song_id") or "") == str(row["song_id"])
                )
            ]
            target = conn.execute(
                "SELECT yt_title AS title, yt_artist AS artist, yt_album AS album FROM tracks WHERE track_uid = ?",
                (target_uid,),
            ).fetchone()
            if target:
                candidates.append(dict(target))
            return bool(source_meta) and any(
                _metadata_rows_equivalent(source_meta, candidate)
                for candidate in candidates
            )

        candidate = conn.execute(
            """
            SELECT video_id, track_uid, score, created_at
            FROM match_attempts
            WHERE service = ?
              AND song_id = ?
              AND COALESCE(video_id, '') != ''
              AND status IN ('matched', 'cached_match', 'proxy_matched', 'manual_override')
            ORDER BY score DESC, created_at DESC
            LIMIT 1
            """,
            (row["service"], row["song_id"]),
        ).fetchone()
        if not candidate:
            if source_meta:
                target_uid = find_track_by_metadata(
                    conn,
                    source_meta.get("title_ko") or source_meta.get("title_en") or "",
                    source_meta.get("artist_ko") or source_meta.get("artist_en") or "",
                    source_meta.get("album_ko") or source_meta.get("album_en") or "",
                )
                if target_uid and approved_target(target_uid):
                    target = conn.execute(
                        "SELECT canonical_yt_video_id FROM tracks WHERE track_uid = ?",
                        (target_uid,),
                    ).fetchone()
                    if target and target["canonical_yt_video_id"]:
                        conn.execute(
                            "UPDATE platform_song_ids SET track_uid = ? WHERE service = ? AND song_id = ?",
                            (target_uid, row["service"], row["song_id"]),
                        )
                        updated_bindings += 1
            continue

        target_uid = find_track_by_video(conn, candidate["video_id"])
        if target_uid and target_uid != row["track_uid"] and approved_target(target_uid):
            conn.execute(
                "UPDATE platform_song_ids SET track_uid = ? WHERE service = ? AND song_id = ?",
                (target_uid, row["service"], row["song_id"]),
            )
            updated_bindings += 1

    duplicate_videos = conn.execute(
        """
        SELECT canonical_yt_video_id
        FROM tracks
        WHERE COALESCE(canonical_yt_video_id, '') != ''
        GROUP BY canonical_yt_video_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicate_videos:
        video_id = row["canonical_yt_video_id"]
        manual_split = conn.execute(
            """
            SELECT 1
            FROM manual_overrides
            WHERE action = 'split'
              AND (
                  canonical_yt_video_id = ?
                  OR target_track_uid IN (
                      SELECT track_uid FROM tracks WHERE canonical_yt_video_id = ?
                  )
              )
            LIMIT 1
            """,
            (video_id, video_id),
        ).fetchone()
        if manual_split:
            continue
        indexed_uid = find_track_by_video(conn, video_id)
        candidates = conn.execute(
            """
            SELECT track_uid, best_score, created_at
            FROM tracks
            WHERE canonical_yt_video_id = ?
            ORDER BY
                CASE WHEN track_uid = ? THEN 0 ELSE 1 END,
                best_score DESC,
                created_at ASC
            """,
            (video_id, indexed_uid or ""),
        ).fetchall()
        if len(candidates) < 2:
            continue
        candidate_uids = [candidate["track_uid"] for candidate in candidates]
        uid_placeholders = ",".join("?" for _ in candidate_uids)
        direct_manual = conn.execute(
            f"""
            SELECT 1
            FROM manual_overrides mo
            LEFT JOIN platform_song_ids ps
              ON ps.service = mo.service AND ps.song_id = mo.song_id
            WHERE mo.action IN ('split', 'block', 'manual_blocked')
              AND (
                    mo.target_track_uid IN ({uid_placeholders})
                 OR ps.track_uid IN ({uid_placeholders})
              )
            LIMIT 1
            """,
            tuple(candidate_uids + candidate_uids),
        ).fetchone()
        if direct_manual:
            continue
        target_uid = candidates[0]["track_uid"]
        conn.execute(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                track_uid = excluded.track_uid,
                is_canonical = 1
            """,
            (video_id, target_uid),
        )
        for candidate in candidates[1:]:
            old_uid = candidate["track_uid"]
            _merge_track_uids(
                conn,
                loser_uid=old_uid,
                winner_uid=target_uid,
                canonical_video=video_id,
            )
            merged_tracks += 1

    return {"updated_bindings": updated_bindings, "merged_tracks": merged_tracks}


def _normalize_playlist_items(
    items: Iterable[Any],
    *,
    require_set_video_id: bool,
    require_contiguous: bool = True,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_set_ids: set[str] = set()
    for fallback_position, item in enumerate(items, 1):
        if isinstance(item, str):
            video_id = item.strip()
            set_video_id = ""
            position = fallback_position
        elif isinstance(item, Mapping):
            video_id = str(
                item.get("videoId")
                or item.get("video_id")
                or item.get("requestedVideoId")
                or ""
            ).strip()
            set_video_id = str(item.get("setVideoId") or item.get("set_video_id") or "").strip()
            try:
                position = int(item.get("position") or fallback_position)
            except (TypeError, ValueError) as exc:
                raise ValueError("Playlist item position must be an integer") from exc
        else:
            raise ValueError("Playlist evidence items must be strings or mappings")
        if not video_id or position < 1:
            raise ValueError("Playlist evidence items need a video ID and positive position")
        if require_set_video_id and not set_video_id:
            raise ValueError(f"Playlist item {position} is missing setVideoId")
        if set_video_id:
            if set_video_id in seen_set_ids:
                raise ValueError(f"Duplicate setVideoId in playlist evidence: {set_video_id}")
            seen_set_ids.add(set_video_id)
        normalized.append(
            {"position": position, "video_id": video_id, "set_video_id": set_video_id}
        )
    if require_contiguous and [item["position"] for item in normalized] != list(
        range(1, len(normalized) + 1)
    ):
        raise ValueError("Playlist snapshot positions must be contiguous and ordered")
    return normalized


def _decode_recovery_payload(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}") if not isinstance(value, dict) else dict(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _serialize_recovery_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _playlist_update_state_fingerprint(run: Mapping[str, Any]) -> str:
    value = json.dumps(
        [
            run.get("update_run_id"),
            run.get("playlist_id"),
            run.get("status"),
            run.get("completed_at"),
            run.get("error") or "",
            run.get("differences_json") or "[]",
            int(run.get("evidence_version") or 0),
            run.get("recovery_payload_json") or "{}",
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _next_evidence_seq(payload: Mapping[str, Any]) -> int:
    values = [
        int(entry.get("seq") or 0)
        for name in ("events", "outcomes")
        for entry in payload.get(name, [])
        if isinstance(entry, Mapping)
    ]
    return max([int(payload.get("next_seq") or 1) - 1, *values], default=0) + 1


def _begin_playlist_update_write(conn: Any) -> bool:
    postgres = type(conn).__name__ == "PostgresConnectionWrapper"
    if not postgres:
        conn.execute("BEGIN IMMEDIATE")
    return postgres


def _locked_playlist_update_run(conn: Any, update_run_id: str, postgres: bool) -> Any:
    suffix = " FOR UPDATE" if postgres else ""
    row = conn.execute(
        "SELECT * FROM playlist_update_runs WHERE update_run_id = ?" + suffix,
        (update_run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown playlist update run: {update_run_id}")
    return row


def _require_playlist_claim(payload: Mapping[str, Any], claim_token: str) -> None:
    expected = str((payload.get("claim") or {}).get("token") or "")
    if not expected or not claim_token or claim_token != expected:
        raise RuntimeError("Playlist update claim token is missing or stale")


def _playlist_payload_is_complete(run: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    if int(run.get("evidence_version") or 0) != 1 or int(payload.get("version") or 0) != 1:
        return False
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("complete") is not True:
        return False
    requested = snapshot.get("requested_video_ids")
    existing = snapshot.get("existing_items")
    if not isinstance(requested, list) or not isinstance(existing, list):
        return False
    if len(requested) != int(run.get("requested_count") or 0):
        return False
    if len(existing) != int(run.get("existing_count") or 0):
        return False
    for item in requested:
        if not isinstance(item, str) or not item:
            return False
    set_ids: list[str] = []
    for position, item in enumerate(existing, 1):
        if (
            not isinstance(item, Mapping)
            or item.get("position") != position
            or not item.get("video_id")
            or not item.get("set_video_id")
        ):
            return False
        set_ids.append(str(item["set_video_id"]))
    claim = payload.get("claim")
    return len(set_ids) == len(set(set_ids)) and isinstance(claim, Mapping) and bool(claim.get("token"))


def _append_payload_entry(
    payload: dict[str, Any],
    collection: str,
    entry: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    stored = dict(entry)
    stored["seq"] = _next_evidence_seq(payload)
    stored["at"] = now
    payload.setdefault(collection, []).append(stored)
    payload["next_seq"] = stored["seq"] + 1
    return stored


def _normalize_playlist_evidence_event(event: Mapping[str, Any]) -> dict[str, Any]:
    phase = str(event.get("phase") or "")
    operation = str(event.get("operation") or "")
    state = str(event.get("state") or "")
    if phase not in PLAYLIST_EVIDENCE_PHASES:
        raise ValueError(f"Unsupported playlist evidence phase: {phase}")
    if operation not in PLAYLIST_EVIDENCE_OPERATIONS:
        raise ValueError(f"Unsupported playlist evidence operation: {operation}")
    if state not in PLAYLIST_EVIDENCE_STATES:
        raise ValueError(f"Unsupported playlist evidence state: {state}")
    try:
        chunk_order = int(event.get("chunk_order") or 0)
        attempt = int(event.get("attempt") or 1)
        intent_seq = int(event.get("intent_seq") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Playlist evidence sequence fields must be integers") from exc
    if chunk_order < 0 or attempt < 1:
        raise ValueError("Playlist evidence chunk and attempt must be non-negative")
    items = _normalize_playlist_items(
        event.get("items") or (),
        require_set_video_id=(operation == "remove" and state == "intent")
        or (operation == "add" and state == "ack")
        or (
            operation == "observe"
            and state == "verified"
            and event.get("observation_complete") is True
        ),
        require_contiguous=False,
    )
    normalized: dict[str, Any] = {
        "phase": phase,
        "operation": operation,
        "state": state,
        "chunk_order": chunk_order,
        "attempt": attempt,
        "items": items,
    }
    for key in ("before_items", "after_items"):
        if key in event:
            normalized[key] = _normalize_playlist_items(
                event.get(key) or (),
                require_set_video_id=True,
                require_contiguous=True,
            )
    if state in {"ack", "ambiguous"}:
        if intent_seq < 1:
            raise ValueError("Playlist evidence receipt needs intent_seq")
        normalized["intent_seq"] = intent_seq
    for key in ("provider_status", "error", "attestation"):
        if event.get(key):
            normalized[key] = str(event[key])
    if event.get("reconciliation_action"):
        normalized["reconciliation_action"] = str(event["reconciliation_action"])
    if "review_checks" in event:
        if not isinstance(event["review_checks"], list):
            raise ValueError("Playlist evidence review_checks must be a list")
        normalized["review_checks"] = json.loads(
            json.dumps(event["review_checks"], ensure_ascii=False, default=str)
        )
    for key in (
        "observation_complete",
        "verification_matches",
        "restore_verified",
        "identity_review_required",
    ):
        if key in event:
            normalized[key] = bool(event[key])
    if "differences" in event:
        if not isinstance(event["differences"], list):
            raise ValueError("Playlist evidence differences must be a list")
        normalized["differences"] = json.loads(
            json.dumps(event["differences"], ensure_ascii=False, default=str)
        )
    if "publication_mode" in event:
        if event["publication_mode"] != "partial" or phase not in {"publish", "reconcile"} or (
            operation, state
        ) != ("observe", "verified"):
            raise ValueError("Partial publication policy requires a publish/reconcile observation")
        effective = event.get("effective_video_ids")
        excluded = event.get("excluded_items")
        if not isinstance(effective, list) or not effective or any(
            not isinstance(video_id, str) or not video_id.strip() for video_id in effective
        ):
            raise ValueError("Partial publication needs a non-empty effective video list")
        if not isinstance(excluded, list) or not excluded:
            raise ValueError("Partial publication needs explicit excluded items")
        exclusions = []
        for item in excluded:
            if not isinstance(item, Mapping) or type(item.get("position")) is not int or item["position"] < 1:
                raise ValueError("Excluded playlist positions must be positive integers")
            if any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("requested_video_id", "actual_video_id", "reason")
            ):
                raise ValueError("Excluded playlist items need requested/actual IDs and a reason")
            exclusions.append({
                "position": item["position"],
                **{key: item[key] for key in ("requested_video_id", "actual_video_id", "reason")},
            })
        normalized.update(
            publication_mode="partial",
            effective_video_ids=list(effective),
            excluded_items=exclusions,
        )
    return normalized


def _latest_partial_publication(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next(
        (
            event for event in reversed(payload.get("events", []))
            if isinstance(event, Mapping) and event.get("publication_mode") == "partial"
        ),
        None,
    )


def _assert_partial_publication_request(payload: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    if event.get("publication_mode") != "partial":
        return
    requested = (payload.get("snapshot") or {}).get("requested_video_ids")
    if not isinstance(requested, list):
        raise ValueError("Partial publication requires the complete original request")
    excluded = event["excluded_items"]
    positions = [item["position"] for item in excluded]
    if positions != sorted(set(positions)) or any(position > len(requested) for position in positions):
        raise ValueError("Excluded playlist positions must be unique, ordered original slots")
    if any(requested[item["position"] - 1] != item["requested_video_id"] for item in excluded):
        raise ValueError("Excluded playlist item does not match the original request")
    if event["effective_video_ids"] != [
        video_id for position, video_id in enumerate(requested, 1) if position not in positions
    ]:
        raise ValueError("Effective playlist must preserve all non-excluded requested slots in order")


def _assert_receipt_matches_intent(payload: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    if event.get("state") not in {"ack", "ambiguous"}:
        return
    intent_seq = int(event["intent_seq"])
    intent = next(
        (
            item
            for item in payload.get("events", [])
            if isinstance(item, Mapping) and int(item.get("seq") or 0) == intent_seq
        ),
        None,
    )
    if not intent or intent.get("state") != "intent" or any(
        intent.get(key) != event.get(key)
        for key in ("phase", "operation", "chunk_order", "attempt")
    ):
        raise ValueError("Playlist evidence receipt does not match its intent")
    if any(
        isinstance(item, Mapping)
        and item.get("state") in {"ack", "ambiguous"}
        and int(item.get("intent_seq") or 0) == intent_seq
        for item in payload.get("events", [])
    ):
        raise ValueError("Playlist evidence intent already has a receipt")
    if event.get("operation") == "add" and event.get("state") == "ack":
        if len(event.get("items", [])) != len(intent.get("items", [])):
            raise ValueError("Add receipt item count does not match its intent")
        used_set_ids = {
            str(item.get("set_video_id") or "")
            for item in (payload.get("snapshot") or {}).get("existing_items", [])
            if isinstance(item, Mapping) and item.get("set_video_id")
        }
        used_set_ids.update(
            str(item.get("set_video_id") or "")
            for prior in payload.get("events", [])
            if isinstance(prior, Mapping)
            and prior.get("operation") == "add"
            and prior.get("state") == "ack"
            for item in prior.get("items", [])
            if isinstance(item, Mapping) and item.get("set_video_id")
        )
        used_set_ids.update(
            str(item.get("set_video_id") or "")
            for prior in payload.get("events", [])
            if isinstance(prior, Mapping)
            for field in ("before_items", "after_items")
            for item in prior.get(field, [])
            if isinstance(item, Mapping) and item.get("set_video_id")
        )
        acknowledged = {
            str(item.get("set_video_id") or "")
            for item in event.get("items", [])
            if isinstance(item, Mapping) and item.get("set_video_id")
        }
        if acknowledged & used_set_ids:
            raise ValueError("Add receipt reuses a previously observed setVideoId")


def _assert_legacy_tail_event(payload: Mapping[str, Any], event: Mapping[str, Any]) -> None:
    claim = payload.get("claim") or {}
    tail = payload.get("tail_recovery") or {}
    if claim.get("capability") != "append_missing_last" or not isinstance(tail, Mapping):
        raise RuntimeError("Legacy tail append was not explicitly claimed")
    requested = tail.get("requested_video_ids")
    if not isinstance(requested, list) or not requested or any(
        not isinstance(video_id, str) or not video_id for video_id in requested
    ):
        raise RuntimeError("Legacy tail append request evidence is incomplete")
    if event.get("phase") != "reconcile_tail":
        return
    operation = event.get("operation")
    state = event.get("state")
    if operation == "add" and state == "intent":
        prior_adds = [
            prior
            for prior in payload.get("events", [])
            if isinstance(prior, Mapping)
            and prior.get("phase") == "reconcile_tail"
            and prior.get("operation") == "add"
        ]
        if prior_adds:
            raise RuntimeError("Legacy tail append already has a mutation attempt")
        before = event.get("before_items")
        if not isinstance(before, list) or len(before) != len(requested) - 1:
            raise ValueError("Legacy tail append needs the complete prefix item snapshot")
        if event.get("verification_matches") is not True:
            raise ValueError("Legacy tail prefix needs a verified strict comparison")
        items = event.get("items") or []
        if len(items) != 1 or items[0].get("video_id") != requested[-1]:
            raise ValueError("Legacy tail append may add only the requested final video")
        return
    if operation == "add" and state in {"ack", "ambiguous"}:
        intent_seq = int(event.get("intent_seq") or 0)
        intent = next(
            (
                prior
                for prior in payload.get("events", [])
                if isinstance(prior, Mapping)
                and int(prior.get("seq") or 0) == intent_seq
            ),
            None,
        )
        if state == "ack":
            before = (intent or {}).get("before_items") or []
            after = event.get("after_items") or []
            acknowledged = event.get("items") or []
            if len(after) != len(requested):
                raise ValueError("Legacy tail receipt needs the complete post-append snapshot")
            expected_set_ids = {
                item.get("set_video_id") for item in [*before, *acknowledged]
            }
            if {item.get("set_video_id") for item in after} != expected_set_ids:
                raise ValueError("Legacy tail receipt changed a pre-existing playlist slot")
        return
    if operation == "observe" and state == "verified":
        prior_intents = [
            prior
            for prior in payload.get("events", [])
            if isinstance(prior, Mapping)
            and prior.get("phase") == "reconcile_tail"
            and prior.get("operation") == "add"
            and prior.get("state") == "intent"
        ]
        if len(prior_intents) != 1:
            raise RuntimeError("Legacy tail observation has no unique append intent")
        if event.get("observation_complete") is not True or not isinstance(
            event.get("verification_matches"), bool
        ):
            raise ValueError("Legacy tail completion needs a full comparison result")
        if event.get("verification_matches") is False and event.get("identity_review_required") is not True:
            raise ValueError("A rejected legacy tail identity needs explicit review evidence")
        observed = event.get("items") or []
        if len(observed) != len(requested):
            raise ValueError("Legacy tail completion count does not match the request")
        before_set_ids = {
            item.get("set_video_id") for item in prior_intents[0].get("before_items", [])
        }
        observed_set_ids = {item.get("set_video_id") for item in observed}
        if not before_set_ids <= observed_set_ids or len(observed_set_ids - before_set_ids) != 1:
            raise ValueError("Legacy tail completion is not an append-only slot change")
        return
    raise RuntimeError("Legacy tail claim only permits one add and its verified observation")


def _compact_old_playlist_recovery_payloads(
    conn: Any,
    cutoff: str,
    *,
    postgres: bool,
) -> None:
    compact_v1 = _serialize_recovery_payload({"version": 1, "pruned": True})
    compact_v0 = _serialize_recovery_payload(
        {"version": 0, "manual_only": True, "pruned": True}
    )
    if postgres:
        conn.execute(
            """
            UPDATE playlist_update_runs
            SET recovery_payload_json = ?
            WHERE evidence_version = 1
              AND status NOT IN ('running', 'mutation_failed', 'recovery_required')
              AND COALESCE(completed_at, created_at)::timestamptz < CAST(? AS timestamptz)
              AND recovery_payload_json <> ?
            """,
            (compact_v1, cutoff, compact_v1),
        )
        conn.execute(
            """
            UPDATE playlist_update_runs
            SET recovery_payload_json = ?
            WHERE evidence_version = 0
              AND status NOT IN ('running', 'mutation_failed', 'recovery_required')
              AND COALESCE(completed_at, created_at)::timestamptz < CAST(? AS timestamptz)
              AND recovery_payload_json NOT IN ('{}', ?)
            """,
            (compact_v0, cutoff, compact_v0),
        )
    else:
        conn.execute(
            """
            UPDATE playlist_update_runs
            SET recovery_payload_json = ?
            WHERE evidence_version = 1
              AND status NOT IN ('running', 'mutation_failed', 'recovery_required')
              AND datetime(COALESCE(completed_at, created_at)) < datetime(?)
              AND recovery_payload_json <> ?
            """,
            (compact_v1, cutoff, compact_v1),
        )
        conn.execute(
            """
            UPDATE playlist_update_runs
            SET recovery_payload_json = ?
            WHERE evidence_version = 0
              AND status NOT IN ('running', 'mutation_failed', 'recovery_required')
              AND datetime(COALESCE(completed_at, created_at)) < datetime(?)
              AND recovery_payload_json NOT IN ('{}', ?)
            """,
            (compact_v0, cutoff, compact_v0),
        )


def record_playlist_update(
    db_path: str | Path,
    *,
    playlist_id: str,
    service: str = "",
    job_name: str = "",
    requested_video_ids: Iterable[str] = (),
    existing_video_ids: Iterable[str] = (),
    existing_items: Iterable[Mapping[str, Any]] | None = None,
    claim_token: str = "",
    dry_run: bool = False,
) -> str:
    init_db(db_path, repair_source_bindings=False)
    requested = [
        str(value).strip() for value in requested_video_ids if value and str(value).strip()
    ]
    legacy_existing = [
        str(value).strip() for value in existing_video_ids if value and str(value).strip()
    ]
    exact_existing = (
        _normalize_playlist_items(existing_items, require_set_video_id=True)
        if existing_items is not None
        else None
    )
    if exact_existing is not None and legacy_existing and legacy_existing != [
        item["video_id"] for item in exact_existing
    ]:
        raise ValueError("existing_items and existing_video_ids describe different snapshots")
    existing = exact_existing or _normalize_playlist_items(
        legacy_existing, require_set_video_id=False
    )
    job_name = require_job_name(job_name)
    now = utc_now_iso()
    item_retention_cutoff = (datetime.fromisoformat(now) - timedelta(days=31)).isoformat()
    run_id = uuid.uuid4().hex
    evidence_version = 1 if exact_existing is not None else 0
    payload: dict[str, Any] = {}
    if evidence_version:
        payload = {
            "version": 1,
            "snapshot": {
                "complete": True,
                "existing_items": existing,
                "requested_video_ids": requested,
            },
            "claim": {
                "token": claim_token or uuid.uuid4().hex,
                "role": "publisher",
                "claimed_at": now,
            },
            "events": [],
            "outcomes": [],
            "next_seq": 1,
        }
    with connect(db_path) as conn:
        postgres = type(conn).__name__ == "PostgresConnectionWrapper"
        if postgres:
            conn.execute(
                "DELETE FROM playlist_update_items "
                "WHERE created_at::timestamptz < CAST(? AS timestamptz)",
                (item_retention_cutoff,),
            )
        else:
            conn.execute(
                "DELETE FROM playlist_update_items "
                "WHERE datetime(created_at) < datetime(?)",
                (item_retention_cutoff,),
            )
        _compact_old_playlist_recovery_payloads(
            conn, item_retention_cutoff, postgres=postgres
        )
        conn.execute(
            """
            INSERT INTO playlist_update_runs(
                update_run_id, playlist_id, service, job_name, started_at,
                dry_run, requested_count, existing_count, status,
                completed_at, error, differences_json, match_started_at,
                evidence_version, recovery_payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, '', '[]', ?, ?, ?, ?)
            """,
            (
                run_id,
                playlist_id,
                normalized_service(service) if service else "",
                job_name,
                now,
                1 if dry_run else 0,
                len(requested),
                len(existing),
                os.environ.get("HYPE_MATCH_STARTED_AT", ""),
                evidence_version,
                _serialize_recovery_payload(payload),
                now,
            ),
        )
        item_sql = """
            INSERT INTO playlist_update_items(
                update_run_id, action, video_id, set_video_id, item_order, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (update_run_id, action, video_id, item_order) DO UPDATE SET
                set_video_id = EXCLUDED.set_video_id,
                created_at = EXCLUDED.created_at
            """
        conn.executemany(
            item_sql,
            [
                (
                    run_id,
                    "existing",
                    item["video_id"],
                    item["set_video_id"],
                    item["position"],
                    now,
                )
                for item in existing
            ],
        )
        conn.executemany(
            item_sql,
            [
                (run_id, "requested", video_id, "", index, now)
                for index, video_id in enumerate(requested, 1)
            ],
        )
        conn.commit()
    return run_id


def append_playlist_update_evidence(
    db_path: str | Path,
    update_run_id: str,
    event: Mapping[str, Any],
    *,
    claim_token: str,
    expected_state_fingerprint: str = "",
) -> dict[str, Any]:
    """Commit one intent/receipt before the publisher advances to its next side effect."""
    normalized = _normalize_playlist_evidence_event(event)
    init_db(db_path, repair_source_bindings=False)
    now = utc_now_iso()
    with connect(db_path) as conn:
        postgres = _begin_playlist_update_write(conn)
        run = _locked_playlist_update_run(conn, update_run_id, postgres)
        if run["status"] not in ACTIVE_PLAYLIST_UPDATE_STATUSES:
            raise RuntimeError(f"Playlist update run is no longer active: {run['status']}")
        current_fingerprint = _playlist_update_state_fingerprint(dict(run))
        if expected_state_fingerprint and expected_state_fingerprint != current_fingerprint:
            raise RuntimeError("Playlist update evidence changed before append")
        payload = _decode_recovery_payload(run["recovery_payload_json"])
        complete = _playlist_payload_is_complete(dict(run), payload)
        if not complete:
            claim = payload.get("claim") or {}
            legacy_observation = (
                normalized["phase"] == "reconcile"
                and normalized["operation"] in {"observe", "finalize"}
            )
            legacy_tail = (
                claim.get("capability") == "append_missing_last"
                and normalized["phase"] == "reconcile_tail"
            )
            if (
                int(run["evidence_version"] or 0) != 0
                or claim.get("role") != "reconcile"
                or not (legacy_observation or legacy_tail)
            ):
                raise RuntimeError("Playlist update has no complete recovery evidence")
            if not expected_state_fingerprint:
                raise RuntimeError("Legacy evidence append requires the exact state fingerprint")
        _require_playlist_claim(payload, claim_token)
        _assert_partial_publication_request(payload, normalized)
        _assert_receipt_matches_intent(payload, normalized)
        if not complete and normalized["phase"] == "reconcile_tail":
            _assert_legacy_tail_event(payload, normalized)
        stored = _append_payload_entry(payload, "events", normalized, now)
        serialized_payload = _serialize_recovery_payload(payload)
        conn.execute(
            "UPDATE playlist_update_runs SET recovery_payload_json = ? WHERE update_run_id = ? AND status = ?",
            (serialized_payload, update_run_id, run["status"]),
        )
        conn.commit()
    updated_run = dict(run)
    updated_run["recovery_payload_json"] = serialized_payload
    return {**stored, "state_fingerprint": _playlist_update_state_fingerprint(updated_run)}


def claim_playlist_update_recovery(
    db_path: str | Path,
    update_run_id: str,
    *,
    expected_claim_token: str,
    workers_quiescent: bool,
    attestation: str,
    expected_state_fingerprint: str = "",
    expected_statuses: Iterable[str] = ACTIVE_PLAYLIST_UPDATE_STATUSES,
    allow_reclaim: bool = False,
    append_missing_last: bool = False,
) -> dict[str, Any]:
    """Fence the old DB writer after an operator has stopped all publisher workers."""
    if not workers_quiescent or not str(attestation or "").strip():
        raise ValueError("Recovery claim requires an explicit worker-quiescence attestation")
    expected = set(expected_statuses)
    if not expected or not expected <= ACTIVE_PLAYLIST_UPDATE_STATUSES:
        raise ValueError("Recovery claim expected statuses must be active statuses")
    init_db(db_path, repair_source_bindings=False)
    now = utc_now_iso()
    with connect(db_path) as conn:
        postgres = _begin_playlist_update_write(conn)
        run = _locked_playlist_update_run(conn, update_run_id, postgres)
        if run["status"] not in expected:
            raise RuntimeError(f"Playlist update status changed: {run['status']}")
        payload = _decode_recovery_payload(run["recovery_payload_json"])
        complete = _playlist_payload_is_complete(dict(run), payload)
        current_claim = payload.get("claim") or {}
        if expected_state_fingerprint and expected_state_fingerprint != _playlist_update_state_fingerprint(dict(run)):
            raise RuntimeError("Playlist update evidence changed before recovery claim")
        if complete:
            if append_missing_last:
                raise ValueError("Tail append is only available for explicitly reviewed legacy audits")
            _require_playlist_claim(payload, expected_claim_token)
        else:
            if int(run["evidence_version"] or 0) != 0:
                raise RuntimeError("Malformed recovery evidence cannot be claimed")
            if not expected_state_fingerprint or expected_state_fingerprint != _playlist_update_state_fingerprint(dict(run)):
                raise RuntimeError("Legacy recovery claim requires the exact observed state fingerprint")
            if current_claim:
                _require_playlist_claim(payload, expected_claim_token)
            elif expected_claim_token:
                raise RuntimeError("Legacy recovery claim token does not match the unclaimed state")
            payload.setdefault("version", 0)
            payload.setdefault("manual_only", True)
            payload.setdefault("events", [])
            payload.setdefault("outcomes", [])
            payload.setdefault("next_seq", 1)
        if current_claim.get("role") == "reconcile" and not allow_reclaim:
            raise RuntimeError("A recovery worker already owns this playlist update")
        if append_missing_last:
            requested_rows = conn.execute(
                """
                SELECT video_id, item_order
                FROM playlist_update_items
                WHERE update_run_id = ? AND action = 'requested'
                ORDER BY item_order
                """,
                (update_run_id,),
            ).fetchall()
            requested = [str(item["video_id"] or "") for item in requested_rows]
            positions = [int(item["item_order"] or 0) for item in requested_rows]
            if (
                not requested
                or len(requested) != int(run["requested_count"] or 0)
                or positions != list(range(1, len(requested) + 1))
                or any(not video_id for video_id in requested)
            ):
                raise RuntimeError("Legacy tail append requires the complete ordered request")
            existing_tail = payload.get("tail_recovery")
            if existing_tail and existing_tail.get("requested_video_ids") != requested:
                raise RuntimeError("Legacy tail append request evidence changed")
            payload["tail_recovery"] = {
                "capability": "append_missing_last",
                "requested_video_ids": requested,
                "requested_count": len(requested),
            }
        tail_capability = append_missing_last or current_claim.get("capability") == "append_missing_last"
        new_token = uuid.uuid4().hex
        payload["claim"] = {
            "token": new_token,
            "role": "reconcile",
            "claimed_at": now,
            "attestation": str(attestation).strip(),
        }
        if tail_capability:
            payload["claim"]["capability"] = "append_missing_last"
        event = _append_payload_entry(
            payload,
            "events",
            {
                "phase": "reconcile",
                "operation": "observe",
                "state": "intent",
                "chunk_order": 0,
                "attempt": 1,
                "items": [],
                "attestation": str(attestation).strip(),
            },
            now,
        )
        serialized_payload = _serialize_recovery_payload(payload)
        conn.execute(
            "UPDATE playlist_update_runs SET recovery_payload_json = ? WHERE update_run_id = ? AND status = ?",
            (serialized_payload, update_run_id, run["status"]),
        )
        conn.commit()
    updated_run = dict(run)
    updated_run["recovery_payload_json"] = serialized_payload
    return {
        "update_run_id": update_run_id,
        "claim_token": new_token,
        "event": event,
        "state_fingerprint": _playlist_update_state_fingerprint(updated_run),
    }


def finish_playlist_update(
    db_path: str | Path,
    update_run_id: str,
    *,
    status: str,
    actual_video_ids: Iterable[str] = (),
    actual_items: Iterable[Mapping[str, Any]] | None = None,
    observation_complete: bool | None = None,
    error: str = "",
    differences: Iterable[Any] = (),
    claim_token: str = "",
    expected_statuses: Iterable[str] = ACTIVE_PLAYLIST_UPDATE_STATUSES,
    expected_state_fingerprint: str = "",
    restore_verified: bool | None = None,
    identity_review_required: bool | None = None,
) -> dict[str, Any]:
    """Advance an audit while preserving every observed outcome in recovery evidence."""
    if status not in PLAYLIST_UPDATE_STATUSES:
        raise ValueError(f"Unsupported playlist update status: {status}")
    if identity_review_required is True and status != "recovery_required":
        raise ValueError("Identity review must remain in recovery_required status")
    expected = set(expected_statuses)
    if not expected:
        raise ValueError("finish_playlist_update requires expected statuses")
    init_db(db_path, repair_source_bindings=False)
    legacy_actual = [
        str(value).strip() for value in actual_video_ids if value and str(value).strip()
    ]
    exact_actual = (
        _normalize_playlist_items(
            actual_items,
            require_set_video_id=bool(observation_complete is not False),
        )
        if actual_items is not None
        else None
    )
    if exact_actual is not None and legacy_actual and legacy_actual != [
        item["video_id"] for item in exact_actual
    ]:
        raise ValueError("actual_items and actual_video_ids describe different observations")
    actual = exact_actual or _normalize_playlist_items(
        legacy_actual, require_set_video_id=False
    )
    complete = bool(exact_actual is not None) if observation_complete is None else bool(observation_complete)
    if complete and exact_actual is None:
        raise ValueError("A complete observation requires actual_items with setVideoIds")
    difference_values = list(differences)
    serialized_differences = json.dumps(
        difference_values, ensure_ascii=False, separators=(",", ":"), default=str
    )
    now = utc_now_iso()
    with connect(db_path) as conn:
        postgres = _begin_playlist_update_write(conn)
        run = _locked_playlist_update_run(conn, update_run_id, postgres)
        current_status = str(run["status"])
        if current_status not in expected:
            raise RuntimeError(
                f"Playlist update status changed: expected {sorted(expected)}, found {current_status}"
            )
        current_fingerprint = _playlist_update_state_fingerprint(dict(run))
        if expected_state_fingerprint and expected_state_fingerprint != current_fingerprint:
            raise RuntimeError("Playlist update evidence changed before finish")
        payload = _decode_recovery_payload(run["recovery_payload_json"])
        if int(run["evidence_version"] or 0) == 1:
            if not _playlist_payload_is_complete(dict(run), payload):
                raise RuntimeError("Playlist update recovery evidence is incomplete")
            _require_playlist_claim(payload, claim_token)
            partial = _latest_partial_publication(payload)
            if partial and status in {"published", "skipped_current"}:
                if (
                    not complete
                    or partial.get("observation_complete") is not True
                    or partial.get("verification_matches") is not True
                    or partial.get("items") != actual
                    or len(actual) != len(partial.get("effective_video_ids") or [])
                ):
                    raise RuntimeError("Partial publication needs a complete verified effective playlist observation")
            outcome = {
                "status": status,
                "error": str(error or ""),
                "differences": difference_values,
                "actual": actual,
                "observation_complete": complete,
            }
            if restore_verified is not None:
                outcome["restore_verified"] = bool(restore_verified)
            if identity_review_required is not None:
                outcome["identity_review_required"] = bool(identity_review_required)
            _append_payload_entry(payload, "outcomes", outcome, now)
        else:
            if (current_status != "running" or payload.get("claim")) and not expected_state_fingerprint:
                raise RuntimeError(
                    "Legacy recovery finalization requires the exact observed state fingerprint"
                )
            if payload.get("claim"):
                _require_playlist_claim(payload, claim_token)
            tail_claim = (payload.get("claim") or {}).get("capability") == "append_missing_last"
            if tail_claim and status not in {"published", "recovery_required"}:
                raise RuntimeError("Legacy tail recovery may only publish or remain pending")
            if tail_claim and status == "published":
                successful_observations = [
                    event
                    for event in payload.get("events", [])
                    if isinstance(event, Mapping)
                    and event.get("phase") == "reconcile_tail"
                    and event.get("operation") == "observe"
                    and event.get("state") == "verified"
                    and event.get("observation_complete") is True
                    and event.get("verification_matches") is True
                ]
                if not successful_observations or not complete:
                    raise RuntimeError("Legacy tail publish needs a fresh complete verified observation")
                if successful_observations[-1].get("items") != actual:
                    raise RuntimeError("Legacy tail finish does not match its verified observation")
            payload.setdefault("version", 0)
            payload.setdefault("manual_only", True)
            outcomes = payload.setdefault("outcomes", [])
            if not outcomes and (
                current_status in {"mutation_failed", "recovery_required"}
                or run["error"]
                or (run["differences_json"] or "[]") != "[]"
            ):
                try:
                    previous_differences = json.loads(run["differences_json"] or "[]")
                except (TypeError, ValueError):
                    previous_differences = []
                _append_payload_entry(
                    payload,
                    "outcomes",
                    {
                        "status": current_status,
                        "error": str(run["error"] or ""),
                        "differences": previous_differences,
                        "actual": [],
                        "observation_complete": False,
                        "preserved_legacy_state": True,
                    },
                    str(run["completed_at"] or run["created_at"] or now),
                )
            outcome = {
                "status": status,
                "error": str(error or ""),
                "differences": difference_values,
                "actual": actual,
                "observation_complete": complete,
            }
            if restore_verified is not None:
                outcome["restore_verified"] = bool(restore_verified)
            if identity_review_required is not None:
                outcome["identity_review_required"] = bool(identity_review_required)
            _append_payload_entry(payload, "outcomes", outcome, now)
        conn.execute(
            "DELETE FROM playlist_update_items WHERE update_run_id = ? AND action = 'actual'",
            (update_run_id,),
        )
        conn.executemany(
            """
            INSERT INTO playlist_update_items(
                update_run_id, action, video_id, set_video_id, item_order, created_at
            ) VALUES (?, 'actual', ?, ?, ?, ?)
            """,
            [
                (
                    update_run_id,
                    item["video_id"],
                    item["set_video_id"],
                    item["position"],
                    now,
                )
                for item in actual
            ],
        )
        conn.execute(
            """
            UPDATE playlist_update_runs
            SET status = ?, completed_at = ?, error = ?, differences_json = ?,
                recovery_payload_json = ?
            WHERE update_run_id = ? AND status = ?
            """,
            (
                status,
                None if status == "running" else now,
                str(error or ""),
                serialized_differences,
                _serialize_recovery_payload(payload),
                update_run_id,
                current_status,
            ),
        )
        result = conn.execute(
            "SELECT * FROM playlist_update_runs WHERE update_run_id = ?",
            (update_run_id,),
        ).fetchone()
        if not result or result["status"] != status:
            raise RuntimeError("Playlist update status changed during finish")
        conn.commit()
    return dict(result)


def _hydrate_playlist_update_result(
    row: Mapping[str, Any],
    item_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(row)
    payload = _decode_recovery_payload(result.get("recovery_payload_json"))
    complete = _playlist_payload_is_complete(result, payload)
    grouped: dict[str, list[dict[str, Any]]] = {
        "requested": [],
        "existing": [],
        "actual": [],
    }
    for raw_item in item_rows:
        item = dict(raw_item)
        grouped.setdefault(str(item["action"]), []).append(
            {
                "position": int(item["item_order"]),
                "video_id": item["video_id"],
                "set_video_id": item.get("set_video_id") or "",
            }
        )
    if complete:
        snapshot = payload["snapshot"]
        grouped["requested"] = [
            {"position": index, "video_id": video_id, "set_video_id": ""}
            for index, video_id in enumerate(snapshot["requested_video_ids"], 1)
        ]
        grouped["existing"] = list(snapshot["existing_items"])
        outcomes = payload.get("outcomes") or []
        if outcomes and isinstance(outcomes[-1], Mapping):
            grouped["actual"] = list(outcomes[-1].get("actual") or [])
    for action, values in grouped.items():
        result[f"{action}_items"] = values
        result[f"{action}_video_ids"] = [item["video_id"] for item in values]
    result["recovery_payload"] = payload
    partial = _latest_partial_publication(payload)
    result["publication_mode"] = "partial" if partial else "full"
    result["effective_video_ids"] = list(partial["effective_video_ids"]) if partial else list(result["requested_video_ids"])
    result["excluded_items"] = list(partial["excluded_items"]) if partial else []
    result["partial_verified"] = bool(
        partial
        and result["status"] == "published"
        and partial.get("observation_complete") is True
        and partial.get("verification_matches") is True
        and partial.get("items") == grouped["actual"]
        and len(grouped["actual"]) == len(result["effective_video_ids"])
    )
    result["evidence_complete"] = complete
    result["recovery_capability"] = "exact" if complete else "manual_only"
    result["claim_token"] = str((payload.get("claim") or {}).get("token") or "")
    result["state_fingerprint"] = _playlist_update_state_fingerprint(result)
    evidence_issues: list[str] = []
    if not complete:
        if int(result.get("evidence_version") or 0) != 1:
            evidence_issues.append("legacy_evidence_version")
        for action, expected_count in (
            ("requested", int(result.get("requested_count") or 0)),
            ("existing", int(result.get("existing_count") or 0)),
        ):
            values = grouped[action]
            if len(values) != expected_count:
                evidence_issues.append(f"{action}_count_mismatch")
            elif [item["position"] for item in values] != list(range(1, len(values) + 1)):
                evidence_issues.append(f"{action}_positions_incomplete")
        if grouped["existing"] and any(not item["set_video_id"] for item in grouped["existing"]):
            evidence_issues.append("existing_set_video_ids_missing")
    result["evidence_issues"] = evidence_issues
    review_entries = sorted(
        [
            entry
            for name in ("events", "outcomes")
            for entry in payload.get(name, [])
            if isinstance(entry, Mapping) and "identity_review_required" in entry
        ],
        key=lambda entry: int(entry.get("seq") or 0),
    )
    result["identity_review_required"] = (
        bool(review_entries[-1]["identity_review_required"]) if review_entries else False
    )
    result["restore_verified"] = any(
        bool(entry.get("restore_verified"))
        for entry in payload.get("outcomes", [])
        if isinstance(entry, Mapping)
    ) or any(
        bool(entry.get("restore_verified"))
        for entry in payload.get("events", [])
        if isinstance(entry, Mapping)
    )
    try:
        result["differences"] = json.loads(result.get("differences_json") or "[]")
    except (TypeError, ValueError):
        result["differences"] = []
    return result


def get_playlist_update_run(
    db_path: str | Path,
    update_run_id: str,
    *,
    read_only: bool = True,
) -> dict[str, Any] | None:
    if not read_only:
        init_db(db_path, repair_source_bindings=False)
    with connect(db_path, read_only=read_only) as conn:
        row = conn.execute(
            "SELECT * FROM playlist_update_runs WHERE update_run_id = ?",
            (update_run_id,),
        ).fetchone()
        if not row:
            return None
        items = conn.execute(
            """
            SELECT action, video_id, set_video_id, item_order
            FROM playlist_update_items
            WHERE update_run_id = ?
            ORDER BY action, item_order
            """,
            (update_run_id,),
        ).fetchall()
    return _hydrate_playlist_update_result(dict(row), [dict(item) for item in items])


def get_pending_playlist_recovery(
    db_path: str | Path,
    playlist_id: str,
) -> dict[str, Any] | None:
    """Return the active claim; incomplete legacy evidence is explicitly manual-only."""
    init_db(db_path, repair_source_bindings=False)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM playlist_update_runs
            WHERE playlist_id = ?
              AND status IN ('running', 'mutation_failed', 'recovery_required')
            ORDER BY started_at DESC, created_at DESC
            LIMIT 1
            """,
            (playlist_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        items = conn.execute(
            """
            SELECT action, video_id, set_video_id, item_order
            FROM playlist_update_items
            WHERE update_run_id = ?
            ORDER BY action, item_order
            """,
            (result["update_run_id"],),
        ).fetchall()
    return _hydrate_playlist_update_result(result, [dict(item) for item in items])



def get_bulk_cached_matches(
    conn: Any,
    service: str,
    tracks: Iterable[Any],
    *,
    metadata_resolver: Any = None,
    read_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Bulk load cache matches for a list of tracks in 5-6 database queries.
    Returns a dictionary mapping: song_id -> cached_dict
    """
    service = normalized_service(service)
    track_rows = [row_dict(t) for t in tracks]
    
    # 1. Collect all song_ids and pre-calculate all metadata lookup keys for each track
    song_ids = []
    track_keys = {} # song_id -> list of metadata lookup keys
    all_lookup_keys = []
    
    for t in track_rows:
        sid = normalize_song_id(service, t)
        if not sid:
            continue
        song_ids.append(sid)
        
        keys = list(dict.fromkeys(
            key for key, _score in _metadata_lookup_key_scores(t, 1.0)
        ))
        track_keys[sid] = keys
        all_lookup_keys.extend(keys)
        
    if not song_ids:
        return {}
        
    # --- Bulk Queries ---
    
    # Query 1: Fetch manual overrides
    overrides = {}
    placeholders = ",".join("?" for _ in song_ids)
    rows_override = conn.execute(
        f"SELECT * FROM manual_overrides WHERE service = ? AND song_id IN ({placeholders})",
        (service, *song_ids)
    ).fetchall()
    for row in rows_override:
        overrides[row["song_id"]] = dict(row)
        
    # Query 2: Fetch platform_song_ids
    song_to_uid = {}
    rows_platform = conn.execute(
        f"SELECT song_id, track_uid FROM platform_song_ids WHERE service = ? AND song_id IN ({placeholders})",
        (service, *song_ids)
    ).fetchall()
    for row in rows_platform:
        song_to_uid[row["song_id"]] = row["track_uid"]
        
    # Query 3: Fetch metadata lookup indexes
    lookup_to_entry: dict[str, dict[str, Any]] = {}
    if all_lookup_keys:
        unique_lookup_keys = list(dict.fromkeys(all_lookup_keys))
        chunks = [unique_lookup_keys[i:i + 500] for i in range(0, len(unique_lookup_keys), 500)]
        for chunk in chunks:
            chunk_placeholders = ",".join("?" for _ in chunk)
            rows_lookup = conn.execute(
                f"SELECT lookup_key, track_uid, source, score FROM metadata_lookup_index WHERE lookup_key IN ({chunk_placeholders})",
                chunk
            ).fetchall()
            for row in rows_lookup:
                lookup_to_entry[row["lookup_key"]] = dict(row)
                
    # Collect all candidate track_uids
    candidate_uids = set(song_to_uid.values()) | {
        entry["track_uid"] for entry in lookup_to_entry.values()
    }
    candidate_uids.update(
        override["target_track_uid"]
        for override in overrides.values()
        if override.get("target_track_uid")
    )
    
    # Query 4: Fetch video IDs for manual overrides if set_canonical
    override_video_ids = [ov["canonical_yt_video_id"] for ov in overrides.values() if ov.get("action") == "set_canonical" and ov.get("canonical_yt_video_id")]
    video_to_uid = {}
    if override_video_ids:
        video_placeholders = ",".join("?" for _ in override_video_ids)
        rows_video = conn.execute(
            f"SELECT video_id, track_uid FROM yt_video_ids WHERE video_id IN ({video_placeholders})",
            override_video_ids
        ).fetchall()
        for row in rows_video:
            video_to_uid[row["video_id"]] = row["track_uid"]
            candidate_uids.add(row["track_uid"])
            
    # Query 5: Fetch tracks details
    tracks_dict = {}
    uid_list = list(candidate_uids)
    uid_chunks = [uid_list[i:i + 500] for i in range(0, len(uid_list), 500)]
    for chunk in uid_chunks:
        uid_placeholders = ",".join("?" for _ in chunk)
        rows_tracks = conn.execute(
            f"SELECT * FROM tracks WHERE track_uid IN ({uid_placeholders})",
            chunk
        ).fetchall()
        for row in rows_tracks:
            tracks_dict[row["track_uid"]] = dict(row)
            
    # Query 6: Fetch track_list details for verifying cached titles & fallback details
    uid_to_metas = {}
    for chunk in uid_chunks:
        uid_placeholders = ",".join("?" for _ in chunk)
        rows_metas = conn.execute(
            f"""
            SELECT ps.track_uid, tl.service, tl.song_id, tl.album_id, tl.title_ko, tl.artist_ko, tl.album_ko, tl.title_en, tl.artist_en, tl.album_en, tl.artwork_url
            FROM platform_song_ids ps
            JOIN track_list tl ON tl.service = ps.service AND tl.song_id = ps.song_id
            WHERE ps.track_uid IN ({uid_placeholders})
            ORDER BY
                CASE tl.service WHEN 'apple' THEN 0 WHEN 'melon' THEN 1 WHEN 'spotify' THEN 2 ELSE 3 END
            """,
            chunk
        ).fetchall()
        for row in rows_metas:
            uid = row["track_uid"]
            uid_to_metas.setdefault(uid, []).append(dict(row))
            
    # Helper to build cache row dict in-memory.
    def make_cache_dict(
        track_uid: str,
        status: str,
        *,
        cache_origin: str,
        lookup_key: str = "",
        manual_action: str = "",
        incoming: dict[str, Any] | None = None,
    ):
        track = tracks_dict.get(track_uid)
        if not track or not track.get("canonical_yt_video_id"):
            return None
        metas = uid_to_metas.get(track_uid) or []
        meta = next(
            (row for row in metas if incoming and _metadata_rows_equivalent(incoming, row)),
            metas[0] if metas else None,
        )
        
        out = {
            "video_id": track["canonical_yt_video_id"],
            "yt_title": track["yt_title"] or "",
            "yt_artist": track["yt_artist"] or "",
            "yt_album": track["yt_album"] or "",
            "score": float(track["best_score"] or 1.0),
            "title_score": 1.0,
            "artist_score": 1.0,
            "album_score": 1.0,
            "yt_result_type": "song",
            "query": f"db_cache:{cache_origin}",
            "status": status,
            "cache_origin": cache_origin,
            "track_uid": track_uid,
            "lookup_key": lookup_key,
            "manual_action": manual_action,
        }
        if meta:
            out.update({
                "title": meta.get("title_ko") or meta.get("title_en") or "",
                "artist": meta.get("artist_ko") or meta.get("artist_en") or "",
                "album": meta.get("album_ko") or meta.get("album_en") or "",
                "artwork_url": meta.get("artwork_url") or "",
            })
        return out
        
    resolver_memo: dict[str, dict[str, str] | None] = {}

    def resolved_video_metadata(video_id: str) -> dict[str, str] | None:
        if not metadata_resolver:
            return None
        if video_id in resolver_memo:
            return resolver_memo[video_id]
        raw = metadata_resolver(video_id)
        if not isinstance(raw, dict) or str(raw.get("video_id") or "").strip() != video_id:
            resolver_memo[video_id] = None
            return None
        keys = (
            "video_id", "title", "artist", "album",
            "title_ko", "title_en", "artist_ko", "artist_en", "album_ko", "album_en",
        )
        resolved = {
            key: (str(raw.get(key) or "").strip() if isinstance(raw.get(key), str) else "")
            for key in keys
        }
        if not _metadata_variants(resolved):
            resolver_memo[video_id] = None
            return None
        resolver_memo[video_id] = resolved
        return resolved

    def refresh_resolved_canonical_metadata(
        track_uid: str,
        video_id: str,
        resolved: dict[str, str],
        incoming: dict[str, Any],
    ) -> None:
        track = tracks_dict.get(track_uid)
        if not track or str(track.get("canonical_yt_video_id") or "") != video_id:
            return
        resolved_rows = [
            {"title": resolved.get("title"), "artist": resolved.get("artist"), "album": resolved.get("album")},
            {"title": resolved.get("title_en"), "artist": resolved.get("artist_en"), "album": resolved.get("album_en")},
            {"title": resolved.get("title_ko"), "artist": resolved.get("artist_ko"), "album": resolved.get("album_ko")},
        ]
        chosen = next(
            (row for row in resolved_rows if _metadata_rows_equivalent(incoming, row)),
            resolved_rows[0],
        )
        title = str(chosen.get("title") or "")
        artist = str(chosen.get("artist") or "")
        album = str(chosen.get("album") or "")
        values = (title, artist, album)
        current = tuple(str(track.get(key) or "") for key in ("yt_title", "yt_artist", "yt_album"))
        verified_key = _yt_metadata_verified_key(video_id, title, artist, album)
        marker_matches = str(track.get("yt_metadata_verified_key") or "") == verified_key
        if values == current and marker_matches:
            return
        if not read_only:
            conn.execute(
                """
                UPDATE tracks
                SET yt_title = ?, yt_artist = ?, yt_album = ?,
                    yt_metadata_verified_key = ?, updated_at = ?
                WHERE track_uid = ? AND canonical_yt_video_id = ?
                """,
                (*values, verified_key, utc_now_iso(), track_uid, video_id),
            )
        track.update({
            "yt_title": title,
            "yt_artist": artist,
            "yt_album": album,
            "yt_metadata_verified_key": verified_key,
        })

    def verify_cache_in_memory(
        track_uid: str,
        incoming: dict[str, Any],
        *,
        exclude_source_binding: bool = False,
    ) -> tuple[str, dict[str, str] | None]:
        candidates = []
        for meta in uid_to_metas.get(track_uid) or []:
            if (
                exclude_source_binding
                and normalized_service(meta.get("service")) == service
                and str(meta.get("song_id") or "") == normalize_song_id(service, incoming)
            ):
                continue
            candidates.append(meta)
        if any(_metadata_rows_equivalent(incoming, candidate) for candidate in candidates):
            return "valid", None
        if candidates:
            return "conflict", None

        track = tracks_dict.get(track_uid) or {}
        video_id = str(track.get("canonical_yt_video_id") or "")
        canonical = {
            "title": track.get("yt_title") or "",
            "artist": track.get("yt_artist") or "",
            "album": track.get("yt_album") or "",
        }
        verified_key = _yt_metadata_verified_key(
            video_id,
            str(track.get("yt_title") or ""),
            str(track.get("yt_artist") or ""),
            str(track.get("yt_album") or ""),
        )
        if str(track.get("yt_metadata_verified_key") or "") == verified_key and _metadata_variants(canonical):
            return (
                ("valid", None)
                if _metadata_rows_equivalent(incoming, canonical)
                else ("conflict", None)
            )
        resolved = resolved_video_metadata(video_id) if video_id else None
        if not resolved:
            return "unknown", None
        if not _metadata_rows_equivalent(incoming, resolved):
            return "conflict", resolved
        refresh_resolved_canonical_metadata(track_uid, video_id, resolved, incoming)
        return "valid", resolved

    logged_rejections: set[tuple[str, str, str]] = set()

    def log_cache_rejection(song_id: str, track_uid: str, state: str) -> None:
        key = (song_id, track_uid, state)
        if key in logged_rejections:
            return
        logged_rejections.add(key)
        if state == "conflict":
            LOG.warning(
                "Cache rejected for %s:%s due to metadata/version mismatch",
                service,
                song_id,
            )
        else:
            LOG.warning(
                "Cache unavailable for %s:%s because canonical metadata verification is incomplete",
                service,
                song_id,
            )
        
    # 2. Evaluate cache matches in-memory for each track
    results = {}
    for t in track_rows:
        sid = normalize_song_id(service, t)
        if not sid:
            continue
            
        # A. Manual override check
        override = overrides.get(sid)
        manual_action = str((override or {}).get("action") or "").lower()
        if override:
            if manual_action in {"block", "manual_blocked"}:
                results[sid] = {
                    "status": "manual_blocked",
                    "cache_origin": "manual",
                    "track_uid": str(override.get("target_track_uid") or ""),
                    "lookup_key": "",
                    "manual_action": manual_action,
                }
                continue
            video_id = override.get("canonical_yt_video_id")
            if video_id:
                # Find track_uid for this video
                track_uid = video_to_uid.get(video_id)
                if track_uid:
                    cached = make_cache_dict(
                        track_uid,
                        status="manual_override",
                        cache_origin="manual",
                        manual_action=manual_action,
                        incoming=t,
                    )
                    if cached:
                        if str(cached.get("video_id") or "") != str(video_id):
                            cached.update({"yt_title": "", "yt_artist": "", "yt_album": ""})
                        cached["video_id"] = video_id
                        cached["query"] = "manual_override"
                else:
                    cached = {
                        "video_id": video_id,
                        "score": 1.0,
                        "status": "manual_override",
                        "query": "manual_override",
                        "cache_origin": "manual",
                        "track_uid": str(override.get("target_track_uid") or ""),
                        "lookup_key": "",
                        "manual_action": manual_action,
                    }
                if cached:
                    results[sid] = cached
                    continue
            target_uid = str(override.get("target_track_uid") or "")
            if target_uid:
                cached = make_cache_dict(
                    target_uid,
                    status="manual_override",
                    cache_origin="manual",
                    manual_action=manual_action,
                    incoming=t,
                )
                if cached:
                    results[sid] = cached
                    continue
                    
        # B. Find by service and song_id
        track_uid = song_to_uid.get(sid)
        if track_uid:
            cached = make_cache_dict(
                track_uid,
                status="cached_match",
                cache_origin="service_song_id",
                manual_action=manual_action,
                incoming=t,
            )
            if cached:
                cache_state, resolved = verify_cache_in_memory(
                    track_uid, t, exclude_source_binding=True,
                )
                if cache_state != "valid":
                    log_cache_rejection(sid, track_uid, cache_state)
                else:
                    if resolved:
                        verified_track = tracks_dict[track_uid]
                        cached.update({
                            "yt_title": verified_track.get("yt_title") or "",
                            "yt_artist": verified_track.get("yt_artist") or "",
                            "yt_album": verified_track.get("yt_album") or "",
                        })
                    results[sid] = cached
                    continue
                    
        # C. Find by metadata lookup
        keys = track_keys.get(sid) or []
        for key in keys:
            entry = lookup_to_entry.get(key)
            if not entry:
                continue
            found_uid = entry["track_uid"]
            cached = make_cache_dict(
                found_uid,
                status="cached_match",
                cache_origin="metadata_lookup",
                lookup_key=key,
                manual_action=manual_action,
                incoming=t,
            )
            if cached:
                cache_state, resolved = verify_cache_in_memory(
                    found_uid, t, exclude_source_binding=True,
                )
                if cache_state == "valid":
                    if resolved:
                        verified_track = tracks_dict[found_uid]
                        cached.update({
                            "yt_title": verified_track.get("yt_title") or "",
                            "yt_artist": verified_track.get("yt_artist") or "",
                            "yt_album": verified_track.get("yt_album") or "",
                        })
                    results[sid] = cached
                    break
                log_cache_rejection(sid, found_uid, cache_state)
                    
    return results
