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
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    env_or_arg,
    load_dotenv,
    load_match_cache,
    make_ytmusic,
    normalize_text,
    search_youtube_music,
    artist_variants,
    title_variants,
    album_variants,
    match_from_prev,
    similarity,
    update_ytmusic_playlist,
    write_json,
)


DEFAULT_MIN_SCORE = 0.6
DEFAULT_MIN_TITLE_SCORE = 0.65
DEFAULT_MIN_ARTIST_SCORE = 0.55
DEFAULT_SEARCH_LIMIT = 25

LOG = logging.getLogger("melon_to_ytmusic_crawl")

DEFAULT_ALBUM_CACHE_TTL = 31

# 동일 앨범에 대한 중복 요청을 방지하기 위한 캐시
# 구조: { album_id: { "name": "album_name", "created_at": "YYYY-MM-DD", "last_checked": "YYYY-MM-DD" } }
_ALBUM_NAME_CACHE: dict[str, dict[str, Any]] = {}


def load_album_cache(log_dir: Path, ttl_days: int = DEFAULT_ALBUM_CACHE_TTL) -> Path:
    """앨범명 캐시를 로드하고 캐시 파일 경로를 반환합니다."""
    db_path = os.environ.get("HYPE_DB_PATH", "")
    if db_path:
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
        return Path(db_path)
    # log_dir의 깊이에 상관없이 프로젝트 루트의 logs/ 디렉토리에 고정하거나 
    # 명확하게 log_dir 상위의 logs 폴더를 찾도록 변경
    # 여기서는 단순화를 위해 실행 기준 logs 루트를 권장
    cache_dir = Path("logs") if not log_dir.parts else log_dir.parents[0] if log_dir.name != "logs" else log_dir
    cache_path = cache_dir / "melon_album_cache.json"

    if cache_path.exists():
        try:
            kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
            today_str = kst_now.strftime("%Y-%m-%d")
            ttl_days = 31

            with open(cache_path, "r", encoding="utf-8") as f:
                raw_cache = json.load(f)
                count_loaded = 0
                count_expired = 0

                for aid, val in raw_cache.items():
                    if isinstance(val, dict):
                        last_checked_str = val.get("last_checked", "")
                        try:
                            last_checked_dt = datetime.strptime(last_checked_str, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=9)))
                            # 마지막으로 차트에서 발견된 지 TTL 이내인 경우만 로드 (Requirement 2)
                            if (kst_now - last_checked_dt).days < ttl_days:
                                if "created_at" not in val:
                                    val["created_at"] = last_checked_str
                                _ALBUM_NAME_CACHE[aid] = val
                                count_loaded += 1
                            else:
                                count_expired += 1
                        except ValueError:
                            val["last_checked"] = today_str
                            _ALBUM_NAME_CACHE[aid] = val
                            count_loaded += 1
                    else:
                        _ALBUM_NAME_CACHE[aid] = {"name": val, "created_at": today_str, "last_checked": today_str}
                        count_loaded += 1
                LOG.info("Loaded %d album names (pruned %d expired) from cache: %s",
                         count_loaded, count_expired, cache_path.name)
        except Exception as e:
            LOG.warning("Failed to load album cache: %s", e)
    return cache_path


def save_album_cache(cache_path: Path):
    """현재 메모리의 캐시를 파일로 저장합니다."""
    db_path = os.environ.get("HYPE_DB_PATH", "")
    if db_path and _ALBUM_NAME_CACHE:
        try:
            from hype_db import connect, init_db, utc_now_iso
            init_db(db_path)
            with connect(db_path) as conn:
                for album_id, item in _ALBUM_NAME_CACHE.items():
                    now = utc_now_iso()
                    conn.execute(
                        """
                        INSERT INTO album_metadata(
                            service, album_id, album_name, created_at, last_checked
                        )
                        VALUES ('melon', ?, ?, ?, ?)
                        ON CONFLICT(service, album_id) DO UPDATE SET
                            album_name = excluded.album_name,
                            last_checked = excluded.last_checked
                        """,
                        (
                            album_id,
                            item.get("name", ""),
                            item.get("created_at") or now,
                            item.get("last_checked") or now,
                        ),
                    )
                conn.commit()
            return
        except Exception as exc:
            LOG.warning("Failed to save album cache to DB: %s", exc)
    if _ALBUM_NAME_CACHE:
        LOG.warning("Album cache was not saved because DB cache is unavailable.")


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
        res = requests.get(url, headers=headers, timeout=10)
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


