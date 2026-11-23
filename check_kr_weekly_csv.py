import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlsplit

URL = "https://charts.youtube.com/charts/TopSongs/kr/weekly"
FILENAME = "youtube-charts-top-songs-kr-weekly-20261119.csv"


def emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def inspect_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if [int(row["Rank"]) for row in rows] != list(range(1, 101)):
        raise ValueError("The file does not contain ranks 1-100 in order")
    ids = [parse_qs(urlsplit(r["YouTube URL"]).query).get("v", [""])[0] for r in rows]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in ids):
        raise ValueError("Missing or invalid chart video ID")
    if not all(r["Track Name"].strip() and r["Artist Names"].strip() for r in rows):
        raise ValueError("Missing title or artist")
    stats = "\n".join("|".join([r["Rank"], r["Previous Rank"], v, r["Views"]]) for r, v in zip(rows, ids))
    same_ids = hashlib.sha256(",".join(ids).encode()).hexdigest() == "d5fe889dc4cd8576eed326a41fc18726a1b2134707ef67149237c83be6c3c811"
    same_stats = hashlib.sha256(stats.encode()).hexdigest() == "e10e89fea0ce3c640e871886ffb8d720ffecdc189f7c6a7e629a7b572c0ea33d"
    same_bytes = hashlib.sha256(path.read_bytes()).hexdigest() == "f7d9d11cec1607038e4112fd3381c5c1b52ef32951c920586c706855f7b72804"
    emit("csv_check", rows=len(rows), ordered_ids_match=same_ids, rank_and_views_match=same_stats,
         identical_file=same_bytes, top_title=rows[0]["Track Name"])
    if not (same_ids and same_stats):
        raise ValueError("Downloaded CSV differs from the verified Safari chart")


def run():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page = browser.new_page(locale="en-US", accept_downloads=True)
            statuses = []
            def record(response):
                parts = urlsplit(response.url)
                if parts.hostname == "charts.youtube.com" and parts.path == "/youtubei/v1/browse":
                    statuses.append(response.status)
                    emit("official_browse", status=response.status)
            page.on("response", record)
            response = page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            emit("chart_document", status=response.status if response else None)
            if not response or not response.ok or urlsplit(page.url).hostname != "charts.youtube.com":
                raise ValueError("Official chart document unavailable")
            date = page.locator(".weekly-chart-calendar-selector #paper-button-container")
            button = page.get_by_role("button", name="download", exact=True)
            challenge = page.locator('iframe[src*="/recaptcha/"][src*="/bframe"]:visible')
            deadline = monotonic() + 45
            since = None
            observed_period = ""
            while monotonic() < deadline:
                if challenge.count():
                    since = monotonic() if since is None else since
                    if monotonic() - since >= 2:
                        emit("interactive_recaptcha", seconds=round(monotonic()-since, 1),
                             download_visible=button.count() > 0 and button.first.is_visible())
                        raise ValueError("The chart requires an interactive CAPTCHA")
                else:
                    since = None
                if date.count() and date.first.is_visible():
                    observed_period = " ".join(date.first.inner_text(timeout=3000).split())
                    normalized = re.sub(r"\s", "", observed_period).replace("–", "-").replace("—", "-")
                    if normalized == "Nov13-19,2026" and button.count() == 1 and button.is_visible() and button.is_enabled():
                        break
                page.wait_for_timeout(200)
            else:
                emit("chart_not_ready", period=observed_period, browse_statuses=statuses,
                     download_visible=button.count() > 0 and button.first.is_visible())
                raise ValueError("The expected period and its download button were not both available")
            emit("download_button_ready", period=observed_period)
            with page.expect_download(timeout=30000) as event:
                button.click(timeout=5000)
            dl = event.value
            if dl.failure() or dl.suggested_filename != FILENAME:
                raise ValueError(f"Download failed or unexpected filename: {dl.suggested_filename}")
            path = Path(FILENAME)
            dl.save_as(path)
            emit("download_received", filename=FILENAME, size=path.stat().st_size)
            inspect_csv(path)
        finally:
            browser.close()


if __name__ == "__main__":
    if len(sys.argv) == 2:
        inspect_csv(Path(sys.argv[1]))
    else:
        run()
