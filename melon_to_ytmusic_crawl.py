#!/usr/bin/env python3
"""
Melon 차트를 YouTube Music 플레이리스트로 동기화합니다.

Usage:
    python melon_to_ytmusic_crawl.py --melon-urls <URL1> <URL2> ...

Features:
    - 멜론 일간/주간 차트 웹 스크래핑 지원
    - Apple Music 매칭 데이터(Proxy)를 활용한 국문/영문 다국어 매칭 강화
    - 앨범명 변형(Variants)을 통한 정밀 매칭 로직 탑재
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ytmusic_playlist_sync import (
    SourceTrack,
    env_or_arg,
    load_dotenv,
    make_ytmusic,
    update_ytmusic_playlist,
    get_resilient_session,
)
from crawler_common import process_matching_pipeline

http_session = get_resilient_session()


DEFAULT_MIN_SCORE = 0.6
DEFAULT_MIN_TITLE_SCORE = 0.65
DEFAULT_MIN_ARTIST_SCORE = 0.55
DEFAULT_SEARCH_LIMIT = 25

LOG = logging.getLogger("melon_to_ytmusic_crawl")

DEFAULT_ALBUM_CACHE_TTL = 31

# 동일 앨범에 대한 중복 요청을 방지하기 위한 캐시
# 구조: { album_id: { "name": "album_name", "created_at": "YYYY-MM-DD", "last_checked": "YYYY-MM-DD" } }
_ALBUM_NAME_CACHE: dict[str, dict[str, Any]] = {}


def load_album_cache(db_path: Path, ttl_days: int = DEFAULT_ALBUM_CACHE_TTL) -> None:
    """앨범명 캐시를 DB에서 로드합니다."""
    if db_path and (db_path.exists() or os.environ.get("SUPABASE_DB_URL")):
        try:
            from hype_db import connect, init_db
            init_db(db_path)
            with connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT album_id, album_name, created_at, last_checked FROM album_metadata WHERE service = 'melon'"
                ).fetchall()
                for row in rows:
                    if row["album_name"]:
                        _ALBUM_NAME_CACHE[row["album_id"]] = {
                            "name": row["album_name"],
                            "created_at": (row["created_at"] or "")[:10],
                            "last_checked": (row["last_checked"] or "")[:10],
                        }
            LOG.info("Loaded %d album names from DB cache.", len(_ALBUM_NAME_CACHE))
        except Exception as exc:
            LOG.warning("Failed to load album cache from DB: %s", exc)


def save_album_cache(db_path: Path):
    """현재 메모리의 캐시를 DB로 저장합니다."""
    if db_path and (db_path.exists() or os.environ.get("SUPABASE_DB_URL")) and _ALBUM_NAME_CACHE:
        try:
            from hype_db import connect, init_db, utc_now_iso
            init_db(db_path)
            now = utc_now_iso()
            params = [
                (
                    album_id,
                    item.get("name", ""),
                    item.get("created_at") or now,
                    item.get("last_checked") or now,
                )
                for album_id, item in _ALBUM_NAME_CACHE.items()
            ]
            with connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO album_metadata(
                        service, album_id, album_name, created_at, last_checked
                    )
                    VALUES ('melon', ?, ?, ?, ?)
                    ON CONFLICT(service, album_id) DO UPDATE SET
                        album_name = excluded.album_name,
                        last_checked = excluded.last_checked
                    """,
                    params,
                )
                conn.commit()
            LOG.info("Saved album cache to DB.")
        except Exception as exc:
            LOG.warning("Failed to save album cache to DB: %s", exc)


def melon_album_url(album_id: str) -> str:
    return f"https://www.melon.com/album/detail.htm?albumId={album_id}" if album_id else ""


