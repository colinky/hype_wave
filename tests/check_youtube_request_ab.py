"""Compare bootstrap libraries with identical headers and fixed cookies, without writes."""
from collections import Counter
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import time
from unittest.mock import MagicMock, Mock, patch

import requests
import check_youtube_cookie_ab as diagnostic

crawl = diagnostic.crawl
HEADER_LIMIT = 262144
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/26.4 Safari/605.1.15")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
           "Accept-Language": "en-US,en;q=0.9", "Connection": "keep-alive"}


def cookie_header(cookies):
    return "; ".join(sorted(f"{c['name']}={c['value']}" for c in cookies))


def cookie_digest(cookies):
    return hmac.new(os.environ["HYPE_DIAGNOSTIC_HMAC_KEY"].encode(),
                    cookie_header(cookies).encode(), hashlib.sha256).hexdigest()


def cookie_signature(cookies):
    return sorted((c["name"], c["value"], c["domain"], c["path"], c["secure"], c["httpOnly"],
                   c.get("expires", -1), c.get("sameSite", "Lax")) for c in cookies)


def bootstrap(library, page, cookies):
    headers = {**HEADERS, "Cookie": cookie_header(cookies)}
    if library == "playwright":
        response = page.context.request.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, headers=headers,
                                            timeout=30000, max_redirects=0, max_retries=0)
        status, text = response.status, response.text()
        header_rows = [(row["name"], row["value"]) for row in response.headers_array]
        response.dispose()
    else:
        assert library == "requests"
        with requests.Session() as session:
            session.headers.clear()
            response = session.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, headers=headers,
                                   timeout=30, allow_redirects=False)
            assert all(response.request.headers[k] == v for k, v in headers.items())
            status, text = response.status_code, response.text
            header_rows = list(response.raw.headers.items())
    assert status == 200, f"Bootstrap HTTP {status}"
    header_bytes = sum(len((name + ": " + value + "\r\n").encode()) for name, value in header_rows) + 2
    assert header_bytes <= HEADER_LIMIT, "Bootstrap headers exceed common 256 KiB limit"
    assert len(header_rows) <= 100, "Bootstrap exceeds common header-count limit"
    assert all(len((name + ": " + value + "\r\n").encode()) <= 65536 for name, value in header_rows)
    return crawl.extract_public_innertube_key(text), header_bytes


def prepare_trial(page, result, library, seed_key, cookies):
    assert page.evaluate("navigator.userAgent") == USER_AGENT
    page.context.add_cookies(cookies)
    assert cookie_signature(page.context.cookies()) == cookie_signature(cookies)
    initial_cookies = page.context.cookies([crawl.YOUTUBE_PUBLIC_CONFIG_URL])
    result["bootstrap_cookie_digest"] = cookie_digest(initial_cookies)
    key, size = bootstrap(library, page, initial_cookies)
    assert key == seed_key, "Public API key differs between the paired conditions"
    result["bootstrap_header_bytes"] = size
    # Discard newly issued cookies in both arms: browser state must be identical within each pair.
    page.context.clear_cookies()
    page.context.add_cookies(cookies)
    assert cookie_signature(page.context.cookies()) == cookie_signature(cookies)
    chart_cookies = page.context.cookies([crawl.DEFAULT_YOUTUBE_CHARTS_URL])
    assert chart_cookies, "Seed cookies do not apply to the chart host"
    result["expected_wire_digest"] = cookie_digest(chart_cookies)
    result["fixed_cookie_state_verified"] = True
    return key


def audit_wire(rows, wire_text):
    wire = [json.loads(line.split("COOKIE_AB_WIRE ", 1)[1])
            for line in wire_text.splitlines() if "COOKIE_AB_WIRE " in line]
    assert len(wire) == len(rows), "Missing or extra final request observations"
    for row, observed in zip(rows, wire):
        assert observed["headers_available"]
        assert hmac.compare_digest(observed["cookie_digest"], row["expected_wire_digest"]), "Actual cookies differ"
        row["wire_cookie_values_verified"] = True
        row["wire_cookie_names"] = sorted(observed["cookie_names"])


