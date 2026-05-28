#!/usr/bin/env python3
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ytmusic_playlist_sync import (
    make_ytmusic,
    update_ytmusic_playlist,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("hypex_aggregator")

def main():
    parser = argparse.ArgumentParser(description="Generate Hypex aggregated playlist.")
    parser.add_argument("--logs-dir", default="logs", help="Directory containing crawl logs")
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--yt-playlist-id", required=True, help="Target YouTube Music Playlist ID")
    parser.add_argument("--job-name", default="Hype-Wave-Daily")
    parser.add_argument("--playlist-name", default="Hype Wave Daily")
    parser.add_argument("--limit", type=int, default=100, help="Number of songs to include")
    parser.add_argument("--yt-auth", default=".secrets/browser.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logs_path = Path(args.logs_dir)
    db_path = Path(args.db_path).expanduser()
    
    # 1. WAVE 판별을 위해 어제 히스토리에서 애플 차트 곡 ID 추출
    history_file = Path("docs/api/history.json")
    previous_apple_videos = set()
    history_data = {}
    
    kst_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    today_str = kst_now.strftime("%Y-%m-%d")

    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history_data = json.load(f)
                # 오늘 날짜를 제외하고 가장 최근의 과거 날짜를 찾습니다 (하루 여러 번 실행 대응)
                past_dates = sorted([d for d in history_data.keys() if d < today_str], reverse=True)
                if past_dates:
                    # 가장 최근 과거 날짜의 데이터에서 애플 순위가 있는 곡들만 수집
                    last_record = history_data[past_dates[0]]
                    previous_apple_videos = {s["video_id"] for s in last_record if s.get("apple_rank")}
        except Exception as e:
            LOG.warning(f"Failed to load history for WAVE detection: {e}")

    # 2. 지수 계산 실행. DB source of truth만 사용합니다.
    if not db_path.exists():
        LOG.error("DB not found: %s", db_path)
        return
    try:
        from hype_db import connect, export_frontend_history, hype_report_for_date, init_schema
        hype_results = []
        with connect(db_path) as conn:
            init_schema(conn)
            latest = conn.execute(
                "SELECT chart_period AS chart_date FROM playlist_order WHERE job_name IN ('KR-Top-100', 'KR-Top-Songs') AND chart_period GLOB '????-??-??' ORDER BY chart_period DESC LIMIT 1"
            ).fetchone()
            if latest:
                report = hype_report_for_date(conn, latest["chart_date"], previous_apple_videos=previous_apple_videos)
                hype_results = [
                    (row["video_id"], {"metadata": row, "score": row.get("hype_index", 0), "ranks": {
                        "Apple-Hype-Input": row.get("apple_rank") or 101,
                        "Melon-Gen-Z": row.get("melon_rank") or 101,
                        "YTMusic-Weekly": row.get("ytmusic_rank") or 101,
                    }})
                    for row in report
                ]
        if hype_results and not args.dry_run:
            export_frontend_history(db_path, args.history_json)
    except Exception as exc:
        LOG.error("DB hype calculation failed: %s", exc)
        return
    
    if not hype_results:
        LOG.error("No songs found to aggregate.")
        return

    top_songs = hype_results[:args.limit]
    video_ids = [vid for vid, stats in top_songs]
    
    # Prepare description
    update_date_str = kst_now.strftime("%Y-%m-%d")
    
    desc = f"Hype Wave Daily\n"
    desc += f"Based on Apple Music, Melon Gen-Z, and YouTube Music charts.\n\n"
    desc += "Top 3 Hype Now:\n"
    for i, (vid, stats) in enumerate(top_songs[:3], 1):
        m = stats["metadata"]
        desc += f"{i}. {m['title']} - {m['artist']} (Index: {int(stats['score'])})\n"
    
    desc += f"\nLast updated: {update_date_str}\n- colinky.github.io/hype_wave"

    LOG.info(f"Aggregated {len(video_ids)} songs for Hypex playlist.")
    
    # Save the results for transparency
    report = []
    # Hype 점수와 상관없이 전체 곡(합집합)을 report에 담아야 Apple/Melon 탭에서 누락되지 않습니다.
    for i, (vid, stats) in enumerate(hype_results, 1):
        apple_rank = stats["ranks"].get("Apple-Hype-Input", 101)
        melon_rank = stats["ranks"].get("Melon-Gen-Z", 101)
        report.append({
            "hype_rank": i,
            "hype_index": round(stats["score"], 2),
            "apple_rank": apple_rank if apple_rank <= 100 else None,
            "melon_rank": melon_rank if melon_rank <= 100 else None,
            **stats["metadata"]
        })
    
    # 오늘 데이터를 추가합니다. (두 차트의 합집합을 고려하여 최대 200위까지 저장)
    history_data[today_str] = report[:200]
    
    # 1달간의 히스토리를 유지합니다. (최대 31개 이력)
    sorted_dates = sorted(history_data.keys(), reverse=True)
    history_data = {d: history_data[d] for d in sorted_dates[:31]}
    
    LOG.info("Updated frontend history from DB at %s", args.history_json)

    # Sync to YTMusic
    ytmusic = make_ytmusic(args.yt_auth)
    update_ytmusic_playlist(
        ytmusic,
        args.yt_playlist_id,
        video_ids,
        description=desc,
        dry_run=args.dry_run,
        db_path=db_path if db_path.exists() else None,
        service="hypex",
        job_name=args.job_name,
        playlist_name=args.playlist_name,
    )
    
    LOG.info("Hype Moment sync completed.")

if __name__ == "__main__":
    main()