def fetch_melon_album_name(album_id: str, ttl_days: int = DEFAULT_ALBUM_CACHE_TTL) -> str:
    """앨범 ID를 사용해 멜론 상세 페이지에서 앨범명을 가져옵니다 (캐시 우선)."""
    if not album_id:
        return ""
    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    today = kst_now.strftime("%Y-%m-%d")    
    if album_id in _ALBUM_NAME_CACHE:
        item = _ALBUM_NAME_CACHE[album_id]
        item["last_checked"] = today
        
        # 저장(수집)한 지 TTL이 지나지 않았다면 캐시 사용, 지났다면 다시 수집 (Requirement 1)
        created_at_str = item.get("created_at", item.get("last_checked", today))
        try:
            created_at_dt = datetime.strptime(created_at_str, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=9)))
            if (kst_now - created_at_dt).days < ttl_days:
                return item["name"]
            LOG.info("Cache entry for album %s is old (%d days). Refetching info.", album_id, (kst_now - created_at_dt).days)
        except ValueError:
            pass

    url = melon_album_url(album_id)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        LOG.info("Fetching album name for ID: %s", album_id)
        res = http_session.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        album_name_el = soup.select_one(".section_info .info .song_name") or soup.select_one(".song_name")
        if album_name_el:
            for hidden in album_name_el.select(".none, .hide, .hidden"):
                hidden.decompose()
            name = album_name_el.get_text(strip=True).strip()
            _ALBUM_NAME_CACHE[album_id] = {"name": name, "created_at": today, "last_checked": today}
            time.sleep(0.1)
            return name
    except Exception as e:
        LOG.warning("Failed to fetch album name for %s: %s", album_id, e)
        if album_id in _ALBUM_NAME_CACHE:
            return _ALBUM_NAME_CACHE[album_id]["name"] # 실패 시 기존 정보 유지
    return ""


def fetch_melon_tracks(url: str, *, limit: int = 100) -> tuple[str, str, str, list[SourceTrack]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    import os
    import glob
    matched_files = glob.glob(url)
    resolved_path = matched_files[0] if matched_files else url
    if os.path.exists(resolved_path) or resolved_path.endswith(".html"):
        with open(resolved_path, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    else:
        response = http_session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        html = response.text
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Try to extract the chart date/period
    chart_date = ""
    yyyymmdd_el = soup.select_one(".yyyymmdd")
    if yyyymmdd_el:
        year_el = yyyymmdd_el.select_one(".year")
        hour_el = yyyymmdd_el.select_one(".hour")
        if year_el:
            chart_date = year_el.get_text(strip=True)
            if hour_el and hour_el.get_text(strip=True):
                chart_date += " " + hour_el.get_text(strip=True)
        else:
            chart_date = yyyymmdd_el.get_text(strip=True)
            
    # Remove hidden texts (e.g., in rank02) before extracting
    for hidden in soup.select(".rank02 span.checkEllipsis"):
        hidden.decompose()
        
    rows = soup.select("#tb_list tbody tr")
    tracks: list[SourceTrack] = []
    
    for i in range(min(limit, len(rows))):
        row = rows[i]
        title_el = row.select_one(".rank01")
        artist_el = row.select_one(".rank02")
        album_el = row.select_one(".rank03")
        
        if not title_el or not artist_el or not album_el:
            continue
 
        title = title_el.get_text(strip=True)
        
        # 상세 페이지 URL 및 곡 ID 추출
        song_id = row.get("data-song-no")
        if not song_id:
            # Fallback: href 속성에서 숫자 추출
            link = row.select_one("a[href*='goSongDetail']")
            if link:
                match = re.search(r"(\d+)", link["href"])
                if match:
                    song_id = match.group(1)
        
        # 앨범 ID도 가능한 경우 attribute/href에서 추출해 album detail URL을 남긴다.
        album_id = row.get("data-album-no") or row.get("data-album-id") or ""
        if not album_id:
            album_link = row.select_one("a[href*='goAlbumDetail'], a[href*='album/detail.htm']")
            if album_link and album_link.get("href"):
                match = re.search(r"albumId=(\d+)|(\d+)", album_link["href"])
                if match:
                    album_id = next(g for g in match.groups() if g)
 
        # 앨범 아트워크 추출
        img_el = row.select_one(".image_typeAll img")
        artwork_url = img_el["src"] if img_el else ""
        
        # Artist parsing: join multiple artists
        artist_links = artist_el.select("a")
        if artist_links:
            artist = ", ".join(a.get_text(strip=True) for a in artist_links)
        else:
            artist = artist_el.get_text(strip=True)
            
        album = album_el.get_text(strip=True)
 
        # Populate global album cache for sharing across tasks (e.g. melon_gen)
        if album_id and album:
            today_str = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            _ALBUM_NAME_CACHE[album_id] = {"name": album, "created_at": today_str, "last_checked": today_str}
        
        tracks.append(
            SourceTrack(
                rank=i + 1,
                title=title,
                artist=artist,
                service="melon",
                album=album,
                source="melon_web_scrape",
                artwork_url=artwork_url,
                song_id=song_id or "",
                album_id=album_id or "",
            )
        )
 
    if "week" in url:
        playlist_name = "Melon Weekly Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
    elif "day" in url:
        playlist_name = "Melon Daily Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
    else:
        playlist_name = "Melon Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
        
    return playlist_name, playlist_desc, chart_date, tracks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Melon chart to YouTube Music crawler."
    )
    parser.add_argument("--env-file", default=".env", help="dotenv file path")
    parser.add_argument("--melon-urls", nargs="+", help="One or more Melon URLs to process")
    parser.add_argument("--track-limit", type=int, default=100)
    parser.add_argument("--yt-auth")
    parser.add_argument("--yt-oauth-client-id")
    parser.add_argument("--yt-oauth-client-secret")
    parser.add_argument("--yt-playlist-id")
    parser.add_argument("--job-name")
    parser.add_argument("--playlist-name")
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--no-db-cache", action="store_true")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--min-title-score", type=float, default=DEFAULT_MIN_TITLE_SCORE)
    parser.add_argument("--min-artist-score", type=float, default=DEFAULT_MIN_ARTIST_SCORE)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--album-cache-ttl", type=int, default=DEFAULT_ALBUM_CACHE_TTL, help="TTL in days for album name cache")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks before saving them to the YouTube Music playlist")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-only", action="store_true", help="Only parse and save raw tracks to DB, skip matching and playlist updates")
    return parser.parse_args()


