"""Check the local or deployed frontend against a Git baseline; no DB or playlist writes."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "docs/api/editors-choice.json").read_text())
PREVIEW_GROUP = '#preview-tracks .editor-preview-group:not([aria-hidden])'


def preview_drift(page):
    return page.locator("#preview-tracks").evaluate("""el => new Promise(resolve => {
        const start = el.scrollLeft;
        let frames = 0;
        function sample() {
            if (++frames === 12) resolve(el.scrollLeft - start);
            else requestAnimationFrame(sample);
        }
        requestAnimationFrame(sample);
    })""")


def verify_preview_motion(page, width):
    page.locator("#preview-year").select_option("2025")
    viewport = page.locator("#preview-tracks")
    button = page.locator("#preview-motion")
    viewport.scroll_into_view_if_needed()
    page.mouse.move(1, 1)
    page.wait_for_function("document.getElementById('preview-tracks').scrollLeft > 10", timeout=7000)
    button.click()
    expect(button).to_have_attribute("aria-pressed", "false")
    assert abs(preview_drift(page)) <= 1
    position = viewport.evaluate("el => el.scrollLeft")
    button.click()
    page.mouse.move(1, 1)
    page.wait_for_function("start => document.getElementById('preview-tracks').scrollLeft > start + 4", arg=position)

    if width in (1440, 390):
        button.click()
        distance = page.locator(PREVIEW_GROUP).evaluate("el => el.offsetWidth")
        viewport.evaluate("(el, distance) => { el.scrollLeft = distance - 2; }", distance)
        button.click()
        page.mouse.move(1, 1)
        page.wait_for_function("distance => document.getElementById('preview-tracks').scrollLeft < distance / 2", arg=distance)
    if width > 600:
        viewport.hover()
        assert abs(preview_drift(page)) <= 1
        page.mouse.move(1, 1)
    page.locator(PREVIEW_GROUP + " a").first.focus()
    assert abs(preview_drift(page)) <= 1
    last = page.locator(PREVIEW_GROUP + " a").last
    last.focus()
    bounds = last.bounding_box()
    window = viewport.bounding_box()
    assert bounds["x"] >= window["x"] - 2 and bounds["x"] + bounds["width"] <= window["x"] + window["width"] + 2
    button.click()
    expect(button).to_have_attribute("aria-pressed", "false")
    page.locator("#preview-year").select_option("2025")
    assert viewport.evaluate("el => el.scrollLeft") == 0


def verify_archive_sources():
    # Counts, publication dates and versions checked against the supplied articles/playlists.
    sources = {
        2025: (16, 26, "PLtawHGpcUVZVYFOB3oaw8i2ZGY-I5W69f"),
        2024: (18, 16, "PLtawHGpcUVZU8_ctxwdUNYAmuT9fuXFKQ"),
        2023: (12, 15, "PLtawHGpcUVZX6FQmzxe-FsANNlBcs5mIG"),
        2022: (16, 14, "PLtawHGpcUVZUkviKcn_g42YyYOCrlu_Kf"),
        2021: (15, 13, "PLtawHGpcUVZXeZYFVGQoc6AA7F66bvut3"),
        2020: (19, 9, "PLtawHGpcUVZUj3SFGm5jsPp4r1w5V1lMN"),
        2019: (14, 8, "PLtawHGpcUVZUeHXq2GyXqukHeE0zspirD"),
        2018: (16, None, "PLtawHGpcUVZU4KaAa00Nefim2Uxw9lZP6"),
        2017: (17, None, "PLtawHGpcUVZUnfybTrmnm0gvf3q9D0aCv"),
    }
    assert [edition["year"] for edition in DATA["editions"]] == list(sources)
    editions = {edition["year"]: edition for edition in DATA["editions"]}
    for year, (count, article, playlist) in sources.items():
        edition = editions[year]
        assert len(edition["tracks"]) == count, year
        if article is None:
            assert "source_url" not in edition
        else:
            assert edition["source_url"] == f"https://brunch.co.kr/@atelier/{article}"
        assert parse_qs(urlsplit(edition["playlist_url"]).query) == {"list": [playlist]}
        assert len({track["title"] for track in edition["tracks"]}) == count
        assert sum(bool(track.get("highlight")) for track in edition["tracks"]) == 1
    assert editions[2020]["published_at"] == "2021-03-07"
    remix = editions[2023]["tracks"][6]
    assert "BIBI, CAMO, MIRANI" in remix["title"] and remix["videos"][0]["id"] == "V6d2Uv0oylo"
    assert [video["id"] for video in editions[2022]["tracks"][5]["videos"]] == ["wPsVStI1M4w", "F_mMkeYQOMs"]
    assert editions[2019]["tracks"][12]["videos"] == [{"id": "YNy2LtJwaJ4", "label": "묶음 무대 듣기"}]
    # These playlist archives omit article links and selection reasons.
    playlist_videos = {
        2018: "Hj48NGuHmMQ uu8J3VC_YKY -gibBYpzZbI dk__IAfOXZE a5fCNNdmaQY -817KjHg144 fW1Cgv63naI VXdXGMWde90 DMcRIihAq9Q NhR2DSMiubw Xu56OVkwdXw awcOWsMqlnk MALHhpkwEw0 cM0lNp3RnVo rT5qaFEcZ3E JDkX8bF0MkY",
        2017: "jUSqBZNnMZ8 PkjaBCLOAec C2FVcsJpj2Q BLavT7w4R9I 9MbXeqCbS4Q 2l4M1ENOXX4 rXP9hU66i_U rIyJ6Zi4N4o WOYuBJFuz2E AXmN6lasHNg GLfFB3fCsBQ CGfa_FdihWs 0tkgGcnRNTE JSOBF_WhqEM rx16fAgz1yM NnRjwEhFU70 _ZkUb7iIOqQ",
    }
    for year, videos in playlist_videos.items():
        edition = editions[year]
        assert edition["published_at"] is None
        assert edition["tracks"][0]["highlight"]
        assert all("reason" not in track for track in edition["tracks"])
        assert [track["videos"][0]["id"] for track in edition["tracks"]] == videos.split()
    return {str(year): count for year, (count, _, _) in sources.items()}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def chart_state(page):
    return page.locator(".song-card").evaluate_all("""cards => cards.map(card => ({
        rank: card.querySelector('.rank').textContent,
        title: card.querySelector('.info h3').textContent,
        artist: card.querySelector('.info p').textContent,
        score: card.querySelector('.index-num').textContent,
        movement: card.querySelector('.trend')?.textContent,
        ranks: card.querySelector('.info p:last-child').textContent,
        links: [...card.querySelectorAll('.service-link')].map(a => a.href)
    }))""")


def ready(page, url):
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator(".song-card").first).to_be_attached(timeout=60000)
    page.wait_for_function("trendChartInstance && trendChartInstance.width > 0")


def no_horizontal_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("""() => ({
        viewport: innerWidth, width: document.documentElement.scrollWidth,
        containers: [...document.querySelectorAll('.editor-preview, .editor-preview-intro, .editor-preview-tracks, .tooltip, .controls')]
            .map(el => ({class: el.className, left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right,
                width: el.clientWidth, scrollWidth: el.scrollWidth, overflow: getComputedStyle(el).overflowX})),
        overflow: [...document.querySelectorAll('body *')]
            .filter(el => el.getBoundingClientRect().right > innerWidth && getComputedStyle(el).visibility !== 'hidden')
            .slice(0, 8).map(el => ({tag: el.tagName, class: el.className, right: el.getBoundingClientRect().right}))
    })""")
    for selector in (".editor-preview", ".editor-heading", ".editor-pick"):
        for box in page.locator(selector).evaluate_all("""elements => elements
            .filter(el => el.getClientRects().length)
            .map(el => ({left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right}))"""):
            assert box["left"] >= 0 and box["right"] <= page.viewport_size["width"], box


def verify(browser_name, output, base_url=None, baseline_ref="HEAD"):
    selection_counts = verify_archive_sources()
    server = None
    if base_url:
        base = base_url.rstrip("/") + "/"
    else:
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "docs")))
        Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/"
    original = subprocess.check_output(["git", "show", f"{baseline_ref}:docs/index.html"], cwd=ROOT, text=True)
    errors = []
    report = {"browser": browser_name, "base_url": base, "baseline_ref": baseline_ref,
              "viewports": [], "chart_comparisons": 0, "selection_counts": selection_counts}
    try:
        with sync_playwright() as playwright:
            browser = getattr(playwright, browser_name).launch()
            baseline = browser.new_page(viewport={"width": 1440, "height": 1100}, locale="ko-KR")
            baseline.on("pageerror", lambda error: errors.append(str(error)))
            baseline.route("**/baseline.html", lambda route: route.fulfill(body=original, content_type="text/html"))
            ready(baseline, base + "baseline.html")
            dates = baseline.locator("#date-selector option").evaluate_all("options => options.slice(0, 2).map(o => o.value)")
            expected = {}
            playlist_urls = {}
            for date in dates:
                baseline.locator("#date-selector").select_option(date)
                for kind in ("hype", "apple", "melon", "ytmusic"):
                    baseline.locator(f'.type-btn[data-value="{kind}"]').click()
                    expected[date, kind] = chart_state(baseline)
                    playlist_urls[kind] = baseline.locator("#yt-playlist-link").get_attribute("href")
            baseline.close()

            for width in (1440, 768, 390, 320):
                page = browser.new_page(viewport={"width": width, "height": 1100 if width > 600 else 1000}, locale="ko-KR", is_mobile=width <= 600, has_touch=width <= 600)
                page.on("pageerror", lambda error: errors.append(str(error)))
                ready(page, base)
                expect(page.locator(".version-label")).to_have_count(0)
                expect(page.get_by_text("v2", exact=True)).to_have_count(0)
                expect(page.locator("#editor-preview")).to_be_visible()
                height = page.locator("#editor-preview").bounding_box()["height"]
                assert height == (120 if width <= 600 else 116), height
                no_horizontal_overflow(page)
                dataset_count = page.evaluate("trendChartInstance.data.datasets.length")
                assert dataset_count == (5 if width <= 600 else 7), dataset_count

                for date in dates:
                    page.locator("#date-selector").select_option(date)
                    for kind in ("hype", "apple", "melon", "ytmusic"):
                        page.locator(f'.type-btn[data-value="{kind}"]').click()
                        assert chart_state(page) == expected[date, kind], (width, date, kind)
                        assert page.locator("#yt-playlist-link").get_attribute("href") == playlist_urls[kind]
                        report["chart_comparisons"] += 1
                preserved = chart_state(page)
                playlist = page.locator("#yt-playlist-link").get_attribute("href")
                verify_preview_motion(page, width)

                if width in (1440, 390):
                    page.locator("#date-selector").select_option(dates[0])
                    page.locator('.type-btn[data-value="hype"]').click()
                    page.evaluate("scrollTo(0, 0)")
                    page.wait_for_function("[...document.querySelectorAll('.song-card')].slice(0, 5).every(card => getComputedStyle(card).opacity === '1')")
                    page.screenshot(path=str(output / f"{width}-charts.png"))
                    page.locator("#date-selector").select_option(dates[-1])
                    page.locator('.type-btn[data-value="ytmusic"]').click()

                if width <= 600:
                    assert page.locator("#preview-tracks").evaluate("el => { el.scrollLeft = 160; return el.scrollLeft; }") > 0
                expect(page.locator("#preview-year option")).to_have_count(len(DATA["editions"]))
                expect(page.locator("#editor-year option")).to_have_count(len(DATA["editions"]))
                for edition in DATA["editions"]:
                    page.locator("#preview-year").select_option(str(edition["year"]))
                    ordered = sorted(edition["tracks"], key=lambda track: not track.get("highlight", False))
                    titles = page.locator(PREVIEW_GROUP + " strong")
                    assert titles.all_text_contents() == [track["title"] for track in ordered]
                    expect(page.locator(PREVIEW_GROUP + " .is-highlight strong")).to_have_count(1)
                    colors = titles.evaluate_all("elements => elements.map(el => getComputedStyle(el).color)")
                    choice_color = page.locator("#preview-title").evaluate("el => getComputedStyle(el).color")
                    assert colors[0] == choice_color and all(color != choice_color for color in colors[1:])
                    if "source_url" not in edition:
                        assert all("Selection Reason" not in label for label in page.locator(PREVIEW_GROUP + " a").evaluate_all("links => links.map(link => link.getAttribute('aria-label'))"))
                    assert page.locator(".editor-preview-copy a").evaluate_all("links => links.every(link => link.tabIndex === -1)")
                    expect(page.locator("#editor-view-all")).to_have_attribute("href", f"#editors-choice-{edition['year']}")
                page.locator("#preview-year").select_option("2024")
                assert page.locator("#preview-tracks").evaluate("el => el.scrollLeft") == 0
                expect(page.locator(PREVIEW_GROUP + " strong").first).to_have_text("Supernova")
                page.locator("#editor-view-all").click()
                expect(page.locator("#editors-view")).to_be_visible()
                expect(page.locator(".editor-pick")).to_have_count(18)
                expect(page.locator("#editor-year")).to_have_value("2024")
                expect(page.locator("#editor-actions a").last).to_have_attribute("href", "https://brunch.co.kr/@atelier/16")
                no_horizontal_overflow(page)

                page.locator("#editor-year").select_option("2025")
                expect(page.locator(".editor-pick")).to_have_count(16)
                expect(page.locator("#preview-year")).to_have_value("2025")
                expect(page.locator(".editor-reason.is-missing")).to_have_count(1)
                expect(page.locator(".editor-track-links a")).to_have_count(16)
                expect(page.locator(".editor-pick.is-highlight h3")).to_have_text("멸종위기사랑")
                assert all(page.locator(".editor-reason").all_text_contents())
                no_horizontal_overflow(page)
                if width in (1440, 390):
                    page.evaluate("scrollTo(0, 0)")
                    page.screenshot(path=str(output / f"{width}-editors-choice.png"))

                page.go_back()
                expect(page.locator("#editor-year")).to_have_value("2024")
                page.go_back()
                expect(page.locator("#charts-view")).to_be_visible()
                assert chart_state(page) == preserved
                assert page.locator("#yt-playlist-link").get_attribute("href") == playlist
                assert page.locator("#date-selector").input_value() == dates[-1]
                page.wait_for_function("trendChartInstance.width > 0")

                page.goto(base + "#editors-choice-2024", wait_until="domcontentloaded")
                expect(page.locator(".editor-pick")).to_have_count(18)
                page.locator("#charts-link").click()
                page.wait_for_function("trendChartInstance && trendChartInstance.width > 0")
                page.locator("#editors-link").click()
                for edition in reversed(DATA["editions"]):
                    year = edition["year"]
                    page.locator("#editor-year").select_option(str(year))
                    expect(page.locator("#editor-year-display")).to_have_text(str(year))
                    expect(page).to_have_url(base + f"#editors-choice-{year}")
                    assert page.locator(".editor-pick h3").all_text_contents() == [track["title"] for track in edition["tracks"]]
                    assert page.locator(".editor-reason").all_text_contents() == [track["reason"] or "No individual selection reason available." for track in edition["tracks"] if "reason" in track]
                    expect(page.locator("#editor-actions a").first).to_have_attribute("href", edition["playlist_url"])
                    if "source_url" in edition:
                        expect(page.locator("#editor-actions a")).to_have_count(2)
                        expect(page.locator("#editor-actions a").last).to_have_attribute("href", edition["source_url"])
                    else:
                        expect(page.locator("#editor-actions a")).to_have_count(1)
                        expect(page.locator(".editor-reason")).to_have_count(0)
                    if edition["published_at"] is None:
                        expect(page.locator("#editor-meta")).to_contain_text("Playlist archive")
                        expect(page.locator("#editor-meta")).not_to_contain_text("null")
                    else:
                        expect(page.locator("#editor-meta")).to_contain_text(edition["published_at"])
                    expect(page.locator(".editor-track-links a")).to_have_count(sum(max(1, len(track["videos"])) for track in edition["tracks"]))
                    no_horizontal_overflow(page)
                    if year in (2019, 2018, 2017) and width in (1440, 390):
                        page.locator("#editor-year").blur()
                        page.evaluate("scrollTo(0, 0)")
                        page.screenshot(path=str(output / f"{width}-editors-choice-{year}.png"))
                page.goto(base + "#editors-choice-2099", wait_until="domcontentloaded")
                expect(page.locator("#editor-year")).to_have_value("2025")
                expect(page).to_have_url(base + "#editors-choice-2025")
                report["viewports"].append({"width": width, "preview_height": height, "trend_tracks": dataset_count, "years": [edition["year"] for edition in DATA["editions"]], "overflow": False})
                page.close()

            page = browser.new_page(locale="ko-KR", reduced_motion="reduce")
            ready(page, base)
            page.locator("#preview-tracks").scroll_into_view_if_needed()
            page.mouse.move(1, 1)
            expect(page.locator("#preview-motion")).to_be_hidden()
            expect(page.locator(".editor-preview-copy")).to_be_hidden()
            expect(page.locator(PREVIEW_GROUP + " a")).to_have_count(16)
            page.wait_for_timeout(1500)
            assert page.locator("#preview-tracks").evaluate("el => el.scrollLeft") == 0
            assert page.locator("#preview-tracks").evaluate("el => { el.scrollLeft = 160; return el.scrollLeft; }") > 0
            assert abs(preview_drift(page)) <= 1
            page.close()

            # Optional fields still reject invalid values; retry must leave the daily chart intact.
            invalid_source = json.loads(json.dumps(DATA))
            invalid_source["editions"][-1]["source_url"] = "https://example.com/article"
            invalid_reason = json.loads(json.dumps(DATA))
            invalid_reason["editions"][-1]["tracks"][0]["reason"] = {"invalid": True}
            for invalid in (None, {"editions": []}, invalid_source, invalid_reason):
                page = browser.new_page(locale="ko-KR")
                recovered = [False]

                def response(route):
                    if recovered[0]:
                        route.fulfill(json=DATA)
                    elif invalid is None:
                        route.fulfill(status=503, body="Unavailable")
                    else:
                        route.fulfill(json=invalid)

                page.route("**/api/editors-choice.json", response)
                ready(page, base)
                assert chart_state(page) == expected[dates[0], "hype"]
                page.locator("#editors-link").click()
                expect(page.locator("#editor-retry")).to_be_visible()
                expect(page.locator("#editor-message")).to_contain_text("Failed to load selected tracks")
                recovered[0] = True
                page.locator("#editor-retry").click()
                expect(page.locator(".editor-pick")).to_have_count(16)
                page.close()

            page = browser.new_page(locale="ko-KR")
            page.route("**/api/history.json", lambda route: route.fulfill(status=503, json={}))
            page.goto(base + "#editors-choice-2025", wait_until="domcontentloaded")
            expect(page.locator(".editor-pick")).to_have_count(16)
            page.close()
            browser.close()
        assert not errors, errors
        report["checks"] = ["source counts, dates and recording versions", "chart parity with Git baseline", "dates and all four services", "trend graph", "all nine years in preview and archive", "all preview tracks, highlight first and exclusive gold title", "continuous flow and seamless wrap", "pause/resume, hover and keyboard focus", "reduced motion and manual scrolling", "selection reasons", "playlist archives without invented publication dates", "history back", "chart state retention", "deep links", "invalid year", "load failure and retry", "independent chart/editorial loading"]
        (output / f"{browser_name}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="webkit")
    parser.add_argument("--output", type=Path, default=ROOT / "validation/web-v2")
    parser.add_argument("--base-url", help="Check a deployed page instead of starting a local server")
    parser.add_argument("--baseline-ref", default="HEAD", help="Git ref containing the chart implementation to compare")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    verify(args.browser, args.output, args.base_url, args.baseline_ref)
