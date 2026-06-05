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

from bs4 import BeautifulSoup

from ytmusic_playlist_sync import (
    SourceTrack,
    env_or_arg,
    load_dotenv,
    make_ytmusic,
    normalize_text,
    update_ytmusic_playlist,
    write_json,
    get_resilient_session,
)
from crawler_common import process_matching_pipeline

http_session = get_resilient_session()


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
    response = http_session.get(url, headers=headers, timeout=30)
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
        response = http_session.get(script_url, headers=headers, timeout=30)
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
    response = http_session.get(
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
        kr_resp = http_session.get(kr_songs_url, headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}, timeout=30)
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
        us_resp = http_session.get(
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
            us_art_resp = http_session.get(us_art_url, headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}, timeout=30)
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
        except Exception as exc:
            if not tracks:
                LOG.warning("trackLockup extraction failed: %s", exc)
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

    # Try Catalog API first
    playlist_match = re.search(
        r"music\.apple\.com/([^/]+)/playlist/(?:[^/]+/)?(pl\.[a-zA-Z0-9\-]+)", playlist_url
    )
    if playlist_match:
        storefront, playlist_id = playlist_match.groups()
        try:
            token = find_apple_web_token(page_html, playlist_url)
            
            # Fetch playlist name and description via API if possible
            playlist_api_url = f"https://api.music.apple.com/v1/catalog/{storefront}/playlists/{playlist_id}"
            playlist_resp = http_session.get(
                playlist_api_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://music.apple.com",
                    "Referer": playlist_url,
                },
                timeout=30,
            )
            if playlist_resp.status_code == 200:
                p_data = playlist_resp.json().get("data", [])[0]
                playlist_name = p_data.get("attributes", {}).get("name", playlist_name)
                playlist_desc = p_data.get("attributes", {}).get("description", {}).get("standard", playlist_desc)
            
            # Fetch playlist tracks via paginated tracks endpoint
            tracks_url = f"https://api.music.apple.com/v1/catalog/{storefront}/playlists/{playlist_id}/tracks?limit=100"
            api_tracks = []
            while tracks_url:
                resp = http_session.get(
                    tracks_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Origin": "https://music.apple.com",
                        "Referer": playlist_url,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                api_tracks.extend(data.get("data", []))
                
                next_path = data.get("next")
                if next_path:
                    tracks_url = urljoin("https://api.music.apple.com", next_path)
                else:
                    tracks_url = None

            tracks = []
            for idx, item in enumerate(api_tracks, 1):
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
                        song_id=item.get("id", ""),
                        source="apple_web_playlist_api",
                        artwork_url=format_artwork_url(attrs.get("artwork", {}).get("url", ""), 100),
                    )
                )
            if tracks:
                LOG.info(
                    "Successfully fetched %d tracks via Apple Music playlist API (source: apple_web_playlist_api)",
                    len(tracks),
                )
                return playlist_name, playlist_desc, tracks, "apple_web_playlist_api"
        except Exception as exc:
            LOG.warning("Failed to fetch tracks via playlist API, falling back to page parsing: %s", exc)

    # Fallback to scraping
    tracks = parse_tracks_from_track_lockup(page_html)
    source = "track_lockup"

    if not tracks:
        LOG.warning("trackLockup returned 0 tracks, falling back to JSON-LD")
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
    db_path = Path(args.db_path).expanduser()
    job_name = args.job_name or "apple_music"
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

    max_retries = 3
    for url in playlist_urls:
        LOG.info("Processing Apple Music playlist: %s", url)
        chart_limit = int(os.environ.get("APPLE_CHART_LIMIT", args.apple_chart_limit))
        for attempt in range(1, max_retries + 1):
            try:
                p_name, p_desc, tracks, source = fetch_apple_tracks(
                    url,
                    chart_limit=chart_limit,
                )
                
                # Validation check: Ensure the track count matches the expected limit
                if len(tracks) != chart_limit:
                    if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
                        LOG.warning(
                            "Track count validation bypassed. Scraped %d tracks, expected %d.",
                            len(tracks), chart_limit
                        )
                    else:
                        raise ValueError(
                            f"Validation Error: Scraped {len(tracks)} tracks, "
                            f"but expected exactly {chart_limit} tracks."
                        )
                
                # Always add to description parts to maintain order and show name
                desc_text = f"[{p_name}] {p_desc}".strip() if p_desc else f"[{p_name}]"
                combined_desc_parts.append(desc_text)

                # Fetch Korean fallback tracks for this URL
                try:
                    if "/us/" in url:
                        _, _, tracks_ko, _ = fetch_apple_tracks(
                            url.replace("/us/", "/kr/"),
                            chart_limit=chart_limit,
                        )
                        for t in tracks_ko:
                            if t.song_id:
                                tracks_ko_map[t.song_id] = t
                    elif "/new/top-charts/" in url:
                        page_html_ko = fetch_html(url)
                        token_ko = find_apple_web_token(page_html_ko, url)
                        chart_url_ko = find_chart_url(page_html_ko, limit=chart_limit)
                        parsed_ko = urlparse(chart_url_ko)
                        query_ko = dict(parse_qsl(parsed_ko.query, keep_blank_values=True))
                        query_ko["l"] = "ko"
                        chart_url_ko = urlunparse((parsed_ko.scheme, parsed_ko.netloc, parsed_ko.path, parsed_ko.params, urlencode(query_ko), parsed_ko.fragment))
                        resp_ko = http_session.get(
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
                break  # Success, exit retry loop
            except Exception as exc:
                LOG.error("Attempt %d failed to scrape/validate Apple Music URL %s: %s", attempt, url, exc)
                if attempt == max_retries:
                    return 1
                else:
                    time.sleep(2)

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
    matched_video_ids = process_matching_pipeline(
        all_tracks=all_tracks,
        tracks_ko_map=tracks_ko_map,
        ytmusic=ytmusic,
        db_path=db_path,
        service="apple",
        job_name=job_name,
        source_variant="default",
        update_date_str=update_date_str,
        started_at=started_at,
        no_db_cache=args.no_db_cache,
        min_score=min_score,
        min_title_score=min_title_score,
        min_artist_score=min_artist_score,
        search_limit=search_limit,
        dry_run=args.dry_run,
        history_json=args.history_json,
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
