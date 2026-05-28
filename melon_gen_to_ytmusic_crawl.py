#!/usr/bin/env python3
"""
Melon 세대별 차트(gen=1, gen=2)를 YouTube Music 플레이리스트로 동기화합니다.

melon_to_ytmusic_crawl.py와 동일한 인증/환경변수/매칭/캐시/Apple proxy/로그/플레이리스트
동기화 파이프라인을 사용하고, Melon에서 트랙 정보를 파싱하는 부분만 세대별 차트
페이지에 맞게 바꾼 버전입니다.
"""

from __future__ import annotations
from bs4 import builder

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from melon_to_ytmusic_crawl import (
    DEFAULT_MIN_ARTIST_SCORE,
    DEFAULT_MIN_SCORE,
    DEFAULT_MIN_TITLE_SCORE,
    DEFAULT_SEARCH_LIMIT,
    configure_logging,
    fetch_melon_album_name,
    load_album_cache,
    save_album_cache,
    run_tracks_pipeline,
)
from ytmusic_playlist_sync import SourceTrack, load_dotenv, unique_values, write_json
from hype_scoring import calculate_rank_score


DEFAULT_MELON_GEN_URL = "https://kkosvc.melon.com/mwk/chart/gen.htm?gen={gen}"
LOG = logging.getLogger("melon_gen_to_ytmusic_crawl")
def first_text(root: BeautifulSoup | Any, selectors: list[str]) -> str:
    for selector in selectors:
        el = root.select_one(selector)
        if not el:
            continue
        text = el.get_text(" ", strip=True)
        if text:
            return text
    return ""


def attr_first(el: Any, names: list[str]) -> str:
    for name in names:
        value = el.get(name)
        if value:
            return str(value).strip()
    return ""


def melon_song_url(song_id: str) -> str:
    return f"https://www.melon.com/song/detail.htm?songId={song_id}" if song_id else ""


def extract_song_id(item: Any) -> str:
    song_id = attr_first(item, ["d-songid", "data-song-id", "data-song-no", "data-songid"])
    if song_id:
        return song_id
    link = item.select_one("a[href*='songId='], a[href*='goSongDetail'], a[href*='playSong']")
    if link and link.get("href"):
        match = re.search(r"songId=(\d+)|(\d{5,})", link["href"])
        if match:
            return next(group for group in match.groups() if group)
    return ""


def extract_album_id(item: Any) -> str:
    album_id = attr_first(item, ["d-albumid", "data-album-id", "data-album-no", "data-albumid"])
    if album_id:
        return album_id
    link = item.select_one("a[href*='albumId='], a[href*='goAlbumDetail']")
    if link and link.get("href"):
        match = re.search(r"albumId=(\d+)|(\d{5,})", link["href"])
        if match:
            return next(group for group in match.groups() if group)
    return ""


def extract_generation_label(soup: BeautifulSoup, gen: str) -> str:
    candidates = [
        first_text(soup, ["h1", "h2", ".title", ".chart_title", ".page_header", ".chart-tit"]),
        soup.title.get_text(" ", strip=True) if soup.title else "",
    ]
    for text in candidates:
        if text and ("세대" in text or "차트" in text):
            return text
    return f"gen={gen}"


