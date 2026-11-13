"""Read-only crossed comparison of native bootstrap headers and newly issued cookies."""
from collections import Counter
import json
import os
from pathlib import Path
import secrets
import sys
import time

import check_youtube_cookie_ab as diagnostic
import check_youtube_request_ab as controls

crawl = diagnostic.crawl
HEADER_NAMES = {"user-agent", "accept", "accept-encoding", "accept-language", "connection"}
CELLS = [(header, cookies) for header in ("playwright_default", "requests_default")
         for cookies in ("none", "issued_playwright", "issued_requests")]


def observe_bootstrap_headers():
    import playwright

    source = Path(playwright.__file__).parent / "driver/package/lib/server/fetch.js"
    text = source.read_text()
    if "FACTOR_BOOTSTRAP_HEADERS " in text:
        assert text.count("FACTOR_BOOTSTRAP_HEADERS ") == 1
        return
    marker = "      request.end();"
    assert text.count(marker) == 1
    observer = '''      if (url.hostname === "www.youtube.com" && url.pathname === "/") {
        request.once("finish", () => console.error("FACTOR_BOOTSTRAP_HEADERS " + JSON.stringify(
          Object.fromEntries(Object.entries(request.getHeaders()).filter(([name]) =>
            ["user-agent", "accept", "accept-encoding", "accept-language", "connection"].includes(name.toLowerCase()))))));
      }
'''
    source.write_text(text.replace(marker, observer + marker))


def last_headers(wire_path):
    lines = [line.split("FACTOR_BOOTSTRAP_HEADERS ", 1)[1]
             for line in Path(wire_path).read_text().splitlines() if "FACTOR_BOOTSTRAP_HEADERS " in line]
    assert lines, "No outgoing bootstrap header observation"
    return json.loads(lines[-1])


def response_key(response):
    assert response.status == 200, f"Bootstrap HTTP {response.status}"
    rows = response.headers_array
    size = sum(len((r["name"] + ": " + r["value"] + "\r\n").encode()) for r in rows) + 2
    assert size <= controls.HEADER_LIMIT and len(rows) <= 100
    assert all(len((r["name"] + ": " + r["value"] + "\r\n").encode()) <= 65536 for r in rows)
    key = crawl.extract_public_innertube_key(response.text())
    response.dispose()
    return key, size


def native_profiles(playwright, wire_path):
    browser = playwright.webkit.launch(headless=True)
    try:
        page = browser.new_page(locale="en-US")
        response = page.context.request.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL,
                                            timeout=30000, max_redirects=0, max_retries=0)
        response_key(response)
        previous = last_headers(wire_path)
        assert previous["user-agent"] == page.evaluate("navigator.userAgent")
    finally:
        browser.close()
    with crawl.get_resilient_session(backoff_factor=2) as session:
        response = session.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, timeout=30, allow_redirects=False)
        assert response.status_code == 200
        current = {name.lower(): value for name, value in response.request.headers.items()}
    assert set(previous) <= HEADER_NAMES and set(current) <= HEADER_NAMES
    assert previous != current, "Native header profiles unexpectedly match"
    return {"playwright_default": previous, "requests_default": current}


def issued_cookies(playwright, profile, wire_path):
    # One transport for both profiles prevents TLS/library differences from confounding header effects.
    browser = playwright.webkit.launch(headless=True)
    try:
        page = browser.new_page(locale="en-US")
        assert page.context.cookies() == []
        response = page.context.request.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, headers=profile,
                                            timeout=30000, max_redirects=0, max_retries=0)
        key, size = response_key(response)
        assert last_headers(wire_path) == profile
        cookies = page.context.cookies()
        assert cookies and page.context.cookies([crawl.DEFAULT_YOUTUBE_CHARTS_URL])
        return key, cookies, size
    finally:
        browser.close()


def prepare(page, result, profile, key, cookies, wire_path):
    page.context.add_cookies(cookies)
    assert controls.cookie_signature(page.context.cookies()) == controls.cookie_signature(cookies)
    result["bootstrap_cookie_digest"] = controls.cookie_digest(page.context.cookies([crawl.YOUTUBE_PUBLIC_CONFIG_URL]))
    response = page.context.request.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, headers=profile,
                                        timeout=30000, max_redirects=0, max_retries=0)
    actual_key, size = response_key(response)
    assert actual_key == key
    assert last_headers(wire_path) == profile, "Actual bootstrap headers differ from the assigned profile"
    # Apply the assigned set in both arms, excluding another uncontrolled cookie issuance.
    page.context.clear_cookies()
    page.context.add_cookies(cookies)
    assert controls.cookie_signature(page.context.cookies()) == controls.cookie_signature(cookies)
    result["expected_wire_digest"] = controls.cookie_digest(page.context.cookies([crawl.DEFAULT_YOUTUBE_CHARTS_URL]))
    result["actual_bootstrap_headers"] = last_headers(wire_path)
    result["bootstrap_header_bytes"] = size
    result["fixed_cookie_state_verified"] = True
    return key


