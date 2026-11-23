"""One-time Melon recovery for the official November 21 daily charts."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import melon_gen_to_ytmusic_crawl as generation
import melon_to_ytmusic_crawl as daily
from hype_db import connect
from sync_all import data_snapshot_ready, publication_result
from sync_validation import run_locked_cli

PERIOD = "2026-11-21"
# Ordered source IDs checked independently by both clients in Actions run 45096221288.
DIGESTS = {
    "default": "2dac453850270cc2319526016eec80dba30cd6f33cf110c41c4f3b2aba96c590",
    "gen10": "772aa16c9c7ff9be1ce6bd9e0f5791ee71d8da875d22235ac067cad2364fbd84",
    "gen20": "22a289e3fd22da972b43cc19468f88e60dd45ed978174fea4447b3fb9fef4498",
    "combined": "9691258fcbcd5120e8df19fd3ca89f19caff9f4e5670242f8b2662eecf687299"
}
TASKS = {t["job_name"]: t for t in json.loads(Path("sync_config.json").read_text())}
FILES = Path(os.environ["RUNNER_TEMP"]) / "melon-20261121"
DB = Path("hype_wave_data.db")


def check_ids(variant, ranks, ids):
    if ranks != list(range(1, 101)) or not all(str(i).isdecimal() for i in ids):
        raise ValueError(f"{variant}: incomplete ranks or source IDs")
    if hashlib.sha256(",".join(ids).encode()).hexdigest() != DIGESTS[variant]:
        raise ValueError(f"{variant}: ordered IDs differ from November 21")


def collect():
    FILES.mkdir(exist_ok=True)
    urls = {"default": TASKS["Top-100-Daily"]["source_urls"][0]}
    urls.update({f"gen{s['gen']}0": s["url"] for s in TASKS["Gen-Z-Daily"]["source_urls"]})
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        for variant, url in urls.items():
            gen = variant != "default"
            selector = ".song.d-listen.list-song__item.list-song__item_theme_rank" if gen else "#tb_list tbody tr"
            page = browser.new_page()
            response = page.goto(url, wait_until="commit", timeout=30000)
            if not response or response.status != 200 or response.url != url:
                raise ValueError(f"{variant}: expected official chart response")
            page.locator(selector).nth(99).wait_for(state="attached", timeout=15000)
            html = page.content()
            page.close()
            soup = BeautifulSoup(html, "html.parser")
            date = soup.select_one(".btn-box__date label" if gen else ".yyyymmdd").get_text(" ", strip=True)
            if not re.fullmatch(r"11\.21\s*기준" if gen else r"2026\.11\.21", date):
                raise ValueError(f"{variant}: unexpected displayed date {date!r}")
            rows = soup.select(selector)
            ranks = [int(re.search(r"\d+", r.select_one(".rank__num" if gen else "span.rank").get_text()).group()) for r in rows]
            ids = [generation.extract_song_id(r) if gen else r.get("data-song-no", "") for r in rows]
            check_ids(variant, ranks, ids)
            (FILES / f"{variant}.html").write_text(html, encoding="utf-8")
            print(f"SOURCE {variant}: {date}, ranks 1-100 verified", flush=True)
        browser.close()


def recover(job):
    task = TASKS[job]
    is_gen = job == "Gen-Z-Daily"
    variant = "combined" if is_gen else "default"
    start = datetime.now(timezone.utc).isoformat()
    os.environ["HYPE_MATCH_STARTED_AT"] = start
    sys.argv = [sys.argv[0], "--yt-auth", os.environ["YTMUSIC_AUTH_FILE"], "--yt-playlist-id", task["target_id"],
                "--job-name", job, "--playlist-name", task["playlist_name"]]
    if is_gen:
        sys.argv.extend(["--melon-generation-gens", "1", "2"])
    else:
        sys.argv.extend(["--melon-urls", str(FILES / "default.html")])

    def parse_generation(gen, **kwargs):
        html = (FILES / f"gen{gen}0.html").read_text(encoding="utf-8")
        return generation.parse_melon_generation_tracks(html, gen, **kwargs)

    publish = daily.update_ytmusic_playlist

    def verified_publish(ytmusic, playlist_id, ids, **kwargs):
        with connect(DB, read_only=True) as conn:
            for source in (["gen10", "gen20", "combined"] if is_gen else ["default"]):
                rows = conn.execute("""SELECT rank_order, song_id FROM playlist_order WHERE service='melon'
                    AND job_name=? AND source_variant=? AND reference_period=? ORDER BY rank_order""",
                    (job, source, PERIOD)).fetchall()
                check_ids(source, [r["rank_order"] for r in rows], [r["song_id"] for r in rows])
                print(f"DB_VERIFIED {job} {source} {PERIOD} ranks=1-100", flush=True)
            if not data_snapshot_ready(conn, service="melon", job_name=job, started_at=start, source_variant=variant):
                raise ValueError("No complete match snapshot for this execution")
            match = conn.execute("""SELECT reference_period,matched_tracks,failed_tracks FROM match_runs
                WHERE service='melon' AND job_name=? AND source_variant=? AND started_at=?""",
                (job, variant, start)).fetchone()
            if match["reference_period"] != PERIOD or playlist_id != task["target_id"]:
                raise ValueError("Unexpected chart period or destination")
            print(f"MATCH_VERIFIED {job} matched={match['matched_tracks']} unmatched={match['failed_tracks']}", flush=True)
        return publish(ytmusic, playlist_id, ids, **kwargs)

    def locked():
        with connect(DB, read_only=True) as conn:
            newest = conn.execute("""SELECT MAX(reference_period) AS period FROM playlist_order
                WHERE service='melon' AND job_name=? AND source_variant=?""", (job, variant)).fetchone()["period"]
            print(f"PREVIOUS {job} latest={newest}", flush=True)
            if newest and newest > PERIOD:
                raise ValueError(f"{job}: newer chart already exists ({newest})")
        with patch.object(generation, "fetch_melon_generation_tracks", parse_generation), patch.object(daily, "update_ytmusic_playlist", verified_publish):
            result = (generation.main if is_gen else daily.main)()
        if result == 0:
            with connect(DB, read_only=True) as conn:
                audit = publication_result(conn, task, start)
            if not audit:
                raise ValueError(f"{job}: no verified publication")
            print(f"PLAYLIST_VERIFIED {job} {PERIOD} mode={audit['mode']} requested={audit['requested']} published={audit['published']}", flush=True)
        return result

    return run_locked_cli(locked)


if __name__ == "__main__":
    action, = sys.argv[1:]
    if action == "collect":
        collect()
    elif action in {"Top-100-Daily", "Gen-Z-Daily"}:
        sys.exit(recover(action))
    else:
        raise ValueError("Unexpected operation")