def parse_melon_generation_tracks(html: str, gen: str, *, limit: int = 100, ttl_days: int = 31) -> tuple[str, str, list[SourceTrack]]:
    """Parse only the Melon generation-chart specific track fields.

    Everything after this SourceTrack list is handed to melon_to_ytmusic_crawl.run_tracks_pipeline
    so that matching and playlist sync remain identical to the normal Melon crawler.
    """
    soup = BeautifulSoup(html, "html.parser")
    generation_label = extract_generation_label(soup, gen)
    chart_date = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    items = soup.select(".song.d-listen.list-song__item.list-song__item_theme_rank")
    if not items:
        items = soup.select(".list-song__item_theme_rank, .list-song__item, [d-songid], [data-song-id], [data-song-no]")

    tracks: list[SourceTrack] = []
    for idx, item in enumerate(items[:limit], 1):
        song_id = extract_song_id(item)
        album_id = extract_album_id(item)

        rank_text = first_text(item, [".rank__num", ".rank", ".list-rank", ".ranking_num", ".rank_num", ".no"])
        rank_match = re.search(r"\d+", rank_text or "")
        rank = int(rank_match.group(0)) if rank_match else idx

        # 곡명 추출: 데이터 속성 우선, 그 다음 하위 요소
        title = attr_first(item, ["d-songname", "data-songname", "data-song-name", "d-song-name"])
        if not title:
            title = first_text(
                item,
                [
                    ".song__text",
                    ".song_name",
                    ".song__title",
                    ".title",
                    ".list-song__title",
                    ".ellipsis.rank01",
                    "a[href*='song']",
                ],
            )

        # 가수 추출: 데이터 속성 우선, 그 다음 하위 요소
        artist = attr_first(item, ["d-artistnames", "data-artistnames", "data-artist-names", "d-artist-names"])
        if not artist:
            artist_links = item.select(".song__singer a, .list-song__artist a, .artist_name a, .artist a, a[href*='artist']")
            if artist_links:
                artist = ", ".join(unique_values([a.get_text(strip=True) for a in artist_links]))
            else:
                artist = first_text(
                    item,
                    [
                        ".song__singer",
                        ".artist_name",
                        ".artist",
                        ".list-song__artist",
                        ".list-song__subtitle",
                        ".ellipsis.rank02",
                    ],
                )


        if not title:
            LOG.warning("Skipping Melon gen=%s row=%d because title is empty. song_id=%s", gen, idx, song_id)
            continue

        artwork_url = attr_first(item, ["d-thumbnail", "data-thumbnail", "d-thumbnail", "data-img"])
        if not artwork_url:
            img_el = item.select_one("img")
            artwork_url = img_el.get("src", "") if img_el else ""


        # 상세 페이지에서 앨범 명칭 수집 (리스트에 정보가 없으므로 fetch 호출)
        album_name = fetch_melon_album_name(album_id, ttl_days=ttl_days) if album_id else ""

        tracks.append(
            SourceTrack(
                rank=rank,
                title=title,
                artist=artist,
                service="melon",
                album=album_name,
                source="melon_generation_chart",
                artwork_url=artwork_url,
                song_id=song_id,
                album_id=album_id,
            )
        )

    playlist_name = f"Melon Generation Chart {gen}0s"
    playlist_desc = f"{playlist_name}"
    return playlist_name, playlist_desc, tracks


