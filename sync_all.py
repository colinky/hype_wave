#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
"""
모든 동기화 작업을 순차적으로 실행하는 통합 스크립트입니다.
1. sync_config.json을 읽어 실행할 작업을 결정합니다.
2. 데이터베이스 캐시(Supabase PostgreSQL 또는 SQLite 로컬 폴백) 연쇄 효과를 활용하여 동기화를 극대화합니다.
"""
LOG = logging.getLogger("sync_all")
KST = timezone(timedelta(hours=9))
DAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def kst_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def is_ytmusic_task(task: dict) -> bool:
    return str(task.get("service") or task.get("type") or "").strip().lower() == "ytmusic"


def is_ytmusic_weekly_chart_task(task: dict) -> bool:
    return is_ytmusic_task(task) and str(task.get("job_name") or "").strip() == "Weekly-Hot-100"


def schedule_retry_days_for_task(task: dict) -> int:
    if is_ytmusic_weekly_chart_task(task):
        return 2
    return 0


def schedule_window(task: dict, now: datetime | None = None) -> tuple[bool, str, datetime | None]:
    schedule = str(task.get("schedule") or "").strip()
    if not schedule:
        return True, "", None
    current = now or kst_now()
    schedule_wd = DAY_MAP.get(schedule.lower())
    if schedule_wd is None:
        return False, f"Unknown schedule day '{schedule}'.", None
    days_since = (current.weekday() - schedule_wd) % 7
    retry_days = schedule_retry_days_for_task(task)
    anchor = current - timedelta(days=days_since)
    if days_since <= retry_days:
        return True, "", anchor
    current_day = current.strftime("%A")
    return False, f"Task schedule '{schedule}' does not match current KST day '{current_day}'.", anchor


def task_enabled(task, now: datetime | None = None):
    # 1. Check basic enabled/disabled toggle
    value = task.get("enabled", True)
    if isinstance(value, str):
        is_enabled = value.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        is_enabled = bool(value)
    
    if not is_enabled:
        return False

    # 2. Check day-of-week schedule (e.g. "Monday")
    ok, reason, _ = schedule_window(task, now)
    if not ok:
        LOG.info(reason)
        return False

    return True