def self_test():
    diagnostic.self_test()
    os.environ["HYPE_DIAGNOSTIC_HMAC_KEY"] = "fixture-only"
    cookies = [{"name": "b", "value": "private-b"}, {"name": "a", "value": "private-a"}]
    assert cookie_digest(cookies) == cookie_digest(list(reversed(cookies)))
    assert cookie_digest(cookies) != cookie_digest(cookies[:1])
    rows = [{"expected_wire_digest": cookie_digest(cookies)}]
    observation = {"headers_available": True, "cookie_digest": cookie_digest(cookies), "cookie_names": ["b", "a"]}
    audit_wire(rows, "COOKIE_AB_WIRE " + json.dumps(observation))
    assert rows[0]["wire_cookie_values_verified"]
    observation["cookie_digest"] = cookie_digest(cookies[:1])
    try:
        audit_wire(rows, "COOKIE_AB_WIRE " + json.dumps(observation))
    except AssertionError:
        pass
    else:
        raise AssertionError("Changed wire cookies must fail verification")
    page = Mock()
    response = page.context.request.get.return_value
    response.status = 200
    response.text.return_value = 'ytcfg.set({"INNERTUBE_API_KEY":"fixture-key"});'
    response.headers_array = [{"name": "Content-Type", "value": "text/html"}]
    assert bootstrap("playwright", page, cookies)[0] == "fixture-key"
    assert page.context.request.get.call_args.kwargs["headers"] == {**HEADERS, "Cookie": cookie_header(cookies)}
    assert page.context.request.get.call_args.kwargs["max_retries"] == 0
    manager = MagicMock()
    client = manager.__enter__.return_value
    reply = client.get.return_value
    reply.status_code, reply.text = 200, response.text.return_value
    reply.request.headers = {**HEADERS, "Cookie": cookie_header(cookies)}
    reply.raw.headers.items.return_value = [("Content-Type", "text/html")]
    with patch.object(requests, "Session", return_value=manager):
        assert bootstrap("requests", None, cookies)[0] == "fixture-key"
    assert client.get.call_args.kwargs["headers"] == page.context.request.get.call_args.kwargs["headers"]
    response.headers_array = [{"name": "X-Large", "value": "x" * HEADER_LIMIT}]
    try:
        bootstrap("playwright", page, cookies)
    except AssertionError:
        pass
    else:
        raise AssertionError("Oversized headers must fail the common limit")
    print("Fixed headers, cookie equality, and wire-verification self-checks passed")


def main(block, wire_path):
    from playwright.sync_api import sync_playwright

    assert block in (1, 2, 3)
    assert os.environ.get("NODE_OPTIONS") == "--max-http-header-size=262144"
    os.environ["HYPE_DIAGNOSTIC_HMAC_KEY"] = secrets.token_hex(32)
    diagnostic.observe_wire_cookie_names()
    output = Path(f"diagnostic-artifacts/request-ab/block-{block}")
    output.mkdir(parents=True, exist_ok=True)
    report = dict(block=block, commit=os.environ.get("GITHUB_SHA"), run_id=os.environ.get("GITHUB_RUN_ID"),
                  runner=os.environ.get("RUNNER_NAME"), headers=HEADERS, header_limit=HEADER_LIMIT,
                  cookie_policy="identical values before bootstrap and chart navigation; discard response cookies",
                  trials=[])

    def save():
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with sync_playwright() as playwright:
        for pair in range((block - 1) * 4 + 1, block * 4 + 1):
            with requests.Session() as session:
                session.headers.clear()
                seed = session.get(crawl.YOUTUBE_PUBLIC_CONFIG_URL, headers=HEADERS, timeout=30, allow_redirects=False)
                assert seed.status_code == 200
                seed_key = crawl.extract_public_innertube_key(seed.text)
                cookies = diagnostic.browser_cookies(session.cookies)
            assert cookies, "No common seed cookies"
            order = ["playwright", "requests"] if pair % 2 else ["requests", "playwright"]
            for library in order:
                time.sleep(3)
                result = diagnostic.run_trial(
                    playwright, library, (pair - 1) * 2 + len(report["trials"]) % 2 + 1, output,
                    prepare=lambda page, row: prepare_trial(page, row, library, seed_key, cookies))
                result["pair"] = pair
                report["trials"].append(result)
                save()
    assert len(report["trials"]) == 8
    assert all(row["outcome"] in {"verified_chart", "interactive_recaptcha", "response_timeout"}
               for row in report["trials"]), "A controlled trial failed before its outcome was observed"
    audit_wire(report["trials"], Path(wire_path).read_text())
    for pair in range(0, len(report["trials"]), 2):
        left, right = report["trials"][pair:pair + 2]
        assert left["bootstrap_cookie_digest"] == right["bootstrap_cookie_digest"]
        assert left["expected_wire_digest"] == right["expected_wire_digest"]
    save()
    counts = Counter((r["mode"], r["outcome"]) for r in report["trials"])
    summary = f"Block {block}: cookie values verified on all 8 final requests.\n\n"
    summary += "| Library | Outcome | Count |\n|---|---|---|\n" + "\n".join(
        f"| {library} | {outcome} | {count} |" for (library, outcome), count in sorted(counts.items()))
    print("LIBRARY_AB_AUDIT " + json.dumps({"block": block, "wire_verified": 8, "counts": dict(
        (library + ":" + outcome, count) for (library, outcome), count in counts.items())}), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write(summary + "\n\n")


if __name__ == "__main__":
    self_test()
    if "--self-test" not in sys.argv:
        main(int(sys.argv[1]), sys.argv[2])
