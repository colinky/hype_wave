"""Read-only live collection and outage check; report cached provenance explicitly."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ytmusic_to_ytmusic_crawl as crawl


def main(output=None):
    with tempfile.TemporaryDirectory() as directory:
        snapshot_dir = Path(directory) / "snapshots"
        shutil.copytree(crawl.SNAPSHOT_DIR, snapshot_dir)
        with patch.object(crawl, "SNAPSHOT_DIR", snapshot_dir):
            return verify_collection(output)


def verify_collection(output):
    normal_entries = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
    crawl.validate_weekly_chart(normal_entries)
    crawl.save_weekly_snapshot(normal_entries)
    with patch.object(crawl.http_session, "get", side_effect=requests.HTTPError("simulated HTTP/API outage")):
        entries = crawl.extract_chart_entries_from_youtube_charts(crawl.DEFAULT_YOUTUBE_CHARTS_URL)
    crawl.validate_weekly_chart(entries)
    assert all(row["source"] == "youtube_charts_weekly_browse_api" for row in entries)
    assert crawl.fetched_period_from_entries(normal_entries) == crawl.fetched_period_from_entries(entries)
    assert [(row["rank"], row["video_id"]) for row in normal_entries] == [(row["rank"], row["video_id"]) for row in entries]
    if output:
        Path(output).write_text(json.dumps({"normal": normal_entries, "http_outage": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    for label, rows in (("normal", normal_entries), ("http_outage", entries)):
        print(f"Official weekly collection verified: mode={label} method={rows[0]['collection_method']} country=kr "
              f"ranks=1..100 period={crawl.fetched_period_from_entries(rows)} "
              f"original_capture={rows[0].get('source_collected_at', 'live response')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
