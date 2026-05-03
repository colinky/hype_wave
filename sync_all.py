#!/usr/bin/env python3
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ytmusic_playlist_sync import cleanup_old_logs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
"""
모든 동기화 작업을 순차적으로 실행하는 통합 스크립트입니다.
1. sync_config.json을 읽어 실행할 작업을 결정합니다.
2. 이전 작업들에서 생성된 매칭 결과(logs/)를 수집하여 다음 작업의 '프록시 데이터'로 전달합니다.
3. Apple Music -> Spotify -> Melon -> Hype Index 순으로 실행하여 데이터 연쇄 효과를 극대화합니다.
"""
LOG = logging.getLogger("sync_all")


def task_enabled(task):
    # 1. Check basic enabled/disabled toggle
    value = task.get("enabled", True)
    if isinstance(value, str):
        is_enabled = value.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        is_enabled = bool(value)
    
    if not is_enabled:
        return False

    # 2. Check day-of-week schedule (e.g. "Monday")
    schedule = task.get("schedule")
    if schedule:
        from datetime import datetime, timezone, timedelta
        # Use KST (UTC+9) as the reference time zone
        kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
        current_day = kst_now.strftime("%A") # Full day name
        # 특정 요일에만 실행되도록 설정된 경우 체크
        if schedule.strip().title() != current_day:
            LOG.info(f"Task schedule '{schedule}' does not match current KST day '{current_day}'.")
            return False

    return True


def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / "sync_config.json"
    if not config_path.exists():
        LOG.error(f"sync_config.json not found at {config_path}!")
        sys.exit(1)

    # Pre-check worker scripts
    for script in ["apple_music_to_ytmusic_crawl.py", "spotify_to_ytmusic_crawl.py", "melon_to_ytmusic_crawl.py", "hype_moment.py"]:
        if not (script_dir / script).exists():
            LOG.error(f"Required script '{script}' not found in {script_dir}.")
            sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    yt_auth = os.environ.get("YTMUSIC_AUTH_FILE", ".secrets/browser.json")
    
    success_count = 0
    skipped_count = 0
    failed_tasks = []
    
    # Root logs directory for the whole project
    logs_root = script_dir / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)

    # 1. 작업 시작 전 오래된 로그 정리 (7일 기준)
    LOG.info("Cleaning up old logs...")
    cleanup_old_logs(logs_root) # rglob("*")을 사용하므로 루트 호출만으로 충분함

    for task in tasks:
        name = task.get("name", "Unknown Task")
        task_type = str(task.get("type", "")).strip().lower()
        source_urls = task.get("source_urls", [])
        target_id = task.get("target_id")

        if not task_enabled(task):
            LOG.info(f"Skipping disabled task '{name}'.")
            skipped_count += 1
            continue
        
        if target_id == "REPLACE_WITH_YOUR_YT_PLAYLIST_ID" or not target_id:
            LOG.warning(f"Skipping task '{name}': Target ID not configured.")
            skipped_count += 1
            continue

        LOG.info(f"=== Starting Task: {name} ({task_type}) ===")
        
        # Collect proxy data from all previous match results
        proxy_data_paths = []
        if task_type in ("spotify", "melon", "hype"):
            if logs_root.exists():
                for d in logs_root.iterdir():
                    if d.is_dir():
                        # Use ANY latest_matches_crawl.json as a potential proxy source
                        p_file = d / "latest_matches_crawl.json"
                        if p_file.exists():
                            proxy_data_paths.append(str(p_file))
        
        cmd = [sys.executable]
        if task_type == "apple":
            cmd.append(str(script_dir / "apple_music_to_ytmusic_crawl.py"))
            cmd.extend(["--apple-playlist-urls"] + source_urls)
            if "apple_chart_limit" in task:
                cmd.extend(["--apple-chart-limit", str(task["apple_chart_limit"])])
        elif task_type == "spotify":
            cmd.append(str(script_dir / "spotify_to_ytmusic_crawl.py"))
            cmd.extend(["--spotify-playlist-urls"] + source_urls)
            if "use_musicbrainz" in task:
                cmd.extend(["--use-musicbrainz", str(task["use_musicbrainz"]).lower()])
            if proxy_data_paths:
                cmd.extend(["--apple-proxy-data", ",".join(proxy_data_paths)])
        elif task_type == "melon":
            cmd.append(str(script_dir / "melon_to_ytmusic_crawl.py"))
            cmd.extend(["--melon-urls"] + source_urls)
            if proxy_data_paths:
                cmd.extend(["--apple-proxy-data", ",".join(proxy_data_paths)])
        elif task_type == "hypex":
            cmd.append(str(script_dir / "hype_moment.py"))
            if "limit" in task:
                cmd.extend(["--limit", str(task["limit"])])
        else:
            LOG.error(f"Unknown task type: {task_type}")
            continue

        cmd.extend(["--yt-auth", yt_auth])
        cmd.extend(["--yt-playlist-id", target_id])
        
        # Handle log directory arguments (hype_moment uses --logs-dir, others use --log-dir)
        if task_type == "hypex":
            cmd.extend(["--logs-dir", str(logs_root)])
        else:
            task_log_dir = logs_root / name
            cmd.extend(["--log-dir", str(task_log_dir)])

        if task.get("shuffle"):
            cmd.append("--shuffle")

        try:
            LOG.debug(f"Running command: {' '.join(cmd)}")
            result = subprocess.run(cmd, check=True)
            LOG.info(f"Successfully finished task: {name}")

            # 2. 작업 성공 시 crawl 데이터 아카이빙 (sync_config.json의 archive 필드 기준)
            should_archive = bool(task.get("archive", False))

            if should_archive:
                task_log_dir = logs_root / name
                # 방금 생성된 트랙 크롤링 로그 파일을 찾아 crawl/ 폴더로 복사
                track_logs = list(task_log_dir.glob("*_tracks_crawl_*.json"))
                if track_logs:
                    latest_track_log = max(track_logs, key=lambda p: p.stat().st_mtime)
                    crawl_dir = script_dir / "crawl"
                    crawl_dir.mkdir(exist_ok=True)
                    
                    kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
                    date_str = kst_now.strftime("%Y-%m-%d")
                    
                    # task_type 대신 고유한 name을 사용하여 파일 덮어쓰기 방지
                    target_archive = crawl_dir / f"{name}_{date_str}.json"
                    shutil.copy2(latest_track_log, target_archive)
                    LOG.info(f"Archived latest track data to {target_archive}")

            success_count += 1
        except subprocess.CalledProcessError as e:
            LOG.error(f"Task '{name}' failed with exit code {e.returncode}")
            failed_tasks.append(name)

    LOG.info(f"=== Sync Summary ===")
    LOG.info(f"Total tasks: {len(tasks)}")
    LOG.info(f"Skipped: {skipped_count}")
    LOG.info(f"Successful: {success_count}")
    if failed_tasks:
        LOG.error(f"Failed tasks: {', '.join(failed_tasks)}")
        sys.exit(1)
    
if __name__ == "__main__":
    main()
