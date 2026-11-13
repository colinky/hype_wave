"""Read-only, same-runner cookie experiment; never persist cookie values or keys."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import ytmusic_to_ytmusic_crawl as crawl


def browser_cookies(jar):
    cookies = []
    for cookie in jar:
        row = dict(name=cookie.name, value=cookie.value, domain=cookie.domain,
                   path=cookie.path, secure=cookie.secure,
                   httpOnly=cookie.has_nonstandard_attr("HttpOnly"))
        if cookie.expires is not None:
            row["expires"] = cookie.expires
        same_site = cookie.get_nonstandard_attr("SameSite")
        if same_site and same_site.capitalize() in {"Strict", "Lax", "None"}:
            row["sameSite"] = same_site.capitalize()
        cookies.append(row)
    return cookies


def cookie_metadata(cookies):
    return sorted([{k: v for k, v in cookie.items() if k != "value"}
                   for cookie in cookies], key=lambda row: (row["domain"], row["name"]))


def is_browse(url):
    parsed = urlparse(url)
    return (parsed.scheme, parsed.netloc, parsed.path) == (
        "https", "charts.youtube.com", "/youtubei/v1/browse")


def observe_wire_cookie_names():
    import playwright

    source = Path(playwright.__file__).parent / "driver/package/lib/server/webkit/wkInterceptableRequest.js"
    text = source.read_text()
    marker = "    const setCookieSeparator = process.platform"
    assert text.count(marker) == 1, "Unexpected pinned Playwright source; cannot instrument safely"
    # Routed WebKit requests expose provisional headers. Observe final protocol metadata only.
    observer = '''    if (this.request.url().split("?")[0] === "https://charts.youtube.com/charts/TopSongs/kr/weekly") {
      const entry = Object.entries(responsePayload.requestHeaders || {}).find(([name]) => name.toLowerCase() === "cookie");
      console.error("COOKIE_AB_WIRE " + JSON.stringify({
        headers_available: !!responsePayload.requestHeaders,
        cookie_names: (entry ? entry[1] : "").split(";").map(item => item.trim().split("=")[0]).filter(Boolean)
      }));
    }
'''
    source.write_text(text.replace(marker, observer + marker))


def run_trial(playwright, mode, trial, output, bootstrap=None):
    print(f"COOKIE_AB_START {trial} {mode}", flush=True)
    result = dict(trial=trial, mode=mode, started_at=datetime.now(timezone.utc).isoformat(),
                  chart_requests=0, chart_responses=0, challenge_visible=False)
    started = time.monotonic()
    key = ""
    stage = "bootstrap"
    browser = playwright.webkit.launch(headless=True)
    result["browser_version"] = browser.version
    try:
        page = browser.new_page(locale="en-US")
        page.set_default_timeout(45000)
        if mode == "legacy_context_request":
            response = page.context.request.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL,
                                                timeout=30000, max_redirects=0)
            result["bootstrap_status"] = response.status
            if response.status != 200:
                raise ValueError("Initial configuration did not return HTTP 200")
            key = crawl.extract_public_innertube_key(response.text())
        else:
            key, cookies = bootstrap
            if mode == "requests_shared":
                page.context.add_cookies(cookies)
                actual = {(c["name"], c["domain"], c["path"]): c["value"]
                          for c in page.context.cookies()}
                assert all(actual.get((c["name"], c["domain"], c["path"])) == c["value"]
                           for c in cookies), "Cookie transfer did not preserve values"
            else:
                assert mode == "requests_unshared"
                assert page.context.cookies() == [], "Unshared context must start empty"
        result["cookies_before_navigation"] = cookie_metadata(page.context.cookies())
        result["cookies_applicable_to_chart"] = cookie_metadata(
            page.context.cookies([crawl.DEFAULT_YOUTUBE_CHARTS_URL]))
        if mode == "requests_shared":
            assert result["cookies_applicable_to_chart"], "Shared cookies do not apply to the chart host"
        stage = "navigation"
        responses = []
        result["recaptcha_responses"] = []
        result["script_paths"] = []

        def on_request(request):
            if is_browse(request.url):
                result["chart_requests"] += 1
            if request.url == crawl.DEFAULT_YOUTUBE_CHARTS_URL:
                header = request.all_headers().get("cookie", "")
                result["navigation_cookie_names"] = sorted(
                    item.strip().split("=", 1)[0] for item in header.split(";") if item.strip())

        def on_response(response):
            parsed = urlparse(response.url)
            if is_browse(response.url):
                responses.append(response)
                result["chart_responses"] += 1
            elif "/recaptcha/" in parsed.path:
                result["recaptcha_responses"].append(
                    {"host": parsed.netloc, "path": parsed.path, "status": response.status})
            elif parsed.netloc == "charts.youtube.com" and parsed.path.endswith(".js"):
                result["script_paths"].append(parsed.path)

        page.on("request", on_request)
        page.on("response", on_response)
        page.route("https://charts.youtube.com/youtubei/v1/browse*",
                   lambda route: route.continue_(url=crawl.chart_request_with_key(route.request.url, key)))
        deadline = time.monotonic() + 45
        navigation = page.goto(crawl.DEFAULT_YOUTUBE_CHARTS_URL, wait_until="domcontentloaded")
        result["page_status"] = navigation.status if navigation else None
        result["user_agent"] = page.evaluate("navigator.userAgent")
        challenge = page.locator('iframe[src*="/recaptcha/"][src*="/bframe"]:visible')
        while not responses and time.monotonic() < deadline:
            if challenge.count():
                # A stable visible challenge distinguishes user interaction from a transient iframe.
                page.wait_for_timeout(2000)
                if challenge.count() and not responses:
                    result["challenge_visible"] = True
                    break
            page.wait_for_timeout(100)
        if responses:
            stage = "validation"
            response = responses[0]
            result["chart_status"] = response.status
            if response.status != 200:
                raise ValueError(f"Chart response HTTP {response.status}")
            rows = crawl.extract_chart_entries_from_browser_json(response.json())
            crawl.validate_weekly_chart(rows)
            start, end = crawl.fetched_period_from_entries(rows)
            start_date, end_date = datetime.fromisoformat(start), datetime.fromisoformat(end)
            label = start_date.strftime("%b %-d")
            if start_date.year != end_date.year:
                label += f", {start_date.year}"
            last = end_date.strftime("%b %-d") if start_date.month != end_date.month else str(end_date.day)
            expected = f"{label}–{last}, {end_date.year}"
            actual = page.locator(".weekly-chart-calendar-selector #paper-button-container").inner_text()
            assert re.sub(r"\s", "", actual) == re.sub(r"\s", "", expected), "Displayed period mismatch"
            result.update(outcome="verified_chart", ranks=len(rows), period=[start, end])
        else:
            result["outcome"] = "interactive_recaptcha" if result["challenge_visible"] else "response_timeout"
        try:
            page.screenshot(path=str(output / f"{trial:02d}-{mode}.png"), timeout=5000)
        except Exception as exc:
            result["screenshot_error_type"] = type(exc).__name__
    except Exception as exc:
        result["outcome"] = f"{stage}_error"
        # Playwright exceptions can include headers on later lines; retain only the first line.
        message = str(exc).splitlines()[0]
        result["error"] = message.replace(key, "<public-key>") if key else message
        result["error_type"] = type(exc).__name__
    finally:
        browser.close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print("COOKIE_AB_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def self_test():
    jar = requests.cookies.RequestsCookieJar()
    jar.set_cookie(requests.cookies.create_cookie(
        "fixture", "not-for-logs", domain=".youtube.com", path="/", secure=True,
        expires=2000000000, rest={"HttpOnly": None, "SameSite": "None"}))
    jar.set_cookie(requests.cookies.create_cookie("session", "secret", domain="www.youtube.com", rest={}))
    converted = browser_cookies(jar)
    assert converted[0] == dict(name="fixture", value="not-for-logs", domain=".youtube.com",
                                path="/", secure=True, expires=2000000000, httpOnly=True, sameSite="None")
    assert "expires" not in converted[1] and not converted[1]["httpOnly"]
    public = json.dumps(cookie_metadata(converted))
    assert "not-for-logs" not in public and "secret" not in public and '"value"' not in public
    assert is_browse("https://charts.youtube.com/youtubei/v1/browse?key=fixture")
    assert not is_browse("https://example.com/youtubei/v1/browse")
    assert not is_browse("http://charts.youtube.com/youtubei/v1/browse")
    print("Cookie conversion, log redaction, and endpoint checks passed")


def main():
    from playwright.sync_api import sync_playwright

    observe_wire_cookie_names()
    output = Path("diagnostic-artifacts/cookie-ab")
    output.mkdir(parents=True, exist_ok=True)
    report = dict(commit=os.environ.get("GITHUB_SHA"), run_id=os.environ.get("GITHUB_RUN_ID"),
                  runner=os.environ.get("RUNNER_NAME"), pairs=[], trials=[])

    def save():
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with sync_playwright() as playwright:
        report["trials"].append(run_trial(playwright, "legacy_context_request", 1, output))
        save()
        for pair in range(1, 5):
            with crawl.get_resilient_session(backoff_factor=2) as session:
                response = session.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, timeout=30, allow_redirects=False)
                if response.status_code != 200:
                    raise ValueError(f"Pair {pair} bootstrap HTTP {response.status_code}")
                key = crawl.extract_public_innertube_key(response.text)
                cookies = browser_cookies(session.cookies)
            assert cookies, "No bootstrap cookies; cookie comparison would be inconclusive"
            report["pairs"].append(dict(pair=pair, cookie_metadata=cookie_metadata(cookies)))
            order = ["requests_unshared", "requests_shared"]
            if pair % 2 == 0:
                order.reverse()
            for mode in order:
                time.sleep(3)
                result = run_trial(playwright, mode, len(report["trials"]) + 1, output, (key, cookies))
                result["pair"] = pair
                report["trials"].append(result)
                save()
        time.sleep(3)
        report["trials"].append(run_trial(playwright, "legacy_context_request", 10, output))
        save()
    counts = Counter((row["mode"], row["outcome"]) for row in report["trials"])
    summary = "| Mode | Outcome | Count |\n|---|---|---|\n" + "\n".join(
        f"| {mode} | {outcome} | {count} |" for (mode, outcome), count in sorted(counts.items()))
    print(summary, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(summary + "\n", encoding="utf-8")
    assert len(report["trials"]) == 10
    # Challenge/timeout are valid observations; infrastructure or validation errors are not.
    assert all(row["outcome"] in {"verified_chart", "interactive_recaptcha", "response_timeout"}
               for row in report["trials"] if row["mode"] != "legacy_context_request"), "Controlled trial error"


if __name__ == "__main__":
    self_test()
    if "--self-test" not in sys.argv:
        main()