def main():
    script_dir = Path(__file__).parent
    load_env_file(script_dir / ".env")
    load_env_file(script_dir / ".secrets" / ".env")

    config_path = script_dir / "sync_config.json"
    if not config_path.exists():
        LOG.error(f"sync_config.json not found at {config_path}!")
        sys.exit(1)

    # Pre-check worker scripts
    required_scripts = [
        "apple_music_to_ytmusic_crawl.py",
        "spotify_to_ytmusic_crawl.py",
        "melon_to_ytmusic_crawl.py",
        "melon_gen_to_ytmusic_crawl.py",
        "ytmusic_to_ytmusic_crawl.py",
        "hype_moment.py"
    ]
    for script in required_scripts:
        if not (script_dir / script).exists():
            LOG.error(f"Required script '{script}' not found in {script_dir}.")
            sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    yt_auth = os.environ.get("YTMUSIC_AUTH_FILE", ".secrets/browser.json")
    
    success_count = 0
    skipped_count = 0
    failed_tasks = []
    task_env = os.environ.copy()
    task_env["HYPE_DEFER_HISTORY_EXPORT"] = "1"
    if os.environ.get("SUPABASE_DB_URL"):
        try:
            from hype_db import connect

            with connect(script_dir / "hype_wave_data.db"):
                pass
            task_env["HYPE_SKIP_POSTGRES_INDEX_CHECK"] = "1"
            LOG.info("Verified Supabase indexes once before running child sync tasks.")
        except Exception as exc:
            LOG.error("Failed to verify Supabase indexes: %s", exc)
            sys.exit(1)
    



    current_kst = kst_now()
    for task in tasks:
        job_name = task.get("job_name") or task.get("name") or "Unknown-Job"
        playlist_name = task.get("playlist_name") or job_name
        task_type = str(task.get("service") or task.get("type", "")).strip().lower()
        source_urls = task.get("source_urls", [])
        target_id = task.get("target_id")

        if not task_enabled(task, current_kst):
            LOG.info(f"Skipping disabled task '{job_name}'.")
            skipped_count += 1
            continue
        
        if target_id == "REPLACE_WITH_YOUR_YT_PLAYLIST_ID" or not target_id:
            LOG.warning(f"Skipping task '{job_name}': Target ID not configured.")
            skipped_count += 1
            continue

        LOG.info(f"=== Starting Task: {job_name} ({task_type}) ===")
        
        entity_limit = task.get("entity_limit") or task.get("apple_chart_limit") or task.get("limit")
        cmd = [sys.executable]
        if task_type == "apple":
            cmd.append(str(script_dir / "apple_music_to_ytmusic_crawl.py"))
            cmd.extend(["--apple-playlist-urls"] + source_urls)
            if entity_limit:
                cmd.extend(["--apple-chart-limit", str(entity_limit)])
        elif task_type == "spotify":
            cmd.append(str(script_dir / "spotify_to_ytmusic_crawl.py"))
            cmd.extend(["--spotify-playlist-urls"] + source_urls)
            if entity_limit:
                cmd.extend(["--spotify-track-limit", str(entity_limit)])
            if "use_musicbrainz" in task:
                cmd.extend(["--use-musicbrainz", str(task["use_musicbrainz"]).lower()])
        elif task_type == "melon":
            cmd.append(str(script_dir / "melon_to_ytmusic_crawl.py"))
            cmd.extend(["--melon-urls"] + source_urls)
            if entity_limit:
                cmd.extend(["--track-limit", str(entity_limit)])
        elif task_type == "melon_gen":
            cmd.append(str(script_dir / "melon_gen_to_ytmusic_crawl.py"))
            gens = [str(item["gen"]) for item in source_urls if isinstance(item, dict) and "gen" in item]
            if gens:
                cmd.extend(["--melon-generation-gens"] + gens)
            if entity_limit:
                cmd.extend(["--track-limit", str(entity_limit)])
        elif task_type == "ytmusic":
            cmd.append(str(script_dir / "ytmusic_to_ytmusic_crawl.py"))
            for url in source_urls:
                if "charts.youtube.com" in url:
                    cmd.extend(["--youtube-charts-url", url])
                else:
                    cmd.extend(["--source-playlist-url", url])
            if entity_limit:
                cmd.extend(["--track-limit", str(entity_limit)])
        elif task_type == "hypex":
            cmd.append(str(script_dir / "hype_moment.py"))
            if entity_limit:
                cmd.extend(["--limit", str(entity_limit)])
        else:
            LOG.error(f"Unknown task type: {task_type}")
            continue

        cmd.extend(["--yt-auth", yt_auth])
        cmd.extend(["--yt-playlist-id", target_id])
        cmd.extend(["--job-name", job_name])
        cmd.extend(["--playlist-name", playlist_name])
        
        if task.get("shuffle"):
            cmd.append("--shuffle")

        try:
            LOG.debug(f"Running command: {' '.join(cmd)}")
            subprocess.run(cmd, check=True, env=task_env)
            LOG.info(f"Successfully finished task: {job_name}")

            success_count += 1

            # After each ytmusic crawl, heal any split track UIDs so that
            # hype_moment aggregation sees all services correctly unified.
            if task_type == "ytmusic":
                heal_script = script_dir / "heal_split_tracks.py"
                db_path = script_dir / "hype_wave_data.db"
                if heal_script.exists() and (db_path.exists() or os.environ.get("SUPABASE_DB_URL")):
                    try:
                        heal_cmd = [sys.executable, str(heal_script), "--db-path", str(db_path)]
                        LOG.info(f"Running heal_split_tracks after '{job_name}'...")
                        subprocess.run(heal_cmd, check=True, env=task_env)
                        LOG.info("heal_split_tracks completed.")
                    except subprocess.CalledProcessError as he:
                        LOG.warning(f"heal_split_tracks failed (non-fatal): exit code {he.returncode}")

        except subprocess.CalledProcessError as e:
            LOG.error(f"Task '{job_name}' failed with exit code {e.returncode}")
            failed_tasks.append(job_name)

    LOG.info("=== Sync Summary ===")
    LOG.info(f"Total tasks: {len(tasks)}")
    LOG.info(f"Skipped: {skipped_count}")
    LOG.info(f"Successful: {success_count}")
    if failed_tasks:
        LOG.error(f"Failed tasks: {', '.join(failed_tasks)}")
        sys.exit(1)

    if success_count:
        try:
            from hype_db import export_frontend_history

            db_path = script_dir / "hype_wave_data.db"
            history_path = script_dir / "docs" / "api" / "history.json"
            export_frontend_history(db_path, history_path)
            LOG.info("Exported frontend history once after all sync tasks.")
        except Exception as exc:
            LOG.error("Failed to export frontend history: %s", exc)
            sys.exit(1)
    
if __name__ == "__main__":
    main()
