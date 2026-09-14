#!/usr/bin/env python3
"""
hype_moment.py
--------------
Aggregates the daily Hype Index metrics and updates the Hypex playlist on YouTube Music.
If SUPABASE_DB_URL is set in the environment, it queries and updates audits directly in
the remote Supabase PostgreSQL database instead of the local SQLite database.
"""
import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ytmusic_playlist_sync import (
    PlaylistMutationUncertain,
    make_ytmusic,
    update_ytmusic_playlist,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("hypex_aggregator")
# Parent orchestration must not infer calculation success from a publish audit:
# authentication and a pending-run guard can fail before that audit exists.
EXIT_CALCULATION_FAILED = 2
EXIT_HISTORY_EXPORT_FAILED = 3
EXIT_PUBLICATION_FAILED = 4


def parse_history_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("history date must use YYYY-MM-DD") from exc


def playlist_description(report: list[dict], history_date: str) -> str:
    updated = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    lines = ["Hype Wave Daily", "Based on Apple Music, Melon, and YT Music charts.",
             "", f"Chart date: {history_date}", "", "Top 3 Hype Now:"]
    lines.extend(f"{i}. {row['title']} - {row['artist']} (Index: {int(row.get('hype_index', 0))})"
                 for i, row in enumerate(report[:3], 1))
    return "\n".join([*lines, "", f"Last updated: {updated}", "- colinky.github.io/hype_wave"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Hypex aggregated playlist.")
    parser.add_argument("--db-path", default="hype_wave_data.db")
    parser.add_argument("--history-json", default="docs/api/history.json")
    parser.add_argument("--yt-playlist-id", required=True, help="Target YouTube Music Playlist ID")
    parser.add_argument("--job-name", default="Hype-Wave-Daily")
    parser.add_argument("--playlist-name", default="Hype Wave Daily")
    parser.add_argument("--limit", type=int, default=100, help="Number of songs to include")
    parser.add_argument("--yt-auth", default=".secrets/browser.json")
    parser.add_argument(
        "--history-date",
        type=parse_history_date,
        help="Generate a specific historical Hype Wave date instead of the latest date",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--defer-publish", action="store_true", help="Calculate without publishing or exporting history")
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser()

    # DB source of truth만 사용합니다.
    if not os.environ.get("SUPABASE_DB_URL") and not db_path.exists():
        LOG.error("DB not found: %s", db_path)
        return EXIT_CALCULATION_FAILED
    try:
        from hype_db import (
            connect,
            compact_frontend_history,
            export_frontend_history,
            hype_inputs,
            init_db,
            latest_hype_history_date,
            validate_frontend_history,
        )
        if args.limit <= 0:
            raise ValueError("Hype playlist limit must be positive")
        if not os.environ.get("SUPABASE_DB_URL") and not args.dry_run and not db_path.exists():
            init_db(db_path, repair_source_bindings=False)
        report = []
        from sync_validation import freeze_outputs, assert_no_active_repair
        tasks = [{"service": "hypex", "job_name": args.job_name, "target_id": args.yt_playlist_id,
                  "playlist_name": args.playlist_name, "entity_limit": args.limit}]
        history_date = args.history_date
        with connect(db_path, read_only=True) as conn:
            assert_no_active_repair(conn)
            apple_jobs = [
                name for name, item in hype_inputs().items()
                if item.get("hype_group") == "apple"
            ] or ["KR-Top-100"]
            placeholders = ",".join("?" for _ in apple_jobs)
            if args.history_date:
                anchor_period = (
                    datetime.strptime(args.history_date, "%Y-%m-%d")
                    - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                ready_rows = conn.execute(
                    f"""
                    SELECT DISTINCT p.job_name
                    FROM playlist_order p
                    WHERE p.job_name IN ({placeholders})
                      AND p.reference_period = ?
                      AND EXISTS (
                          SELECT 1
                          FROM match_runs mr
                          WHERE mr.service = p.service
                            AND mr.job_name = p.job_name
                            AND mr.source_variant = p.source_variant
                            AND mr.reference_period = p.reference_period
                            AND mr.status = 'completed'
                            AND mr.completed_at IS NOT NULL
                      )
                    """,
                    (*apple_jobs, anchor_period),
                ).fetchall()
                ready_jobs = {row["job_name"] for row in ready_rows}
                missing_jobs = sorted(set(apple_jobs) - ready_jobs)
                if missing_jobs:
                    raise RuntimeError(
                        f"Historical Hype date {args.history_date} requires completed Apple "
                        f"snapshot {anchor_period}; missing: {', '.join(missing_jobs)}"
                    )
            if history_date is None:
                history_date = latest_hype_history_date(conn)
            if history_date:
                snapshot = freeze_outputs(conn, tasks, history_date=history_date)
                report = snapshot["report"]
                validate_frontend_history(compact_frontend_history({history_date: report}), history_date)
        if not report:
            raise RuntimeError("No songs found to aggregate")
    except Exception as exc:
        LOG.error("DB hype calculation failed: %s", exc)
        return EXIT_CALCULATION_FAILED

    video_ids = snapshot["outputs"][0]["video_ids"]
    LOG.info("Aggregated %s songs for Hypex playlist.", len(video_ids))
    if args.dry_run or args.defer_publish:
        LOG.info("Calculation completed; playlist and history publication are deferred.")
        return 0

    from sync_validation import PlaybackBlocked, assert_frozen, require_playable, verifier_for
    from sync_all import require_healthy

    publication_failed = False
    try:
        ytmusic = make_ytmusic(args.yt_auth)
        verifier = verifier_for(ytmusic)
        require_healthy(verifier)
        all_video_ids = [row["video_id"] for row in report]
        require_playable(verifier, all_video_ids)

        def guard():
            require_healthy(verifier)
            with connect(db_path, read_only=True) as conn:
                assert_frozen(conn, snapshot, tasks)
            require_healthy(verifier)

        guard()
        try:
            update_ytmusic_playlist(
                ytmusic, args.yt_playlist_id, video_ids,
                description=playlist_description(report[:args.limit], history_date),
                dry_run=False, db_path=db_path, service="hypex", job_name=args.job_name,
                playlist_name=args.playlist_name, playability_verifier=verifier, before_mutation=guard,
            )
        except (PlaybackBlocked, PlaylistMutationUncertain):
            raise
        except Exception as exc:
            publication_failed = True
            LOG.error("Hype playlist publication requires retry: %s", exc)
            require_healthy(verifier, force=True)
        guard()
        require_playable(verifier, all_video_ids)
    except Exception as exc:
        LOG.error("Hype publication validation failed; history is blocked: %s", exc)
        return EXIT_PUBLICATION_FAILED

    if os.environ.get("HYPE_DEFER_HISTORY_EXPORT") not in {"1", "true", "TRUE"}:
        try:
            guard()
            export_frontend_history(db_path, args.history_json, expected_date=history_date,
                                    reports_by_date={history_date: report})
        except Exception as exc:
            LOG.error("Hype history export failed: %s", exc)
            return EXIT_HISTORY_EXPORT_FAILED
    if publication_failed:
        return EXIT_PUBLICATION_FAILED

    LOG.info("Hype Moment sync completed.")
    return 0

if __name__ == "__main__":
    from sync_validation import run_locked_cli
    raise SystemExit(run_locked_cli(main))
