from __future__ import annotations

import logging
import os
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    bilingual_cache_read_only,
    get_verified_video_metadata,
    localized_source_fields,
    match_from_prev,
    search_youtube_music,
)

LOG = logging.getLogger("crawler_common")


def load_verified_matching_cache(
    conn: Any,
    *,
    service: str,
    tracks: list[dict[str, Any]],
    ytmusic: Any = None,
    read_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load cache without holding a database transaction during network calls.

    Callers must commit their preceding writes before entering this read phase.
    Re-read after network I/O so changed bindings and manual policy are checked.
    """
    from hype_db import get_bulk_cached_matches

    pending: dict[str, None] = {}

    def collect_video(video_id: str) -> None:
        pending[video_id] = None

    cached = get_bulk_cached_matches(
        conn, service=service, tracks=tracks,
        metadata_resolver=collect_video if ytmusic is not None else None,
        read_only=True,
    )
    conn.commit()
    if ytmusic is None:
        return cached

    if pending:
        metadata_cache: dict[str, Any] = {}
        resolved = {
            video_id: get_verified_video_metadata(ytmusic, video_id, metadata_cache=metadata_cache)
            for video_id in pending
        }
        cached = get_bulk_cached_matches(
            conn, service=service, tracks=tracks,
            metadata_resolver=resolved.get, read_only=read_only,
        )

    from hype_db_common import normalized_service, normalize_song_id, row_dict
    from hype_db_store import manual_override
    from sync_validation import PlaybackBlocked, require_playable, validate_matches, verifier_for
    from ytmusic_playlist_sync import ALIASES, normalize_text

    service = normalized_service(service)
    aliases = dict(ALIASES.overrides)
    missing = [row_dict(row) for row in tracks if normalize_song_id(service, row_dict(row)) not in cached]
    if not missing:
        return cached

    def binding(song_id):
        row = conn.execute(
            "SELECT t.* FROM platform_song_ids p JOIN tracks t ON t.track_uid=p.track_uid "
            "WHERE p.service=? AND p.song_id=?", (service, song_id),
        ).fetchone()
        return dict(row) if row else {}

    candidates, before = [], {}
    for source in missing:
        song_id = normalize_song_id(service, source)
        # A manual choice takes precedence even when its target is not cached.
        if manual_override(conn, service, song_id) or any(
            aliases.get("|".join(normalize_text(source.get(field + suffix) or "")
                                for field in ("title", "artist")))
            for suffix in ("", "_ko", "_en")
        ):
            continue
        prior = binding(song_id)
        if prior.get("canonical_yt_video_id"):
            before[song_id] = (prior["track_uid"], prior["canonical_yt_video_id"])
            candidates.append({**source, "song_id": song_id, "track_uid": prior["track_uid"],
                               "video_id": prior["canonical_yt_video_id"], "status": "cached_match",
                               "score": float(prior.get("best_score") or 0),
                               "query": "db_cache:validated_current_binding",
                               "cache_origin": "validated_current_binding"})
    conn.commit()
    if not candidates:
        return cached
    verifier = verifier_for(ytmusic)
    require_playable(verifier, [])
    healthy = []
    for candidate in candidates:
        video_id = candidate["video_id"]
        observed = verifier.verify(video_id)
        if observed.get("state") == "unavailable":
            cached[candidate["song_id"]] = {"status": "unavailable", "excluded_video_ids": [video_id]}
        elif observed.get("state") == "playable":
            healthy.append(candidate)
        else:
            raise PlaybackBlocked(f"Existing recording is uncertain: {video_id}")
    if healthy:
        # Legacy cross-source string comparisons may miss reviewed locales or
        # releases. Only fresh exact metadata, playback and the complete normal
        # validator can promote this already-bound ID; stored text is no proof.
        validated = validate_matches(conn, service=service, sources=missing, matches=healthy,
                                     client=ytmusic, verifier=verifier)
        for match in validated:
            cached[match["song_id"]] = match
    conn.commit()
    for song_id, expected in before.items():
        current = binding(song_id)
        if (current.get("track_uid"), current.get("canonical_yt_video_id")) != expected:
            raise PlaybackBlocked(f"Existing binding changed during cache validation: {service}:{song_id}")
        if manual_override(conn, service, song_id) or ALIASES.overrides != aliases:
            raise PlaybackBlocked(f"Manual policy changed during cache validation: {service}:{song_id}")
    conn.commit()
    return cached


def process_matching_pipeline(
    *,
    all_tracks: list[SourceTrack],
    raw_tracks: list[SourceTrack] | None = None,
    tracks_ko_map: dict[str, SourceTrack] | None = None,
    ytmusic: Any,
    db_path: Path,
    service: str,
    job_name: str,
    source_variant: str = "default",
    update_date_str: str,
    started_at: str,
    no_db_cache: bool = False,
    min_score: float = 0.6,
    min_title_score: float = 0.65,
    min_artist_score: float = 0.55,
    search_limit: int = 25,
    dry_run: bool = False,
    history_json: str = "docs/api/history.json",
    reference_period: str | None = None,
    chart_period: str | None = None,
    extra_raw_snapshots: list[dict[str, Any]] | None = None,
) -> list[str]:
    """
    공통 매칭 파이프라인: 캐시 조회, 검색, 중복 체크, DB 저장 및 플레이리스트 업데이트용 비디오 ID 목록 반환.
    """
    from hype_db import connect, persist_crawled_tracks, persist_crawl_run, export_frontend_history
    from sync_validation import verifier_for, playable_cache, validate_matches

    verifier = verifier_for(ytmusic)
    verifier.check_health()

    if tracks_ko_map is None:
        tracks_ko_map = {}
    if raw_tracks is None:
        raw_tracks = all_tracks

    def korean_track_for(track: SourceTrack) -> SourceTrack | None:
        return tracks_ko_map.get(track.song_id) or tracks_ko_map.get(str(track.rank))

    def localized_row(track: SourceTrack) -> dict[str, Any]:
        row = vars(track).copy()
        row.update(localized_source_fields(track, korean_track_for(track)))
        return row

    LOG.info(
        "Matching settings: min_score=%.2f min_title_score=%.2f min_artist_score=%.2f search_limit=%d",
        min_score,
        min_title_score,
        min_artist_score,
        search_limit,
    )

    matches: list[MatchResult] = []
    seen_video_ids: set[str] = set()

    # Open a single persistent connection context for the entire pipeline run
    with (
        bilingual_cache_read_only() if dry_run else nullcontext(),
        connect(db_path, read_only=dry_run) as conn,
    ):
        # Pre-populate cache in bulk
        bulk_cache = {}
        try:
            bulk_cache = load_verified_matching_cache(
                conn,
                service=service,
                tracks=[localized_row(track) for track in all_tracks],
                ytmusic=None if no_db_cache else ytmusic,
                read_only=True,
            )
            if no_db_cache:
                # Disabling automatic cache reuse must not disable manual policy.
                bulk_cache = {key: value for key, value in bulk_cache.items()
                              if value.get("status") in {"manual_blocked", "manual_override"}}
            conn.commit()
            bulk_cache = playable_cache(bulk_cache, verifier)
        except Exception as exc:
            conn.rollback()
            raise RuntimeError("Unable to load matching cache/manual policy safely") from exc

        for track in all_tracks:
            # Get Korean fallback track if available
            track_ko = korean_track_for(track)

            # Check cache first
            match = None
            
            # 2a. DB cache check
            cached = bulk_cache.get(track.song_id) if track.song_id else None
            if cached:
                if cached.get("status") == "manual_blocked":
                    localized = localized_source_fields(track, track_ko)
                    match = MatchResult(
                        rank=track.rank,
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        service=service,
                        song_id=track.song_id,
                        status="manual_blocked",
                        **localized,
                    )
                elif cached.get("video_id"):
                    match = match_from_prev(track, cached, track_ko=track_ko, status=cached.get("status", "cached_match"))

            # 3. Active YouTube Music Search (if not cached)
            did_search = False
            if not match:
                match = search_youtube_music(
                    ytmusic,
                    track,
                    track_ko=track_ko,
                    min_score=min_score,
                    min_title_score=min_title_score,
                    min_artist_score=min_artist_score,
                    limit=search_limit,
                    excluded_video_ids=set((cached or {}).get("excluded_video_ids", [])),
                    playability_verifier=verifier,
                )
                did_search = True

            # Keep canonical matches intact for DB alias binding; dedupe only target output.
            if match.video_id and match.video_id in seen_video_ids:
                LOG.warning(
                    "playlist_duplicate_suppressed: '%s' / '%s' — video_id %s already selected (source: %s)",
                    track.title,
                    track.artist,
                    match.video_id,
                    match.status,
                )
            elif match.video_id:
                seen_video_ids.add(match.video_id)

            matches.append(match)
            LOG.info(
                "[%03d/%03d] %s %.3f - %s / %s / %s",
                track.rank,
                len(all_tracks),
                match.status,
                match.score,
                track.title,
                track.artist,
                track.album,
            )
            if did_search:
                time.sleep(0.2)

        matches = validate_matches(conn, service=service, sources=[localized_row(track) for track in all_tracks],
                                   matches=matches, client=ytmusic, verifier=verifier)
        matched_video_ids = list(dict.fromkeys(match["video_id"] for match in matches if match.get("video_id")))
        failed = [match for match in matches if not match.get("video_id")]
        LOG.info(
            "Matched %d/%d source tracks. Failed %d; unique playlist items %d.",
            len(matches) - len(failed),
            len(all_tracks),
            len(failed),
            len(matched_video_ids),
        )

        # 6. Database Persistence & Exporter
        if not dry_run:
            try:
                for snapshot in extra_raw_snapshots or ():
                    persist_crawled_tracks(
                        db_path, service=service, job_name=job_name,
                        source_variant=snapshot["source_variant"], chart_date=snapshot["chart_date"],
                        reference_period=snapshot["reference_period"], tracks=snapshot["tracks"],
                        conn=conn, commit=False,
                    )
                persist_crawled_tracks(
                    db_path, service=service, job_name=job_name, source_variant=source_variant,
                    chart_date=update_date_str, reference_period=reference_period or chart_period,
                    tracks=[localized_row(track) for track in raw_tracks], conn=conn, commit=False,
                )
                persist_crawl_run(
                    db_path,
                    service=service,
                    job_name=job_name,
                    source_variant=source_variant,
                    chart_date=update_date_str,
                    reference_period=reference_period or chart_period,
                    started_at=started_at,
                    tracks=all_tracks,
                    matches=matches,
                    conn=conn,
                    skip_playlist_order=True,
                    commit=False,
                )
                conn.commit()
                if os.environ.get("HYPE_DEFER_HISTORY_EXPORT") not in {"1", "true", "TRUE"}:
                    export_frontend_history(db_path, history_json)
            except Exception as exc:
                LOG.error("Failed to persist %s run to DB: %s", service, exc)
                raise exc

    return matched_video_ids
