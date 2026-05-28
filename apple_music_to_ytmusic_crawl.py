#!/usr/bin/env python3
"""
Update an existing YouTube Music playlist by crawling one or more Apple Music public web pages.

Usage:
    python apple_music_to_ytmusic_crawl.py --apple-playlist-urls <URL1> <URL2> ...

Features:
    - Supports merging multiple Apple Music playlists into a single YTMusic playlist.
    - Automatic deduplication by Apple AdamID.
    - Fetches both US (English) and KR (Korean) metadata for maximum YouTube matching accuracy.
    - Scrapes server-rendered Apple Music pages or uses Chart API when available.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    env_or_arg,
    load_dotenv,
    load_match_cache,
    make_ytmusic,
    match_from_prev,
    normalize_text,
    search_youtube_music,
    update_ytmusic_playlist,
    write_json,
)


DEFAULT_APPLE_PLAYLIST_URL = "https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c"
DEFAULT_APPLE_CHART_LIMIT = 100
DEFAULT_MIN_SCORE = 0.6
DEFAULT_MIN_TITLE_SCORE = 0.65
DEFAULT_MIN_ARTIST_SCORE = 0.55
DEFAULT_SEARCH_LIMIT = 25


LOG = logging.getLogger("apple_music_to_ytmusic_crawl")


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ko-KR;q=0.8,ko;q=0.7",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def find_apple_web_token(page_html: str, page_url: str) -> str:
    script_sources = re.findall(r'<script[^>]+src="([^"]+)"', page_html)
    candidate_sources = [
        source
        for source in script_sources
        if "/assets/index" in source or "/musickit/" in source
    ]

    token_pattern = re.compile(r"eyJ[a-zA-Z0-9_\-.]{100,}")
    headers = {"User-Agent": "Mozilla/5.0"}

    for source in candidate_sources:
        script_url = urljoin(page_url, source)
        response = requests.get(script_url, headers=headers, timeout=30)
        response.raise_for_status()
        script_text = response.content.decode("utf-8", errors="replace")
        match = token_pattern.search(script_text)
        if match:
            return match.group(0)

    raise RuntimeError("Could not find Apple Music web developer token in page assets.")


def find_chart_url(page_html: str, *, limit: int) -> str:
    match = re.search(r'(?P<url>/v1/catalog/kr/charts\?[^"]*types=songs)', page_html)
    if not match:
        return (
            "https://api.music.apple.com/v1/catalog/kr/charts?"
            f"chart=most-played&genre=34&l=en-US&limit={limit}&types=songs"
        )

    parsed = urlparse("https://api.music.apple.com" + html_lib.unescape(match.group("url")))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.pop("offset", None)
    query["limit"] = str(limit)
    query["l"] = "en-US"
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


def format_artwork_url(url: str, size: int = 100) -> str:
    if not url:
        return ""
    return url.replace("{w}", str(size)).replace("{h}", str(size))


def parse_tracks_from_chart_api(payload: dict[str, Any]) -> list[SourceTrack]:
    """Parse tracks brief info from Apple Music charts API endpoint."""
    songs_data = payload.get("results", {}).get("songs", [])[0].get("data", []) if payload.get("results", {}).get("songs") else []
    tracks: list[SourceTrack] = []
    
    for idx, item in enumerate(songs_data, 1):
        attrs = item.get("attributes", {})
        title = attrs.get("name", "").strip()
        if not title:
            continue
            
        tracks.append(
            SourceTrack(
                rank=idx,
                title=title,
                artist=attrs.get("artistName", "").strip(),
                service="apple",
                album=attrs.get("albumName", "").strip(),
                song_id=str(item.get("id", "") or ""),
                source="apple_web_chart_api",
                artwork_url=format_artwork_url(attrs.get("artwork", {}).get("url", ""), 100),
            )
        )

    return tracks


def fetch_apple_chart_tracks(page_url: str, *, limit: int) -> tuple[str, str, list[SourceTrack], str]:
    page_html = fetch_html(page_url)
    token = find_apple_web_token(page_html, page_url)
    chart_url = find_chart_url(page_html, limit=limit)
    LOG.info("Chart API URL: %s", chart_url)
    response = requests.get(
        chart_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://music.apple.com",
            "Referer": page_url,
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    songs_data_brief = payload.get("results", {}).get("songs", [])[0].get("data", []) if payload.get("results", {}).get("songs") else []
    brief_ids = [item["id"] for item in songs_data_brief if item.get("id")]
    
    if not brief_ids:
        raise RuntimeError("No tracks were extracted from Apple Music charts API.")

    # Fetch full KR metadata including artist relationships to get artist IDs
    songs_data = []
    for i in range(0, len(brief_ids), 100):
        chunk = brief_ids[i : i + 100]
        ids_str = ",".join(chunk)
        kr_songs_url = f"https://api.music.apple.com/v1/catalog/kr/songs?ids={ids_str}&include=artists"
        kr_resp = requests.get(kr_songs_url, headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}, timeout=30)
        if kr_resp.status_code == 200:
            songs_data.extend(kr_resp.json().get("data", []))

    # Store original (likely Korean) metadata as fallback
    id_to_kr_attrs = {item["id"]: item.get("attributes", {}) for item in songs_data if item.get("id")}
    ids = list(id_to_kr_attrs.keys())
    
    if not ids:
        raise RuntimeError("No tracks were extracted from Apple Music charts API.")

    rank_by_id = {song_id: idx + 1 for idx, song_id in enumerate(ids)}
    song_id_to_artist_id = {}
    for item in songs_data:
        sid = item.get("id")
        rel_artists = item.get("relationships", {}).get("artists", {}).get("data", [])
        if sid and rel_artists:
            song_id_to_artist_id[sid] = rel_artists[0]["id"]

    # Fetch English metadata from US storefront using collected IDs
    tracks_en: list[SourceTrack] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        ids_str = ",".join(chunk)
        us_songs_url = f"https://api.music.apple.com/v1/catalog/us/songs?ids={ids_str}"
        us_resp = requests.get(
            us_songs_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://music.apple.com",
            },
            timeout=30,
        )
        us_resp.raise_for_status()
        us_payload = us_resp.json()
        us_data = us_payload.get("data", [])
        
        # Map IDs to their English attributes
        id_to_en_attrs = {item["id"]: item["attributes"] for item in us_data}

        # Fetch missing artists in English
        needed_artist_ids = {song_id_to_artist_id[aid] for aid in chunk if aid not in id_to_en_attrs and aid in song_id_to_artist_id}
        id_to_en_artist_name = {}
        if needed_artist_ids:
            artists_str = ",".join(list(needed_artist_ids)[:100])
            us_art_url = f"https://api.music.apple.com/v1/catalog/us/artists?ids={artists_str}"
            us_art_resp = requests.get(us_art_url, headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}, timeout=30)
            if us_art_resp.status_code == 200:
                for art_item in us_art_resp.json().get("data", []):
                    id_to_en_artist_name[art_item["id"]] = art_item["attributes"].get("name")

        for song_id in chunk:
            # Prefer English Song, fallback to Korean Song
            attrs = id_to_en_attrs.get(song_id) or id_to_kr_attrs.get(song_id, {})
            rank = rank_by_id[song_id]
            
            # If we used KR fallback, try to at least use the English Artist Name
            artist_name = attrs.get("artistName", "").strip()
            if song_id not in id_to_en_attrs:
                art_id = song_id_to_artist_id.get(song_id)
                if art_id and art_id in id_to_en_artist_name:
                    artist_name = id_to_en_artist_name[art_id]

            source_label = "apple_web_chart_api_us_lookup" if song_id in id_to_en_attrs else "apple_web_chart_api_kr_fallback"
            
            tracks_en.append(
                SourceTrack(
                    rank=rank,
                    title=attrs.get("name", "").strip(),
                    artist=artist_name,
                    service="apple",
                    album=attrs.get("albumName", "").strip(),
                    song_id=song_id,
                    source=source_label,
                    artwork_url=format_artwork_url(attrs.get("artwork", {}).get("url", ""), 100),
                )
            )

    desc_match = re.search(r'<meta (?:property="og:description"|name="description") content="([^"]+)"', page_html)
    playlist_desc = html_lib.unescape(desc_match.group(1)) if desc_match else ""
    
    return f"Apple Music Top {len(tracks_en)} Songs: Korea", playlist_desc, tracks_en, "apple_web_chart_api"


def extract_balanced_json_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    raise ValueError("Could not find the end of the embedded JSON object.")


def extract_track_lockup_object(page_html: str) -> dict[str, Any]:
    marker = '"itemKind":"trackLockup"'
    marker_index = page_html.find(marker)
    if marker_index == -1:
        raise ValueError("Apple Music trackLockup JSON marker was not found.")

    object_start = page_html.rfind('{"id":"track-list', 0, marker_index)
    if object_start == -1:
        object_start = page_html.rfind("{", 0, marker_index)

    raw_json = extract_balanced_json_object(page_html, object_start)
    return json.loads(raw_json)


def parse_tracks_from_track_lockup(page_html: str) -> list[SourceTrack]:
    """Fallback: Parse tracks from trackLockup JSON objects embedded in the HTML."""
    tracks: list[SourceTrack] = []
    
    while True:
        try:
            lockup = extract_track_lockup_object(page_html)
            if not lockup:
                break
            
            # Remove processed block to find the next one
            marker_index = page_html.find('"itemKind":"trackLockup"')
            page_html = page_html[marker_index + len('"itemKind":"trackLockup"') :]
            
            title = lockup.get("title", "").strip()
            artist = lockup.get("artists", [{}])[0].get("name", "").strip()
            if title:
                tracks.append(
                    SourceTrack(
                        rank=0,
                        title=title,
                        artist=artist,
                        service="apple",
                        source="track_lockup",
                    )
                )
        except Exception:
            break
    return tracks


def parse_tracks_from_json_ld(page_html: str) -> list[SourceTrack]:
    """Fallback: Parse tracks from schema.org MusicPlaylist JSON-LD."""
    soup = BeautifulSoup(page_html, "html.parser")
    ld_script = soup.find("script", type="application/ld+json")
    if not ld_script:
        return []
        
    try:
        data = json.loads(ld_script.string or "")
        
        # Apple Music playlist page data is normally nested inside an MusicPlaylist object
        tracks_data = data.get("track", []) if data.get("@type") == "MusicPlaylist" else []
        
        tracks = []
        for idx, item in enumerate(tracks_data, 1):
            if item.get("@type") == "MusicRecording":
                title = item.get("name", "").strip()
                if not title:
                    continue
                tracks.append(
                    SourceTrack(
                        rank=idx,
                        title=title,
                        artist="",
                        service="apple",
                        source="json_ld",
                    )
                )
            if tracks:
                return tracks
    except Exception:
        pass

    return []


def fetch_apple_tracks(playlist_url: str, *, chart_limit: int) -> tuple[str, str, list[SourceTrack], str]:
    if "/new/top-charts/songs" in playlist_url:
        return fetch_apple_chart_tracks(playlist_url, limit=chart_limit)

    page_html = fetch_html(playlist_url)
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', page_html)
    playlist_name = html_lib.unescape(title_match.group(1)) if title_match else "Apple Music Top 100"
    
    desc_match = re.search(r'<meta (?:property="og:description"|name="description") content="([^"]+)"', page_html)
    playlist_desc = html_lib.unescape(desc_match.group(1)) if desc_match else ""

    try:
        tracks = parse_tracks_from_track_lockup(page_html)
        source = "track_lockup"
    except Exception as exc:
        LOG.warning("trackLockup parse failed: %s", exc)
        tracks = parse_tracks_from_json_ld(page_html)
        source = "json_ld"

    if not tracks:
        raise RuntimeError("No tracks were extracted from the Apple Music web page.")

    return playlist_name, playlist_desc, tracks, source



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily Apple Music Korea Top 100 web crawler to YouTube Music updater."
    )
    parser.add_argument("--env-file", default=".env", help="dotenv file path")
    parser.add_argument("--apple-playlist-urls", nargs="+", help="One or more Apple Music playlist URLs to merge")
    parser.add_argument("--apple-chart-limit", type=int, default=DEFAULT_APPLE_CHART_LIMIT)
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

    playlist_urls = args.apple_playlist_urls or os.environ.get(
        "APPLE_PLAYLIST_URL", DEFAULT_APPLE_PLAYLIST_URL
    ).split(",")
    # Clean up whitespace and empty strings
    playlist_urls = [url.strip() for url in playlist_urls if url.strip()]

    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE")
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID")
    log_dir = Path(args.log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    db_path = Path(args.db_path).expanduser()
    job_name = args.job_name or log_dir.name
    playlist_name = args.playlist_name or job_name
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    # Generate update date string in KST (UTC+9)
    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")

    all_tracks: list[SourceTrack] = []
    tracks_ko_map: dict[str, SourceTrack] = {}
    seen_song_ids: set[str] = set()
    combined_desc_parts = []

    for url in playlist_urls:
        LOG.info("Processing Apple Music playlist: %s", url)
        try:
            p_name, p_desc, tracks, source = fetch_apple_tracks(
                url,
                chart_limit=int(os.environ.get("APPLE_CHART_LIMIT", args.apple_chart_limit)),
            )
            
            # Always add to description parts to maintain order and show name
            desc_text = f"[{p_name}] {p_desc}".strip() if p_desc else f"[{p_name}]"
            combined_desc_parts.append(desc_text)

            # Fetch Korean fallback tracks for this URL
            try:
                if "/us/" in url:
                    _, _, tracks_ko, _ = fetch_apple_tracks(
                        url.replace("/us/", "/kr/"),
                        chart_limit=int(os.environ.get("APPLE_CHART_LIMIT", args.apple_chart_limit)),
                    )
                    for t in tracks_ko:
                        if t.song_id:
                            tracks_ko_map[t.song_id] = t
                elif "/new/top-charts/" in url:
                    page_html_ko = fetch_html(url)
                    token_ko = find_apple_web_token(page_html_ko, url)
                    chart_url_ko = find_chart_url(page_html_ko, limit=int(os.environ.get("APPLE_CHART_LIMIT", args.apple_chart_limit)))
                    parsed_ko = urlparse(chart_url_ko)
                    query_ko = dict(parse_qsl(parsed_ko.query, keep_blank_values=True))
                    query_ko["l"] = "ko"
                    chart_url_ko = urlunparse((parsed_ko.scheme, parsed_ko.netloc, parsed_ko.path, parsed_ko.params, urlencode(query_ko), parsed_ko.fragment))
                    resp_ko = requests.get(
                        chart_url_ko,
                        headers={"Authorization": f"Bearer {token_ko}", "Origin": "https://music.apple.com"},
                        timeout=30,
                    )
                    if resp_ko.status_code == 200:
                        tracks_ko = parse_tracks_from_chart_api(resp_ko.json())
                        for t in tracks_ko:
                            if t.song_id:
                                tracks_ko_map[t.song_id] = t
                            tracks_ko_map[str(t.rank)] = t
            except Exception as exc:
                LOG.warning("Failed to fetch Korean fallback tracks for %s: %s", url, exc)

            # Deduplicate and aggregate
            new_count = 0
            for t in tracks:
                if t.song_id and t.song_id not in seen_song_ids:
                    seen_song_ids.add(t.song_id)
                    all_tracks.append(t)
                    new_count += 1
                elif not t.song_id:
                    all_tracks.append(t)
                    new_count += 1
            LOG.info("Added %d new tracks from '%s' (Total: %d)", new_count, p_name, len(all_tracks))

        except Exception as exc:
            LOG.error("Failed to fetch tracks from Apple Music URL %s: %s", url, exc)
            if len(playlist_urls) == 1:
                return 1

    if not all_tracks:
        LOG.error("No tracks collected from any of the provided URLs.")
        return 1

    # Re-rank tracks after aggregation
    for i, t in enumerate(all_tracks, 1):
        t.rank = i

    footer = f"\n\nLast updated: {update_date_str}\n\nAuto-generated by Github Actions.\n- colinky.github.io/hype_wave"
    full_desc = "\n".join(combined_desc_parts)
    if len(playlist_urls) > 1:
        full_desc = f"Merged from\n{full_desc}{footer}".strip()
    else:
        full_desc = f"{full_desc}{footer}".strip()
    
    LOG.info(
        "Matching settings: min_score=%.2f min_title_score=%.2f min_artist_score=%.2f search_limit=%d",
        min_score,
        min_title_score,
        min_artist_score,
        search_limit,
    )
    ytmusic = make_ytmusic(yt_auth, yt_oauth_client_id, yt_oauth_client_secret)
    matches: list[MatchResult] = []
    seen_video_ids: set[str] = set()
    
    # Load previous results to speed up matching
    match_cache = load_match_cache(log_dir)

    for track in all_tracks:
        track_ko = tracks_ko_map.get(track.song_id) or tracks_ko_map.get(str(track.rank))
        
        # Check cache first
        t_norm = normalize_text(track.title)
        a_norm = normalize_text(track.artist)
        cache_key = f"{t_norm}|{a_norm}"
        
        match = None
        if not args.no_db_cache:
            try:
                from hype_db import get_cached_match
                cached = get_cached_match(
                    db_path,
                    service="apple",
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
                        service="apple",
                        song_id=track.song_id,
                        status="manual_blocked",
                    )
                elif cached and cached.get("video_id"):
                    match = match_from_prev(track, cached, track_ko=track_ko, status=cached.get("status", "cached_match"))
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
                title_en=track.title,
                artist_en=track.artist,
                album_en=track.album,
                title_ko=track_ko.title if track_ko else prev.get("title_ko", ""),
                artist_ko=track_ko.artist if track_ko else prev.get("artist_ko", ""),
                album_ko=track_ko.album if track_ko else prev.get("album_ko", ""),
                song_id=track.song_id,
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
                service="apple",
                job_name=job_name,
                chart_date=update_date_str,
                started_at=started_at,
                tracks=all_tracks,
                matches=matches,
            )
            export_frontend_history(db_path, args.history_json)
        except Exception as exc:
            LOG.warning("Failed to persist Apple run to DB: %s", exc)

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
        service="apple",
        job_name=job_name,
        playlist_name=playlist_name,
    )

    LOG.info("Done. dry_run=%s", args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
