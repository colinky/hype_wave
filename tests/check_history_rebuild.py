"""Verify database access and export the latest completed history without publishing."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hype_db import connect, export_frontend_history, latest_hype_history_date, validate_frontend_history


def main(output, db_path="hype_wave_data.db"):
    today = datetime.now(timezone.utc).date().isoformat()
    with connect(db_path, read_only=True) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        latest = latest_hype_history_date(conn)
        if latest is None or latest > today:
            raise ValueError("No valid completed history date is available")
        sources = [dict(row) for row in conn.execute("""
            SELECT service, job_name, source_variant, MAX(reference_period) AS reference_period
            FROM match_runs WHERE status = 'completed' AND completed_at IS NOT NULL
            GROUP BY service, job_name, source_variant
            ORDER BY service, job_name, source_variant
        """).fetchall()]
    output = Path(output)
    payload = export_frontend_history(db_path, output, full_rebuild=True, expected_date=latest)
    validate_frontend_history(payload, latest)
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert payload["dates"][0] == latest
    summary = {
        "database_connected": True, "current_date_utc": today,
        "latest_history_date": latest, "ready_for_today": latest == today,
        "history_days": len(payload["dates"]),
        "latest_chart_tracks": len(payload["rankings"][latest]),
        "completed_sources": sources,
    }
    output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"\nDB connection succeeded; history rebuilt through **{latest}** "
                         f"({summary['history_days']} days). UTC today: **{today}**. "
                         f"Ready for today: **{summary['ready_for_today']}**. "
                         "Database was read-only; no playlist edits or publication.\n")
    return summary


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:
        print(f"History rebuild check failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
