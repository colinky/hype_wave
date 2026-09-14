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
    - Uses exact Spotify page/embed album metadata without substituting other catalogs.
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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ytmusic_playlist_sync import (
    SourceTrack,
    env_or_arg,
    load_dotenv,
    make_ytmusic,
    artist_variants,
    normalize_text,
    similarity,
    update_ytmusic_playlist,
    get_resilient_session,
)
from crawler_common import process_matching_pipeline
from hype_db_common import dedupe_source_tracks

http_session = get_resilient_session()


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
        if text and not text.startswith(("spotify:", "https://", "http://")):
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
        if text and not text.startswith(("spotify:", "https://", "http://")):
            return text

    nested_keys = ("album", "albumOfTrack", "release", "releaseOfTrack")
    for key in nested_keys:
        text = first_text_from_mapping(value.get(key))
        if text and not text.startswith(("spotify:", "https://", "http://")):
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


def _spotify_resource_id(value: Any, kind: str = "track") -> str:
    raw = text_field(value)
    if re.fullmatch(r"[A-Za-z0-9]{22}", raw):
        return raw
    if raw.startswith(f"spotify:{kind}:"):
        candidate = raw[len(f"spotify:{kind}:"):]
        return candidate if re.fullmatch(r"[A-Za-z0-9]{22}", candidate) else ""
    url = urlsplit(raw)
    if url.scheme != "https" or url.netloc != "open.spotify.com":
        return ""
    match = re.fullmatch(rf"/(?:intl-[^/]+/)?(?:embed/)?{kind}/([A-Za-z0-9]{{22}})/?", url.path)
    return match.group(1) if match else ""


def mapping_has_track_id(value: dict[str, Any], track_id: str) -> bool:
    identities = {_spotify_resource_id(value.get(key)) for key in ("id", "uri", "gid", "shareUrl", "url", "@id")}
    identities.discard("")
    return bool(track_id) and identities == {track_id}


def album_name_from_page_data(item: dict[str, Any], data: dict[str, Any], track_id: str) -> str:
    if not track_id or _spotify_resource_id(track_id) != track_id:
        return ""
    for mapping in iter_mappings([item, data]):
        if mapping_has_track_id(mapping, track_id):
            album_name = candidate_album_name(mapping)
            if album_name:
                return album_name
    return ""


def official_spotify_album_from_html(page_html: str, track_id: str) -> str:
    """Accept only metadata attached to the requested Spotify track identity."""
    if not track_id or _spotify_resource_id(track_id) != track_id:
        return ""
    soup = BeautifulSoup(page_html, "html.parser")
    meta = {tag.get("property") or tag.get("name"): tag.get("content", "") for tag in soup.find_all("meta")}
    if meta.get("og:url") and _spotify_resource_id(meta["og:url"]) != track_id:
        return ""
    for script in soup.find_all("script", type=("application/json", "application/ld+json")):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        album = album_name_from_page_data({}, data, track_id)
        if album:
            return album
    # Spotify's exact track page exposes the album title in its Open Graph
    # description and its identity in music:album; unrelated page cards do not.
    if _spotify_resource_id(meta.get("og:url")) != track_id or not _spotify_resource_id(meta.get("music:album"), "album"):
        return ""
    parts = meta.get("og:description", "").rsplit(" · ", 2)
    if len(parts) != 3 or parts[1] != "Song" or not re.fullmatch(r"\d{4}", parts[2]):
        return ""
    credits = parts[0].split(" · ", 1)
    return credits[1].strip() if len(credits) == 2 else ""


