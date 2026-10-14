"""Opt-in live check: force HTTP failure, collect via browser, never write DB/playlists."""
from pathlib import Path
import json
import sys
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ytmusic_to_ytmusic_crawl as crawl


def main(output=None):
    with patch.object(crawl.http_session, "get", side_effect=requests.HTTPError("simulated HTTP/API outage")):
        entries = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
    crawl.validate_weekly_chart(entries)
    assert all(row["source"] == "youtube_charts_weekly_browse_api" for row in entries)
    if output:
        Path(output).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Browser fallback verified: country=kr ranks=1..100 period={crawl.fetched_period_from_entries(entries)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