def resolve_melon_reference_period(job_name: str, chart_dates: list[str]) -> str | None:
    if not chart_dates:
        return None
    # Let's inspect the first chart_date
    date_str = chart_dates[0]
    if not date_str:
        return None
    # E.g. "2026.05.18 ~ 2026.05.24"
    if "~" in date_str:
        match = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", date_str)
        if match:
            y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
            try:
                from datetime import datetime
                dt = datetime(y, m, d)
                iso_year, iso_week, _ = dt.isocalendar()
                return f"{iso_year}-W{iso_week:02d}"
            except Exception as e:
                LOG.warning("Failed to parse ISO week from weekly date %s: %s", date_str, e)
    else:
        # E.g. "2026.05.29 12:00" or "2026.05.29"
        match = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", date_str)
        if match:
            base_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            return base_date
    return None


def run_tracks_pipeline(
    args: argparse.Namespace,
    all_tracks: list[SourceTrack],
    combined_desc_parts: list[str],
    *,
    log_prefix: str = "melon",
    empty_message: str = "No tracks collected from any of the provided URLs.",
    reference_period: str | None = None,
) -> int:
    """Run the shared Melon -> YouTube Music matching and sync pipeline.

    The caller is responsible only for collecting SourceTrack objects and
    description parts. Authentication, environment handling, Apple proxy lookup,
    YouTube Music matching, cache reuse, output writing, shuffle, and playlist
    update remain identical for normal Melon charts and Melon generation charts.
    """
    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE", required=not getattr(args, "db_only", False))
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID", required=not getattr(args, "db_only", False))
    job_name = getattr(args, "job_name", None) or "melon"
    playlist_name = getattr(args, "playlist_name", None) or job_name
    source_variant = getattr(args, "source_variant", "default")
    db_path = Path(args.db_path).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")

    if not all_tracks:
        LOG.error(empty_message)
        return 1

    # Re-rank tracks after aggregation
    for i, t in enumerate(all_tracks, 1):
        t.rank = i

    # If --db-only is specified, persist raw tracks and exit immediately without matching or playlist updates
    if getattr(args, "db_only", False):
        if not args.dry_run:
            try:
                from hype_db import persist_crawled_tracks
                persist_crawled_tracks(
                    db_path,
                    service="melon",
                    job_name=job_name,
                    source_variant=source_variant,
                    chart_date=update_date_str,
                    reference_period=reference_period,
                    tracks=all_tracks,
                )
                LOG.info("Persisted raw chart order for %s to playlist_order table (DB-only mode).", job_name)
            except Exception as exc:
                LOG.error("Failed to persist raw chart order to DB: %s", exc)
                raise exc
        LOG.info("Done (DB-only mode). dry_run=%s", args.dry_run)
        return 0

    footer = f"\n\nLast updated: {update_date_str}\n\nAuto-generated by Github Actions.\n- colinky.github.io/hype_wave"
    full_desc = ("\n".join(combined_desc_parts) + footer).strip()

    LOG.info(
        "Matching settings: min_score=%.2f min_title_score=%.2f min_artist_score=%.2f search_limit=%d",
        min_score,
        min_title_score,
        min_artist_score,
        search_limit,
    )

    ytmusic = make_ytmusic(yt_auth, yt_oauth_client_id, yt_oauth_client_secret, language="ko")
    matched_video_ids = process_matching_pipeline(
        all_tracks=all_tracks,
        ytmusic=ytmusic,
        db_path=db_path,
        service="melon",
        job_name=job_name,
        source_variant=source_variant,
        update_date_str=update_date_str,
        started_at=started_at,
        no_db_cache=args.no_db_cache,
        min_score=min_score,
        min_title_score=min_title_score,
        min_artist_score=min_artist_score,
        search_limit=search_limit,
        dry_run=args.dry_run,
        history_json=args.history_json,
        reference_period=reference_period,
    )
    if args.shuffle:
        LOG.info("Shuffling %d tracks before saving to playlist.", len(matched_video_ids))
        random.shuffle(matched_video_ids)

    update_ytmusic_playlist(
        ytmusic,
        yt_playlist_id,
        matched_video_ids,
        description=full_desc,
        dry_run=args.dry_run,
        db_path=db_path,
        service="melon",
        job_name=job_name,
        playlist_name=playlist_name,
    )

    LOG.info("Done. dry_run=%s", args.dry_run)
    return 0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    configure_logging()

    melon_urls = args.melon_urls or []
    if not melon_urls:
        LOG.error("No Melon URLs provided.")
        return 1

    db_path = Path(args.db_path).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    load_album_cache(db_path, ttl_days=args.album_cache_ttl)

    all_tracks: list[SourceTrack] = []
    combined_desc_parts: list[str] = []
    parsed_chart_dates: list[str] = []

    max_retries = 3
    for url in melon_urls:
        LOG.info("Processing Melon URL: %s", url)
        for attempt in range(1, max_retries + 1):
            try:
                p_name, p_desc, chart_dt, tracks = fetch_melon_tracks(url, limit=args.track_limit)
                
                # Validation check: Ensure the track count matches the expected limit
                if len(tracks) != args.track_limit:
                    raise ValueError(
                        f"Validation Error: Scraped {len(tracks)} tracks, "
                        f"but expected exactly {args.track_limit} tracks."
                    )

                combined_desc_parts.append(p_desc)
                parsed_chart_dates.append(chart_dt)
                all_tracks.extend(tracks)
                LOG.info("Added %d tracks from '%s'", len(tracks), p_name)
                break  # Success, exit retry loop
            except Exception as exc:
                LOG.error("Attempt %d failed to scrape/validate Melon URL %s: %s", attempt, url, exc)
                if attempt == max_retries:
                    if len(melon_urls) == 1:
                        return 1
                else:
                    time.sleep(2)

    save_album_cache(db_path)

    # Resolve reference period (week or day) from the parsed chart date
    job_name = getattr(args, "job_name", None) or "melon"
    reference_period = resolve_melon_reference_period(job_name, parsed_chart_dates)
    LOG.info("Resolved Melon reference period: %s", reference_period)

    return run_tracks_pipeline(
        args,
        all_tracks,
        combined_desc_parts,
        log_prefix="melon",
        empty_message="No tracks collected from any of the provided URLs.",
        reference_period=reference_period,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
