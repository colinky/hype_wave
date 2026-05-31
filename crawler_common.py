from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    match_from_prev,
    normalize_text,
    search_youtube_music,
    similarity,
    title_variants,
    artist_variants,
    album_variants,
)

LOG = logging.getLogger("crawler_common")


def process_matching_pipeline(
    *,
    all_tracks: list[SourceTrack],
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
) -> list[str]:
    """
    공통 매칭 파이프라인: 캐시 조회, 검색, 중복 체크, DB 저장 및 플레이리스트 업데이트용 비디오 ID 목록 반환.
    """
    if not dry_run:
        try:
            from hype_db import persist_crawled_tracks
            persist_crawled_tracks(
                db_path,
                service=service,
                job_name=job_name,
                source_variant=source_variant,
                chart_date=update_date_str,
                reference_period=reference_period or chart_period,
                tracks=all_tracks,
            )
            LOG.info("Persisted raw chart order for %s to playlist_order table.", job_name)
        except Exception as exc:
            LOG.error("Failed to persist raw chart order to DB: %s", exc)
            raise exc

    if tracks_ko_map is None:
        tracks_ko_map = {}

    LOG.info(
        "Matching settings: min_score=%.2f min_title_score=%.2f min_artist_score=%.2f search_limit=%d",
        min_score,
        min_title_score,
        min_artist_score,
        search_limit,
    )

    matches: list[MatchResult] = []
    seen_video_ids: set[str] = set()

    for track in all_tracks:
        # Get Korean fallback track if available
        track_ko = tracks_ko_map.get(track.song_id) or tracks_ko_map.get(str(track.rank))

        # Check cache first
        match = None
        
        # 2a. DB cache check
        if not no_db_cache:
            try:
                from hype_db import get_cached_match
                cached = get_cached_match(
                    db_path,
                    service=service,
                    song_id=track.song_id,
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                )
                if cached and cached.get("status") == "manual_blocked":
                    match = MatchResult(
                        rank=track.rank,
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        service=service,
                        song_id=track.song_id,
                        status="manual_blocked",
                    )
                elif cached and cached.get("video_id"):
                    match = match_from_prev(track, cached, track_ko=track_ko, status=cached.get("status", "cached_match"))
            except Exception as exc:
                LOG.warning("DB cache lookup failed for %s: %s", track.title, exc)

        # 3. Active YouTube Music Search (if not cached)
        if not match:
            match = search_youtube_music(
                ytmusic,
                track,
                track_ko=track_ko,
                min_score=min_score,
                min_title_score=min_title_score,
                min_artist_score=min_artist_score,
                limit=search_limit,
                ignore_video_ids=seen_video_ids,
            )

        # 5. Duplicate Check
        if match.video_id and match.video_id in seen_video_ids:
            LOG.warning(
                "duplicate_skipped: '%s' / '%s' — video_id %s already in playlist (source: %s)",
                track.title,
                track.artist,
                match.video_id,
                match.status,
            )
            match.query = f"dup_of:{match.video_id}"
            match.status = "duplicate_skipped"
            match.video_id = None
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
        time.sleep(0.2)

    matched_video_ids = [match.video_id for match in matches if match.video_id]
    failed = [match for match in matches if not match.video_id]
    LOG.info("Matched %d/%d tracks. Failed/skipped %d.", len(matched_video_ids), len(all_tracks), len(failed))

    # 6. Database Persistence & Exporter
    if not dry_run:
        try:
            from hype_db import export_frontend_history, persist_crawl_run
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
            )
            export_frontend_history(db_path, history_json)
        except Exception as exc:
            LOG.error("Failed to persist %s run to DB: %s", service, exc)
            raise exc

    return matched_video_ids
