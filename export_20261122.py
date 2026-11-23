"""Assemble the November 22 edition from completed source snapshots."""
import hashlib
import json
from pathlib import Path

from hype_db_reports import (
    build_hype_report_from_rows, compact_frontend_history, connect, fetch_hype_rows_for_dates,
    inflate_frontend_history, prune_history, validate_frontend_history,
)

DATE = "2026-11-22"
SOURCE = "2026-11-21"
path = Path("docs/api/history.json")
before = json.loads(path.read_bytes())
assert before["dates"][0] == SOURCE
validate_frontend_history(before, SOURCE)
with connect("hype_wave_data.db", read_only=True) as conn:
    rows = fetch_hype_rows_for_dates(conn, [DATE])[DATE]
for job, variant in [("KR-Top-100", "default"), ("Top-100-Daily", "default"),
                     ("Gen-Z-Daily", "combined"), ("Weekly-Hot-100", "default")]:
    selected = [r for r in rows if r["job_name"] == job and r["source_variant"] == variant]
    periods = sorted({r["reference_period"] for r in selected})
    print(f"INPUT {job} {variant} period={periods} identified_rows={len(selected)}", flush=True)
    if job != "Weekly-Hot-100":
        assert periods == [SOURCE], f"Missing November 21 completed input: {job}"
    else:
        assert len(periods) == 1, "Missing completed weekly input"
    if job in {"Top-100-Daily", "Gen-Z-Daily"}:
        assert sorted(r["rank_order"] for r in selected) == list(range(1, 101))

# Carry the original single explicitly selected by the preceding main commit.
old_id, original_id = "AdUKC_vbyRs", "Q8a1Im3LIQo"
original = before["tracks"][original_id]
for index, row in enumerate(rows):
    if row["video_id"] == old_id:
        rows[index] = {**dict(row), "video_id": original_id,
                       **{k: original[k] for k in ("yt_title", "yt_artist", "yt_album")}}
report = build_hype_report_from_rows(rows)
assert len(report) == len({r["video_id"] for r in report})
history = inflate_frontend_history(before)
history[DATE] = report
result = compact_frontend_history(prune_history(history, reference_date=DATE, days=31))
if original_id in result["tracks"]:
    result["tracks"][original_id] = original
assert old_id not in result["tracks"] and result["dates"][0] == DATE
for day in result["dates"]:
    validate_frontend_history(result, day)
    if day != DATE:
        assert result["rankings"][day] == before["rankings"][day]
assert set(result["tracks"]) == {video for daily in result["rankings"].values() for video in daily}
path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
assert json.loads(path.read_bytes()) == result
print("EDITION", DATE, "songs", len(report), "days", len(result["dates"]), "oldest", result["dates"][-1])
for row in report:
    if row.get("melon_genz_rank") == 42:
        print("GEN42", row["title"], row["artist"], "hype_rank", row["hype_rank"])
print("FILE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
