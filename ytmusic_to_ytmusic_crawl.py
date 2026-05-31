#!/usr/bin/env python3
"""
YouTube Music chart/playlist 항목을 YouTube Music 플레이리스트로 동기화합니다.

핵심 기능:
- YouTube Music playlist 항목 수집
- live/MV/clip/video 항목을 가능한 경우 canonical song으로 보수적으로 resolve
- resolve 실패 시 원본 video를 유지하고 audit에 남김
- 수동 override 없이 YouTube Music 내부 metadata/watch/search 로직으로만 resolve
- GitHub Actions 안정성을 위해 multiprocessing 없이 순차 처리
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ytmusic_playlist_sync import (
    duration_to_seconds,
    env_or_arg,
    load_dotenv,
    make_ytmusic,
    normalize_video_title,
    resolve_video_to_song,
    update_ytmusic_playlist,
    write_json,
    ytmusic_url,
)

LOG = logging.getLogger("ytmusic_to_ytmusic_crawl")
DEFAULT_PLAYLIST_URL = "https://music.youtube.com/playlist?list=PL4fGSI1pDJn6jXS_Tv_N9B8Z0HTRVJE0m"
DEFAULT_YOUTUBE_CHARTS_URL = "https://charts.youtube.com/charts/TopSongs/kr/weekly"
ACCEPTED_MAPPING_STATUSES = {"resolved_to_song", "already_song", "cached_match", "proxy_matched", "manual_override", "kept_original_video"}


def playlist_id_from_url(value: str) -> str:
    if "list=" in value:
        return value.split("list=", 1)[1].split("&", 1)[0]
    return value


def video_id_from_url(value: str) -> str:
    parsed = urlparse(value or "")
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    match = re.search(r"(?:youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", value or "")
    return match.group(1) if match else ""


def compact_date(value: str | None) -> str:
    value = str(value or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}$", value):
        return value.replace("-", "")
    if re.match(r"\d{8}$", value):
        return value
    return ""


def youtube_charts_period_url(base_url: str, start: str | None, end: str | None) -> str:
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    # YouTube Charts does not serve historical weekly pages at
    # /YYYYMMDD-YYYYMMDD reliably. Always load the normal chart page and pass
    # the requested end date only to the internal browse API query.
    path = re.sub(r"/\d{8}-\d{8}$", "/weekly", path)
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def youtube_charts_browse_query(chart_period_end: str | None) -> str:
    query = {
        "perspective": "CHART_DETAILS",
        "chart_params_country_code": "kr",
        "chart_params_chart_type": "TRACKS",
        "chart_params_period_type": "WEEKLY",
    }
    end_date = compact_date(chart_period_end)
    if end_date:
        query["chart_params_end_date"] = end_date
    return urlencode(query)


def youtube_charts_source_info(
    page_url: str,
    chart_period_end: str | None,
    *,
    csv_url_candidates: list[str] | None = None,
) -> dict[str, Any]:
    csv_url_candidates = csv_url_candidates or []
    return {
        "page_url": page_url,
        "csv_url": csv_url_candidates[0] if csv_url_candidates else "",
        "csv_url_candidates": csv_url_candidates,
        "csv_delivery": "static_csv_url" if csv_url_candidates else "client_generated_blob",
        "browse_api_endpoint": "https://charts.youtube.com/youtubei/v1/browse?prettyPrint=false",
        "browse_api_path": "/youtubei/v1/browse?prettyPrint=false",
        "browse_api_browse_id": "FEmusic_analytics_charts_home",
        "browse_api_query": youtube_charts_browse_query(chart_period_end),
        "note": (
            "YouTube Charts download button generates a CSV Blob in browser JS from browse API data; "
            "no static CSV URL is exposed when csv_url is empty."
        ),
    }


def discover_youtube_charts_source_info(
    url: str,
    *,
    chart_period_start: str | None = None,
    chart_period_end: str | None = None,
) -> dict[str, Any]:
    fetch_url = youtube_charts_period_url(url, chart_period_start, chart_period_end)
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"}
    candidates: list[str] = []
    error = ""
    try:
        response = requests.get(fetch_url, headers=headers, timeout=30)
        response.raise_for_status()
        candidates = find_csv_urls(response.text, fetch_url)
    except Exception as exc:
        error = str(exc)
    info = youtube_charts_source_info(fetch_url, chart_period_end, csv_url_candidates=candidates)
    if error:
        info["discovery_error"] = error
    return info


def infer_chart_period_end_from_path(path: str | Path | None) -> str:
    if not path:
        return ""
    match = re.search(r"(\d{8})", Path(path).name)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def default_week_start(chart_period_end: str | None) -> str:
    end = compact_date(chart_period_end)
    if not end:
        return ""
    try:
        end_dt = datetime.strptime(end, "%Y%m%d")
    except ValueError:
        return ""
    return (end_dt - timedelta(days=6)).strftime("%Y-%m-%d")


def extract_chart_entries_from_csv(path: str | Path, *, limit: int = 100) -> list[dict[str, Any]]:
    return extract_chart_entries_from_csv_text(Path(path).expanduser().read_text(encoding="utf-8-sig"), limit=limit)


def extract_chart_entries_from_csv_text(csv_text: str, *, limit: int = 100) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in csv.DictReader(csv_text.splitlines()):
        video_id = video_id_from_url(row.get("YouTube URL", "") or row.get("URL", "") or row.get("Video URL", ""))
        if not video_id:
            continue
        entries.append(
            {
                "rank": int(row.get("Rank") or len(entries) + 1),
                "video_id": video_id,
                "title": row.get("Track Name", "") or row.get("Title", ""),
                "artist": row.get("Artist Names", "") or row.get("Artist", "") or row.get("Artists", ""),
                "source": "youtube_charts_weekly_csv",
                "views": row.get("Views", ""),
                "previous_rank": row.get("Previous Rank", ""),
            }
        )
        if len(entries) >= limit:
            break
    return entries


def dashed_date(value: str | None) -> str:
    compact = compact_date(value)
    if not compact:
        return ""
    try:
        return datetime.strptime(compact, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def compare_chart_entries(reference: list[dict[str, Any]], crawled: list[dict[str, Any]]) -> dict[str, Any]:
    crawled_by_rank = {int(row.get("rank") or 0): row for row in crawled}
    rank_video_mismatches = []
    title_mismatches = []
    artist_mismatches = []
    missing_ranks = []
    for ref in reference:
        rank = int(ref.get("rank") or 0)
        got = crawled_by_rank.get(rank)
        if not got:
            missing_ranks.append(rank)
            continue
        if str(ref.get("video_id") or "") != str(got.get("video_id") or ""):
            rank_video_mismatches.append(
                {
                    "rank": rank,
                    "reference_video_id": ref.get("video_id", ""),
                    "crawled_video_id": got.get("video_id", ""),
                    "reference_title": ref.get("title", ""),
                    "crawled_title": got.get("title", ""),
                }
            )
        if str(ref.get("title") or "").casefold() != str(got.get("title") or "").casefold():
            title_mismatches.append(
                {
                    "rank": rank,
                    "reference_title": ref.get("title", ""),
                    "crawled_title": got.get("title", ""),
                }
            )
        if str(ref.get("artist") or "").casefold() != str(got.get("artist") or "").casefold():
            artist_mismatches.append(
                {
                    "rank": rank,
                    "reference_artist": ref.get("artist", ""),
                    "crawled_artist": got.get("artist", ""),
                }
            )
    return {
        "reference_count": len(reference),
        "crawled_count": len(crawled),
        "missing_rank_count": len(missing_ranks),
        "rank_video_mismatch_count": len(rank_video_mismatches),
        "title_mismatch_count": len(title_mismatches),
        "artist_mismatch_count": len(artist_mismatches),
        "missing_ranks": missing_ranks,
        "rank_video_mismatches": rank_video_mismatches,
        "title_mismatches": title_mismatches,
        "artist_mismatches": artist_mismatches,
    }


def looks_like_youtube_charts_csv(text: str) -> bool:
    header = text.splitlines()[0] if text.splitlines() else ""
    return "Rank" in header and "Track Name" in header and "YouTube URL" in header


def find_csv_urls(html: str, page_url: str) -> list[str]:
    urls: list[str] = []
    patterns = [
        r'https?://[^"\']+?\.csv(?:\?[^"\']*)?',
        r'["\'](?P<url>/[^"\']*?(?:csv|download)[^"\']*)["\']',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            raw = match.groupdict().get("url") or match.group(0).strip("\"'")
            raw = raw.replace("\\u0026", "&").replace("\\/", "/")
            if "csv" not in raw.lower() and "download" not in raw.lower():
                continue
            full = urljoin(page_url, raw)
            if full not in urls:
                urls.append(full)
    return urls


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_json_objects_from_html(html: str) -> list[Any]:
    objects: list[Any] = []
    for pattern in (
        r"ytInitialData\s*=\s*(\{.*?\})\s*;",
        r"AF_initDataCallback\((\{.*?\})\);",
        r"window\.__data\s*=\s*(\{.*?\})\s*;",
    ):
        for match in re.finditer(pattern, html, flags=re.DOTALL):
            try:
                objects.append(json.loads(match.group(1)))
            except Exception:
                continue
    return objects


def extract_ytcfg_from_html(html: str) -> dict[str, Any]:
    match = re.search(r"ytcfg\.set\((\{.*?\})\);", html, flags=re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except Exception:
        return {}


def extract_youtube_charts_period(data: dict[str, Any]) -> tuple[str, str]:
    request_end = ""
    latest_end = ""
    for node in walk_json(data):
        if not isinstance(node, dict):
            continue
        chart_params = node.get("chartParams")
        if isinstance(chart_params, dict):
            if chart_params.get("chartType") == "CHART_TYPE_TRACKS" and chart_params.get("chartPeriodType") == "CHART_PERIOD_TYPE_WEEKLY":
                request_end = dashed_date(chart_params.get("endDate")) or request_end
        available = node.get("availableChartsInfo")
        if isinstance(available, list):
            for info in available:
                if not isinstance(info, dict):
                    continue
                if info.get("chartType") == "CHART_TYPE_TRACKS" and info.get("chartPeriodType") == "CHART_PERIOD_TYPE_WEEKLY":
                    latest_end = dashed_date(info.get("latestEndDate")) or latest_end
    end = request_end or latest_end
    return default_week_start(end), end


def best_thumbnail_url(item: dict[str, Any]) -> str:
    thumbnail = item.get("thumbnail") if isinstance(item, dict) else None
    thumbnails = thumbnail.get("thumbnails") if isinstance(thumbnail, dict) else None
    if not isinstance(thumbnails, list):
        return ""
    candidates = [row for row in thumbnails if isinstance(row, dict) and row.get("url")]
    if not candidates:
        return ""
    best = max(candidates, key=lambda row: int(row.get("width") or 0) * int(row.get("height") or 0))
    return str(best.get("url") or "")


def extract_chart_entries_from_browse_json(
    data: dict[str, Any],
    *,
    limit: int = 100,
    locale: str = "",
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    chart_period_start, chart_period_end = extract_youtube_charts_period(data)
    for node in walk_json(data):
        track_views = node.get("trackViews") if isinstance(node, dict) else None
        if not isinstance(track_views, list):
            continue
        for item in track_views:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("encryptedVideoId") or item.get("videoId") or "")
            if not video_id or video_id in seen:
                continue
            artists = item.get("artists") or []
            artist = ", ".join(
                str(artist_row.get("name") or "")
                for artist_row in artists
                if isinstance(artist_row, dict) and artist_row.get("name")
            )
            metadata = item.get("chartEntryMetadata") or {}
            rank = metadata.get("currentPosition") if isinstance(metadata, dict) else None
            previous_rank = metadata.get("previousPosition") if isinstance(metadata, dict) else ""
            seen.add(video_id)
            entries.append(
                {
                    "rank": int(rank or len(entries) + 1),
                    "video_id": video_id,
                    "song_id": video_id,
                    "atv_external_video_id": str(item.get("atvExternalVideoId") or ""),
                    "title": str(item.get("name") or ""),
                    "artist": artist,
                    "album": "",
                    "artwork_url": best_thumbnail_url(item),
                    "source": "youtube_charts_weekly_browse_api",
                    "views": str(item.get("viewCount") or ""),
                    "previous_rank": previous_rank,
                    "chart_period_start": chart_period_start,
                    "chart_period_end": chart_period_end,
                    "locale": locale,
                }
            )
            if len(entries) >= limit:
                return sorted(entries, key=lambda row: int(row.get("rank") or 0))
    return sorted(entries, key=lambda row: int(row.get("rank") or 0))


def merge_localized_chart_entries(primary: list[dict[str, Any]], localized: list[dict[str, Any]], locale: str) -> list[dict[str, Any]]:
    localized_by_video = {row.get("video_id"): row for row in localized if row.get("video_id")}
    localized_by_alt = {row.get("atv_external_video_id"): row for row in localized if row.get("atv_external_video_id")}
    localized_by_rank = {row.get("rank"): row for row in localized if row.get("rank")}
    merged = []
    for row in primary:
        out = dict(row)
        match = (
            localized_by_video.get(row.get("video_id"))
            or localized_by_alt.get(row.get("atv_external_video_id"))
            or localized_by_rank.get(row.get("rank"))
        )
        if match:
            out[f"title_{locale}"] = match.get("title", "")
            out[f"artist_{locale}"] = match.get("artist", "")
            out[f"album_{locale}"] = match.get("album", "")
            out["artwork_url"] = out.get("artwork_url") or match.get("artwork_url", "")
        else:
            out.setdefault(f"title_{locale}", "")
            out.setdefault(f"artist_{locale}", "")
            out.setdefault(f"album_{locale}", "")
        merged.append(out)
    return merged


def extract_chart_entries_from_browse_api(
    html: str,
    page_url: str,
    *,
    limit: int = 100,
    chart_period_end: str | None = None,
) -> list[dict[str, Any]]:
    config = extract_ytcfg_from_html(html)
    context = config.get("INNERTUBE_CONTEXT")
    if not isinstance(context, dict):
        return []
    end_date = compact_date(chart_period_end)
    if not end_date:
        end_date = ""
    query = youtube_charts_browse_query(end_date)
    locale_rows: dict[str, list[dict[str, Any]]] = {}
    for locale, accept_language in (
        ("en", "en-US,en;q=0.9,ko;q=0.8"),
        ("ko", "ko-KR,ko;q=0.9,en-US;q=0.8"),
    ):
        localized_context = copy.deepcopy(context)
        localized_context.setdefault("client", {})["hl"] = locale
        localized_context.setdefault("client", {})["gl"] = "KR"
        response = requests.post(
            "https://charts.youtube.com/youtubei/v1/browse?prettyPrint=false",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": accept_language,
                "Content-Type": "application/json",
                "Origin": "https://charts.youtube.com",
                "Referer": page_url,
            },
            json={"context": localized_context, "browseId": "FEmusic_analytics_charts_home", "query": query},
            timeout=30,
        )
        response.raise_for_status()
        locale_rows[locale] = extract_chart_entries_from_browse_json(response.json(), limit=limit, locale=locale)
    primary = locale_rows.get("en") or locale_rows.get("ko") or []
    merged = merge_localized_chart_entries(primary, locale_rows.get("en", []), "en")
    merged = merge_localized_chart_entries(merged, locale_rows.get("ko", []), "ko")
    for row in merged:
        row["title"] = row.get("title_en") or row.get("title") or row.get("title_ko") or ""
        row["artist"] = row.get("artist_en") or row.get("artist") or row.get("artist_ko") or ""
        row["album"] = row.get("album_en") or row.get("album") or row.get("album_ko") or ""
    return merged


def text_from_runs(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "simpleText" in value:
            return str(value.get("simpleText") or "")
        if "runs" in value and isinstance(value["runs"], list):
            return " ".join(str(run.get("text") or "") for run in value["runs"] if isinstance(run, dict)).strip()
    return ""


def extract_chart_entries_from_embedded_json(html: str, *, limit: int = 100) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obj in extract_json_objects_from_html(html):
        for node in walk_json(obj):
            video_id = str(node.get("videoId") or node.get("video_id") or "")
            if not video_id or video_id in seen:
                continue
            title = text_from_runs(node.get("title")) or str(node.get("title") or "")
            artists: list[str] = []
            for key in ("subtitle", "secondaryText", "shortBylineText", "longBylineText"):
                text = text_from_runs(node.get(key))
                if text:
                    artists.append(text)
            artist = artists[0] if artists else str(node.get("artist") or node.get("artists") or "")
            if not title:
                continue
            seen.add(video_id)
            entries.append(
                {
                    "rank": len(entries) + 1,
                    "video_id": video_id,
                    "title": title,
                    "artist": artist,
                    "source": "youtube_charts_weekly_embedded_json",
                }
            )
            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break
    return entries


def extract_chart_entries_from_youtube_charts(
    url: str,
    *,
    limit: int = 100,
    chart_period_start: str | None = None,
    chart_period_end: str | None = None,
) -> list[dict[str, Any]]:
    """charts.youtube.com의 정적 HTML/내장 JSON에서 가능한 chart metadata를 best-effort로 추출합니다."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"}
    fetch_url = youtube_charts_period_url(url, chart_period_start, chart_period_end)
    try:
        response = requests.get(fetch_url, headers=headers, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        LOG.warning("Failed to fetch YouTube Charts page: %s", exc)
        return []
    html = response.text
    if looks_like_youtube_charts_csv(html):
        return extract_chart_entries_from_csv_text(html, limit=limit)

    for csv_url in find_csv_urls(html, fetch_url):
        try:
            csv_response = requests.get(csv_url, headers=headers, timeout=30)
            csv_response.raise_for_status()
        except Exception as exc:
            LOG.debug("Failed to fetch candidate CSV %s: %s", csv_url, exc)
            continue
        if looks_like_youtube_charts_csv(csv_response.text):
            entries = extract_chart_entries_from_csv_text(csv_response.text, limit=limit)
            if entries:
                LOG.info("Fetched YouTube Charts CSV from %s", csv_url)
                return entries

    try:
        browse_entries = extract_chart_entries_from_browse_api(
            html,
            fetch_url,
            limit=limit,
            chart_period_end=chart_period_end,
        )
        if browse_entries:
            return browse_entries
    except Exception as exc:
        LOG.debug("Failed to fetch YouTube Charts browse API: %s", exc)

    embedded = extract_chart_entries_from_embedded_json(html, limit=limit)
    if embedded:
        return embedded

    entries: list[dict[str, Any]] = []
    # Best-effort: videoId/title/artist가 JSON blob에 노출되는 경우를 처리
    for match in re.finditer(r'"videoId"\s*:\s*"(?P<video_id>[^"]+)"', html):
        start = max(0, match.start() - 800)
        end = min(len(html), match.end() + 1200)
        blob = html[start:end]
        title_match = re.search(r'"title"\s*:\s*"(?P<title>(?:\\.|[^"])*)"', blob)
        artist_match = re.search(r'"name"\s*:\s*"(?P<artist>(?:\\.|[^"])*)"', blob)
        video_id = match.group("video_id")
        if not video_id or any(e["video_id"] == video_id for e in entries):
            continue
        entries.append({
            "rank": len(entries) + 1,
            "video_id": video_id,
            "title": json.loads(f'"{title_match.group("title")}"') if title_match else "",
            "artist": json.loads(f'"{artist_match.group("artist")}"') if artist_match else "",
            "source": "youtube_charts_weekly",
        })
        if len(entries) >= limit:
            break
    if entries:
        return entries

    # HTML fallback
    soup = BeautifulSoup(html, "html.parser")
    for idx, a in enumerate(soup.select('a[href*="watch?v="]')[:limit], 1):
        href = a.get("href", "")
        m = re.search(r"v=([A-Za-z0-9_-]{6,})", href)
        if not m:
            continue
        entries.append({"rank": idx, "video_id": m.group(1), "title": a.get_text(" ", strip=True), "artist": "", "source": "youtube_charts_weekly"})
    return entries


def fetch_ytmusic_playlist_entries(ytmusic, playlist_url_or_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    playlist_id = playlist_id_from_url(playlist_url_or_id)
    playlist = ytmusic.get_playlist(playlist_id, limit=limit)
    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(playlist.get("tracks", [])[:limit], 1):
        artists = item.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
        entries.append({
            "source": "youtube_music_playlist",
            "rank": idx,
            "original_video_id": item.get("videoId", ""),
            "original_url": ytmusic_url(item.get("videoId")),
            "original_title": item.get("title", ""),
            "original_artist_or_channel": artist or item.get("author", ""),
            "duration_seconds_original": duration_to_seconds(item.get("duration")),
            "album": (item.get("album") or {}).get("name", "") if isinstance(item.get("album"), dict) else "",
        })
    return entries


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YouTube Music chart playlist to YouTube Music playlist sync")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--yt-auth")
    p.add_argument("--yt-oauth-client-id")
    p.add_argument("--yt-oauth-client-secret")
    p.add_argument("--yt-playlist-id")
    p.add_argument("--db-path", default="hype_wave_data.db")
    p.add_argument("--history-json", default="docs/api/history.json")
    p.add_argument("--source-playlist-url", default=DEFAULT_PLAYLIST_URL)
    p.add_argument("--youtube-charts-url", default=DEFAULT_YOUTUBE_CHARTS_URL)
    p.add_argument("--youtube-charts-csv")
    p.add_argument("--compare-csv", help="Compare crawled YouTube Charts rows with a downloaded CSV file")
    p.add_argument("--source-report-json", help="Write discovered chart source path/API metadata and comparison report")
    p.add_argument("--chart-period-start")
    p.add_argument("--chart-period-end")
    p.add_argument("--prefer-youtube-charts", action="store_true", default=True)
    p.add_argument("--use-source-playlist", action="store_true", help="Use --source-playlist-url instead of YouTube Charts webpage")
    p.add_argument("--job-name", default="Weekly-Hot-100")
    p.add_argument("--playlist-name", default="YouTube Music Weekly Hot 100")
    p.add_argument("--track-limit", type=int, default=100)
    p.add_argument("--search-limit", type=int, default=10)
    p.add_argument("--resolve-threshold", type=float, default=0.86)
    p.add_argument("--db-only", action="store_true", help="Persist DB/export only and skip YouTube Music playlist updates")
    p.add_argument("--no-resolve", action="store_true", help="Import chart rows without resolving chart videos through YTMusic search")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    use_youtube_charts = bool(args.prefer_youtube_charts and not args.use_source_playlist)
    inferred_end = infer_chart_period_end_from_path(args.youtube_charts_csv)
    chart_period_end = args.chart_period_end or inferred_end
    chart_period_start = args.chart_period_start or default_week_start(chart_period_end)
    needs_ytmusic_for_source = not args.youtube_charts_csv and not use_youtube_charts
    # Even when --db-only, we need ytmusic client to resolve Live/MV → song type
    # (unless --no-resolve is set). Without it, every cache-miss creates a split unmatched track_uid.
    needs_ytmusic = needs_ytmusic_for_source or not args.db_only or not args.no_resolve
    yt_auth = env_or_arg(args.yt_auth, "YTMUSIC_AUTH_FILE", required=needs_ytmusic)
    yt_playlist_id = env_or_arg(args.yt_playlist_id, "YTMUSIC_PLAYLIST_ID", required=not args.db_only)
    yt_client_id = args.yt_oauth_client_id or os.environ.get("YTMUSIC_OAUTH_CLIENT_ID", "")
    yt_client_secret = args.yt_oauth_client_secret or os.environ.get("YTMUSIC_OAUTH_CLIENT_SECRET", "")
    ytmusic = make_ytmusic(yt_auth, yt_client_id, yt_client_secret, language="ko") if needs_ytmusic else None

    if args.youtube_charts_csv:
        chart_entries = extract_chart_entries_from_csv(args.youtube_charts_csv, limit=args.track_limit)
    else:
        chart_entries = (
            extract_chart_entries_from_youtube_charts(
                args.youtube_charts_url,
                limit=args.track_limit,
                chart_period_start=chart_period_start,
                chart_period_end=chart_period_end,
            )
            if use_youtube_charts
            else []
        )
    if chart_entries:
        LOG.info("Fetched %d YouTube Charts entries for audit/reference", len(chart_entries))
        if not chart_period_end:
            chart_period_end = str(chart_entries[0].get("chart_period_end") or "")
        if not chart_period_start:
            chart_period_start = str(chart_entries[0].get("chart_period_start") or default_week_start(chart_period_end))
    if not chart_period_end:
        chart_period_end = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    if not chart_period_start:
        chart_period_start = default_week_start(chart_period_end)

    source_report: dict[str, Any] = {}
    if use_youtube_charts:
        source_report = discover_youtube_charts_source_info(
            args.youtube_charts_url,
            chart_period_start=chart_period_start,
            chart_period_end=chart_period_end,
        )
        if args.compare_csv:
            comparison = compare_chart_entries(
                extract_chart_entries_from_csv(args.compare_csv, limit=args.track_limit),
                chart_entries,
            )
            source_report["comparison"] = comparison
            LOG.info(
                "CSV comparison: reference=%d crawled=%d rank_video_mismatches=%d title_mismatches=%d artist_mismatches=%d",
                comparison["reference_count"],
                comparison["crawled_count"],
                comparison["rank_video_mismatch_count"],
                comparison["title_mismatch_count"],
                comparison["artist_mismatch_count"],
            )
        if args.source_report_json:
            report_path = Path(args.source_report_json).expanduser()
            write_json(report_path, source_report)
            LOG.info("Wrote YouTube Charts source report: %s", report_path)

    if chart_entries:
        entries = [
            {
                "source": row.get("source", "youtube_charts_weekly"),
                "rank": row.get("rank"),
                "original_video_id": row.get("video_id", ""),
                "original_title": row.get("title", ""),
                "original_artist_or_channel": row.get("artist", ""),
                "duration_seconds_original": 0,
                "album": row.get("album", ""),
                "title_en": row.get("title_en", ""),
                "artist_en": row.get("artist_en", ""),
                "album_en": row.get("album_en", ""),
                "title_ko": row.get("title_ko", ""),
                "artist_ko": row.get("artist_ko", ""),
                "album_ko": row.get("album_ko", ""),
                "artwork_url": row.get("artwork_url", ""),
            }
            for row in chart_entries
        ]
    else:
        if not ytmusic:
            LOG.error("No YouTube Charts entries found and YTMusic source playlist fallback is unavailable")
            return 1
        entries = fetch_ytmusic_playlist_entries(ytmusic, args.source_playlist_url, limit=args.track_limit)
    if not entries:
        LOG.error("No chart entries were collected")
        return 1

    db_path = Path(args.db_path).expanduser()

    from hype_db import connect, get_bulk_cached_matches, persist_crawled_tracks, persist_crawl_run, export_frontend_history

    with connect(db_path) as conn:
        # Prepopulate cache in bulk
        bulk_cache = {}
        if not args.no_resolve:
            try:
                tracks_for_cache = []
                for row in entries:
                    tracks_for_cache.append({
                        "song_id": row.get("original_video_id") or "",
                        "title": row.get("original_title") or "",
                        "artist": row.get("original_artist_or_channel") or "",
                        "album": row.get("album") or "",
                    })
                bulk_cache = get_bulk_cached_matches(conn, service="ytmusic", tracks=tracks_for_cache)
            except Exception as exc:
                LOG.warning("Failed to bulk pre-populate cache: %s", exc)

        # Persist raw crawled tracks to playlist_order immediately (if not dry-run)
        from hype_db import reference_period_for_date
        reference_period = reference_period_for_date(args.job_name, chart_period_end)
        if not args.dry_run:
            try:
                raw_tracks = []
                for row in entries:
                    song_id = row.get("original_video_id") or ""
                    raw_tracks.append({
                        "rank": row.get("rank"),
                        "service": "ytmusic",
                        "song_id": song_id,
                        "title": row.get("original_title") or "",
                        "artist": row.get("original_artist_or_channel") or "",
                        "album": row.get("album") or "",
                        "source": row.get("source", "youtube_music_playlist"),
                        "title_en": row.get("title_en", ""),
                        "artist_en": row.get("artist_en", ""),
                        "album_en": row.get("album_en", ""),
                        "title_ko": row.get("title_ko", ""),
                        "artist_ko": row.get("artist_ko", ""),
                        "album_ko": row.get("album_ko", ""),
                        "artwork_url": row.get("artwork_url", ""),
                    })
                persist_crawled_tracks(
                    db_path,
                    service="ytmusic",
                    job_name=args.job_name,
                    source_variant="default",
                    chart_date=chart_period_end,
                    reference_period=reference_period,
                    tracks=raw_tracks,
                    conn=conn,
                )
                LOG.info("Persisted raw chart order for %s to playlist_order table.", args.job_name)
            except Exception as exc:
                LOG.error("Failed to persist raw chart order to DB: %s", exc)
                raise exc

        resolved_rows: list[dict[str, Any]] = []
        video_ids: list[str] = []
        seen: set[str] = set()

        for row in entries:
            original_id = row.get("original_video_id", "")
            if not original_id:
                row.update({"mapping_status": "failed", "mapping_reason": "missing_original_video_id"})
                resolved_rows.append(row)
                continue
            if args.no_resolve:
                out = {
                    **row,
                    "mapping_status": "failed",
                    "mapping_reason": "resolve_skipped",
                    "mapping_score": 0.0,
                    "resolved_video_id": "",
                    "resolved_title": "",
                    "resolved_artist": "",
                    "resolved_album": "",
                    "duration_seconds_resolved": 0,
                }
                resolved_rows.append(out)
                LOG.info("[%03d/%03d] resolve_skipped %s", row["rank"], len(entries), original_id)
                continue
            
            # DB cache check using in-memory bulk cache dict
            cached = bulk_cache.get(original_id) if original_id else None
            if cached and cached.get("status") != "manual_blocked" and cached.get("video_id"):
                out = {
                    **row,
                    "resolved_video_id": cached.get("video_id", ""),
                    "resolved_title": cached.get("yt_title") or cached.get("title") or row.get("original_title", ""),
                    "resolved_artist": cached.get("yt_artist") or cached.get("artist") or row.get("original_artist_or_channel", ""),
                    "resolved_album": cached.get("yt_album") or cached.get("album") or row.get("album", ""),
                    "mapping_status": cached.get("status") or "cached_match",
                    "mapping_reason": cached.get("query") or "db_cache",
                    "mapping_score": float(cached.get("score") or 1.0),
                    "duration_seconds_resolved": 0,
                }
                rid = out.get("resolved_video_id")
                if rid and rid not in seen:
                    seen.add(rid)
                    video_ids.append(rid)
                elif rid:
                    out["mapping_status"] = "duplicate_skipped"
                    out["mapping_reason"] = (out.get("mapping_reason", "") + ";duplicate").strip(";")
                resolved_rows.append(out)
                LOG.info("[%03d/%03d] %s %.3f %s -> %s", row["rank"], len(entries), out["mapping_status"], out.get("mapping_score", 0.0), original_id, rid)
                continue
            if not ytmusic:
                out = {
                    **row,
                    "mapping_status": "kept_original_video",
                    "mapping_reason": "db_cache_miss_no_ytmusic_fallback",
                    "mapping_score": 1.0,
                    "resolved_video_id": original_id,
                    "resolved_title": row.get("original_title", ""),
                    "resolved_artist": row.get("original_artist_or_channel", ""),
                    "resolved_album": row.get("album", ""),
                    "duration_seconds_resolved": 0,
                }
                resolved_rows.append(out)
                LOG.info("[%03d/%03d] db_cache_miss_no_ytmusic_fallback %s", row["rank"], len(entries), original_id)
                continue
            resolution = resolve_video_to_song(
                ytmusic,
                video_id=original_id,
                title=row.get("original_title", ""),
                artist=row.get("original_artist_or_channel", ""),
                duration_seconds=int(row.get("duration_seconds_original") or 0),
                threshold=args.resolve_threshold,
                search_limit=args.search_limit,
            )
            out = {**row, **resolution, "duration_seconds_resolved": 0}
            if not out.get("resolved_title") and out.get("resolved_video_id") == original_id:
                out["resolved_title"] = row.get("original_title", "")
            rid = out.get("resolved_video_id")
            accepted_status = out.get("mapping_status") in ACCEPTED_MAPPING_STATUSES
            if rid and accepted_status and rid not in seen:
                seen.add(rid)
                video_ids.append(rid)
            elif rid and accepted_status:
                out["mapping_status"] = "duplicate_skipped"
                out["mapping_reason"] = (out.get("mapping_reason", "") + ";duplicate").strip(";")
            elif rid:
                out["mapping_reason"] = (out.get("mapping_reason", "") + ";unaccepted_video_type").strip(";")
            resolved_rows.append(out)
            LOG.info("[%03d/%03d] %s %.3f %s -> %s", row["rank"], len(entries), out["mapping_status"], out.get("mapping_score", 0.0), original_id, rid)
            time.sleep(0.2)

        audit = {
            "source": "youtube_music_chart_playlist",
            "total": len(entries),
            "resolved_to_song": sum(1 for r in resolved_rows if r.get("mapping_status") == "resolved_to_song"),
            "kept_original_video": sum(1 for r in resolved_rows if r.get("mapping_status") == "kept_original_video"),
            "ambiguous": sum(1 for r in resolved_rows if r.get("mapping_status") == "ambiguous"),
            "failed": sum(1 for r in resolved_rows if r.get("mapping_status") == "failed"),
            "already_song": sum(1 for r in resolved_rows if r.get("mapping_status") == "already_song"),
            "override_used": 0,
        }
        chart_date = chart_period_end
        playlist_name = args.playlist_name or args.job_name
        from hype_db import reference_period_for_date
        reference_period = reference_period_for_date(args.job_name, chart_date)
        if not args.dry_run:
            try:
                tracks = []
                matches = []
                for row in resolved_rows:
                    song_id = row.get("original_video_id") or row.get("resolved_video_id") or ""
                    tracks.append({
                        "rank": row.get("rank"),
                        "service": "ytmusic",
                        "song_id": song_id,
                        "title": row.get("original_title") or row.get("resolved_title") or "",
                        "artist": row.get("original_artist_or_channel") or row.get("resolved_artist") or "",
                        "album": row.get("album") or row.get("resolved_album", ""),
                        "source": row.get("source", "youtube_music_playlist"),
                        "title_en": row.get("title_en", ""),
                        "artist_en": row.get("artist_en", ""),
                        "album_en": row.get("album_en", ""),
                        "title_ko": row.get("title_ko", ""),
                        "artist_ko": row.get("artist_ko", ""),
                        "album_ko": row.get("album_ko", ""),
                        "artwork_url": row.get("artwork_url", ""),
                    })
                    matches.append({
                        "rank": row.get("rank"),
                        "service": "ytmusic",
                        "song_id": song_id,
                        "title": row.get("original_title") or row.get("resolved_title") or "",
                        "artist": row.get("original_artist_or_channel") or row.get("resolved_artist") or "",
                        "album": row.get("album") or row.get("resolved_album", ""),
                        "title_en": row.get("title_en", ""),
                        "artist_en": row.get("artist_en", ""),
                        "album_en": row.get("album_en", ""),
                        "title_ko": row.get("title_ko", ""),
                        "artist_ko": row.get("artist_ko", ""),
                        "album_ko": row.get("album_ko", ""),
                        "artwork_url": row.get("artwork_url", ""),
                        "video_id": (row.get("resolved_video_id") or "") if row.get("mapping_status") in ACCEPTED_MAPPING_STATUSES else "",
                        "yt_title": row.get("resolved_title") or row.get("original_title") or "",
                        "yt_artist": row.get("resolved_artist") or row.get("original_artist_or_channel") or "",
                        "yt_album": row.get("resolved_album") or row.get("album", ""),
                        "score": row.get("mapping_score", 1.0),
                        "status": "matched" if row.get("mapping_status") in ACCEPTED_MAPPING_STATUSES else row.get("mapping_status", "failed"),
                        "query": row.get("mapping_reason", "ytmusic_chart"),
                    })
                persist_crawl_run(
                    db_path,
                    service="ytmusic",
                    job_name=args.job_name,
                    source_variant="default",
                    chart_date=chart_date,
                    reference_period=reference_period,
                    started_at=started_at,
                    tracks=tracks,
                    matches=matches,
                    conn=conn,
                )
                export_frontend_history(db_path, args.history_json)
            except Exception as exc:
                LOG.warning("Failed to persist YouTube Music chart run to DB: %s", exc)

    if args.db_only:
        LOG.info("Skipped YouTube Music playlist update because --db-only is set")
        LOG.info("Done. db_only=%s dry_run=%s audit=%s", args.db_only, args.dry_run, audit)
        return 0

    if not ytmusic:
        LOG.error("YTMusic client is required for playlist updates")
        return 1
    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    desc = f"YouTube Music Korea weekly chart sync\nLast updated: {kst_now:%Y-%m-%d}\n\nAuto-generated by Github Actions."
    update_ytmusic_playlist(
        ytmusic,
        yt_playlist_id,
        video_ids,
        description=desc,
        dry_run=args.dry_run,
        db_path=db_path,
        service="ytmusic",
        job_name=args.job_name,
        playlist_name=playlist_name,
    )
    LOG.info("Done. playlist_items=%d dry_run=%s audit=%s", len(video_ids), args.dry_run, audit)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
