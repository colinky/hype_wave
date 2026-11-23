"""Read the official Melon weekly chart once per client on one Actions runner."""
from datetime import datetime, timedelta
import hashlib
import json
import re
from time import monotonic
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import requests


URL = "https://www.melon.com/chart/week/index.htm"
# Same header and timeout as fetch_melon_tracks; this probe makes one attempt.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
ROWS = "#tb_list tbody tr"
today = datetime.now(ZoneInfo("Asia/Seoul")).date()
this_monday = today - timedelta(days=today.weekday())
target = [str(this_monday - timedelta(days=n)) for n in (7, 1)]


def verify(html):
    soup = BeautifulSoup(html, "html.parser")
    period = soup.select_one(".yyyymmdd")
    dates = re.findall(r"\d{4}\.\d{2}\.\d{2}", period.get_text() if period else "")
    dates = [s.replace(".", "-") for s in dates]
    rows = soup.select(ROWS)
    ranks, ids = [], []
    for row in rows:
        label = row.select_one("span.rank")
        value = re.search(r"\d+", label.get_text() if label else "")
        ranks.append(int(value[0]) if value else None)
        ids.append(row.get("data-song-no", ""))
    complete = ranks == list(range(1, 101)) and all(s.isdecimal() for s in ids)
    return dict(rows=len(rows), ranks_and_ids_ok=complete, period=dates,
                target_week=target, target_ok=complete and dates == target,
                chart_digest=hashlib.sha256(",".join(ids).encode()).hexdigest() if complete else None)


def official(url):
    u = urlsplit(url)
    return u.scheme == "https" and u.hostname == "www.melon.com" and u.path == "/chart/week/index.htm"


def read_requests(result):
    result["phase"] = "navigation"
    with requests.get(URL, headers=HEADERS, timeout=30, allow_redirects=False) as response:
        result["http"] = response.status_code
        if response.status_code == 200 and official(response.url):
            result["phase"] = "parse"
            result.update(verify(response.text))


def read_browser(result):
    result["phase"] = "launch"
    with sync_playwright() as p:
        browser = p.webkit.launch(headless=True)
        try:
            page = browser.new_page()
            result["phase"] = "navigation"
            response = page.goto(URL, wait_until="commit", timeout=30_000)
            result["http"] = response.status if response else None
            if result["http"] != 200 or not official(response.url):
                return
            result["phase"] = "chart_render"
            page.locator(ROWS).nth(99).wait_for(state="attached", timeout=15_000)
            result["phase"] = "parse"
            result.update(verify(page.content()))
        finally:
            browser.close()


for name, read in (("requests", read_requests), ("playwright_webkit", read_browser)):
    result = {"client": name, "http": None, "target_ok": False}
    start = monotonic()
    try:
        read(result)
    except Exception as exc:
        result.update(error=type(exc).__name__, detail=str(exc).splitlines()[0][:500])
    result["elapsed_s"] = round(monotonic() - start, 1)
    print("MELON_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    if name == "playwright_webkit" and not result["target_ok"]:
        raise SystemExit("Browser did not verify the target week's 1..100 ranks and song IDs.")