def fetch_official_spotify_album(track_id: str, *, album_cache: dict[str, str] | None = None) -> str:
    """Read a public exact-track page; the cache belongs to this collection run."""
    if not track_id or _spotify_resource_id(track_id) != track_id:
        return ""
    if album_cache is not None and track_id in album_cache:
        return album_cache[track_id]
    album = ""
    try:
        response = http_session.get(f"https://open.spotify.com/track/{track_id}",
                                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
        response.raise_for_status()
        if _spotify_resource_id(response.url) == track_id:
            album = official_spotify_album_from_html(response.content.decode("utf-8", errors="replace"), track_id)
    except Exception as exc:
        LOG.warning("Official Spotify album unavailable for %s (%s)", track_id, type(exc).__name__)
    if album_cache is not None:
        album_cache[track_id] = album
    return album


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
            response = http_session.get(
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
    use_musicbrainz: bool = False,
    match_cache: dict[str, dict[str, Any]] | None = None,
    official_album_cache: dict[str, str] | None = None,
) -> tuple[str, str, list[SourceTrack], str]:
    if official_album_cache is None:
        official_album_cache = {}
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
    
    response = http_session.get(embed_url, headers=headers, params=params, timeout=30)
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
        if use_musicbrainz:
            LOG.warning("MusicBrainz album enrichment is disabled for Spotify source identity; only official albums are used.")
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
            if not album_name:
                album_name = fetch_official_spotify_album(track_id, album_cache=official_album_cache)
            else:
                official_album_cache[track_id] = album_name
            
            tracks.append(
                SourceTrack(
                    rank=len(tracks) + 1,
                    title=title,
                    artist=artist_str,
                    service="spotify",
                    album=album_name,
                    song_id=track_id,
                    source="spotify_embed_scrape",
                    locale="ko" if market.upper() == "KR" else "en",
                )
            )
            if limit and len(tracks) >= limit:
                break
        
        if len(raw_items) >= 100:
            LOG.info(f"Note: Scraped {len(tracks)} tracks. Spotify embed is typically limited to the first 100 tracks.")
        source = "spotify_embed_scrape"
        return playlist_name, playlist_desc, tracks, source
        
    except Exception as exc:
        LOG.debug("Full JSON data: %s", json.dumps(data)[:1000])
        raise RuntimeError(f"Failed to parse Spotify JSON: {exc}")


def load_targeted_spotify_cache(
    db_path: Path,
    tracks: list[SourceTrack],
    *,
    no_db_cache: bool,
) -> dict[str, dict[str, Any]]:
    if no_db_cache or not tracks:
        return {}
    if not db_path.exists() and not os.environ.get("SUPABASE_DB_URL"):
        return {}
    try:
        from hype_db import connect, get_bulk_cached_matches

        # Album enrichment is read-only; exact video refresh belongs to the
        # shared matching pipeline once the YouTube client is available.
        with connect(db_path, read_only=True) as conn:
            return get_bulk_cached_matches(conn, service="spotify", tracks=tracks, read_only=True)
    except Exception as exc:
        LOG.warning("Failed to load targeted Spotify cache: %s", exc)
        return {}


def apply_spotify_album_cache(
    tracks: list[SourceTrack],
    cache_by_song_id: dict[str, dict[str, Any]],
) -> int:
    """Compatibility no-op: cached YouTube albums are not Spotify source data."""
    return 0


def enrich_spotify_albums_with_musicbrainz(
    tracks: list[SourceTrack],
    *,
    use_musicbrainz: bool,
) -> int:
    """Compatibility no-op: a third-party release is not exact source evidence."""
    if use_musicbrainz:
        LOG.warning("MusicBrainz album enrichment is disabled for Spotify source identity; only official albums are used.")
    return 0


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
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--no-db-cache", action="store_true")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--min-title-score", type=float, default=DEFAULT_MIN_TITLE_SCORE)
    parser.add_argument("--min-artist-score", type=float, default=DEFAULT_MIN_ARTIST_SCORE)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--use-musicbrainz", default=None, help="Deprecated compatibility option; source albums now require official Spotify metadata")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks before saving them to the YouTube Music playlist")
    parser.add_argument("--defer-publish", action="store_true", help="Match, validate, and persist tracks without updating the target playlist")
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
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID", required=not args.defer_publish)
    job_name = args.job_name or "spotify"
    playlist_name = args.playlist_name or job_name
    db_path = Path(args.db_path).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(db_path)
    
    min_score = float(os.environ.get("MATCH_MIN_SCORE", args.min_score))
    min_title_score = float(os.environ.get("MATCH_MIN_TITLE_SCORE", args.min_title_score))
    min_artist_score = float(os.environ.get("MATCH_MIN_ARTIST_SCORE", args.min_artist_score))
    search_limit = int(os.environ.get("SEARCH_LIMIT", args.search_limit))
    spotify_track_limit = os.environ.get("SPOTIFY_TRACK_LIMIT")
    track_limit = int(spotify_track_limit) if spotify_track_limit else args.spotify_track_limit
    use_musicbrainz = parse_bool(
        args.use_musicbrainz if args.use_musicbrainz is not None else os.environ.get("USE_MUSICBRAINZ"),
        default=False,
    )
    if use_musicbrainz:
        LOG.warning("--use-musicbrainz is retained for compatibility but cannot replace official Spotify source albums.")
    started_at = os.environ.get("HYPE_MATCH_STARTED_AT") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    update_date_str = kst_now.strftime("%Y-%m-%d")

    raw_tracks: list[SourceTrack] = []
    tracks_ko_map: dict[str, SourceTrack] = {}
    combined_desc_parts = []
    official_album_cache: dict[str, str] = {}
    
    max_retries = 3
    for url in spotify_urls:
        LOG.info("Processing Spotify playlist: %s", url)
        for attempt in range(1, max_retries + 1):
            try:
                # Fetch English version (market=US)
                p_name, p_desc, tracks, source = fetch_spotify_tracks_scraped(
                    url,
                    market="US",
                    limit=track_limit,
                    use_musicbrainz=False,
                    official_album_cache=official_album_cache,
                )
                
                # Check validation if there's an expected count (based on job_name)
                from hype_db import get_expected_track_count
                expected = get_expected_track_count(job_name)
                if expected is not None and len(tracks) != expected:
                    if os.environ.get("BYPASS_TRACK_COUNT_VAL") == "true":
                        LOG.warning(
                            "Track count validation bypassed. Scraped %d tracks, expected %d.",
                            len(tracks), expected
                        )
                    else:
                        raise ValueError(
                            f"Validation Error: Scraped {len(tracks)} tracks, "
                            f"but expected exactly {expected} tracks."
                        )
                
                # Always add to description parts to maintain order and show name
                desc_text = f"[{p_name}] {p_desc}".strip() if p_desc else f"[{p_name}]"
                combined_desc_parts.append(desc_text)

                # Source locales must be refreshed even when every match is cached.
                # The shared official album cache avoids duplicate exact-track reads.
                try:
                    LOG.info("Fetching current Korean Spotify source metadata...")
                    _, _, tracks_ko, _ = fetch_spotify_tracks_scraped(
                        url,
                        market="KR",
                        limit=track_limit,
                        use_musicbrainz=False,
                        official_album_cache=official_album_cache,
                    )
                    for track_ko in tracks_ko:
                        if track_ko.song_id:
                            tracks_ko_map[track_ko.song_id] = track_ko
                except Exception as exc:
                    # Korean metadata remains optional; never invent a fresh KO
                    # observation from cached display metadata after a read failure.
                    LOG.warning("Korean Spotify source refresh failed for %s; only observed locales will update: %s", url, exc)

                raw_tracks.extend(tracks)
                LOG.info("Added %d raw tracks from '%s' (Raw total: %d)", len(tracks), p_name, len(raw_tracks))
                break  # Success, exit retry loop
            except Exception as exc:
                LOG.error("Attempt %d failed to scrape/validate Spotify URL %s: %s", attempt, url, exc)
                if attempt == max_retries:
                    if len(spotify_urls) == 1:
                        return 1
                else:
                    time.sleep(2)

    if not raw_tracks:
        LOG.error("No tracks collected from any of the provided URLs.")
        return 1

    # Raw slots retain every source occurrence; matching uses the first source identity only.
    for i, t in enumerate(raw_tracks, 1):
        t.rank = i
    all_tracks = dedupe_source_tracks(raw_tracks, "spotify")
    LOG.info("Prepared %d raw slots and %d effective Spotify tracks.", len(raw_tracks), len(all_tracks))

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
    LOG.info("Spotify source albums use exact official Spotify metadata only.")
    ytmusic = make_ytmusic(yt_auth, yt_oauth_client_id, yt_oauth_client_secret)
    matched_video_ids = process_matching_pipeline(
        all_tracks=all_tracks,
        raw_tracks=raw_tracks,
        tracks_ko_map=tracks_ko_map,
        ytmusic=ytmusic,
        db_path=db_path,
        service="spotify",
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

    if args.defer_publish:
        LOG.info("Publication deferred; matching, validation, and persistence completed for %s.", job_name)
        return 0

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
    from sync_validation import run_locked_cli
    try:
        raise SystemExit(run_locked_cli(main))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
