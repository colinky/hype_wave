"""List unapproved Spotify/Apple display-name pairs from existing chart data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hype_db import connect
from sync_all import load_env_file
from ytmusic_playlist_sync import artist_name_key


def extract_candidates(rows):
    groups = {}
    for row in rows:
        ko = row["artist_ko"].strip()
        en = row["artist_en"].strip()
        pair = artist_name_key(ko), artist_name_key(en)
        if not all(pair) or pair[0] == pair[1]:
            continue
        group = groups.setdefault(pair, {"names": [ko, en], "observations": []})
        group["observations"].append({
            "service": row["service"], "song_id": row["song_id"],
            "title_ko": row["title_ko"], "title_en": row["title_en"],
        })
    return [groups[key] for key in sorted(groups)]


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "diagnostic-artifacts/artist-alias-candidates.json")
    args = parser.parse_args()
    load_env_file(root / ".env")
    load_env_file(root / ".secrets/.env")
    with connect(root / "hype_wave_data.db", read_only=True) as db:
        rows = db.execute("""
            SELECT service, song_id, title_ko, title_en, artist_ko, artist_en FROM track_list
            WHERE service IN ('apple', 'spotify') AND artist_ko <> '' AND artist_en <> ''
            ORDER BY service, song_id
        """).fetchall()
    candidates = extract_candidates(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(candidates)} candidate name pairs written to {args.output}")
