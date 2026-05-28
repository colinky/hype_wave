#!/usr/bin/env python3
"""
Update an existing YouTube Music playlist from one or more public Spotify playlists.
This version uses web scraping (via the Spotify embed page) instead of the official API,
meaning it doesn't require a Spotify Premium account or API credentials.

Usage:
    python spotify_to_ytmusic_crawl.py --spotify-playlist-urls <URL1> <URL2> ...

Features:
    - Supports merging multiple Spotify playlists into a single YTMusic playlist.
    - Automatic deduplication by Spotify Track ID.
    - Fetches both US (English) and KR (Korean) metadata for maximum YouTube matching accuracy.
    - Uses Spotify page/embed data and enriches missing album names through MusicBrainz.
    - No Spotify API credentials are required.
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
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests

from ytmusic_playlist_sync import (
    MatchResult,
    SourceTrack,
    artist_variants,
    env_or_arg,
    load_dotenv,
    load_match_cache,
    make_ytmusic,
    normalize_text,
    search_youtube_music,
    similarity,
    cleanup_old_logs,
    update_ytmusic_playlist,
    write_json,
    artist_variants,
    title_variants,
    album_variants,
    match_from_prev,
)


DEFAULT_SPOTIFY_PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DWT9uTRZAYj0c"
DEFAULT_MIN_SCORE = 0.6
DEFAULT_MIN_TITLE_SCORE = 0.65
DEFAULT_MIN_ARTIST_SCORE = 0.55
DEFAULT_SEARCH_LIMIT = 25
MUSICBRAINZ_MIN_TITLE_SCORE = 0.82
MUSICBRAINZ_MIN_ARTIST_SCORE = 0.55
MUSICBRAINZ_REQUEST_INTERVAL_SECONDS = 1.1
MUSICBRAINZ_BAD_RELEASE_MARKERS = (
    "live",
    "karaoke",
    "instrumental",
    "sped up",
    "slowed",
    "nightcore",
    "remix",
)
MUSICBRAINZ_BAD_SECONDARY_TYPES = {
    "Compilation",
    "DJ-mix",
    "Live",
    "Mixtape/Street",
    "Remix",
}

LOG = logging.getLogger("spotify_to_ytmusic_crawl")
_last_musicbrainz_request_at = 0.0


def spotify_playlist_id(value: str) -> str:
    match = re.search(r"(?:playlist/|spotify:playlist:)([A-Za-z0-9]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", value):
        return value
    raise ValueError(f"Could not parse Spotify playlist id: {value}")


def parse_bool(value: str | bool | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def text_field(value: Any) -> str:
    return str(value or "").strip() if not isinstance(value, (dict, list)) else ""


def first_text_from_mapping(value: Any, keys: tuple[str, ...] = ("name", "title")) -> str:
    if not isinstance(value, dict):
        return text_field(value)
    for key in keys:
        text = text_field(value.get(key))
        if text:
            return text
    return ""


def candidate_album_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""

    direct_keys = (
        "albumName",
        "album_name",
        "albumTitle",
        "album_title",
        "releaseName",
        "release_name",
        "releaseTitle",
        "release_title",
    )
    for key in direct_keys:
        text = text_field(value.get(key))
        if text:
            return text

    nested_keys = ("album", "albumOfTrack", "release", "releaseOfTrack")
    for key in nested_keys:
        text = first_text_from_mapping(value.get(key))
        if text:
            return text

    for key, nested in value.items():
        lower_key = str(key).lower()
        if "album" not in lower_key and "release" not in lower_key:
            continue
        text = first_text_from_mapping(nested)
        if text:
            return text

    return ""


def iter_mappings(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_mappings(nested)
    elif isinstance(value, list):
        for item in value:
            yield from iter_mappings(item)


def mapping_has_track_id(value: dict[str, Any], track_id: str) -> bool:
    if not track_id:
        return False
    for key in ("id", "uri", "gid", "shareUrl", "url"):
        raw = value.get(key)
        if isinstance(raw, str) and (raw == track_id or raw.endswith(track_id) or f"/track/{track_id}" in raw):
            return True
    return False


def album_name_from_page_data(item: dict[str, Any], data: dict[str, Any], track_id: str) -> str:
    album_name = candidate_album_name(item)
    if album_name:
        return album_name

    for mapping in iter_mappings(item):
        album_name = candidate_album_name(mapping)
        if album_name:
            return album_name

    if track_id:
        for mapping in iter_mappings(data):
            if mapping_has_track_id(mapping, track_id):
                album_name = candidate_album_name(mapping)
                if album_name:
                    return album_name
    return ""


def spotify_artist_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def musicbrainz_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def musicbrainz_rate_limit() -> None:
    global _last_musicbrainz_request_at
    elapsed = time.monotonic() - _last_musicbrainz_request_at
    if elapsed < MUSICBRAINZ_REQUEST_INTERVAL_SECONDS:
        time.sleep(MUSICBRAINZ_REQUEST_INTERVAL_SECONDS - elapsed)
    _last_musicbrainz_request_at = time.monotonic()


def musicbrainz_recording_artists(recording: dict[str, Any]) -> str:
    names: list[str] = []
    for credit in recording.get("artist-credit", []):
        if isinstance(credit, dict):
            name = text_field(credit.get("name"))
            if not name and isinstance(credit.get("artist"), dict):
                name = text_field(credit["artist"].get("name"))
            if name:
                names.append(name)
        elif isinstance(credit, str):
            names.append(credit)
    return " ".join(names)


def musicbrainz_release_score(title: str, release: dict[str, Any]) -> tuple[float, str]:
    if not isinstance(release, dict):
        return -1.0, ""

    release_title = text_field(release.get("title"))
    if not release_title:
        return -1.0, ""

    if release.get("status") != "Official":
        return -1.0, ""

    release_group = release.get("release-group", {})
    if not isinstance(release_group, dict):
        release_group = {}

    secondary_types = release_group.get("secondary-types") or []
    if any(secondary_type in MUSICBRAINZ_BAD_SECONDARY_TYPES for secondary_type in secondary_types):
        return -1.0, ""

    source_title_norm = normalize_text(title)
    release_title_norm = normalize_text(release_title)
    for marker in MUSICBRAINZ_BAD_RELEASE_MARKERS:
        if marker in release_title_norm and marker not in source_title_norm:
            return -1.0, ""

    primary_type = release_group.get("primary-type")
    score = 0.25
    if primary_type == "Album":
        score += 0.35
    elif primary_type == "Single":
        score += 0.30
    elif primary_type == "EP":
        score += 0.20

    if release.get("country") in {"XW", "US", "KR", "GB", "JP"}:
        score += 0.05

    if release_title_norm == source_title_norm:
        score += 0.10

    if "deluxe" in release_title_norm or "version" in release_title_norm:
        score -= 0.05

    return score, release_title


def best_release_title(title: str, recording: dict[str, Any]) -> tuple[float, str]:
    releases = recording.get("releases", [])
    if not isinstance(releases, list):
        return -1.0, ""

    best_score = -1.0
    best_title = ""
    for release in releases:
        release_score, release_title = musicbrainz_release_score(title, release)
        if release_score > best_score:
            best_score = release_score
            best_title = release_title

    return best_score, best_title


def musicbrainz_recording_score(title: str, artist: str, recording: dict[str, Any]) -> tuple[float, float, float]:
    mb_title = text_field(recording.get("title"))
    mb_artists = musicbrainz_recording_artists(recording)
    title_score = similarity(title, mb_title)

    source_artist = spotify_artist_text(artist)
    artist_candidates = artist_variants(source_artist) if source_artist else [""]
    artist_score = max(
        (similarity(candidate, mb_artists) for candidate in artist_candidates if candidate),
        default=0.0,
    )
    score = (title_score * 0.65) + (artist_score * 0.35)
    return score, title_score, artist_score


@lru_cache(maxsize=4096)
def get_album_from_musicbrainz(title: str, artist: str) -> str:
    """Return a MusicBrainz release title for a Spotify track when confidence is high."""
    title = title.strip()
    artist = spotify_artist_text(artist)
    if not title or not artist:
        return ""

    main_artist = artist_variants(artist)[0] if artist_variants(artist) else artist
    queries = [
        f'recording:"{musicbrainz_escape(title)}" AND artist:"{musicbrainz_escape(main_artist)}"',
        f'recording:"{musicbrainz_escape(title)}"',
    ]

    best_album = ""
    best_score = 0.0
    headers = {
        "User-Agent": os.environ.get(
            "MUSICBRAINZ_USER_AGENT",
            "app_to_you/1.0 (https://github.com/yule/app_to_you)",
        )
    }

    for query in queries:
        try:
            musicbrainz_rate_limit()
            response = requests.get(
                "https://musicbrainz.org/ws/2/recording",
                params={"query": query, "limit": 10, "fmt": "json"},
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            LOG.debug("MusicBrainz lookup failed for '%s' / '%s': %s", title, artist, exc)
            continue

        for recording in data.get("recordings", []):
            if not isinstance(recording, dict):
                continue
            release_score, album = best_release_title(title, recording)
            if not album:
                continue
            recording_score, title_score, artist_score = musicbrainz_recording_score(title, artist, recording)
            if title_score < MUSICBRAINZ_MIN_TITLE_SCORE or artist_score < MUSICBRAINZ_MIN_ARTIST_SCORE:
                continue
            score = recording_score + (release_score * 0.25)
            if score > best_score:
                best_score = score
                best_album = album

        if best_album:
            break

    if best_album and normalize_text(best_album):
        LOG.debug(
            "MusicBrainz album matched %.3f - %s / %s / %s",
            best_score,
            title,
            artist,
            best_album,
        )
        return best_album
    return ""


def fetch_spotify_tracks_scraped(
    playlist_url: str,
    *,
    market: str = "US",
    limit: int | None = None,
    use_musicbrainz: bool = True,
    match_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, list[SourceTrack], str]:
    playlist_id = spotify_playlist_id(playlist_url)
    # Use the embed page which is more stable for scraping
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9" if market == "US" else "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    # Adding market and locale params to force localization
    params = {
        "utm_source": "generator",
        "market": market,
        "locale": "ko" if market == "KR" else "en",
    }
    
    response = requests.get(embed_url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    
    html = response.text
    
    # Extract JSON from <script id="__NEXT_DATA__" type="application/json">...</script>
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', html)
    if not match:
        raise RuntimeError(f"Could not find __NEXT_DATA__ JSON in Spotify embed page for {playlist_id}")
    
    data = json.loads(match.group(1))
    
    # The structure is usually data -> props -> pageProps -> state -> data -> entity
    # Or simplified in embed: data -> props -> pageProps -> state -> ...
    try:
        props = data.get("props", {})
        page_props = props.get("pageProps", {})
        state = page_props.get("state", {})
        
        # In modern embed, it's often in state['data']['entity']
        entity = state.get("data", {}).get("entity", {})
        if not entity:
            # Fallback for different versions
            entity = page_props.get("entity", {})

        playlist_name = entity.get("title", f"Spotify Playlist {playlist_id}")
        playlist_desc = entity.get("description", "")
        
        raw_items = []
        # Try different paths for track items
        if "trackList" in entity:
            raw_items = entity["trackList"]
        elif "tracks" in entity:
            raw_items = entity["tracks"]
        elif "content" in entity and "items" in entity["content"]:
            raw_items = entity["content"]["items"]
            
        if not raw_items:
            LOG.warning(f"No tracks found in Spotify embed page for {playlist_id}. This might be a private or empty playlist.")

        tracks: list[SourceTrack] = []
        musicbrainz_album_count = 0
        for i, item in enumerate(raw_items):
            # item is usually a track object directly in embed
            # track_id fallback to title+artist if uri/id is missing
            raw_uri = item.get("uri") or item.get("id") or ""
            track_id = raw_uri.split(":")[-1] if ":" in raw_uri else raw_uri
            
            title = item.get("title", "").strip()
            if not title:
                continue
                
            # Artists are in 'subtitle' or a list
            artist_str = item.get("subtitle", "").strip()
            
            album_name = album_name_from_page_data(item, data, track_id)
            if use_musicbrainz and not album_name:
                # Lazy MusicBrainz: check cache first
                t_norm = normalize_text(title)
                a_norm = normalize_text(artist_str)
                cache_key = f"{t_norm}|{a_norm}"
                
                if match_cache and cache_key in match_cache:
                    album_name = match_cache[cache_key].get("album", "")
                
                if not album_name:
                    album_name = get_album_from_musicbrainz(title, artist_str)
                    if album_name:
                        musicbrainz_album_count += 1
            
            tracks.append(
                SourceTrack(
                    rank=len(tracks) + 1,
                    title=title,
                    artist=artist_str,
                    service="spotify",
                    album=album_name,
                    song_id=track_id,
                    source="spotify_embed_scrape",
                )
            )
            if limit and len(tracks) >= limit:
                break
        
        if len(raw_items) >= 100:
            LOG.info(f"Note: Scraped {len(tracks)} tracks. Spotify embed is typically limited to the first 100 tracks.")
        if musicbrainz_album_count:
            LOG.info("Enriched %d Spotify album names via MusicBrainz", musicbrainz_album_count)
                
        source = "spotify_embed_scrape+musicbrainz" if use_musicbrainz else "spotify_embed_scrape"
        return playlist_name, playlist_desc, tracks, source
        
    except Exception as exc:
        LOG.debug("Full JSON data: %s", json.dumps(data)[:1000])
        raise RuntimeError(f"Failed to parse Spotify JSON: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Spotify playlist to YouTube Music crawler (Scraping version)."
    )
    parser.add_argument("--env-file", default=".env", help="dotenv file path")
    parser.add_argument("--spotify-playlist-urls", nargs="+", help="One or more Spotify playlist URLs to merge")
    parser.add_argument("--spotify-track-limit", type=int)
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
    parser.add_argument("--use-musicbrainz", default=None, help="true/false. Enrich missing Spotify album names with MusicBrainz (default: true)")
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

    spotify_urls = args.spotify_playlist_urls or os.environ.get(
        "SPOTIFY_PLAYLIST_URL", DEFAULT_SPOTIFY_PLAYLIST_URL
    ).split(",")
    # Clean up whitespace and empty strings
    spotify_urls = [url.strip() for url in spotify_urls if url.strip()]
    
    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE")
    yt_oauth_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_oauth_client_secret = args.yt_oauth_client_secret or os.environ.get(
        "YTMUSIC_OAUTH_CLIENT_SECRET", ""
    )
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID")
    log_dir = Path(args.log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    job_name = args.job_name or log_dir.name
    playlist_name = args.playlist_name or job_name
    db_path = Path(args.db_path).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    
    # Load previous results to speed up matching AND lazy MusicBrainz
    match_cache = load_match_cache(log_dir)
    
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    spotify_track_limit = os.environ.get("SPOTIFY_TRACK_LIMIT")
    track_limit = int(spotify_track_limit) if spotify_track_limit else args.spotify_track_limit
    use_musicbrainz = parse_bool(
        args.use_musicbrainz if args.use_musicbrainz is not None else os.environ.get("USE_MUSICBRAINZ"),
        default=True,
    )
    
   # Legacy JSON proxy loading is disabled; DB metadata_lookup_index is the proxy cache.
    apple_proxy: dict[str, dict[str, Any]] = {}
    proxy_paths = ""

    if proxy_paths:
        for p_path in proxy_paths.split(","):
            p_path = p_path.strip()
            if not p_path:
                continue
            path = Path(p_path)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        proxy_list = json.load(f)
                        for item in proxy_list:
                            # Index by ALL possible combinations of titles and artists for maximum robustness
                            all_titles = [item.get("title"), item.get("title_ko"), item.get("title_en")]
                            all_artists = [item.get("artist"), item.get("artist_ko"), item.get("artist_en")]
                            
                            for t in all_titles:
                                for a in all_artists:
                                    if not t or not a: continue
                                    t_norm = normalize_text(t)
                                    a_norm = normalize_text(a)
                                    if t_norm and a_norm:
                                        apple_proxy[f"{t_norm}|{a_norm}"] = item
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
                            # 공식 음원('song') 우선순위 및 앨범 점수 반영
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

    all_tracks: list[SourceTrack] = []
    tracks_ko_map: dict[str, SourceTrack] = {}
    seen_spotify_ids: set[str] = set()
    combined_desc_parts = []
    
    for url in spotify_urls:
        LOG.info("Processing Spotify playlist: %s", url)
        try:
            # Fetch English version (market=US)
            p_name, p_desc, tracks, source = fetch_spotify_tracks_scraped(
                url,
                market="US",
                limit=track_limit,
                use_musicbrainz=use_musicbrainz,
                match_cache=match_cache,
            )
            
            # Always add to description parts to maintain order and show name
            desc_text = f"[{p_name}] {p_desc}".strip() if p_desc else f"[{p_name}]"
            combined_desc_parts.append(desc_text)
            
            # Check if we need Korean fallback for any new tracks
            needs_ko_fallback = False
            for t in tracks:
                t_norm = normalize_text(t.title)
                a_norm = normalize_text(t.artist)
                cache_key = f"{t_norm}|{a_norm}"
                
                # If a song is NOT in cache, we need to try getting its Korean metadata
                if cache_key not in match_cache:
                    needs_ko_fallback = True
                    break
            
            if needs_ko_fallback:
                # Fetch Korean fallback version (market=KR)
                try:
                    LOG.info("New tracks detected. Fetching Korean metadata fallback from Spotify (KR)...")
                    _, _, tracks_ko, _ = fetch_spotify_tracks_scraped(
                        url,
                        market="KR",
                        limit=track_limit,
                        use_musicbrainz=use_musicbrainz,
                        match_cache=match_cache,
                    )
                    for t in tracks_ko:
                        if t.song_id:
                            tracks_ko_map[t.song_id] = t
                except Exception as exc:
                    LOG.warning("Failed to fetch Korean fallback tracks for %s: %s", url, exc)
            else:
                LOG.info("All tracks in '%s' are already in cache. Skipping Korean metadata fallback fetch.", p_name)

            # Deduplicate and aggregate
            new_count = 0
            for t in tracks:
                dedupe_key = t.song_id or f"{normalize_text(t.title)}|{normalize_text(t.artist)}"
                if dedupe_key not in seen_spotify_ids:
                    seen_spotify_ids.add(dedupe_key)
                    all_tracks.append(t)
                    new_count += 1
            LOG.info("Added %d new tracks from '%s' (Total: %d)", new_count, p_name, len(all_tracks))
            
        except Exception as exc:
            LOG.error("Failed to scrape Spotify URL %s: %s", url, exc)
            if len(spotify_urls) == 1:
                return 1

    if not all_tracks:
        LOG.error("No tracks collected from any of the provided URLs.")
        return 1

    # Re-rank tracks after aggregation
    for i, t in enumerate(all_tracks, 1):
        t.rank = i

    footer = f"\n\nLast updated: {update_date_str}\n\nAuto-generated by Github Actions.\n- colinky.github.io/hype_wave"
    full_desc = "\n".join(combined_desc_parts)
    if len(spotify_urls) > 1:
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
    LOG.info("MusicBrainz album enrichment: %s", use_musicbrainz)
    ytmusic = make_ytmusic(yt_auth, yt_oauth_client_id, yt_oauth_client_secret)
    matches: list[MatchResult] = []
    seen_video_ids: set[str] = set()
    
    # Matching loop
    for track in all_tracks:
        track_ko = None
        proxy_item = None
        
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
                    service="spotify",
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
                        service="spotify",
                        song_id=track.song_id,
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
            track_ko = tracks_ko_map.get(track.song_id)
            
            # Use Apple proxy data to enrich search
            # Try finding proxy by full title, or parts of title if it contains parentheses
            proxy_item = apple_proxy.get(f"{t_norm}|{a_norm}")
            if not proxy_item and "(" in track.title:
                # Try matching just the part before or inside parentheses
                parts = re.findall(r"([^()]+)", track.title)
                for part in parts:
                    p_norm = normalize_text(part)
                    if p_norm and f"{p_norm}|{a_norm}" in apple_proxy:
                        proxy_item = apple_proxy[f"{p_norm}|{a_norm}"]
                        break

            if proxy_item:
                # Create a synthetic track_ko if we don't have one from Spotify but have one from Apple
                if not track_ko and proxy_item.get("title_ko"):
                    track_ko = SourceTrack(
                        rank=track.rank,
                        title=proxy_item["title_ko"],
                        artist=proxy_item.get("artist_ko", track.artist),
                        album=proxy_item.get("album_ko", track.album),
                        source="apple_proxy"
                    )
                
                # Enrich English metadata if it's better in Apple
                if proxy_item.get("title_en") and similarity(track.title, proxy_item["title_en"]) > 0.8:
                    track.title = proxy_item["title_en"]
                if proxy_item.get("artist_en") and similarity(track.artist, proxy_item["artist_en"]) < 0.9:
                    track.artist = proxy_item["artist_en"]                    
                if not track.album and proxy_item.get("album_en"):
                    track.album = proxy_item["album_en"]
                if proxy_item.get("artwork_url"):
                    track.artwork_url = proxy_item["artwork_url"]

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
                        title_ko=p.get("title_ko", track_ko.title if track_ko else ""),
                        artist_ko=p.get("artist_ko", track_ko.artist if track_ko else ""),
                        album_ko=p.get("album_ko", track_ko.album if track_ko else ""),
                        song_id=track.song_id,
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
        
        # If match failed but we have a proxy video_id, try a direct match as last resort
        if not match.video_id and proxy_item and proxy_item.get("video_id"):
            LOG.info("Match failed for '%s', trying proxy video_id: %s", track.title, proxy_item["video_id"])
            try:
                # We can't easily verify the video_id without a call, but search_youtube_music
                # could be modified to accept a hint. For now, let's just log and maybe 
                # we can trust the proxy if it was recently matched.
                # In this case, we'll just use it if the proxy item had a high score.
                if proxy_item.get("score", 0) >= 0.85:
                    match.video_id = proxy_item["video_id"]
                    match.status = "proxy_matched"
                    match.score = proxy_item.get("score", 0.9)
                    match.yt_title = proxy_item.get("yt_title", "")
                    match.yt_artist = proxy_item.get("yt_artist", "")
            except Exception:
                pass

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
                service="spotify",
                job_name=job_name,
                chart_date=update_date_str,
                started_at=started_at,
                tracks=all_tracks,
                matches=matches,
            )
            export_frontend_history(db_path, args.history_json)
        except Exception as exc:
            LOG.warning("Failed to persist Spotify run to DB: %s", exc)

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
        service="spotify",
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
