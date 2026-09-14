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


def classify_task_failure(conn, task: dict, started_at: str, *, returncode: int | None = None) -> str:
    """Identify the failure phase independently of source data eligibility."""
    job_name = task.get("job_name") or task.get("name") or ""
    service = str(task.get("service") or task.get("type") or "")
    if service == "hypex":
        from hype_moment import (
            EXIT_CALCULATION_FAILED, EXIT_HISTORY_EXPORT_FAILED, EXIT_PUBLICATION_FAILED,
        )
        return {
            EXIT_CALCULATION_FAILED: "calculation",
            EXIT_HISTORY_EXPORT_FAILED: "export",
            EXIT_PUBLICATION_FAILED: "publish",
        }.get(returncode, "task_or_unknown")
    audit = conn.execute(
        """SELECT status FROM playlist_update_runs WHERE job_name = ?
           AND playlist_id = ? AND match_started_at = ?
           ORDER BY started_at DESC LIMIT 1""",
        (job_name, task.get("target_id") or "", started_at),
    ).fetchone()
    if not audit or audit["status"] not in {
        "verification_failed", "mutation_failed", "restored", "recovery_required",
    }:
        return "task_or_unknown"
    return "publish"


def mark_history_ready() -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            stream.write("history_ready=true\n")


def source_command(task: dict, script_dir: Path, yt_auth: str) -> list[str]:
    """Collection and matching still run; publishers belong to the parent phase."""
    service = task["service"]
    scripts = {
        "apple": "apple_music_to_ytmusic_crawl.py",
        "spotify": "spotify_to_ytmusic_crawl.py",
        "melon": "melon_to_ytmusic_crawl.py",
        "melon_gen": "melon_gen_to_ytmusic_crawl.py",
        "ytmusic": "ytmusic_to_ytmusic_crawl.py",
    }
    if service not in scripts:
        raise ValueError(f"Unknown source task type: {service}")
    script = script_dir / scripts[service]
    if not script.is_file():
        raise FileNotFoundError(f"Required source script is missing: {script.name}")
    cmd = [sys.executable, str(script), "--defer-publish"]
    urls = task.get("source_urls", [])
    limit = task.get("entity_limit") or task.get("apple_chart_limit") or task.get("limit")
    if service == "apple":
        cmd += ["--apple-playlist-urls", *urls]
    elif service == "spotify":
        cmd += ["--spotify-playlist-urls", *urls]
        if "use_musicbrainz" in task:
            cmd += ["--use-musicbrainz", str(task["use_musicbrainz"]).lower()]
    elif service == "melon":
        cmd += ["--melon-urls", *urls]
    elif service == "melon_gen":
        gens = [str(item["gen"]) for item in urls if isinstance(item, dict) and "gen" in item]
        if gens:
            cmd += ["--melon-generation-gens", *gens]
    else:
        for url in urls:
            cmd += ["--youtube-charts-url" if "charts.youtube.com" in url else "--source-playlist-url", url]
    if limit:
        flag = {"apple": "--apple-chart-limit", "spotify": "--spotify-track-limit"}.get(service, "--track-limit")
        cmd += [flag, str(limit)]
    cmd += ["--yt-auth", yt_auth, "--yt-playlist-id", task["target_id"],
            "--job-name", task["job_name"], "--playlist-name", task.get("playlist_name") or task["job_name"],
            "--db-path", str(script_dir / "hype_wave_data.db")]
    if task.get("shuffle"):
        cmd.append("--shuffle")
    return cmd


def require_healthy(verifier, *, force: bool = False) -> None:
    from sync_validation import PlaybackBlocked

    health = verifier.check_health(force=force)
    if health.get("run_health") != "healthy" or health.get("auth_state") != "authenticated":
        raise PlaybackBlocked(f"YouTube Music environment is not healthy: {health.get('reason_code', 'unknown')}")


