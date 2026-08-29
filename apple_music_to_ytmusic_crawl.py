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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from hype_db_common import dedupe_source_tracks
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


DEFAULT_APPLE_PLAYLIST_URL = "https://music.apple.com/kr/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c"
DEFAULT_APPLE_CHART_LIMIT = 100
DEFAULT_MIN_SCORE = 0.6
DEFAULT_MIN_TITLE_SCORE = 0.65
DEFAULT_MIN_ARTIST_SCORE = 0.55
DEFAULT_SEARCH_LIMIT = 25
OFFICIAL_KR_JOBS = {"KR-Top-100", "KR-Top-Songs"}
OFFICIAL_KR_LIMITS = {"KR-Top-100": 100, "KR-Top-Songs": 200}


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
            f"chart=most-played&genre=34&l=ko-KR&limit={limit}&types=songs"
        )

    parsed = urlparse("https://api.music.apple.com" + html_lib.unescape(match.group("url")))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.pop("offset", None)
    query["limit"] = str(limit)
    query["l"] = "ko-KR"
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
                locale="ko",
            )
        )

    return tracks


def _apple_api_headers(token: str, page_url: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "https://music.apple.com",
        "User-Agent": "Mozilla/5.0",
    }
    if page_url:
        headers["Referer"] = page_url
    return headers


