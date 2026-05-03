#!/usr/bin/env python3
import argparse
import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ytmusic_playlist_sync import (
    make_ytmusic,
    update_ytmusic_playlist,
    write_json,
    normalize_text,
    title_variants,
    artist_variants,
    album_variants
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("hypex_aggregator")

def calculate_hype_index(logs_dir: Path, yesterday_apple_ids: set[str] = None):
    """
    Hype Index(화제성 지수) 계산:
    서로 다른 차트(Apple, Melon)에서 동일한 곡을 식별하여 모멘텀을 계산합니다.
    - 1순위: Video ID를 기준으로 그룹화
    - 2순위: 다국어 곡 제목 및 아티스트 변형을 사용하여 플랫폼 간 곡 병합
    - WAVE 판별: 어제 애플 차트에 없었으나 오늘 진입하고 멜론에는 없는 곡
    """
    yesterday_apple_ids = yesterday_apple_ids or set()
    song_groups = []
    vid_to_idx = {}    # video_id -> group index
    meta_to_idx = {}   # "normalized_title|normalized_artist" -> group index
    
    if not logs_dir.exists():
        LOG.error(f"Logs directory {logs_dir} does not exist.")
        return []

    for task_dir in logs_dir.iterdir():
        if not task_dir.is_dir():
            continue
            
        matches_file = task_dir / "latest_matches_crawl.json"
        if not matches_file.exists():
            continue
            
        task_name = task_dir.name
        
        try:
            with open(matches_file, "r", encoding="utf-8") as f:
                matches = json.load(f)
            
            for m in matches:
                title = m.get("title")
                artist = m.get("artist")
                video_id = m.get("video_id")
                rank = m.get("rank")
                
                if not title or not artist or not rank or rank <= 0:
                    continue
                
                # 1. Find existing group
                group_idx = None

                # Priority 1: Find group by video_id (strongest identifier)
                if video_id and video_id in vid_to_idx:
                    group_idx = vid_to_idx[video_id]
                
                # Priority 2: If no group found by video_id, and video_id is missing,
                # then try to find by metadata variants.
                if group_idx is None and not video_id:
                    # Use a more robust check for metadata variants to find an existing group
                    # This part of the logic is for when a video_id is NOT available.
                    # It tries to group based on similar titles/artists.
                    # The problem is that `meta_to_idx` can point to a group that *does* have a video_id.
                    # If we are here, it means the current item has NO video_id.
                    # So, we can safely merge it into a group that matches its metadata.
                    
                    # The previous logic was trying to use `title_variants` and `artist_variants`
                    # to *find* a group. This is what caused the problem.
                    # Instead, we should use a strict normalized title|artist key for `meta_to_idx`
                    # and only use it if video_id is missing.
                    
                    # Let's use the strict normalized title|artist for lookup here.
                    strict_meta_key = f"{normalize_text(title)}|{normalize_text(artist)}"
                    if strict_meta_key in meta_to_idx:
                        group_idx = meta_to_idx[strict_meta_key]

                # 2. Create new group if not found
                if group_idx is None:
                    group_idx = len(song_groups)
                    song_groups.append({
                        "metadata": {},
                        "ranks": {},
                        "best_rank": 999,
                        "video_ids": set()
                    })
                
                stats = song_groups[group_idx]

                # 3. Update mappings for future lookups
                if video_id:
                    vid_to_idx[video_id] = group_idx
                    stats["video_ids"].add(video_id)
                
                # Always register the current track's strict normalized title|artist to this group.
                # This ensures that if a future track (without a video_id) matches this metadata,
                # it will be grouped correctly.
                meta_to_idx[f"{normalize_text(title)}|{normalize_text(artist)}"] = group_idx

                # 4. 원본 서비스 URL 수집 (누적)
                source_url = m.get("url")
                if source_url:
                    if "Apple" in task_name:
                        # Only update URL if current rank is better or URL is not set
                        if "apple_url" not in stats["metadata"] or rank < stats["ranks"].get(task_name, 999):
                            stats["metadata"]["apple_url"] = source_url
                    elif "Melon" in task_name:
                        if "melon_url" not in stats["metadata"] or rank < stats["ranks"].get(task_name, 999):
                            stats["metadata"]["melon_url"] = source_url
                    elif "Spotify" in task_name:
                        if "spotify_url" not in stats["metadata"] or rank < stats["ranks"].get(task_name, 999):
                            stats["metadata"]["spotify_url"] = source_url

                # Update rank for this specific task
                # If multiple matches for same song in same task (rare), keep best rank
                if task_name not in stats["ranks"] or rank < stats["ranks"][task_name]:
                    stats["ranks"][task_name] = rank
                
                # Keep metadata from the best overall rank across all charts
                if not stats["metadata"] or rank < stats["best_rank"]:
                    metadata_fields = {
                        "title": title,
                        "artist": artist,
                        "album": m.get("album"),
                        "yt_title": m.get("yt_title"),
                        "yt_artist": m.get("yt_artist"),
                        "video_id": video_id,
                        "artwork_url": m.get("artwork_url")
                    }
                    # 기존 URL 정보를 유지하면서 메타데이터 업데이트
                    for k, v in metadata_fields.items():
                        if v: stats["metadata"][k] = v
                    stats["best_rank"] = rank
                    
        except Exception as e:
            LOG.warning(f"Failed to process {matches_file}: {e}")
            import traceback
            LOG.debug(traceback.format_exc())

    final_list = []
    for stats in song_groups:
        apple_rank = stats["ranks"].get("Apple-KR-Top-100", 101)
        melon_rank = stats["ranks"].get("Melon-KR-Top-100-Daily", 101)
        
        video_id = stats["metadata"].get("video_id")
        if not video_id and stats["video_ids"]:
            video_id = list(stats["video_ids"])[0]
            
        if not video_id:
            continue
            
        # 방식 1: 선형(Linear) 점수 (중하위권 순위 차이 강조)
        apple_score = max(101 - apple_rank , 0) 
        melon_score = max(101 - melon_rank , 0) 
        score_linear = (apple_score * 0.2) + (apple_score - melon_score)

        # 방식 2: 역수(Reciprocal) 점수 (최상위권 파급력 강조)
        # 1위 = 100점, 10위 = 10점, 100위 = 1점 기준
        apple_power = 100.0 / apple_rank if apple_rank <= 100 else 0.0
        melon_power = 100.0 / melon_rank if melon_rank <= 100 else 0.0
        score_reciprocal = (apple_power * 0.2) + (apple_power - melon_power)

        # 두 방식의 가중평균값 적용
        total_score = (score_linear * 2 + score_reciprocal) / 3.0

        # [WAVE 판별] 애플에는 있고 멜론에는 없는 '폭발 직전' 구간
        is_wave = (apple_rank <= 100) and (melon_rank > 100)
        # [NEW WAVE] 어제는 애플에 없었는데 오늘 처음 들어온 '신선한' 진입
        is_new_wave = is_wave and (video_id not in yesterday_apple_ids)

        if is_wave:
            stats["metadata"]["is_wave"] = True
        if is_new_wave:
            stats["metadata"]["is_new_wave"] = True

        # 애플 뮤직이나 멜론 차트 중 하나라도 진입한 곡은 모두 포함합니다.
        if apple_score > 0 or melon_score > 0:
            stats["score"] = total_score
            final_list.append((video_id, stats))

    # Sort by score descending
    sorted_songs = sorted(
        final_list,
        key=lambda x: x[1]["score"],
        reverse=True
    )
    
    return sorted_songs

def main():
    parser = argparse.ArgumentParser(description="Generate Hypex aggregated playlist.")
    parser.add_argument("--logs-dir", default="logs", help="Directory containing crawl logs")
    parser.add_argument("--yt-playlist-id", required=True, help="Target YouTube Music Playlist ID")
    parser.add_argument("--limit", type=int, default=100, help="Number of songs to include")
    parser.add_argument("--yt-auth", default=".secrets/browser.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logs_path = Path(args.logs_dir)
    
    # 1. WAVE 판별을 위해 어제 히스토리에서 애플 차트 곡 ID 추출
    history_file = Path("docs/api/history.json")
    yesterday_apple_ids = set()
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
                    yesterday_apple_ids = {s["video_id"] for s in last_record if s.get("apple_rank")}
        except Exception as e:
            LOG.warning(f"Failed to load history for WAVE detection: {e}")

    # 2. 지수 계산 실행 (어제 데이터 전달)
    hype_results = calculate_hype_index(logs_path, yesterday_apple_ids)
    
    if not hype_results:
        LOG.error("No songs found to aggregate.")
        return

    top_songs = hype_results[:args.limit]
    video_ids = [vid for vid, stats in top_songs]
    
    # Prepare description
    update_date_str = kst_now.strftime("%Y-%m-%d")
    
    desc = f"Hype Wave Daily\n"
    desc += f"Based on Apple Music and Melon charts.\n\n"
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
        apple_rank = stats["ranks"].get("Apple-KR-Top-100", 101)
        melon_rank = stats["ranks"].get("Melon-KR-Top-100-Daily", 101)
        report.append({
            "hype_rank": i,
            "hype_index": round(stats["score"], 2),
            "apple_rank": apple_rank if apple_rank <= 100 else None,
            "melon_rank": melon_rank if melon_rank <= 100 else None,
            **stats["metadata"]
        })
    
    write_json(logs_path / "hype_moment_latest.json", report)

    # 오늘 데이터를 추가합니다. (두 차트의 합집합을 고려하여 최대 200위까지 저장)
    history_data[today_str] = report[:200]
    
    # 1달간의 히스토리를 유지합니다. (최대 31개 이력)
    sorted_dates = sorted(history_data.keys(), reverse=True)
    history_data = {d: history_data[d] for d in sorted_dates[:31]}
    
    write_json(history_file, history_data)
    LOG.info(f"Updated historical data for web at {history_file}")

    # Sync to YTMusic
    ytmusic = make_ytmusic(args.yt_auth)
    update_ytmusic_playlist(
        ytmusic,
        args.yt_playlist_id,
        video_ids,
        description=desc,
        dry_run=args.dry_run
    )
    
    LOG.info("Hype Moment sync completed.")

if __name__ == "__main__":
    main()