def run_sync(tasks: list[dict], script_dir: Path, yt_auth: str) -> int:
    """Collect all inputs, freeze once, then publish and export that same report."""
    from hype_db import connect, export_frontend_history, validate_frontend_history
    from hype_db_common import normalized_service
    from hype_moment import playlist_description
    from sync_validation import PlaybackBlocked, assert_frozen, assert_no_active_repair, freeze_outputs, require_playable, verifier_for
    from ytmusic_playlist_sync import (
        PlaylistMutationUncertain, get_existing_playlist_items, make_ytmusic, update_ytmusic_playlist,
    )

    active = []
    for item in tasks:
        if not task_enabled(item):
            continue
        task = {**item, "job_name": item.get("job_name") or item.get("name") or "Unknown-Job",
                "service": str(item.get("service") or item.get("type") or "").strip().lower()}
        if not task.get("target_id") or task["target_id"] == "REPLACE_WITH_YOUR_YT_PLAYLIST_ID":
            if task.get("include_in_hype"):
                raise PlaybackBlocked(f"Required source target is not configured: {task['job_name']}")
            LOG.warning("Skipping unconfigured task: %s", task["job_name"])
            continue
        active.append(task)
    if not active:
        LOG.info("No scheduled tasks to run.")
        return 0
    sources = [task for task in active if task["service"] != "hypex"]
    commands = [(task, source_command(task, script_dir, yt_auth)) for task in sources]
    db_path = script_dir / "hype_wave_data.db"
    task_env = {**os.environ, "HYPE_DEFER_HISTORY_EXPORT": "1", "HYPE_SYNC_PARENT_PID": str(os.getpid())}
    with connect(db_path, read_only=True) as conn:
        assert_no_active_repair(conn)

    # Reject a broken authenticated environment before any child can write a snapshot.
    require_healthy(verifier_for(make_ytmusic(yt_auth)))
    if os.environ.get("SUPABASE_DB_URL"):
        with connect(db_path):
            pass
        task_env["HYPE_SKIP_POSTGRES_INDEX_CHECK"] = "1"
    failures, anchor_dates = [], {}
    for task, cmd in commands:
        started_at = datetime.now(timezone.utc).isoformat()
        task["match_started_at"] = started_at
        child_env = {**task_env, "HYPE_MATCH_STARTED_AT": started_at}
        try:
            LOG.info("Collecting and matching source: %s", task["job_name"])
            subprocess.run(cmd, check=True, env=child_env)
            with connect(db_path, read_only=True) as conn:
                variant = "combined" if task["service"] == "melon_gen" else "default"
                if not data_snapshot_ready(conn, service=task["service"], job_name=task["job_name"],
                                           started_at=started_at, source_variant=variant):
                    raise PlaybackBlocked("This execution has no complete validated matching snapshot")
                if task.get("include_in_hype") and task.get("hype_group") == "apple":
                    anchor = conn.execute(
                        "SELECT reference_period FROM match_runs WHERE service=? AND job_name=? "
                        "AND source_variant=? AND started_at=? ORDER BY created_at DESC LIMIT 1",
                        (normalized_service(task["service"]), task["job_name"], variant, started_at),
                    ).fetchone()
                    anchor_dates[task["job_name"]] = (
                        datetime.strptime(anchor["reference_period"], "%Y-%m-%d") + timedelta(days=1)
                    ).strftime("%Y-%m-%d")
        except Exception as exc:
            failures.append(task["job_name"])
            LOG.error("Source phase failed for %s: %s", task["job_name"], exc)
    if failures:
        LOG.error("New outputs blocked by source failures: %s", ", ".join(failures))
        return 1
    if len(set(anchor_dates.values())) > 1:
        raise PlaybackBlocked(f"This execution's Apple anchor dates disagree: {anchor_dates}")

    # Identity maintenance cannot happen after an earlier playlist was published.
    if any(task["service"] == "ytmusic" for task in sources):
        heal_script = script_dir / "heal_split_tracks.py"
        if not heal_script.is_file():
            raise PlaybackBlocked("Required identity maintenance script is missing")
        subprocess.run([sys.executable, str(heal_script), "--db-path", str(db_path)],
                       check=True, env=task_env)

    with connect(db_path, read_only=True) as conn:
        snapshot = freeze_outputs(conn, active, history_date=next(iter(anchor_dates.values()), None))
    history_date = snapshot["history_date"]
    from hype_db import compact_frontend_history
    validate_frontend_history(compact_frontend_history({history_date: snapshot["report"]}), history_date)
    video_ids = list(dict.fromkeys(
        [vid for output in snapshot["outputs"] for vid in output["video_ids"]]
        + [row["video_id"] for row in snapshot["report"]]
    ))
    # Collection can take longer than a verification budget. Start a fresh observation
    # phase with a new client rather than extending expired evidence from the preflight.
    client = make_ytmusic(yt_auth)
    verifier = verifier_for(client)
    require_healthy(verifier)
    require_playable(verifier, video_ids)
    for output in snapshot["outputs"]:
        items = get_existing_playlist_items(client, output["playlist_id"])
        require_playable(verifier, output["video_ids"], items=items)

    def guard():
        require_healthy(verifier)
        with connect(db_path, read_only=True) as conn:
            assert_no_active_repair(conn)
            assert_frozen(conn, snapshot, active)
        require_healthy(verifier)

    publish_failures = []
    for output in snapshot["outputs"]:
        # Publication guards and owned-item moves can outlast a preceding
        # playlist's verification budget. Observe this playlist afresh.
        client = make_ytmusic(yt_auth)
        verifier = verifier_for(client)
        require_healthy(verifier)
        require_playable(verifier, output["video_ids"])
        guard()
        description = playlist_description(snapshot["report"], history_date) if output["service"] == "hypex" else ""
        try:
            update_ytmusic_playlist(
                client, output["playlist_id"], output["video_ids"], description=description,
                dry_run=False, db_path=db_path, service=output["service"], job_name=output["job_name"],
                playlist_name=output["playlist_name"], playability_verifier=verifier, before_mutation=guard,
            )
        except (PlaybackBlocked, PlaylistMutationUncertain):
            # Playback uncertainty is a data gate, not a retryable playlist transport error.
            raise
        except Exception as exc:
            publish_failures.append(output["job_name"])
            LOG.error("Playlist publication failed for %s: %s", output["job_name"], exc)
            require_healthy(verifier, force=True)
            guard()
    client = make_ytmusic(yt_auth)
    verifier = verifier_for(client)
    require_healthy(verifier)
    guard()
    require_playable(verifier, video_ids)
    guard()
    history_path = script_dir / "docs" / "api" / "history.json"
    payload = export_frontend_history(db_path, history_path, expected_date=history_date,
                                      reports_by_date={history_date: snapshot["report"]})
    validate_frontend_history(payload, history_date)
    written = json.loads(history_path.read_text(encoding="utf-8"))
    validate_frontend_history(written, history_date)
    if written != payload:
        raise PlaybackBlocked("Written history differs from the frozen export")
    mark_history_ready()
    LOG.info("Published from one frozen snapshot; history is ready. Publication retries: %s", publish_failures)
    return int(bool(publish_failures))


def main():
    script_dir = Path(__file__).parent
    load_env_file(script_dir / ".env")
    load_env_file(script_dir / ".secrets" / ".env")
    try:
        tasks = json.loads((script_dir / "sync_config.json").read_text(encoding="utf-8"))
        from sync_validation import sync_run_lock
        with sync_run_lock(script_dir / "hype_wave_data.db"):
            result = run_sync(tasks, script_dir, os.environ.get("YTMUSIC_AUTH_FILE", ".secrets/browser.json"))
    except Exception as exc:
        LOG.error("Sync stopped before further outputs: %s", exc)
        result = 1
    if result:
        raise SystemExit(result)


if __name__ == "__main__":
    main()