def fetch_melon_tracks(url: str, *, limit: int = 100, log_dir: str) -> tuple[str, str, list[SourceTrack]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    html = response.text
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Try to extract the chart date/period
    chart_date = ""
    date_badge = soup.select_one(".yyyymmdd .year")
    if date_badge:
        # For weekly: .yyyymmdd -> .year, .hour
        # HTML structure: <span class="year">2026.04.20 ~ 2026.04.26</span>
        year_text = soup.select_one(".yyyymmdd .year")
        hour_text = soup.select_one(".yyyymmdd .hour")
        if year_text:
            chart_date = year_text.get_text(strip=True)
        if hour_text and hour_text.get_text(strip=True):
            chart_date += " " + hour_text.get_text(strip=True)
            
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

    log_dir = Path(log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    log_prefix = "melon"
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir.mkdir(parents=True, exist_ok=True)
        
    if "week" in url:
        playlist_name = "Melon Weekly Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
    elif "day" in url:
        playlist_name = "Melon Daily Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
    else:
        playlist_name = "Melon Chart"
        playlist_desc = f"{chart_date} 장르종합" if chart_date else "장르종합"
        
    return playlist_name, playlist_desc, tracks


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
    parser.add_argument("--log-dir")
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--no-db-cache", action="store_true")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--min-title-score", type=float, default=DEFAULT_MIN_TITLE_SCORE)
    parser.add_argument("--min-artist-score", type=float, default=DEFAULT_MIN_ARTIST_SCORE)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--album-cache-ttl", type=int, default=DEFAULT_ALBUM_CACHE_TTL, help="TTL in days for album name cache")
    parser.add_argument("--apple-proxy-data", help="Path to one or more Apple matches_crawl.json files to use as metadata proxy")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks before saving them to the YouTube Music playlist")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run_tracks_pipeline(
    args: argparse.Namespace,
    all_tracks: list[SourceTrack],
    combined_desc_parts: list[str],
    *,
    log_prefix: str = "melon",
    empty_message: str = "No tracks collected from any of the provided URLs.",
) -> int:
    """Run the shared Melon -> YouTube Music matching and sync pipeline.

    The caller is responsible only for collecting SourceTrack objects and
    description parts. Authentication, environment handling, Apple proxy lookup,
    YouTube Music matching, cache reuse, output writing, shuffle, and playlist
    update remain identical for normal Melon charts and Melon generation charts.
    """
    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE")
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID")
    log_dir = Path(args.log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    job_name = getattr(args, "job_name", None) or log_dir.name
    playlist_name = getattr(args, "playlist_name", None) or job_name
    source_variant = getattr(args, "source_variant", "default")
    db_path = Path(args.db_path).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    
    # Legacy JSON proxy loading is disabled; DB metadata_lookup_index is the proxy cache.
    apple_proxy: dict[str, dict[str, Any]] = {}
    proxy_paths = ""

    if proxy_paths:
        for p_path in proxy_paths.split(","):
            p_path = p_path.strip()
            if not p_path: continue
            path = Path(p_path)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        proxy_list = json.load(f)
                        for item in proxy_list:
                            # 프록시 조회를 위해 곡명, 아티스트, 앨범명의 모든 변형 조합을 키로 등록
                            titles = set()
                            for k in ["title", "title_ko", "title_en"]:
                                if item.get(k): titles.update(title_variants(item[k]))
                            
                            artists = set()
                            for k in ["artist", "artist_ko", "artist_en"]:
                                if item.get(k): artists.update(artist_variants(item[k]))

                            albums = set()
                            for k in ["album", "album_ko", "album_en"]:
                                if item.get(k): albums.update(album_variants(item[k]))
                            if not albums: albums.add("") # 앨범명이 없는 경우 대비

                            # 품질 점수(Reliability) 계산 로직 강화
                            # 1. 앨범 일치 여부 가중치 (50%)
                            # 2. 결과 타입이 'song'인 경우 가중치 추가 (보너스 0.1)
                            type_bonus = 0.1 if item.get("yt_result_type") == "song" else 0.0
                            quality = (item.get("score", 0.0) + type_bonus) * (0.5 + (item.get("album_score", 1.0) * 0.5))

                            for t in titles:
                                for a in artists:
                                    for al in albums:
                                        keys = [
                                            f"{normalize_text(t)}|{normalize_text(a)}|{normalize_text(al)}",
                                            f"{normalize_text(t)}|{normalize_text(a)}"
                                        ]
                                        for key in keys:
                                            # 더 품질이 좋은 매칭 결과가 있다면 덮어쓰지 않음
                                            existing = apple_proxy.get(key)
                                            if existing:
                                                existing_q = existing.get("score", 0.0) * (0.5 + (existing.get("album_score", 1.0) * 0.5))
                                                if existing_q >= quality:
                                                    continue
                                            apple_proxy[key] = item

                    LOG.info("Loaded %d proxy entries (with variants) from %s", len(proxy_list), path.name)
                except Exception as exc:
                    LOG.warning("Failed to load apple proxy data from %s: %s", path, exc)

    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")

    if not all_tracks:
        LOG.error(empty_message)
        return 1

    # Re-rank tracks after aggregation
    for i, t in enumerate(all_tracks, 1):
        t.rank = i

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
    matches: list[MatchResult] = []
    seen_video_ids: set[str] = set()

    # Load previous results to speed up matching
    match_cache = load_match_cache(log_dir)

    for track in all_tracks:
        proxy_item = None
        track_ko = None
        
        t_norm = normalize_text(track.title)
        a_norm = normalize_text(track.artist)
        
        # Check cache first
        cache_key = f"{t_norm}|{a_norm}"
        match = None
        if not args.no_db_cache:
            try:
                from hype_db import get_cached_match
                cached = get_cached_match(
                    db_path,
                    service="melon",
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
                        service="melon",
                        song_id=track.song_id,
                        album_id=track.album_id,
                        status="manual_blocked",
                    )
                elif cached and cached.get("video_id"):
                    match = match_from_prev(track, cached, status=cached.get("status", "cached_match"))
            except Exception as exc:
                LOG.warning("DB cache lookup failed for %s: %s", track.title, exc)
        if not match and cache_key in match_cache:
            prev = match_cache[cache_key]
            # Only reuse cache if it was a perfect match (score >= 1.0)
            if prev.get("video_id") and prev.get("score", 0) >= 1.0:
                match = MatchResult(
                    rank=track.rank,
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                    title_en=prev.get("title_en", track.title),
                    artist_en=prev.get("artist_en", track.artist),
                    album_en=prev.get("album_en", track.album),
                    title_ko=prev.get("title_ko", track.title),
                    artist_ko=prev.get("artist_ko", track.artist),
                    album_ko=prev.get("album_ko", track.album),
                    artwork_url=prev.get("artwork_url", track.artwork_url),
                    video_id=prev["video_id"],
                    yt_title=prev.get("yt_title", ""),
                    yt_artist=prev.get("yt_artist", ""),
                    yt_album=prev.get("yt_album", ""),
                    score=prev.get("score", 1.0),
                    title_score=prev.get("title_score", 1.0),
                    artist_score=prev.get("artist_score", 1.0),
                    album_score=prev.get("album_score", 1.0),
                    yt_result_type=prev.get("yt_result_type", "song"),
                    query=prev.get("query", "cached"),
                    status="cached_match",
                )

        if not match:
            # Try finding proxy by variants (including album)
            al_norm = normalize_text(track.album)
            proxy_item = apple_proxy.get(f"{t_norm}|{a_norm}|{al_norm}") or apple_proxy.get(f"{t_norm}|{a_norm}")
            
            if not proxy_item:
                # Check variants for proxy lookup
                for tv in title_variants(track.title):
                    for av in artist_variants(track.artist):
                        for alv in (album_variants(track.album) + [""]):
                            key_with_album = f"{normalize_text(tv)}|{normalize_text(av)}|{normalize_text(alv)}"
                            if key_with_album in apple_proxy:
                                proxy_item = apple_proxy[key_with_album]
                                break
                        if not proxy_item:
                            key_no_album = f"{normalize_text(tv)}|{normalize_text(av)}"
                            if key_no_album in apple_proxy:
                                proxy_item = apple_proxy[key_no_album]
                        if proxy_item: break
                    if proxy_item: break

            if not proxy_item:
                # Try matching parts if title has parentheses/brackets
                parts = re.findall(r"([^()\[\]\uFF08\uFF09]+)", track.title)
                for part in parts:
                    p_norm = normalize_text(part)
                    if p_norm and f"{p_norm}|{a_norm}" in apple_proxy:
                        proxy_item = apple_proxy[f"{p_norm}|{a_norm}"]
                        break
            
            track_ko = None
            if proxy_item:
                # Enrich with Apple metadata
                if proxy_item.get("artwork_url"):
                    track.artwork_url = proxy_item["artwork_url"]
                if proxy_item.get("title_ko") and similarity(track.title, proxy_item["title_ko"]) > 0.8:
                    track_ko = SourceTrack(
                        rank=track.rank,
                        title=proxy_item["title_ko"],
                        artist=proxy_item.get("artist_ko", track.artist),
                        album=proxy_item.get("album_ko", track.album),
                        source="apple_proxy"
                    )
                
                # Enrich English metadata if it's better in Apple (Melon is usually Korean)
                if proxy_item.get("title_en") and similarity(track.title, proxy_item["title_en"]) < 0.9:
                    # If Melon title is Korean, Apple title_en is likely the English version we need
                    track.title = proxy_item["title_en"]
                if proxy_item.get("artist_en") and similarity(track.artist, proxy_item["artist_en"]) < 0.9:
                    track.artist = proxy_item["artist_en"]                    
                if not track.album and proxy_item.get("album_en"):
                    track.album = proxy_item["album_en"]
                
                # *** PROXY FIRST: If proxy has a high-confidence video_id, use it directly ***
                # Don't bother searching if we already have a verified answer
                if proxy_item.get("video_id") and proxy_item.get("score", 0) >= 0.85:
                    p = proxy_item
                    match = MatchResult(
                        rank=track.rank,
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        title_en=p.get("title_en", track.title),
                        artist_en=p.get("artist_en", track.artist),
                        album_en=p.get("album_en", track.album),
                        title_ko=p.get("title_ko", ""),
                        artist_ko=p.get("artist_ko", ""),
                        album_ko=p.get("album_ko", ""),
                        artwork_url=track.artwork_url,
                        video_id=p["video_id"],
                        yt_title=p.get("yt_title", ""),
                        yt_artist=p.get("yt_artist", ""),
                        yt_album=p.get("yt_album", ""),
                        score=p.get("score", 1.0),
                        title_score=p.get("title_score", 1.0),
                        artist_score=p.get("artist_score", 1.0),
                        album_score=p.get("album_score", 1.0),
                        yt_result_type=p.get("yt_result_type", "song"),
                        query=p.get("query", "proxy"),
                        status="proxy_matched"
                    )
                if proxy_item.get("video_id") and proxy_item.get("score", 0) >= 0.9:
                    match = match_from_prev(track, proxy_item, track_ko=track_ko, status="proxy_matched")

            if not match:
                match = search_youtube_music(
                    ytmusic,
                    track,
                    track_ko,
                    min_score=min_score,
                    min_title_score=min_title_score,
                    min_artist_score=min_artist_score,
                    limit=search_limit,
                    ignore_video_ids=seen_video_ids,
                )
        
        # Fallback to proxy video_id if search also failed
        if not match.video_id and proxy_item and proxy_item.get("video_id"):
            if proxy_item.get("score", 0) >= 0.85:
                match.video_id = proxy_item["video_id"]
                match.status = "proxy_matched"
                match.score = proxy_item.get("score", 0.9)
                match.yt_title = proxy_item.get("yt_title", "")
                match.yt_artist = proxy_item.get("yt_artist", "")
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

    if not args.dry_run:
        try:
            from hype_db import export_frontend_history, persist_crawl_run
            persist_crawl_run(
                db_path,
                service="melon",
                job_name=job_name,
                source_variant=source_variant,
                chart_date=update_date_str,
                started_at=started_at,
                tracks=all_tracks,
                matches=matches,
            )
            export_frontend_history(db_path, args.history_json)
        except Exception as exc:
            LOG.warning("Failed to persist Melon run to DB: %s", exc)

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

    # 명시적인 로그 디렉토리가 지정되지 않은 경우, 차트 타입에 맞는 하위 폴더를 생성합니다.
    if not args.log_dir:
        base_logs = Path(os.environ.get("LOG_DIR", "logs")).expanduser()
        first_url = melon_urls[0]
        if "week" in first_url:
            subfolder = "Melon-KR-Top-100-Weekly"
        elif "day" in first_url:
            subfolder = "Melon-KR-Top-100-Daily"
        else:
            subfolder = "Melon-Chart"
        args.log_dir = str(base_logs / subfolder)

    log_dir = Path(args.log_dir).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(Path(args.db_path).expanduser())
    cache_path = load_album_cache(log_dir, ttl_days=args.album_cache_ttl)

    all_tracks: list[SourceTrack] = []
    combined_desc_parts: list[str] = []

    for url in melon_urls:
        LOG.info("Processing Melon URL: %s", url)
        try:
            p_name, p_desc, tracks = fetch_melon_tracks(url, limit=args.track_limit, log_dir=args.log_dir)

            combined_desc_parts.append(p_desc)
            all_tracks.extend(tracks)
            LOG.info("Added %d tracks from '%s'", len(tracks), p_name)

        except Exception as exc:
            LOG.error("Failed to scrape Melon URL %s: %s", url, exc)
            if len(melon_urls) == 1:
                return 1

    save_album_cache(cache_path)

    return run_tracks_pipeline(
        args,
        all_tracks,
        combined_desc_parts,
        log_prefix="melon",
        empty_message="No tracks collected from any of the provided URLs.",
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