def order_for_round(round_number):
    # Rotate positions and reverse the two extra rounds to limit order effects.
    order = [CELLS[(i + (round_number - 1) % 6) % 6] for i in (0, 1, 5, 2, 4, 3)]
    return list(reversed(order)) if round_number > 6 else order


def self_test():
    controls.self_test()
    for round_number in range(1, 9):
        assert len(order_for_round(round_number)) == 6
        assert set(order_for_round(round_number)) == set(CELLS)
    assert all(Counter(cell for n in range(1, 9) for cell in order_for_round(n))[cell] == 8 for cell in CELLS)
    print("Crossed header/cookie assignments and privacy checks passed")


def main(block, wire_path):
    from playwright.sync_api import sync_playwright

    assert block in (1, 2, 3, 4)
    assert os.environ.get("NODE_OPTIONS") == "--max-http-header-size=262144"
    os.environ["HYPE_DIAGNOSTIC_HMAC_KEY"] = secrets.token_hex(32)
    diagnostic.observe_wire_cookie_names()
    observe_bootstrap_headers()
    output = Path(f"diagnostic-artifacts/header-cookie-factors/block-{block}")
    output.mkdir(parents=True, exist_ok=True)
    report = dict(block=block, commit=os.environ.get("GITHUB_SHA"), run_id=os.environ.get("GITHUB_RUN_ID"),
                  runner=os.environ.get("RUNNER_NAME"), transport="Playwright APIRequestContext throughout",
                  header_limit=controls.HEADER_LIMIT, cookie_sets=[], trials=[])

    def save():
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with sync_playwright() as playwright:
        profiles = native_profiles(playwright, wire_path)
        report["native_profiles"] = profiles
        print("FACTOR_NATIVE_PROFILES " + json.dumps(profiles), flush=True)
        for round_number in range((block - 1) * 2 + 1, block * 2 + 1):
            sets = {"none": []}
            keys = []
            source_order = list(profiles) if round_number % 2 else list(reversed(profiles))
            for name in source_order:
                key, cookies, size = issued_cookies(playwright, profiles[name], wire_path)
                sets["issued_playwright" if name == "playwright_default" else "issued_requests"] = cookies
                keys.append(key)
            assert keys[0] == keys[1]
            assert controls.cookie_signature(sets["issued_playwright"]) != controls.cookie_signature(sets["issued_requests"])
            report["cookie_sets"].append({"round": round_number, "sets": {
                name: {"metadata": diagnostic.cookie_metadata(cookies), "digest": controls.cookie_digest(cookies)}
                for name, cookies in sets.items()}})
            for position, (header_name, cookie_name) in enumerate(order_for_round(round_number)):
                time.sleep(3)
                trial = (round_number - 1) * 6 + position + 1
                result = diagnostic.run_trial(
                    playwright, header_name + "__" + cookie_name, trial, output,
                    prepare=lambda page, row: prepare(page, row, profiles[header_name], keys[0], sets[cookie_name], wire_path))
                result.update(round=round_number, header_profile=header_name, cookie_condition=cookie_name)
                report["trials"].append(result)
                save()
    assert len(report["trials"]) == 12
    assert all(r["outcome"] in {"verified_chart", "interactive_recaptcha", "response_timeout"}
               for r in report["trials"]), "Controlled trial error"
    controls.audit_wire(report["trials"], Path(wire_path).read_text())
    for round_number in range((block - 1) * 2 + 1, block * 2 + 1):
        for condition in sets:
            pair = [r for r in report["trials"] if r["round"] == round_number and r["cookie_condition"] == condition]
            assert len(pair) == 2 and pair[0]["expected_wire_digest"] == pair[1]["expected_wire_digest"]
            assert pair[0]["bootstrap_cookie_digest"] == pair[1]["bootstrap_cookie_digest"]
    save()
    counts = Counter((r["header_profile"], r["cookie_condition"], r["outcome"]) for r in report["trials"])
    summary = f"Block {block}: 12 actual cookie transmissions and bootstrap header assignments verified.\n\n"
    summary += "| Headers | Cookies | Outcome | Count |\n|---|---|---|---|\n" + "\n".join(
        f"| {header} | {cookie} | {outcome} | {count} |" for (header, cookie, outcome), count in sorted(counts.items()))
    print("FACTOR_AB_AUDIT " + json.dumps({"block": block, "wire_verified": 12,
        "counts": {"|".join(cell): count for cell, count in counts.items()}}), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write(summary + "\n\n")


if __name__ == "__main__":
    self_test()
    if "--self-test" not in sys.argv:
        main(int(sys.argv[1]), sys.argv[2])
