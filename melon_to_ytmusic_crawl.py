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


def fetch_melon_tracks(url: str, *, limit: int = 100) -> tuple[str, str, list[SourceTrack]]:
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
        
        melon_url = f"https://www.melon.com/song/detail.htm?songId={song_id}" if song_id else ""
        
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
        
        tracks.append(
            SourceTrack(
                rank=i + 1,
                title=title,
                artist=artist,
                album=album,
                apple_id=f"melon_{song_id}" if song_id else f"melon_idx_{i}",
                url=melon_url,
                source="melon_web_scrape",
                artwork_url=artwork_url,
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
    parser.add_argument("--log-dir")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--min-title-score", type=float, default=DEFAULT_MIN_TITLE_SCORE)
    parser.add_argument("--min-artist-score", type=float, default=DEFAULT_MIN_ARTIST_SCORE)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--apple-proxy-data", help="Path to one or more Apple matches_crawl.json files to use as metadata proxy")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks before saving them to the YouTube Music playlist")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    melon_urls = args.melon_urls or []
    if not melon_urls:
        LOG.error("No Melon URLs provided.")
        return 1
        
    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE")
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID")
    log_dir = Path(args.log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    
    # Load Apple proxy data for better matching (localized titles, albums, and video_ids)
    apple_proxy: dict[str, dict[str, Any]] = {}
    proxy_paths = args.apple_proxy_data or os.environ.get("APPLE_PROXY_DATA", "")
    if proxy_paths:
        for p_path in proxy_paths.split(","):
            p_path = p_path.strip()
            if not p_path: continue
            path = Path(p_path)
            if path.exists():
                try:
                    import json
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

                            for t in titles:
                                for a in artists:
                                    for al in albums:
                                        key = f"{normalize_text(t)}|{normalize_text(a)}|{normalize_text(al)}"
                                        apple_proxy[key] = item
                                        # 앨범명 없이도 조회 가능하도록 추가 인덱싱
                                        apple_proxy[f"{normalize_text(t)}|{normalize_text(a)}"] = item

                    LOG.info("Loaded %d proxy entries (with variants) from %s", len(proxy_list), path.name)
                except Exception as exc:
                    LOG.warning("Failed to load apple proxy data from %s: %s", path, exc)

    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")

    all_tracks: list[SourceTrack] = []
    combined_desc_parts = []
    
    for url in melon_urls:
        LOG.info("Processing Melon URL: %s", url)
        try:
            p_name, p_desc, tracks = fetch_melon_tracks(url, limit=args.track_limit)
            
            combined_desc_parts.append(p_desc)
            
            for t in tracks:
                all_tracks.append(t)
            LOG.info("Added %d tracks from '%s'", len(tracks), p_name)
            
        except Exception as exc:
            LOG.error("Failed to scrape Melon URL %s: %s", url, exc)
            if len(melon_urls) == 1:
                return 1

    if not all_tracks:
        LOG.error("No tracks collected from any of the provided URLs.")
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
    log_dir.mkdir(parents=True, exist_ok=True)
    write_json(log_dir / f"melon_tracks_crawl_{started_at}.json", [asdict(track) for track in all_tracks])

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
        if cache_key in match_cache:
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
                    apple_id=track.apple_id,
                    url=track.url,
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
                    status="cached_match"
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
                if not track.album and proxy_item.get("album_en"):
                    track.album = proxy_item["album_en"]
                
                # *** PROXY FIRST: If proxy has a high-confidence video_id, use it directly ***
                # Don't bother searching if we already have a verified answer
                if proxy_item.get("video_id") and proxy_item.get("score", 0) >= 1.0:
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
                        apple_id=track.apple_id,
                        url=track.url,
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

    write_json(log_dir / f"matches_crawl_{started_at}.json", [asdict(match) for match in matches])
    write_json(log_dir / "latest_matches_crawl.json", [asdict(match) for match in matches])
    write_json(log_dir / "latest_failed_crawl.json", [asdict(match) for match in failed])

    if args.shuffle:
        LOG.info("Shuffling %d tracks before saving to playlist.", len(matched_video_ids))
        random.shuffle(matched_video_ids)

    update_ytmusic_playlist(
        ytmusic,
        yt_playlist_id,
        matched_video_ids,
        description=full_desc,
        dry_run=args.dry_run,
    )

    LOG.info("Done. dry_run=%s", args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
