from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from hype_db_common import (
    clean_track_title,
    compact_metadata_key,
    has_feature_mismatch,
    has_version_mismatch,
    infer_album_id,
    legacy_to_job_name,
    match_method_for_status,
    metadata_key,
    normalize_song_id,
    normalize_source_variant,
    normalized_service,
    reference_period_for_date,
    require_job_name,
    row_dict,
    stable_uid,
    strip_parens_from_title,
    utc_now_iso,
)
from hype_db_schema import connect, init_db

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
    "upsert_chart_rank",
    "start_match_run",
    "record_match_attempt",
    "record_match_candidates",
    "cleanup_old_attempts_and_candidates",
    "get_expected_track_count",
    "persist_crawled_tracks",
    "persist_crawl_run",
    "repair_failed_source_bindings",
    "record_playlist_update",
    "get_bulk_cached_matches",
]

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
    conn.execute(
        """
        INSERT INTO tracks (
            track_uid, canonical_yt_video_id, yt_title, yt_artist, yt_album,
            match_status, best_score, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(track_uid) DO UPDATE SET
            canonical_yt_video_id = COALESCE(tracks.canonical_yt_video_id, excluded.canonical_yt_video_id),
            yt_title = COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title),
            yt_artist = COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist),
            yt_album = COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album),
            match_status = CASE
                WHEN excluded.match_status != 'failed' THEN excluded.match_status
                ELSE tracks.match_status
            END,
            best_score = CASE WHEN COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0) THEN COALESCE(excluded.best_score, 0) ELSE COALESCE(tracks.best_score, 0) END,
            updated_at = excluded.updated_at
        """,
        (track_uid, video_id, yt_title, yt_artist, yt_album, status, score, now, now),
    )
    if video_id:
        conn.execute(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                is_canonical = CASE WHEN excluded.is_canonical > yt_video_ids.is_canonical THEN excluded.is_canonical ELSE yt_video_ids.is_canonical END
            """,
            (video_id, track_uid),
        )
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
        (
            normalized_service(service),
            song_id,
            infer_album_id(service, row),
            row.get("title_ko") or row.get("title", ""),
            row.get("artist_ko") or row.get("artist", ""),
            row.get("album_ko") or row.get("album", ""),
            "" if normalized_service(service) == "melon" else (row.get("title_en") or row.get("title", "")),
            "" if normalized_service(service) == "melon" else (row.get("artist_en") or row.get("artist", "")),
            "" if normalized_service(service) == "melon" else (row.get("album_en") or row.get("album", "")),
            row.get("artwork_url", ""),
        ),
    )


def track_list_metadata_params(
    *,
    service: str,
    song_id: str,
    row: dict[str, Any],
) -> tuple[Any, ...]:
    service = normalized_service(service)
    return (
        service,
        song_id,
        infer_album_id(service, row),
        row.get("title_ko") or row.get("title", ""),
        row.get("artist_ko") or row.get("artist", ""),
        row.get("album_ko") or row.get("album", ""),
        "" if service == "melon" else (row.get("title_en") or row.get("title", "")),
        "" if service == "melon" else (row.get("artist_en") or row.get("artist", "")),
        "" if service == "melon" else (row.get("album_en") or row.get("album", "")),
        row.get("artwork_url", ""),
    )


def metadata_lookup_params(*, track_uid: str, row: dict[str, Any], source: str, score: float) -> list[tuple[Any, ...]]:
    params: list[tuple[Any, ...]] = []
    candidates = [
        (row.get("title"), row.get("artist"), row.get("album")),
        (row.get("title_en"), row.get("artist_en"), row.get("album_en")),
        (row.get("title_ko"), row.get("artist_ko"), row.get("album_ko")),
    ]
    for title, artist, album in candidates:
        if not title or not artist:
            continue
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
                params.append((key, track_uid, source, effective_score))
    return params


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
    if override and override["action"] == "block":
        return stable_uid(f"blocked:{service}:{song_id}")
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
    status = str(merged.get("status") or ("matched" if video_id else "failed"))
    score = float(merged.get("score") or 0)
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
    override = manual_override(conn, service, song_id)
    override_video = override["canonical_yt_video_id"] if override else None
    canonical_video = override_video or video_id

    if existing_video and video_id and existing_video != video_id and not override_video:
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
        video_id=canonical_video,
        yt_title=merged.get("yt_title", ""),
        yt_artist=merged.get("yt_artist", ""),
        yt_album=merged.get("yt_album", ""),
        status=status,
        score=score,
    )
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
                not bound_canonical
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
    upsert_metadata_lookup(conn, track_uid=track_uid, row=merged, source=status, score=score)
    return track_uid


def upsert_chart_rank(
    conn: sqlite3.Connection,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    chart_date: str,
    reference_period: str | None = None,
    chart_period: str | None = None,
    song_id: str,
    track_uid: str,
    rank_order: int,
    album_id: str = "",
) -> None:
    if not song_id or not rank_order:
        return
    service = normalized_service(service)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    ref_p = reference_period or chart_period
    period = reference_period_for_date(job_name, chart_date, ref_p)
    if not period:
        return
    conn.execute(
        """
        INSERT INTO playlist_order(
            service, job_name, source_variant, reference_period, song_id, rank_order
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
            rank_order = excluded.rank_order
        """,
        (
            service,
            job_name,
            source_variant,
            period,
            song_id,
            int(rank_order),
        ),
    )


def start_match_run(
    conn: sqlite3.Connection,
    *,
    service: str,
    job_name: str = "",
    source_variant: str = "default",
    started_at: str,
    source: str = "",
    total_tracks: int = 0,
) -> str:
    service = normalized_service(service)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    run_id = hashlib.sha1(f"{service}|{job_name}|{started_at}".encode("utf-8")).hexdigest()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO match_runs(run_id, service, job_name, source_variant, started_at, source, total_tracks, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            job_name = excluded.job_name,
            source_variant = excluded.source_variant,
            total_tracks = excluded.total_tracks,
            source = excluded.source
        """,
        (run_id, service, job_name, source_variant, started_at, source, total_tracks, now),
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
    ref_p = reference_period or chart_period
    resolved_period = reference_period_for_date(job_name, chart_date, ref_p)
    if resolved_period and not (normalized_service(service) == "melon" and job_name == "Gen-Z-Daily" and source_variant == "combined"):
        LOG.info("Cleaning up existing playlist_order records for %s / %s / %s (period: %s)", service, job_name, source_variant, resolved_period)
        conn.execute(
            """
            DELETE FROM playlist_order
            WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
            """,
            (normalized_service(service), job_name, source_variant, resolved_period)
        )

    # 1. Fetch all existing UIDs in one select query
    song_ids = [normalize_song_id(service, t) for t in track_rows if normalize_song_id(service, t)]
    existing_uids = {}
    if song_ids:
        placeholders = ",".join("?" for _ in song_ids)
        rows = conn.execute(
            f"SELECT song_id, track_uid FROM platform_song_ids WHERE service = ? AND song_id IN ({placeholders})",
            (normalized_service(service), *song_ids)
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
        platform_song_ids_params.append((normalized_service(service), song_id, track_uid))
        
        # Collect track_list metadata params
        album_id = infer_album_id(service, track)
        title_ko = str(track.get("title_ko") or track.get("title") or "").strip()
        artist_ko = str(track.get("artist_ko") or track.get("artist") or "").strip()
        album_ko = str(track.get("album_ko") or track.get("album") or "").strip()
        title_en = str(track.get("title_en") or "").strip()
        artist_en = str(track.get("artist_en") or "").strip()
        album_en = str(track.get("album_en") or "").strip()
        artwork_url = str(track.get("artwork_url") or "").strip()
        
        track_list_params.append((
            normalized_service(service),
            song_id,
            album_id,
            title_ko,
            artist_ko,
            album_ko,
            title_en,
            artist_en,
            album_en,
            artwork_url
        ))
        
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
                yt_title = COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title),
                yt_artist = COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist),
                yt_album = COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album),
                match_status = CASE
                    WHEN excluded.match_status != 'failed' THEN excluded.match_status
                    ELSE tracks.match_status
                END,
                best_score = CASE WHEN COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0) THEN COALESCE(excluded.best_score, 0) ELSE COALESCE(tracks.best_score, 0) END,
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
            ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
                rank_order = excluded.rank_order
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
    init_db(db_path)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    
    # Validation check: Ensure the track count matches the expected count
    expected = get_expected_track_count(job_name)
    if expected is not None and len(track_rows) != expected:
        if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
            LOG.warning(
                "Track count validation bypassed. Job '%s' has %d tracks, expected %d.",
                job_name, len(track_rows), expected
            )
        else:
            raise ValueError(
                f"Validation Error: Job '{job_name}' has {len(track_rows)} tracks, "
                f"but expected exactly {expected} tracks. Aborting database persistence to prevent corruption. "
                f"Set BYPASS_TRACK_COUNT_VAL=true to bypass."
            )

    if conn is not None:
        _persist_crawled_tracks_impl(conn, service, job_name, source_variant, chart_date, reference_period, chart_period, tracks)
        if commit:
            conn.commit()
    else:
        with connect(db_path) as new_conn:
            _persist_crawled_tracks_impl(new_conn, service, job_name, source_variant, chart_date, reference_period, chart_period, tracks)


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
    should_write_playlist_order = not skip_playlist_order and not (
        service == "melon" and job_name == "Gen-Z-Daily" and source_variant == "combined"
    )
    if resolved_period and should_write_playlist_order:
        LOG.info("Cleaning up existing playlist_order records for %s / %s / %s (period: %s)", service, job_name, source_variant, resolved_period)
        conn.execute(
            """
            DELETE FROM playlist_order
            WHERE service = ? AND job_name = ? AND source_variant = ? AND reference_period = ?
            """,
            (service, job_name, source_variant, resolved_period),
        )
    elif skip_playlist_order:
        LOG.info("Skipping playlist_order rewrite for %s / %s / %s; raw chart order was already persisted.", service, job_name, source_variant)

    run_id = start_match_run(
        conn,
        service=service,
        job_name=job_name,
        source_variant=source_variant,
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
            "SELECT track_uid, canonical_yt_video_id, match_status FROM tracks WHERE track_uid IN",
            [uid for uid in known_uids if uid],
        )
    }

    now = utc_now_iso()
    tracks_params: list[tuple[Any, ...]] = []
    yt_video_ids_params: list[tuple[Any, ...]] = []
    platform_song_ids_params: list[tuple[Any, ...]] = []
    track_list_params: list[tuple[Any, ...]] = []
    playlist_order_params: list[tuple[Any, ...]] = []
    match_attempt_params: list[tuple[Any, ...]] = []
    match_candidate_params: list[tuple[Any, ...]] = []
    metadata_params: list[tuple[Any, ...]] = []
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
        status = str(merged.get("status") or ("matched" if video_id else "failed"))
        score = float(merged.get("score") or 0)
        override = overrides.get(song_id) if song_id else None

        if status in failed_statuses or (service == "spotify" and str(song_id).startswith("fallback:")):
            track_uid = stable_uid(f"unmatched:{service}:{song_id or metadata_key(merged.get('title'), merged.get('artist'), merged.get('album'))}")
            canonical_video = song_id if (service == "ytmusic" and song_id and not str(song_id).startswith("fallback:")) else None
        elif override and override.get("action") == "block":
            track_uid = stable_uid(f"blocked:{service}:{song_id}")
            canonical_video = None
            status = "manual_blocked"
        elif override and override.get("target_track_uid"):
            track_uid = override["target_track_uid"]
            canonical_video = video_id
        elif override and override.get("canonical_yt_video_id"):
            canonical_video = override["canonical_yt_video_id"]
            track_uid = video_to_uid.get(canonical_video) or stable_uid(f"yt:{canonical_video}")
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

        tracks_params.append((
            track_uid,
            canonical_video,
            merged.get("yt_title", ""),
            merged.get("yt_artist", ""),
            merged.get("yt_album", ""),
            status,
            score,
            now,
            now,
        ))
        if canonical_video:
            yt_video_ids_params.append((canonical_video, track_uid))
        if (
            song_id
            and canonical_video
            and status not in failed_statuses
            and not (service == "spotify" and str(song_id).startswith("fallback:"))
        ):
            platform_song_ids_params.append((service, song_id, track_uid))
        if song_id and not (service == "spotify" and str(song_id).startswith("fallback:")):
            track_list_params.append(track_list_metadata_params(service=service, song_id=song_id, row=merged))
        if canonical_video and status not in failed_statuses:
            metadata_params.extend(metadata_lookup_params(track_uid=track_uid, row=merged, source=status, score=score))
        rank_order = int(match.get("rank") or source_row.get("rank") or 0)
        if song_id and rank_order and should_write_playlist_order and resolved_period:
            playlist_order_params.append((service, job_name, source_variant, resolved_period, song_id, rank_order))
        if song_id:
            match_method, origin_method, _ = match_method_for_status(match.get("status"), match.get("query"))
            match_attempt_params.append((
                run_id,
                service,
                song_id,
                track_uid,
                rank_order,
                match.get("video_id", ""),
                float(match.get("score") or 0),
                float(match.get("title_score") or 0),
                float(match.get("artist_score") or 0),
                float(match.get("album_score") or 0),
                match.get("yt_result_type", ""),
                match.get("query", ""),
                match.get("status", ""),
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
        if match.get("video_id") and match.get("status") != "duplicate_skipped":
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
                yt_title = COALESCE(NULLIF(excluded.yt_title, ''), tracks.yt_title),
                yt_artist = COALESCE(NULLIF(excluded.yt_artist, ''), tracks.yt_artist),
                yt_album = COALESCE(NULLIF(excluded.yt_album, ''), tracks.yt_album),
                match_status = CASE WHEN excluded.match_status != 'failed' THEN excluded.match_status ELSE tracks.match_status END,
                best_score = CASE WHEN COALESCE(excluded.best_score, 0) >= COALESCE(tracks.best_score, 0) THEN COALESCE(excluded.best_score, 0) ELSE COALESCE(tracks.best_score, 0) END,
                updated_at = excluded.updated_at
            """,
            tracks_params,
        )
    if yt_video_ids_params:
        conn.executemany(
            """
            INSERT INTO yt_video_ids(video_id, track_uid, is_canonical)
            VALUES (?, ?, 1)
            ON CONFLICT(video_id) DO UPDATE SET
                is_canonical = CASE WHEN excluded.is_canonical > yt_video_ids.is_canonical THEN excluded.is_canonical ELSE yt_video_ids.is_canonical END
            """,
            yt_video_ids_params,
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
    if playlist_order_params:
        conn.executemany(
            """
            INSERT INTO playlist_order(service, job_name, source_variant, reference_period, song_id, rank_order)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, job_name, source_variant, reference_period, song_id) DO UPDATE SET
                rank_order = excluded.rank_order
            """,
            playlist_order_params,
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
    conn.execute(
        """
        UPDATE match_runs
        SET matched_tracks = ?, failed_tracks = ?, cache_hits = ?, proxy_hits = ?
        WHERE run_id = ?
        """,
        (matched_count, failed_count, cache_hits, proxy_hits, run_id),
    )
    cleanup_old_attempts_and_candidates(conn, days=15)


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
    init_db(db_path)
    job_name = require_job_name(job_name)
    source_variant = normalize_source_variant(source_variant)
    track_rows = [row_dict(t) for t in tracks]
    match_rows = [row_dict(m) for m in matches]
    
    # Validation check: Ensure the track count matches the expected count
    expected = get_expected_track_count(job_name)
    if expected is not None and len(track_rows) != expected:
        if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
            LOG.warning(
                "Track count validation bypassed. Job '%s' has %d tracks, expected %d.",
                job_name, len(track_rows), expected
            )
        else:
            raise ValueError(
                f"Validation Error: Job '{job_name}' has {len(track_rows)} tracks, "
                f"but expected exactly {expected} tracks. Aborting database persistence to prevent corruption. "
                f"Set BYPASS_TRACK_COUNT_VAL=true to bypass."
            )

    if conn is not None:
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
        if override and override["action"] in {"block", "split"}:
            continue
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
            meta = conn.execute(
                """
                SELECT tl.title_ko, tl.artist_ko, tl.album_ko, tl.title_en, tl.artist_en, tl.album_en
                FROM track_list tl
                WHERE tl.service = ? AND tl.song_id = ?
                """,
                (row["service"], row["song_id"]),
            ).fetchone()
            if meta:
                target_uid = find_track_by_metadata(
                    conn,
                    meta["title_ko"] or meta["title_en"] or "",
                    meta["artist_ko"] or meta["artist_en"] or "",
                    meta["album_ko"] or meta["album_en"] or "",
                )
                if target_uid:
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
        if not target_uid:
            target_uid = candidate["track_uid"] or stable_uid(f"yt:{candidate['video_id']}")
            ensure_track(conn, track_uid=target_uid, video_id=candidate["video_id"], status="matched", score=float(candidate["score"] or 1.0))
        if target_uid and target_uid != row["track_uid"]:
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
            conn.execute("UPDATE platform_song_ids SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE metadata_lookup_index SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE match_attempts SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE yt_video_ids SET track_uid = ? WHERE track_uid = ?", (target_uid, old_uid))
            conn.execute("UPDATE manual_overrides SET target_track_uid = ? WHERE target_track_uid = ?", (target_uid, old_uid))
            conn.execute("DELETE FROM tracks WHERE track_uid = ?", (old_uid,))
            merged_tracks += 1

    return {"updated_bindings": updated_bindings, "merged_tracks": merged_tracks}


def record_playlist_update(
    db_path: str | Path,
    *,
    playlist_id: str,
    service: str = "",
    job_name: str = "",
    requested_video_ids: Iterable[str] = (),
    existing_video_ids: Iterable[str] = (),
    dry_run: bool = False,
) -> str:
    init_db(db_path)
    requested = [v for v in requested_video_ids if v]
    existing = [v for v in existing_video_ids if v]
    job_name = require_job_name(job_name)
    now = utc_now_iso()
    run_id = hashlib.sha1(
        f"{playlist_id}|{service}|{job_name}|{now}|{len(requested)}".encode("utf-8")
    ).hexdigest()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO playlist_update_runs(
                update_run_id, playlist_id, service, job_name, started_at,
                dry_run, requested_count, existing_count, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now,
            ),
        )
        item_sql = """
            INSERT INTO playlist_update_items(
                update_run_id, action, video_id, item_order, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (update_run_id, action, video_id, item_order) DO UPDATE SET
                created_at = EXCLUDED.created_at
            """
        conn.executemany(
            item_sql,
            [
                (run_id, "existing", video_id, index, now)
                for index, video_id in enumerate(existing, 1)
            ],
        )
        conn.executemany(
            item_sql,
            [
                (run_id, "requested", video_id, index, now)
                for index, video_id in enumerate(requested, 1)
            ],
        )
        conn.commit()
    return run_id



def get_bulk_cached_matches(conn: Any, service: str, tracks: Iterable[Any]) -> dict[str, dict[str, Any]]:
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
        
        # Precompute metadata keys for this track
        title = str(t.get("title_ko") or t.get("title") or "").strip()
        artist = str(t.get("artist_ko") or t.get("artist") or "").strip()
        album = str(t.get("album_ko") or t.get("album") or "").strip()
        
        keys = [metadata_key(title, artist, album), compact_metadata_key(title, artist)]
        cleaned = clean_track_title(title)
        if cleaned != title:
            keys.append(compact_metadata_key(cleaned, artist))
            if album:
                keys.append(metadata_key(cleaned, artist, album))
        
        stripped_title = strip_parens_from_title(title)
        if stripped_title != title and stripped_title != cleaned:
            keys.append(compact_metadata_key(stripped_title, artist))
            if album:
                keys.append(metadata_key(stripped_title, artist, album))
                
        stripped_artist = strip_parens_from_title(artist)
        if stripped_artist != artist:
            keys.append(compact_metadata_key(title, stripped_artist))
            keys.append(compact_metadata_key(cleaned, stripped_artist))
            if album:
                keys.append(metadata_key(title, stripped_artist, album))
                
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
    lookup_to_uid = {}
    if all_lookup_keys:
        chunks = [all_lookup_keys[i:i + 500] for i in range(0, len(all_lookup_keys), 500)]
        for chunk in chunks:
            chunk_placeholders = ",".join("?" for _ in chunk)
            rows_lookup = conn.execute(
                f"SELECT lookup_key, track_uid FROM metadata_lookup_index WHERE lookup_key IN ({chunk_placeholders})",
                chunk
            ).fetchall()
            for row in rows_lookup:
                lookup_to_uid[row["lookup_key"]] = row["track_uid"]
                
    # Collect all candidate track_uids
    candidate_uids = set(song_to_uid.values()) | set(lookup_to_uid.values())
    
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
            
    if not candidate_uids:
        return {}
        
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
    def make_cache_dict(track_uid, status):
        track = tracks_dict.get(track_uid)
        if not track or not track.get("canonical_yt_video_id"):
            return None
        metas = uid_to_metas.get(track_uid) or []
        meta = metas[0] if metas else None
        
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
            "query": "db_cache",
            "status": status,
        }
        if meta:
            out.update({
                "title": meta.get("title_ko") or meta.get("title_en") or "",
                "artist": meta.get("artist_ko") or meta.get("artist_en") or "",
                "album": meta.get("album_ko") or meta.get("album_en") or "",
                "artwork_url": meta.get("artwork_url") or "",
            })
        return out
        
    # Helper to verify cached title in-memory.
    def verify_title_in_memory(track_uid, title, artist, threshold=0.4):
        from ytmusic_playlist_sync import similarity
        metas = uid_to_metas.get(track_uid) or []
        if not metas:
            return True
        existing_titles = []
        for m in metas:
            if m.get("title_ko"):
                existing_titles.append(m["title_ko"])
            if m.get("title_en"):
                existing_titles.append(m["title_en"])
        existing_titles = list(set(existing_titles))
        canonical_title = (tracks_dict.get(track_uid) or {}).get("yt_title") or ""
        if canonical_title and has_version_mismatch(title, canonical_title):
            return False
        if not existing_titles:
            return True
        if all(has_feature_mismatch(title, existing) for existing in existing_titles):
            return False
        if all(has_version_mismatch(title, existing) for existing in existing_titles):
            return False
        best_sim = max(similarity(title, t, is_title=True) for t in existing_titles)
        return best_sim >= threshold
        
    # 2. Evaluate cache matches in-memory for each track
    results = {}
    for t in track_rows:
        sid = normalize_song_id(service, t)
        if not sid:
            continue
            
        title = str(t.get("title_ko") or t.get("title") or "").strip()
        artist = str(t.get("artist_ko") or t.get("artist") or "").strip()
        album = str(t.get("album_ko") or t.get("album") or "").strip()
        
        # A. Manual override check
        override = overrides.get(sid)
        if override:
            if override.get("action") == "block":
                results[sid] = {"status": "manual_blocked"}
                continue
            video_id = override.get("canonical_yt_video_id")
            if video_id:
                # Find track_uid for this video
                track_uid = video_to_uid.get(video_id)
                if track_uid:
                    cached = make_cache_dict(track_uid, status="manual_override")
                else:
                    cached = {"video_id": video_id, "score": 1.0, "status": "manual_override", "query": "manual_override"}
                if cached:
                    results[sid] = cached
                    continue
                    
        # B. Find by service and song_id
        track_uid = song_to_uid.get(sid)
        if track_uid:
            cached = make_cache_dict(track_uid, status="cached_match")
            if cached:
                if title and not verify_title_in_memory(track_uid, title, artist):
                    LOG.warning("Cache rejected for %s:%s — title mismatch with '%s'", service, sid, title)
                else:
                    results[sid] = cached
                    continue
                    
        # C. Find by metadata lookup
        keys = track_keys.get(sid) or []
        found_uid = None
        for key in keys:
            found_uid = lookup_to_uid.get(key)
            if found_uid:
                break
        if found_uid:
            cached = make_cache_dict(found_uid, status="cached_match")
            if cached:
                if title and not verify_title_in_memory(found_uid, title, artist):
                    pass
                else:
                    results[sid] = cached
                    continue
                    
    return results