def merge_melon_generation_tracks(
    tracks_gen1: list[SourceTrack],
    tracks_gen2: list[SourceTrack],
    *,
    top_k: int = 50,
    log_dir: str
) -> tuple[str, str, list[SourceTrack]]:
    """
    Merge gen=1 and gen=2 tracks into Generation Z chart.

    - parse_melon_generation_tracks 결과를 그대로 입력으로 받는다.
    - melon_to_ytmusic_crawl pipeline에는 SourceTrack 리스트만 넘긴다.
    """

    from collections import defaultdict

    def normalize_key(title: str, artist: str) -> str:
        return re.sub(r"\s+", "", f"{title.lower()}::{artist.lower()}")

    # 1. track map 생성
    merged: dict[str, dict] = {}

    def get_key(track: SourceTrack) -> str:
        if track.song_id:
            return f"id::{track.song_id}"
        return f"norm::{normalize_key(track.title, track.artist)}"

    # gen1
    for t in tracks_gen1:
        key = get_key(t)
        merged.setdefault(key, {"track": t, "gen1_rank": None, "gen2_rank": None})
        merged[key]["gen1_rank"] = t.rank

    # gen2
    for t in tracks_gen2:
        key = get_key(t)
        merged.setdefault(key, {"track": t, "gen1_rank": None, "gen2_rank": None})
        merged[key]["gen2_rank"] = t.rank

    # 2. 점수 계산
    scored = []

    for key, v in merged.items():
        # .get()을 사용하여 키 에러 방지
        t = v.get("track")
        gen1_rank = v.get("gen1_rank")
        gen2_rank = v.get("gen2_rank")

        gen1 = calculate_rank_score(gen1_rank)
        gen2 = calculate_rank_score(gen2_rank)

        combined_score = gen1 * 0.60 + gen2 * 0.40

        scored.append(
            {
                "track": t,
                "gen1_rank": gen1_rank,
                "gen2_rank": gen2_rank,
                "combined_score": combined_score,
            }
        )

    # 3. 정렬
    def sort_key(x):
        return (
            -x["combined_score"],
            x["gen1_rank"] or 999,
            x["gen2_rank"] or 999,
        )

    scored.sort(key=sort_key)

    merge_rows: list[dict] = []
    for idx, item in enumerate(scored, 1):
        t = item["track"]
        merge_rows.append({
            "final_rank": idx,
            "title": t.title,
            "artist": t.artist,
            "album": t.album,
            "song_id": t.song_id,
            "album_id": t.album_id,    
            "source": t.source,
            "artwork_url": t.artwork_url,
            "gen1_rank": item["gen1_rank"],
            "gen2_rank": item["gen2_rank"],
            "combined_score": round(item["combined_score"], 2),
        })

    log_dir = Path(log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # 4. top_k 선택
    top = scored[:top_k]

    # 5. SourceTrack 재구성
    merged_tracks: list[SourceTrack] = []
    merge_rows: list[dict] = []

    for idx, item in enumerate(top, 1):
        t = item["track"]

        merged_tracks.append(
            SourceTrack(
                rank=idx,
                title=t.title,
                artist=t.artist,
                service="melon",
                album=t.album,
                source="melon_generation_z",
                artwork_url=t.artwork_url,
                song_id=t.song_id,
                album_id=t.album_id,
            )
        )

    playlist_name = "Melon Generation Z Chart"
    playlist_desc = f"{playlist_name}"

    return playlist_name, playlist_desc, merged_tracks, merge_rows


def fetch_melon_generation_tracks(gen: str, *, limit: int = 100, ttl_days: int = 31) -> tuple[str, str, list[SourceTrack]]:
    url = DEFAULT_MELON_GEN_URL.format(gen=gen)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Referer": "https://m.melon.com/",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return parse_melon_generation_tracks(response.text, gen, limit=limit, ttl_days=ttl_days)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Melon generation chart to YouTube Music crawler.")
    parser.add_argument("--env-file", default=".env", help=argparse.SUPPRESS)
    parser.add_argument("--melon-generation-gens", nargs="+", default=["1", "2"], help="Melon generation chart ids to process, e.g. 1 2")
    parser.add_argument("--track-limit", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=100)    
    parser.add_argument("--yt-auth", help=argparse.SUPPRESS, default='.secrets/browser.json')
    parser.add_argument("--yt-oauth-client-id", help=argparse.SUPPRESS)
    parser.add_argument("--yt-oauth-client-secret", help=argparse.SUPPRESS)
    parser.add_argument("--yt-playlist-id", help=argparse.SUPPRESS, default='PLtawHGpcUVZXC7USbMWiv9sbdFAHpTKqL')
    parser.add_argument("--job-name")
    parser.add_argument("--playlist-name")
    parser.add_argument("--chart-date")
    parser.add_argument("--log-dir")
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--no-db-cache", action="store_true")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--min-title-score", type=float, default=DEFAULT_MIN_TITLE_SCORE)
    parser.add_argument("--min-artist-score", type=float, default=DEFAULT_MIN_ARTIST_SCORE)
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--album-cache-ttl", type=int, default=31, help="TTL in days for album name cache")
    parser.add_argument("--apple-proxy-data", help="Path to one or more Apple matches_crawl.json files to use as metadata proxy")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle the tracks before saving them to the YouTube Music playlist")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)
    configure_logging()

    melon_generation_gens = args.melon_generation_gens or []
    if not melon_generation_gens:
        LOG.error("No Melon generation ids provided.")
        return 1

    # 로그 디렉토리 및 하위 폴더 결정
    base_log_dir = Path(args.log_dir or os.environ.get("LOG_DIR", "logs")).expanduser()

    # sync_all 등에서 전달받은 경로가 단순 logs 루트이거나 지정되지 않은 경우에만 자동 폴더 생성
    if not args.log_dir or Path(args.log_dir).name == "logs":
        if len(melon_generation_gens) == 1:
            gen_id = melon_generation_gens[0]
            subfolder = f"Melon-Gen{gen_id}-Top-100-Daily"
            args.log_dir = str(base_log_dir / subfolder) if base_log_dir.name == "logs" else str(base_log_dir)
        else:
            args.log_dir = str(base_log_dir / "Melon-Gen-Z-Top-100-Daily")

    log_dir = Path(args.log_dir).expanduser()
    if not args.no_db_cache:
        os.environ["HYPE_DB_PATH"] = str(Path(args.db_path).expanduser())
    cache_path = load_album_cache(log_dir, ttl_days=args.album_cache_ttl)

    gen_tracks_map: dict[int, list[SourceTrack]] = {}
    gen_desc_map: dict[int, str] = {}

    all_tracks: list[SourceTrack] = []
    combined_desc_parts: list[str] = []

    for gen in melon_generation_gens:
        LOG.info("Processing Melon generation chart: gen=%s", gen)
        try:
            playlist_name, playlist_desc, tracks = fetch_melon_generation_tracks(
                gen,
                limit=args.track_limit,
                ttl_days=args.album_cache_ttl,
            )

            gen_tracks_map[int(gen)] = tracks
            gen_desc_map[int(gen)] = playlist_desc

            LOG.info("Added %d tracks from '%s'", len(tracks), playlist_name)

        except Exception as exc:
            LOG.error("Failed to scrape Melon generation chart gen=%s: %s", gen, exc)
            if len(melon_generation_gens) == 1:
                return 1

    # ㅁ merge 조건: gen=1,2 둘 다 있는 경우만
    if set(map(int, melon_generation_gens)) == {1, 2}:
        LOG.info("Applying Generation Z merge logic (gen=1,2)")

        tracks1 = gen_tracks_map.get(1, [])
        tracks2 = gen_tracks_map.get(2, [])

        playlist_name, playlist_desc, all_tracks, merge_rows = merge_melon_generation_tracks(
            tracks1,
            tracks2,
            top_k=args.top_k,
            log_dir=args.log_dir,
        )

        combined_desc_parts = [playlist_desc]

    else:
        # 기존 동작 유지 (절대 변경 금지)
        for gen in melon_generation_gens:
            all_tracks.extend(gen_tracks_map.get(int(gen), []))
            combined_desc_parts.append(gen_desc_map.get(int(gen), ""))

    # 업데이트된 캐시 저장
    save_album_cache(cache_path)

    args.source_variant = "combined"
    args.job_name = args.job_name or "Gen-Z-Daily"
    args.playlist_name = args.playlist_name or "mel_zdc_to_ytm"
    result = run_tracks_pipeline(
        args,
        all_tracks,
        combined_desc_parts,
        log_prefix="melon_gen",
        empty_message="No tracks collected from any of the provided Melon generation charts.",
    )
    if result == 0 and not args.no_db_cache and not args.dry_run:
        try:
            from hype_db import export_frontend_history, persist_crawl_run

            chart_date = args.chart_date or datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            job_name = args.job_name or "mel_zdc_to_ytm"
            for gen_id, source_variant in ((1, "gen10"), (2, "gen20")):
                tracks = gen_tracks_map.get(gen_id, [])
                if not tracks:
                    continue
                persist_crawl_run(
                    args.db_path,
                    service="melon",
                    job_name=job_name,
                    source_variant=source_variant,
                    chart_date=chart_date,
                    started_at=f"{started_at}_gen{gen_id}",
                    tracks=tracks,
                    matches=tracks,
                )
            export_frontend_history(args.db_path, args.history_json)
        except Exception as exc:
            LOG.warning("Failed to persist split Melon Gen-Z order to DB: %s", exc)
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
