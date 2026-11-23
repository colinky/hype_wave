"""Assemble the November 22 edition from completed source snapshots."""
import hashlib
import json
import logging
import os
from pathlib import Path

from crawler_common import process_matching_pipeline
from ytmusic_playlist_sync import SourceTrack, make_ytmusic
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
logging.basicConfig(level=logging.INFO)
with connect("hype_wave_data.db", read_only=True) as conn:
    rows = fetch_hype_rows_for_dates(conn, [DATE])[DATE]
    apple = [dict(r) for r in conn.execute("""
        SELECT p.*, tl.album_id, tl.title_en, tl.artist_en, tl.album_en, tl.artwork_url,
               COALESCE(NULLIF(tl.title_ko,''), tl.title_en) AS title,
               COALESCE(NULLIF(tl.artist_ko,''), tl.artist_en) AS artist,
               COALESCE(NULLIF(tl.album_ko,''), tl.album_en) AS album,
               ps.track_uid, t.canonical_yt_video_id AS video_id, t.yt_title, t.yt_artist, t.yt_album
        FROM playlist_order p LEFT JOIN track_list tl ON tl.service=p.service AND tl.song_id=p.song_id
        LEFT JOIN platform_song_ids ps ON ps.service=p.service AND ps.song_id=p.song_id
        LEFT JOIN tracks t ON t.track_uid=ps.track_uid
        WHERE p.service='apple' AND p.job_name='KR-Top-100' AND p.source_variant='default'
          AND p.reference_period=? ORDER BY p.rank_order
        """, (SOURCE,)).fetchall()]

assert [r["rank_order"] for r in apple] == list(range(1, 101))
assert len({r["song_id"] for r in apple}) == 100
# The original run committed the 100 source slots before a YouTube metadata outage.
# Replay matching on those slots with the normal policy and read-only DB connections.
if not any(r["job_name"] == "KR-Top-100" and r["reference_period"] == SOURCE for r in rows):
    tracks, korean = [], {}
    for r in apple:
        assert r["title"] and r["artist"]
        keys = dict(rank=r["rank_order"], service="apple", song_id=r["song_id"], album_id=r["album_id"] or "",
                    artwork_url=r["artwork_url"] or "")
        tracks.append(SourceTrack(**keys, title=r["title_en"] or r["title"], artist=r["artist_en"] or r["artist"],
                                  album=r["album_en"] or r["album"] or "", locale="en"))
        korean[r["song_id"]] = SourceTrack(**keys, title=r["title"], artist=r["artist"], album=r["album"] or "", locale="ko")
    ids = process_matching_pipeline(all_tracks=tracks, raw_tracks=tracks, tracks_ko_map=korean,
        ytmusic=make_ytmusic(os.environ["YTMUSIC_AUTH_FILE"]), db_path=Path("hype_wave_data.db"),
        service="apple", job_name="KR-Top-100", update_date_str=DATE, started_at=DATE, reference_period=SOURCE,
        dry_run=True)
    assert len(ids) == len(set(ids)) == len(tracks), "Could not verify all Apple source slots"
    for row, video_id in zip(apple, ids, strict=True):
        if row["video_id"] != video_id:
            row.update(yt_title="", yt_artist="", yt_album="")
        row.update(video_id=video_id, chart_date=DATE)
    rows = [r for r in rows if r["job_name"] != "KR-Top-100"] + apple
    rows.sort(key=lambda r: (r["service"], r["job_name"], r["source_variant"], r["rank_order"], r["song_id"]))
    print("APPLE_REPLAY all 100 archived source slots matched in rank order, read-only", flush=True)
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

# Preserve the two recording decisions already reviewed on main.
originals = {"AdUKC_vbyRs": "Q8a1Im3LIQo", "ZrPE-8vPPRQ": "h0b4nw7NDhA"}
for index, row in enumerate(rows):
    if row["video_id"] in originals:
        original_id = originals[row["video_id"]]
        original = before["tracks"][original_id]
        rows[index] = {**dict(row), "video_id": original_id,
                       **{k: original[k] for k in ("yt_title", "yt_artist", "yt_album")}}
report = build_hype_report_from_rows(rows)
assert len(report) == len({r["video_id"] for r in report})
history = inflate_frontend_history(before)
history[DATE] = report
result = compact_frontend_history(prune_history(history, reference_date=DATE, days=31))
for old_id, original_id in originals.items():
    if original_id in result["tracks"]:
        result["tracks"][original_id] = before["tracks"][original_id]
    assert old_id not in result["tracks"]
assert result["dates"][0] == DATE
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