def _fetch_song_resource_map(
    token: str,
    *,
    storefront: str,
    song_ids: list[str],
    locale: str,
    equivalents: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch song resources keyed by the input storefront ID."""
    resources: dict[str, dict[str, Any]] = {}
    for start in range(0, len(song_ids), 300):
        chunk = song_ids[start : start + 300]
        query_key = "filter[equivalents]" if equivalents else "ids"
        query = urlencode({query_key: ",".join(chunk), "l": locale})
        url = f"https://api.music.apple.com/v1/catalog/{storefront}/songs?{query}"
        try:
            response = http_session.get(
                url,
                headers=_apple_api_headers(token),
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            LOG.warning(
                "Apple %s song enrichment failed for %d IDs: %s",
                "equivalent" if equivalents else storefront,
                len(chunk),
                exc,
            )
            continue

        returned = {
            str(item.get("id") or ""): item
            for item in payload.get("data", [])
            if item.get("id")
        }
        if not equivalents:
            resources.update(returned)
            continue

        mappings = (
            payload.get("meta", {})
            .get("filters", {})
            .get("equivalents", {})
        )
        for source_id in chunk:
            for equivalent in mappings.get(source_id, []):
                equivalent_id = str(equivalent.get("id") or "")
                if equivalent_id in returned:
                    resources[source_id] = returned[equivalent_id]
                    break
    return resources


def _merge_attributes(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    merged.update({key: value for key, value in overlay.items() if value not in (None, "")})
    return merged


def _localized_tracks_from_items(
    items: list[dict[str, Any]],
    *,
    token: str,
    source: str,
    authoritative_storefront: str = "kr",
) -> tuple[list[SourceTrack], dict[str, SourceTrack]]:
    """Keep KR slots/IDs authoritative while enriching their display metadata in English."""
    if authoritative_storefront.lower() != "kr":
        tracks: list[SourceTrack] = []
        for rank, item in enumerate(items, 1):
            item_type = str(item.get("type") or "songs")
            if item_type != "songs":
                raise ValueError(
                    f"Unsupported Apple playlist resource type at rank {rank}: {item_type}"
                )
            attrs = item.get("attributes") or {}
            tracks.append(
                SourceTrack(
                    rank=rank,
                    title=str(attrs.get("name") or "").strip(),
                    artist=str(attrs.get("artistName") or "").strip(),
                    service="apple",
                    album=str(attrs.get("albumName") or "").strip(),
                    song_id=str(item.get("id") or ""),
                    source=source,
                    artwork_url=format_artwork_url(
                        (attrs.get("artwork") or {}).get("url", ""), 100
                    ),
                    locale="en",
                )
            )
        return tracks, {}

    song_ids = list(
        dict.fromkeys(str(item.get("id") or "") for item in items if item.get("id"))
    )
    brief_by_id = {
        str(item.get("id")): item.get("attributes") or {}
        for item in items
        if item.get("id")
    }
    kr_resources = _fetch_song_resource_map(
        token,
        storefront="kr",
        song_ids=song_ids,
        locale="ko-KR",
    )
    kr_attrs = {
        song_id: _merge_attributes(
            brief_by_id.get(song_id, {}),
            kr_resources.get(song_id, {}).get("attributes", {}),
        )
        for song_id in song_ids
    }

    en_resources = _fetch_song_resource_map(
        token,
        storefront="kr",
        song_ids=song_ids,
        locale="en-US",
    )
    missing_en_ids = [
        song_id
        for song_id in song_ids
        if not str(
            en_resources.get(song_id, {}).get("attributes", {}).get("name") or ""
        ).strip()
    ]
    equivalent_en = _fetch_song_resource_map(
        token,
        storefront="us",
        song_ids=missing_en_ids,
        locale="en-US",
        equivalents=True,
    )

    tracks: list[SourceTrack] = []
    tracks_ko_map: dict[str, SourceTrack] = {}
    for rank, item in enumerate(items, 1):
        item_type = str(item.get("type") or "songs")
        if item_type != "songs":
            raise ValueError(
                f"Unsupported Apple playlist resource type at rank {rank}: {item_type}"
            )

        song_id = str(item.get("id") or "")
        korean = kr_attrs.get(song_id, item.get("attributes") or {})
        english_resource = en_resources.get(song_id) or equivalent_en.get(song_id)
        english = english_resource.get("attributes", {}) if english_resource else {}
        use_english = bool(str(english.get("name") or "").strip())
        primary = english if use_english else korean

        track_ko = SourceTrack(
            rank=rank,
            title=str(korean.get("name") or "").strip(),
            artist=str(korean.get("artistName") or "").strip(),
            service="apple",
            album=str(korean.get("albumName") or "").strip(),
            song_id=song_id,
            source=f"{source}_kr",
            artwork_url=format_artwork_url(
                (korean.get("artwork") or {}).get("url", ""), 100
            ),
            locale="ko",
        )
        track = SourceTrack(
            rank=rank,
            title=str(primary.get("name") or "").strip(),
            artist=str(primary.get("artistName") or "").strip(),
            service="apple",
            album=str(primary.get("albumName") or "").strip(),
            song_id=song_id,
            source=(
                f"{source}_en"
                if song_id in en_resources and use_english
                else f"{source}_us_equivalent"
                if use_english
                else f"{source}_kr_fallback"
            ),
            artwork_url=format_artwork_url(
                (primary.get("artwork") or {}).get("url", ""), 100
            ),
            locale="en" if use_english else "ko",
        )
        tracks.append(track)
        if song_id:
            tracks_ko_map.setdefault(song_id, track_ko)
        tracks_ko_map[str(rank)] = track_ko

    return tracks, tracks_ko_map


def _chart_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    song_charts = payload.get("results", {}).get("songs", [])
    chart = song_charts[0] if song_charts else payload
    if not isinstance(chart, dict):
        return [], ""
    return chart.get("data", []), str(chart.get("next") or payload.get("next") or "")


def fetch_apple_chart_tracks(
    page_url: str,
    *,
    limit: int,
) -> tuple[str, str, list[SourceTrack], str, dict[str, SourceTrack]]:
    page_html = fetch_html(page_url)
    token = find_apple_web_token(page_html, page_url)
    chart_url = find_chart_url(page_html, limit=limit)
    LOG.info("Chart API URL: %s", chart_url)

    raw_items: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    while chart_url and len(raw_items) < limit:
        if chart_url in seen_pages:
            raise RuntimeError(f"Apple chart pagination loop detected: {chart_url}")
        seen_pages.add(chart_url)
        response = http_session.get(
            chart_url,
            headers=_apple_api_headers(token, page_url),
            timeout=30,
        )
        response.raise_for_status()
        page_items, next_path = _chart_page(response.json())
        raw_items.extend(page_items[: limit - len(raw_items)])
        chart_url = (
            urljoin("https://api.music.apple.com", next_path) if next_path else ""
        )

    if not raw_items:
        raise RuntimeError("No tracks were extracted from Apple Music charts API.")

    tracks, tracks_ko_map = _localized_tracks_from_items(
        raw_items,
        token=token,
        source="apple_web_chart_api",
    )
    desc_match = re.search(
        r'<meta (?:property="og:description"|name="description") content="([^"]+)"',
        page_html,
    )
    playlist_desc = html_lib.unescape(desc_match.group(1)) if desc_match else ""
    return (
        f"Apple Music Top {len(tracks)} Songs: Korea",
        playlist_desc,
        tracks,
        "apple_web_chart_api",
        tracks_ko_map,
    )


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
                        rank=len(tracks) + 1,
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


def fetch_apple_tracks(
    playlist_url: str,
    *,
    chart_limit: int,
) -> tuple[str, str, list[SourceTrack], str, dict[str, SourceTrack]]:
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
                playlist_data = playlist_resp.json().get("data", [])
                if playlist_data:
                    attributes = playlist_data[0].get("attributes") or {}
                    playlist_name = attributes.get("name") or playlist_name
                    description = attributes.get("description") or {}
                    if isinstance(description, dict):
                        playlist_desc = description.get("standard") or playlist_desc
            
            # Fetch playlist tracks via paginated tracks endpoint
            locale = "ko-KR" if storefront.lower() == "kr" else "en-US"
            tracks_url = (
                f"https://api.music.apple.com/v1/catalog/{storefront}/playlists/"
                f"{playlist_id}/tracks?limit=100&l={locale}"
            )
            api_tracks = []
            seen_track_pages: set[str] = set()
            while tracks_url:
                if tracks_url in seen_track_pages:
                    raise RuntimeError(
                        f"Apple playlist pagination loop detected: {tracks_url}"
                    )
                seen_track_pages.add(tracks_url)
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

            tracks, tracks_ko_map = _localized_tracks_from_items(
                api_tracks,
                token=token,
                source="apple_web_playlist_api",
                authoritative_storefront=storefront,
            )
            if tracks:
                LOG.info(
                    "Successfully fetched %d tracks via Apple Music playlist API (source: apple_web_playlist_api)",
                    len(tracks),
                )
                return (
                    playlist_name,
                    playlist_desc,
                    tracks,
                    "apple_web_playlist_api",
                    tracks_ko_map,
                )
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

    locale = "ko" if urlparse(playlist_url).path.startswith("/kr/") else "en"
    tracks_ko_map: dict[str, SourceTrack] = {}
    for track in tracks:
        track.locale = locale
        if locale == "ko":
            tracks_ko_map[str(track.rank)] = track
    return playlist_name, playlist_desc, tracks, source, tracks_ko_map


def parse_reference_period(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("reference period must use YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid reference period: {value}") from exc


def validate_official_options(
    job_name: str,
    playlist_urls: list[str],
    *,
    shuffle: bool,
    chart_limit: int | None = None,
) -> None:
    if job_name not in OFFICIAL_KR_JOBS:
        return
    if len(playlist_urls) != 1:
        raise ValueError(f"{job_name} requires exactly one Apple Music source URL")
    parsed = urlparse(playlist_urls[0])
    if (
        parsed.scheme != "https"
        or parsed.hostname != "music.apple.com"
        or not parsed.path.startswith("/kr/")
    ):
        raise ValueError(f"{job_name} requires a music.apple.com/kr/... source URL")
    if shuffle:
        raise ValueError(f"{job_name} must preserve the authoritative Apple rank order")
    expected_limit = OFFICIAL_KR_LIMITS[job_name]
    if chart_limit is not None and chart_limit != expected_limit:
        raise ValueError(f"{job_name} requires --apple-chart-limit {expected_limit}")


def validate_raw_tracks(
    tracks: list[SourceTrack],
    *,
    chart_limit: int,
    dynamic_chart: bool,
    require_song_ids: bool,
) -> None:
    valid_count = (
        0.95 * chart_limit <= len(tracks) <= chart_limit
        if dynamic_chart
        else len(tracks) == chart_limit
    )
    if not valid_count:
        if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
            LOG.warning(
                "Track count validation bypassed. Scraped %d tracks, expected %d.",
                len(tracks),
                chart_limit,
            )
        else:
            expectation = "95%-100% of" if dynamic_chart else "exactly"
            raise ValueError(
                f"Validation Error: Scraped {len(tracks)} tracks, "
                f"but expected {expectation} {chart_limit} tracks."
            )

    ranks = [track.rank for track in tracks]
    if ranks != list(range(1, len(tracks) + 1)):
        raise ValueError("Validation Error: Apple source ranks are not contiguous and ordered")
    if any(not track.title.strip() for track in tracks):
        raise ValueError("Validation Error: Apple source contains a track without a title")
    if require_song_ids and any(not track.song_id for track in tracks):
        raise ValueError("Validation Error: authoritative Apple source contains a track without an AdamID")


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
    parser.add_argument("--reference-period", type=parse_reference_period)
    parser.add_argument(
        "--skip-playlist-update",
        action="store_true",
        help="Persist crawling and matching results without mutating the target playlist",
    )
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
    playlist_urls = [url.strip() for url in playlist_urls if url.strip()]
    job_name = args.job_name or "apple_music"
    chart_limit = int(os.environ.get("APPLE_CHART_LIMIT", args.apple_chart_limit))
    try:
        validate_official_options(
            job_name,
            playlist_urls,
            shuffle=args.shuffle,
            chart_limit=chart_limit,
        )
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2

    if chart_limit <= 0:
        LOG.error("APPLE_CHART_LIMIT must be positive")
        return 2

    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE")
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_ID", ""
    )
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(
        args.yt_playlist_id,
        "YTMUSIC_PLAYLIST_ID",
        required=not args.skip_playlist_update,
    )
    db_path = Path(args.db_path).expanduser()
    playlist_name = args.playlist_name or job_name
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")
    # Preserve the normal Apple daily -1 day rule unless an explicit backfill date is supplied.
    reference_period = args.reference_period

    raw_tracks: list[SourceTrack] = []
    tracks_ko_map: dict[str, SourceTrack] = {}
    combined_desc_parts: list[str] = []

    max_retries = 3
    for url in playlist_urls:
        LOG.info("Processing Apple Music playlist: %s", url)
        for attempt in range(1, max_retries + 1):
            try:
                p_name, p_desc, tracks, source, source_ko_map = fetch_apple_tracks(
                    url,
                    chart_limit=chart_limit,
                )
                validate_raw_tracks(
                    tracks,
                    chart_limit=chart_limit,
                    dynamic_chart=source == "apple_web_chart_api",
                    require_song_ids=job_name in OFFICIAL_KR_JOBS,
                )
                break
            except Exception as exc:
                LOG.error(
                    "Attempt %d failed to scrape/validate Apple Music URL %s: %s",
                    attempt,
                    url,
                    exc,
                )
                if attempt == max_retries:
                    return 1
                time.sleep(2)

        desc_text = f"[{p_name}] {p_desc}".strip() if p_desc else f"[{p_name}]"
        combined_desc_parts.append(desc_text)
        raw_tracks.extend(tracks)
        for key, track_ko in source_ko_map.items():
            tracks_ko_map.setdefault(key, track_ko)
        LOG.info(
            "Added %d raw slots from '%s' (Total raw slots: %d)",
            len(tracks),
            p_name,
            len(raw_tracks),
        )

    if not raw_tracks:
        LOG.error("No tracks collected from any of the provided URLs.")
        return 1

    effective_tracks = dedupe_source_tracks(raw_tracks, "apple")
    LOG.info(
        "Apple source slots: raw=%d effective=%d duplicate_ids=%d",
        len(raw_tracks),
        len(effective_tracks),
        len(raw_tracks) - len(effective_tracks),
    )

    footer = (
        f"\n\nLast updated: {update_date_str}\n\n"
        "Auto-generated by Github Actions.\n- colinky.github.io/hype_wave"
    )
    full_desc = "\n".join(combined_desc_parts)
    if len(playlist_urls) > 1:
        full_desc = f"Merged from\n{full_desc}{footer}".strip()
    else:
        full_desc = f"{full_desc}{footer}".strip()

    ytmusic = make_ytmusic(yt_auth, yt_oauth_client_id, yt_oauth_client_secret)
    matched_video_ids = process_matching_pipeline(
        all_tracks=effective_tracks,
        raw_tracks=raw_tracks,
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
        reference_period=reference_period,
    )

    if args.skip_playlist_update:
        LOG.info(
            "Skipping target playlist update; crawl and matching persistence completed for %s.",
            reference_period,
        )
    else:
        if args.shuffle:
            LOG.info(
                "Shuffling %d tracks before saving to playlist.",
                len(matched_video_ids),
            )
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

    LOG.info(
        "Done. dry_run=%s reference_period=%s skip_playlist_update=%s",
        args.dry_run,
        reference_period,
        args.skip_playlist_update,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
