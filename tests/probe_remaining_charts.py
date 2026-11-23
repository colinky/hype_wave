"""Read-only paired chart checks. Run from the repository root with python -m."""
from datetime import datetime, timedelta
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from time import monotonic
from unittest.mock import patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import melon_gen_to_ytmusic_crawl as gen
import ytmusic_to_ytmusic_crawl as yt

logging.basicConfig(level=logging.INFO)
today = datetime.now(ZoneInfo("Asia/Seoul")).date()
daily_target = [(today - timedelta(days=1)).isoformat()]
last_thursday = today - timedelta(days=(today.weekday() - 3) % 7 or 7)
weekly_target = [(last_thursday - timedelta(days=6)).isoformat(), last_thursday.isoformat()]

DAILY = "https://www.melon.com/chart/day/index.htm"
DAILY_ROWS = "#tb_list tbody tr"
GEN_ROWS = ".song.d-listen.list-song__item.list-song__item_theme_rank"
DAILY_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
GEN_HEADERS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
               "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8", "Referer": "https://m.melon.com/"}


def signature(ids):
    return hashlib.sha256(",".join(ids).encode()).hexdigest()


def melon(label, client, result):
    generation = label.startswith("gen")
    url = gen.DEFAULT_MELON_GEN_URL.format(gen=label[-1]) if generation else DAILY
    selector = GEN_ROWS if generation else DAILY_ROWS
    if client == "requests":
        response = requests.get(url, headers=GEN_HEADERS if generation else DAILY_HEADERS,
                                timeout=30, allow_redirects=False)
        result["http"] = response.status_code
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected a chart page (HTTP 200)")
        html = response.content
    else:
        with sync_playwright() as pw:
            browser = pw.webkit.launch(headless=True)
            try:
                page = browser.new_page()
                response = page.goto(url, wait_until="commit", timeout=30_000)
                result["http"] = response.status if response else None
                if result["http"] != 200 or urlsplit(response.url)[:4] != urlsplit(url)[:4]:
                    raise ValueError("Did not receive the official chart page")
                page.locator(selector).nth(99).wait_for(state="attached", timeout=15_000)
                html = page.content()
            finally:
                browser.close()
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(selector)
    result["rows"] = len(rows)
    date_el = soup.select_one(".btn-box__date label" if generation else ".yyyymmdd")
    result["displayed"] = date_el.get_text(" ", strip=True) if date_el else ""
    rank_selector = ".rank__num" if generation else "span.rank"
    ranks = []
    for row in rows:
        element = row.select_one(rank_selector)
        rank = re.search(r"\d+", element.get_text(strip=True) if element else "")
        ranks.append(int(rank[0]) if rank else None)
    ids = [gen.extract_song_id(row) if generation else row.get("data-song-no", "") for row in rows]
    if ranks != list(range(1, 101)) or not all(s.isdecimal() for s in ids):
        raise ValueError("Rank sequence or song ID validation failed")
    tracks = []
    if generation:
        if not re.search(r"\d{2}\.\d{2}", result["displayed"]):
            raise ValueError("Missing displayed source date")
        # Album enrichment requests are outside this read-only chart comparison.
        with patch.object(gen, "fetch_melon_album_name", return_value=""):
            _, _, period, tracks = gen.parse_melon_generation_tracks(str(soup), label[-1])
        if [r.song_id for r in tracks] != ids or any(not t.title or not t.artist for t in tracks):
            raise ValueError("Incomplete generation chart metadata")
    else:
        date = re.search(r"\d{4}\.\d{2}\.\d{2}", result["displayed"])
        if not date or any(not row.select_one(".rank01 a") or not row.select_one(".rank02 a") for row in rows):
            raise ValueError("Missing date or chart metadata")
        period = date[0].replace(".", "-")
    result.update(period=[period], digest=signature(ids))
    return tracks


def youtube(client, result):
    if client == "requests":
        # Isolate the normal source reader. The independent browser arm runs once below.
        with patch.object(yt, "extract_chart_entries_from_browser", return_value=[]):
            entries = yt.extract_chart_entries_from_youtube_charts(yt.DEFAULT_YOUTUBE_CHARTS_URL)
    else:
        entries = yt.extract_chart_entries_from_browser(yt.DEFAULT_YOUTUBE_CHARTS_URL)
    result["rows"] = len(entries)
    yt.validate_weekly_chart(entries)
    result.update(method=entries[0].get("collection_method"), period=list(yt.fetched_period_from_entries(entries)),
                  digest=signature([row["video_id"] for row in entries]))


def run():
    subjects = ("daily", "gen1", "gen2", "youtube")
    results, generations = {}, {}
    for label in subjects:
        target = weekly_target if label == "youtube" else daily_target
        for client in ("requests", "webkit"):
            start = monotonic()
            result = {"chart": label, "client": client, "target": target, "read_ok": False, "target_ok": False}
            try:
                if label == "youtube":
                    youtube(client, result)
                else:
                    tracks = melon(label, client, result)
                    if tracks:
                        generations[label, client] = tracks
                result.update(read_ok=True, target_ok=(result["period"] == target))
            except Exception as exc:
                result.update(error=type(exc).__name__, detail=str(exc).splitlines()[0][:300])
            result["seconds"] = round(monotonic() - start, 1)
            results[label, client] = result
            print("CHART_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    merged_results = {}
    for client in ("requests", "webkit"):
        combined = {"client": client, "rows": 0, "target_ok": False}
        if all((g, client) in generations for g in ("gen1", "gen2")):
            g1, g2 = (results[g, client] for g in ("gen1", "gen2"))
            if g1["period"] == g2["period"]:
                _, _, merged, _ = gen.merge_melon_generation_tracks(
                    generations["gen1", client], generations["gen2", client], top_k=100)
                ranks_ok = [t.rank for t in merged] == list(range(1, 101))
                combined.update(rows=len(merged), period=g1["period"], digest=signature([t.song_id for t in merged]),
                                target_ok=g1["target_ok"] and g2["target_ok"] and ranks_ok)
        merged_results[client] = combined
        print("MERGE_RESULT " + json.dumps(combined), flush=True)
    return (all(results[label, "webkit"]["target_ok"] for label in subjects)
            and merged_results["webkit"]["target_ok"])


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
