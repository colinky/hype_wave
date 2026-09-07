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


def schedule_window(task: dict, now: datetime | None = None) -> tuple[bool, str, datetime | None]:
    schedule = str(task.get("schedule") or "").strip()
    if not schedule:
        return True, "", None
    current = now or kst_now()
    schedule_wd = DAY_MAP.get(schedule.lower())
    if schedule_wd is None:
        return False, f"Unknown schedule day '{schedule}'.", None
    days_since = (current.weekday() - schedule_wd) % 7
    anchor = current - timedelta(days=days_since)
    if days_since == 0:
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


def failed_hype_inputs(tasks: list[dict], failed_tasks: list[str]) -> list[str]:
    inputs = {
        str(task.get("job_name") or "").strip()
        for task in tasks
        if task.get("include_in_hype")
    }
    return sorted(inputs.intersection(failed_tasks))


def data_snapshot_ready(conn, *, service: str, job_name: str, started_at: str,
                        source_variant: str = "default") -> bool:
    """Verify this execution's committed source/match snapshot, not an older run.

    Individual unmatched/blocked songs remain allowed, as in the crawler contract;
    every effective source must nevertheless have a final, accounted-for result.
    """
    from hype_db_common import normalized_service
    from hype_db_store import _validate_raw_tracks

    service = normalized_service(service)
    run = conn.execute(
        """SELECT * FROM match_runs WHERE service = ? AND job_name = ?
           AND source_variant = ? AND started_at = ?
           ORDER BY created_at DESC LIMIT 1""",
        (service, job_name, source_variant, started_at),
    ).fetchone()
    if not run or run["status"] != "completed" or not run["completed_at"]:
        return False
    raw = [dict(row) for row in conn.execute(
        """SELECT song_id, rank_order AS rank FROM playlist_order
           WHERE service = ? AND job_name = ? AND source_variant = ?
             AND reference_period = ? ORDER BY rank_order""",
        (service, job_name, source_variant, run["reference_period"]),
    ).fetchall()]
    try:
        _validate_raw_tracks(service, job_name, raw)
    except ValueError:
        return False
    effective = {}
    for row in raw:
        effective.setdefault(row["song_id"], row["rank"])
    attempts = conn.execute(
        """SELECT song_id, rank_order, status, video_id FROM match_attempts
           WHERE run_id = ? AND service = ?""", (run["run_id"], service),
    ).fetchall()
    final_statuses = {"matched", "cached_match", "proxy_matched", "manual_override",
                      "failed", "manual_blocked", "duplicate_skipped"}
    if len(attempts) != len(effective) or any(a["status"] not in final_statuses for a in attempts):
        return False
    if {(a["song_id"], a["rank_order"]) for a in attempts} != set(effective.items()):
        return False
    matched = sum(bool(a["video_id"]) and a["status"] not in
                  {"failed", "manual_blocked", "duplicate_skipped"} for a in attempts)
    return (run["total_tracks"] == len(effective)
            and run["matched_tracks"] == matched
            and run["failed_tracks"] == len(effective) - matched)


def classify_task_failure(conn, task: dict, started_at: str) -> str:
    """Only a recorded publication failure with ready data is publish-only."""
    job_name = task.get("job_name") or task.get("name") or ""
    service = str(task.get("service") or task.get("type") or "")
    audit = conn.execute(
        """SELECT status FROM playlist_update_runs WHERE job_name = ?
           AND playlist_id = ? AND match_started_at = ?
           ORDER BY started_at DESC LIMIT 1""",
        (job_name, task.get("target_id") or "", started_at),
    ).fetchone()
    if not audit or audit["status"] not in {
        "verification_failed", "mutation_failed", "restored", "recovery_required",
    }:
        return "data"
    if service == "hypex":
        return "publish"
    variant = "combined" if service == "melon_gen" else "default"
    return "publish" if data_snapshot_ready(
        conn, service=service, job_name=job_name, started_at=started_at,
        source_variant=variant,
    ) else "data"


def mark_history_ready() -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            stream.write("history_ready=true\n")


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
    publish_failed_tasks = []
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

        if task_type == "hypex":
            blocked_by = failed_hype_inputs(tasks, failed_tasks)
            if blocked_by:
                LOG.error(
                    "Skipping Hype sync because upstream Hype input tasks failed: %s",
                    ", ".join(blocked_by),
                )
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

        execution_started_at = datetime.now(timezone.utc).isoformat()
        child_env = {**task_env, "HYPE_MATCH_STARTED_AT": execution_started_at}
        data_ready = False
        try:
            LOG.debug(f"Running command: {' '.join(cmd)}")
            subprocess.run(cmd, check=True, env=child_env)
            LOG.info(f"Successfully finished task: {job_name}")

            success_count += 1
            data_ready = True
        except subprocess.CalledProcessError as e:
            failure_phase = "data"
            try:
                from hype_db import connect
                with connect(script_dir / "hype_wave_data.db") as conn:
                    failure_phase = classify_task_failure(conn, task, execution_started_at)
            except Exception as exc:
                LOG.error("Could not verify failed task's committed snapshot: %s", exc)
            if failure_phase == "publish":
                publish_failed_tasks.append(job_name)
                data_ready = True
                LOG.error("Task '%s' publication failed; committed chart data is ready", job_name)
            else:
                failed_tasks.append(job_name)
                LOG.error(f"Task '{job_name}' failed with exit code {e.returncode}")

        # Identity maintenance consumes the committed snapshot, not publication.
        if task_type == "ytmusic" and data_ready:
            heal_script = script_dir / "heal_split_tracks.py"
            db_path = script_dir / "hype_wave_data.db"
            if heal_script.exists() and (db_path.exists() or os.environ.get("SUPABASE_DB_URL")):
                try:
                    heal_cmd = [sys.executable, str(heal_script), "--db-path", str(db_path)]
                    LOG.info(f"Running heal_split_tracks after '{job_name}'...")
                    subprocess.run(heal_cmd, check=True, env=child_env)
                    LOG.info("heal_split_tracks completed.")
                except subprocess.CalledProcessError as he:
                    LOG.warning(f"heal_split_tracks failed (non-fatal): exit code {he.returncode}")

    LOG.info("=== Sync Summary ===")
    LOG.info(f"Total tasks: {len(tasks)}")
    LOG.info(f"Skipped: {skipped_count}")
    LOG.info(f"Successful: {success_count}")
    if (success_count or publish_failed_tasks) and not failed_hype_inputs(tasks, failed_tasks):
        try:
            from hype_db import export_frontend_history

            db_path = script_dir / "hype_wave_data.db"
            history_path = script_dir / "docs" / "api" / "history.json"
            payload = export_frontend_history(db_path, history_path)
            if not payload or not payload.get("dates"):
                raise RuntimeError("History export has no completed chart dates")
            LOG.info("Exported frontend history once after all sync tasks.")
            mark_history_ready()
        except Exception as exc:
            LOG.error("Failed to export frontend history: %s", exc)
            sys.exit(1)
    if failed_tasks:
        LOG.error("Data/task failures: %s", ", ".join(failed_tasks))
    if publish_failed_tasks:
        LOG.error("Publication failures requiring retry: %s", ", ".join(publish_failed_tasks))
    if failed_tasks or publish_failed_tasks:
        sys.exit(1)
    
if __name__ == "__main__":
    main()
