from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hype_scoring import calculate_rank_score

from hype_db_common import (
    DEFAULT_HYPE_WEIGHTS,
    build_track_url,
    hype_identity_key,
    hype_inputs,
    normalized_service,
    reference_period_for_date,
)
from hype_db_schema import connect, init_db

LOG = logging.getLogger("hype_db")
__all__ = [
    "export_frontend_history",
    "latest_hype_history_date",
    "validate_frontend_history",
    "inflate_frontend_history",
    "compact_frontend_history",
    "repair_frontend_history",
    "carry_history_identity",
    "write_frontend_history",
    "prune_history",
    "previous_apple_videos_for_history",
    "fetch_hype_rows_for_dates",
    "hype_report_for_date",
    "build_hype_report_from_rows",
]


TRACK_METADATA_FIELDS = (
    "video_id",
    "title",
    "artist",
    "album",
    "yt_title",
    "yt_artist",
    "yt_album",
    "artwork_url",
    "apple_url",
    "melon_url",
    "spotify_url",
)
DAILY_RANKING_FIELDS = (
    "hype_rank",
    "hype_index",
    "apple_rank",
    "melon_rank",
    "melon_genz_rank",
    "ytmusic_rank",
)
HISTORY_METADATA_OVERRIDES = tuple(field for field in TRACK_METADATA_FIELDS if field != "video_id")
HISTORY_SOURCE_FIELDS = ("apple_url", "melon_url", "spotify_url")

_COMPLETED_SNAPSHOT_SQL = """
EXISTS (
    SELECT 1
    FROM match_runs mr
    WHERE mr.service = p.service
      AND mr.job_name = p.job_name
      AND mr.source_variant = p.source_variant
      AND mr.reference_period = p.reference_period
      AND mr.status = 'completed'
      AND mr.completed_at IS NOT NULL
)
"""


def _is_history_v2(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == 2
        and isinstance(payload.get("tracks"), dict)
        and isinstance(payload.get("rankings"), dict)
    )


def _looks_like_history_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "")))


def _history_identity_exclusions(payload: Any = None, additions: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    existing = payload.get("identity_exclusions", []) if _is_history_v2(payload) else []
    if not isinstance(existing, list):
        raise ValueError("History identity exclusions must be a list")
    result, seen = [], {}
    for entry in [*existing, *additions]:
        if not isinstance(entry, dict) or set(entry) != {"video_ids", "reason", "evidence_ref"}:
            raise ValueError("History identity exclusion requires video IDs, reason and evidence reference")
        videos = entry["video_ids"]
        if (not isinstance(videos, list) or len(videos) < 2
                or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in videos)
                or len(set(videos)) != len(videos)
                or any(not isinstance(entry[k], str) or not entry[k].strip() for k in ("reason", "evidence_ref"))):
            raise ValueError("History identity exclusion is malformed")
        key = tuple(sorted(videos))
        value = {"video_ids": list(videos), "reason": entry["reason"], "evidence_ref": entry["evidence_ref"]}
        if key in seen and seen[key] != value:
            raise ValueError("Conflicting history identity exclusion evidence")
        if key not in seen:
            result.append(value)
            seen[key] = value
    return result


def _validate_excluded_identities(history: dict[str, list[dict[str, Any]]], exclusions: list[dict[str, Any]]) -> None:
    if not exclusions:
        return
    protected = {vid for exclusion in exclusions for vid in exclusion["video_ids"]}
    keys = {vid: {vid} for vid in protected}
    for rows in history.values():
        for row in rows:
            if row.get("video_id") in protected:
                identity = row.get("identity_key", row["video_id"])
                if not isinstance(identity, str) or not identity.strip():
                    raise ValueError("Excluded recording has an invalid history identity")
                keys[row["video_id"]].add(identity)
    for exclusion in exclusions:
        videos = exclusion["video_ids"]
        for index, video in enumerate(videos):
            if any(keys[video] & keys[other] for other in videos[index + 1:]):
                raise ValueError("Excluded recordings cannot share a history identity")


def inflate_frontend_history(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Return the legacy in-memory shape: date -> list of complete song rows."""
    if not isinstance(payload, dict):
        return {}

    if not _is_history_v2(payload):
        return {
            date: [dict(item) for item in rows if isinstance(item, dict)]
            for date, rows in payload.items()
            if _looks_like_history_date(date) and isinstance(rows, list)
        }

    tracks = payload.get("tracks") or {}
    rankings = payload.get("rankings") or {}
    raw_dates = payload.get("dates")
    dates = [
        date for date in (raw_dates if isinstance(raw_dates, list) else rankings.keys())
        if _looks_like_history_date(str(date))
    ]

    history: dict[str, list[dict[str, Any]]] = {}
    for date in dates:
        daily = rankings.get(date)
        if not isinstance(daily, dict):
            continue
        rows: list[dict[str, Any]] = []
        for video_id, ranking in daily.items():
            if not isinstance(ranking, dict):
                continue
            if "video_id" in ranking:
                raise ValueError("A history ranking cannot override its video ID")
            metadata = tracks.get(video_id) if isinstance(tracks.get(video_id), dict) else {}
            row = dict(metadata)
            row.update(ranking)
            row["video_id"] = video_id
            rows.append(row)
        rows.sort(key=lambda row: (row.get("hype_rank") or 9999, row.get("title") or "", row.get("video_id") or ""))
        history[date] = rows
    return history


def compact_frontend_history(
    history: dict[str, list[dict[str, Any]]],
    *,
    days: int = 31,
    generated_at: str | None = None,
    identity_exclusions: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Normalize date-keyed complete rows into shared track metadata plus daily rankings."""
    exclusions = _history_identity_exclusions(history, identity_exclusions)
    if _is_history_v2(history):
        history = inflate_frontend_history(history)
    _validate_excluded_identities(history, exclusions)
    dates = sorted(
        [date for date, rows in history.items() if _looks_like_history_date(date) and isinstance(rows, list)],
        reverse=True,
    )
    tracks: dict[str, dict[str, Any]] = {}
    rankings: dict[str, dict[str, dict[str, Any]]] = {}

    for date in dates:
        daily: dict[str, dict[str, Any]] = {}
        for row in sorted(history.get(date, []), key=lambda item: (item.get("hype_rank") or 9999, item.get("title") or "")):
            if not isinstance(row, dict):
                continue
            video_id = str(row.get("video_id") or "").strip()
            if not video_id:
                continue

            if video_id in daily:
                raise ValueError(f"History chart {date} repeats video ID {video_id}")
            metadata = {field: row.get(field, "") for field in HISTORY_METADATA_OVERRIDES}
            if any(not isinstance(value, str) for value in metadata.values()):
                raise ValueError(f"History chart {date} has non-text metadata")
            track = tracks.setdefault(video_id, {"video_id": video_id, **metadata})
            daily[video_id] = {field: row.get(field) for field in DAILY_RANKING_FIELDS}
            # Empty strings are meaningful overrides; missing overrides inherit.
            daily[video_id].update({field: value for field, value in metadata.items() if value != track[field]})
            if "identity_key" in row:
                key = row["identity_key"]
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("History identity keys must be nonempty strings")
                daily[video_id]["identity_key"] = key
        rankings[date] = daily

    referenced_video_ids = {
        video_id
        for daily in rankings.values()
        for video_id in daily.keys()
    }
    tracks = {
        video_id: tracks[video_id]
        for video_id in tracks
        if video_id in referenced_video_ids
    }

    return {
        "schema_version": 2,
        **({"identity_exclusions": exclusions} if exclusions else {}),
        "generated_at": generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "days": days,
        "dates": dates,
        "tracks": tracks,
        "rankings": rankings,
    }


def carry_history_identity(
    report: Iterable[dict[str, Any]],
    history: Any,
    *,
    identity_links: Iterable[dict[str, str]] = (),
    identity_exclusions: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Carry identity through exact source IDs or caller-verified replacement receipts.

    Links must reference retained verification evidence. This pure function does
    not verify recordings or infer equivalence from names, albums or current UIDs.
    The caller retains each evidence_ref in its repair manifest.
    """
    exclusions = _history_identity_exclusions(history, identity_exclusions)
    predecessors: dict[str, str] = {}
    for link in identity_links:
        old, new, evidence = (link.get(field) for field in ("old_video_id", "new_video_id", "evidence_ref"))
        if any(not isinstance(value, str) or not value.strip() for value in (old, new, evidence)) or old == new:
            raise ValueError("A history identity link requires distinct video IDs and an evidence reference")
        if new in predecessors and predecessors[new] != old:
            raise ValueError(f"Conflicting history identity links for {new}")
        predecessors[new] = old

    def origin(video_id: str) -> str:
        seen = set()
        while video_id in predecessors:
            if video_id in seen:
                raise ValueError("Cyclic history identity links")
            seen.add(video_id)
            video_id = predecessors[video_id]
        return video_id

    for video_id in predecessors:
        origin(video_id)
    for exclusion in exclusions:
        if len({origin(vid) for vid in exclusion["video_ids"]}) != len(exclusion["video_ids"]):
            raise ValueError("An explicit identity link joins excluded recordings")
    protected = {origin(vid) for exclusion in exclusions for vid in exclusion["video_ids"]}
    previous = inflate_frontend_history(history)
    _validate_excluded_identities(previous, exclusions)
    prior_rows = [row for date in sorted(previous) for row in previous[date]]
    parents: dict[tuple[str, str], tuple[str, str]] = {}

    def component(node: tuple[str, str]) -> tuple[str, str]:
        parents.setdefault(node, node)
        root = node
        while parents[root] != root:
            root = parents[root]
        while parents[node] != node:
            next_node = parents[node]
            parents[node] = root
            node = next_node
        return root

    def nodes(row: dict[str, Any]) -> list[tuple[str, str]]:
        video = origin(row["video_id"])
        # Shared source URLs cannot prove that two held recording versions are
        # equivalent. Explicit verified links may still identify aliases of one.
        source_scope = video if video in protected else ""
        return [("video", origin(row["video_id"]))] + [
            (field + ":" + source_scope, row[field]) for field in HISTORY_SOURCE_FIELDS if row.get(field)
        ]

    # Only prior verified rows and explicit receipts connect source identities.
    # An incoming row that would merge two existing components is ambiguous.
    for row in prior_rows:
        keys = nodes(row)
        for key in keys:
            parents[component(key)] = component(keys[0])
    prior_by_component: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in prior_rows:
        prior_by_component.setdefault(component(nodes(row)[0]), []).append(row)

    carried = []
    for source in report:
        row = dict(source)
        video_id = str(row.get("video_id") or "")
        matches = {component(key) for key in nodes(row) if key in parents}
        if len(matches) > 1:
            raise ValueError(f"History source bindings disagree for {video_id}")
        candidates = prior_by_component.get(next(iter(matches), None), [])
        explicit = {item["identity_key"] for item in candidates if item.get("identity_key")}
        if row.get("identity_key"):
            explicit.add(row["identity_key"])
        if len(explicit) > 1:
            raise ValueError(f"Conflicting verified history identities for {video_id}")
        key = next(iter(explicit), None)
        if key is None:
            key = origin(candidates[0]["video_id"]) if candidates else origin(video_id)
        if key != video_id or explicit:
            row["identity_key"] = key
        carried.append(row)
    _validate_excluded_identities({**previous, "incoming": carried}, exclusions)
    return carried


def repair_frontend_history(
    existing_payload: Any,
    reports_by_date: dict[str, list[dict[str, Any]]],
    *,
    identity_links: Iterable[dict[str, str]] = (),
    identity_exclusions: Iterable[dict[str, Any]] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Replace only explicit dates using frozen reports; never read a DB or prune.

    Build reports with build_hype_report_from_rows using captured raw rows joined
    to the manifest's chosen mapping. No live canonical lookup occurs here.
    """
    if not _is_history_v2(existing_payload) or not reports_by_date:
        raise ValueError("Scoped history repair requires existing schema 2 history and explicit dates")
    days = existing_payload.get("days", 31)
    for date in existing_payload.get("dates", []):
        validate_frontend_history(existing_payload, date, days=days)
    history = inflate_frontend_history(existing_payload)
    links = tuple(identity_links)
    exclusions = _history_identity_exclusions(existing_payload, identity_exclusions)
    for date in sorted(reports_by_date):
        if not _looks_like_history_date(date):
            raise ValueError(f"Invalid history repair date: {date}")
        previous_rows = {row["video_id"]: row for row in history.get(date, [])}
        incoming = [dict(row) for row in reports_by_date[date]]
        def identity_fields(row):
            return {key: value for key, value in (row or {}).items() if key not in DAILY_RANKING_FIELDS}
        changed = [index for index, row in enumerate(incoming)
                   if identity_fields(row) != identity_fields(previous_rows.get(row["video_id"]))]
        # A newer identity receipt must not rewrite untouched rows on an older
        # date. Identity-only repairs explicitly include the intended key.
        carried = carry_history_identity([incoming[index] for index in changed], history,
                                         identity_links=links, identity_exclusions=exclusions)
        for index, row in zip(changed, carried):
            incoming[index] = row
        history[date] = incoming
    payload = compact_frontend_history(history, days=days, generated_at=generated_at,
                                       identity_exclusions=exclusions)
    for date in payload["dates"]:
        validate_frontend_history(payload, date, days=days)
    unchanged = set(history) - set(reports_by_date)
    before, after = inflate_frontend_history(existing_payload), inflate_frontend_history(payload)
    if any(before[date] != after[date] for date in unchanged):
        raise ValueError("Scoped history repair changed an undeclared date")
    return payload


def _backup_legacy_history_file(path: Path, payload: Any) -> None:
    if not path.exists() or _is_history_v2(payload):
        return
    backup = path.with_name("history.v1.backup.json")
    if backup.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"history.v1.backup.{stamp}.json")
    shutil.copy2(path, backup)
    LOG.info("Backed up legacy frontend history to %s", backup)


def _apple_history_anchor_jobs() -> list[str]:
    return [
        name for name, item in hype_inputs().items()
        if item.get("hype_group") == "apple"
    ] or ["KR-Top-100"]


def latest_hype_history_date(conn) -> str | None:
    """Resolve the latest completed Apple anchor, never the wall-clock date."""
    jobs = _apple_history_anchor_jobs()
    placeholders = ",".join("?" for _ in jobs)
    rows = conn.execute(
        f"""SELECT p.job_name, MAX(p.reference_period) AS reference_period FROM playlist_order p
            WHERE p.job_name IN ({placeholders})
              AND p.reference_period LIKE '____-__-__'
              AND {_COMPLETED_SNAPSHOT_SQL}
            GROUP BY p.job_name""", jobs,
    ).fetchall()
    if not rows:
        return None
    periods = {row["reference_period"] for row in rows}
    if {row["job_name"] for row in rows} != set(jobs) or len(periods) != 1:
        raise RuntimeError("Completed Apple anchor jobs have missing or inconsistent reference periods")
    return _history_date_from_hype_anchor_reference(next(iter(periods)))


def validate_frontend_history(payload: Any, expected_date: str, *, days: int = 31) -> None:
    """Reject an empty, stale or malformed intended chart before publication."""
    if not _is_history_v2(payload) or payload.get("days") != days:
        raise ValueError("History has an invalid schema or retention policy")
    exclusions = _history_identity_exclusions(payload)
    if exclusions:
        _validate_excluded_identities(inflate_frontend_history(payload), exclusions)
    dates = payload.get("dates")
    if (not isinstance(dates, list) or not dates or len(dates) > days
            or any(not isinstance(day, str) or not _looks_like_history_date(day) for day in dates)
            or dates != sorted(set(dates), reverse=True)
            or set(dates) != set(payload["rankings"]) or expected_date not in dates):
        raise ValueError(f"History does not contain the intended chart date {expected_date}")
    newest = datetime.strptime(dates[0], "%Y-%m-%d")
    cutoff = newest - timedelta(days=days - 1)
    if any(not cutoff <= datetime.strptime(day, "%Y-%m-%d") <= newest for day in dates):
        raise ValueError("History exceeds its retention window")
    daily = payload["rankings"][expected_date]
    if not isinstance(daily, dict) or not daily:
        raise ValueError(f"History chart {expected_date} is empty")
    ranks = []
    identities = set()
    for video_id, ranking in daily.items():
        track = payload["tracks"].get(video_id)
        if (not isinstance(video_id, str) or not video_id.strip()
                or not isinstance(track, dict) or track.get("video_id") != video_id
                or not isinstance(ranking, dict)):
            raise ValueError(f"History chart {expected_date} has invalid track metadata")
        allowed = {*DAILY_RANKING_FIELDS, *HISTORY_METADATA_OVERRIDES, "identity_key"}
        if set(ranking) - allowed:
            raise ValueError(f"History chart {expected_date} has unsupported ranking fields")
        row = {**track, **ranking}
        if (any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("title", "artist"))
                or any(not isinstance(row.get(key, ""), str) for key in HISTORY_METADATA_OVERRIDES)):
            raise ValueError(f"History chart {expected_date} has invalid hydrated metadata")
        identity = row.get("identity_key", video_id)
        if not isinstance(identity, str) or not identity.strip() or identity in identities:
            raise ValueError(f"History chart {expected_date} has invalid or duplicate song identity")
        identities.add(identity)
        rank, score = ranking.get("hype_rank"), ranking.get("hype_index")
        if (type(rank) is not int or rank <= 0
                or type(score) not in (int, float) or not math.isfinite(score) or score < 0
                or any(value is not None and (type(value) is not int or value <= 0)
                       for value in (ranking.get(key) for key in DAILY_RANKING_FIELDS[2:]))):
            raise ValueError(f"History chart {expected_date} has invalid scores or ranks")
        ranks.append(rank)
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise ValueError(f"History chart {expected_date} has duplicate or missing ranks")


def export_frontend_history(
    db_path: str | Path,
    output_path: str | Path,
    *,
    days: int = 31,
    full_rebuild: bool = False,
    expected_date: str | None = None,
    reports_by_date: dict[str, list[dict[str, Any]]] | None = None,
    identity_links: Iterable[dict[str, str]] = (),
    identity_exclusions: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Export current charts, optionally from a caller's frozen publication report.

    Supplied reports bypass DB access. Historical repairs use the pure scoped
    helper instead, so normal retention cannot expire an unrelated date.
    """
    out = Path(output_path)
    existing_payload: Any = {}
    previous_hash = None
    if out.exists():
        raw = out.read_bytes()
        previous_hash = hashlib.sha256(raw).hexdigest()
        existing_payload = json.loads(raw)
    previous_history = inflate_frontend_history(existing_payload)
    exclusions = _history_identity_exclusions(existing_payload, identity_exclusions)
    if reports_by_date is None:
        path = Path(db_path)
        if not path.exists() and not os.environ.get("SUPABASE_DB_URL"):
            raise RuntimeError(f"History database not found: {path}")
        with connect(path, read_only=True) as conn:
            apple_playlists = _apple_history_anchor_jobs()
            placeholders = ",".join("?" for _ in apple_playlists)
            limit = days if full_rebuild or not out.exists() else 1
            date_rows = conn.execute(
                f"""
                SELECT DISTINCT p.reference_period AS chart_date
                FROM playlist_order p
                WHERE p.job_name IN ({placeholders})
                  AND p.reference_period LIKE '____-__-__'
                  AND {_COMPLETED_SNAPSHOT_SQL}
                ORDER BY chart_date DESC LIMIT ?
                """, (*apple_playlists, limit),
            ).fetchall()
            dates = [_history_date_from_hype_anchor_reference(row["chart_date"]) for row in date_rows]
            rows_by_date = fetch_hype_rows_for_dates(conn, dates)
            reports_by_date = {date: build_hype_report_from_rows(rows_by_date.get(date, [])) for date in dates}
    if not reports_by_date:
        raise RuntimeError("History export has no completed Apple anchor dates")
    dates = sorted(reports_by_date)
    intended_date = expected_date or max(dates)
    if intended_date not in dates:
        raise RuntimeError(f"History export did not rebuild intended chart date {intended_date}")
    if previous_history and max(dates) < max(previous_history):
        raise ValueError("Use scoped history repair for a historical-only update")
    history = {} if full_rebuild else dict(previous_history)
    links = tuple(identity_links)
    for date in dates:
        # Include the prior version of this date when inheriting a repaired key.
        identity_history = {**previous_history, **history}
        identity_history = {day: rows for day, rows in identity_history.items() if day <= date}
        history[date] = carry_history_identity(reports_by_date[date], identity_history, identity_links=links,
                                               identity_exclusions=exclusions)
        validate_frontend_history(compact_frontend_history({date: history[date]}, days=days,
                                  identity_exclusions=exclusions), date, days=days)
    history = prune_history(history, reference_date=max(dates), days=days)
    payload = compact_frontend_history(history, days=days, identity_exclusions=exclusions)
    return write_frontend_history(payload, out, expected_date=intended_date, expected_file_sha256=previous_hash)


def write_frontend_history(
    payload: dict[str, Any],
    output_path: str | Path,
    *,
    expected_date: str,
    expected_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate all hydrated dates and atomically write under caller-owned exclusion."""
    out = Path(output_path)
    days = payload.get("days", 31)
    intended_date = expected_date
    validate_frontend_history(payload, intended_date, days=days)
    for date in payload["dates"]:
        validate_frontend_history(payload, date, days=days)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=out.parent,
            prefix=f".{out.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        validate_frontend_history(
            json.loads(temporary_path.read_text(encoding="utf-8")), intended_date, days=days,
        )
        current = out.read_bytes() if out.exists() else None
        if expected_file_sha256 is not None and (current is None or hashlib.sha256(current).hexdigest() != expected_file_sha256):
            raise RuntimeError("History changed since the validated snapshot was captured")
        if current is not None:
            _backup_legacy_history_file(out, json.loads(current))
        os.replace(temporary_path, out)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return payload


def _history_date_from_hype_anchor_reference(reference_period: str) -> str:
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(reference_period, "%Y-%m-%d")
        return (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return reference_period


def _daily_reference_cutoff_for_history_date(history_date: str) -> str:
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(history_date, "%Y-%m-%d")
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return history_date


def _weekly_reference_cutoff_for_history_date(
    history_date: str, input_config: dict[str, dict[str, Any]],
) -> str:
    """Use the last scheduled chart day, not a future week within the same ISO week.

    This is a schedule-based reconstruction: legacy completion timestamps can
    contain migration/backfill times and cannot reproduce actual publication delays.
    """
    for job_name, item in input_config.items():
        if item.get("hype_group") != "ytmusic":
            continue
        chart_day = datetime.strptime(history_date, "%Y-%m-%d")
        schedule = str(item.get("schedule") or "").strip().lower()
        if schedule:
            weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            chart_day -= timedelta(days=(chart_day.weekday() - weekdays.index(schedule)) % 7)
        # YouTube's weekly source period ends on Thursday. A Monday collection
        # therefore belongs to the previous ISO week, not that Monday's week.
        chart_day -= timedelta(days=(chart_day.weekday() - 3) % 7)
        return reference_period_for_date(job_name, chart_day.strftime("%Y-%m-%d"))
    return history_date


def prune_history(
    history: dict[str, list[dict[str, Any]]],
    *,
    reference_date: str,
    days: int,
) -> dict[str, list[dict[str, Any]]]:
    from datetime import datetime, timedelta

    try:
        reference_dt = datetime.strptime(reference_date, "%Y-%m-%d")
        cutoff = reference_dt - timedelta(days=max(days - 1, 0))
    except Exception:
        reference_dt = None
        cutoff = None
    filtered: dict[str, list[dict[str, Any]]] = {}
    for date in sorted(history.keys(), reverse=True):
        if cutoff:
            try:
                current_dt = datetime.strptime(date, "%Y-%m-%d")
                if current_dt > reference_dt or current_dt < cutoff:
                    continue
            except Exception:
                continue
        filtered[date] = history[date]
    return filtered


def previous_apple_videos_for_history(
    conn: sqlite3.Connection,
    history: Any,
    date: str,
) -> set[str]:
    inflated_history = inflate_frontend_history(history)
    previous_dates = sorted([item for item in inflated_history if item < date], reverse=True)
    if previous_dates:
        return {row["video_id"] for row in inflated_history[previous_dates[0]] if row.get("apple_rank")}

    apple_playlists = _apple_history_anchor_jobs()
    placeholders = ",".join("?" for _ in apple_playlists)
    row = conn.execute(
        f"""
        SELECT DISTINCT p.reference_period AS chart_date
        FROM playlist_order p
        WHERE p.job_name IN ({placeholders})
          AND p.reference_period LIKE '____-__-__'
          AND p.reference_period < ?
          AND {_COMPLETED_SNAPSHOT_SQL}
        ORDER BY chart_date DESC
        LIMIT 1
        """,
        (*apple_playlists, _daily_reference_cutoff_for_history_date(date)),
    ).fetchone()
    if not row:
        return set()
    previous_date = _history_date_from_hype_anchor_reference(row["chart_date"])
    report = build_hype_report_from_rows(fetch_hype_rows_for_dates(conn, [previous_date]).get(previous_date, []))
    return {item["video_id"] for item in report if item.get("apple_rank")}


def fetch_hype_rows_for_dates(
    conn: sqlite3.Connection, dates: list[str], *, include_unmatched: bool = False,
) -> dict[str, list[Any]]:
    if not dates:
        return {}
    input_config = hype_inputs()
    date_pairs = [
        (
            date,
            _daily_reference_cutoff_for_history_date(date),
            _weekly_reference_cutoff_for_history_date(date, input_config),
        )
        for date in dates
    ]
    values_sql = ", ".join("(?, ?, ?)" for _ in date_pairs)
    params: list[Any] = []
    for history_date, daily_cutoff, target_week in date_pairs:
        params.extend([history_date, daily_cutoff, target_week])
    rows = conn.execute(
        f"""
        WITH dates(chart_date, daily_cutoff, target_week) AS (
            VALUES {values_sql}
        ),
        effective AS (
            SELECT
                d.chart_date,
                p.service,
                p.job_name,
                p.source_variant,
                MAX(p.reference_period) AS eff_period
            FROM dates d
            JOIN playlist_order p
              ON (
                    (
                        p.reference_period NOT LIKE '%-W%'
                        AND p.reference_period <= d.daily_cutoff
                    )
                    OR (
                        p.reference_period LIKE '%-W%'
                        AND p.reference_period <= d.target_week
                    )
                 )
             AND {_COMPLETED_SNAPSHOT_SQL}
            GROUP BY d.chart_date, p.service, p.job_name, p.source_variant
        )
        SELECT
            e.chart_date,
            ps.track_uid,
            p.service,
            p.job_name,
            p.source_variant,
            p.reference_period,
            p.song_id,
            mr.run_id,
            ma.status,
            ma.video_id AS matched_video_id,
            tl.album_id,
            p.rank_order,
            t.canonical_yt_video_id AS video_id,
            t.yt_title,
            t.yt_artist,
            t.yt_album,
            COALESCE(NULLIF(tl.title_ko, ''), tl.title_en) AS title,
            COALESCE(NULLIF(tl.artist_ko, ''), tl.artist_en) AS artist,
            COALESCE(NULLIF(tl.album_ko, ''), tl.album_en) AS album,
            tl.title_ko, tl.artist_ko, tl.title_en, tl.artist_en,
            tl.artwork_url
        FROM playlist_order p
        JOIN effective e
          ON e.service = p.service
         AND e.job_name = p.job_name
         AND e.source_variant = p.source_variant
         AND p.reference_period = e.eff_period
        JOIN match_runs mr
          ON mr.service = p.service AND mr.job_name = p.job_name
         AND mr.source_variant = p.source_variant AND mr.reference_period = p.reference_period
         AND mr.status = 'completed' AND mr.completed_at IS NOT NULL
         AND NOT EXISTS (
             SELECT 1 FROM match_runs newer
             WHERE newer.service = mr.service AND newer.job_name = mr.job_name
               AND newer.source_variant = mr.source_variant AND newer.reference_period = mr.reference_period
               AND newer.status = 'completed' AND newer.completed_at IS NOT NULL
               AND (newer.created_at > mr.created_at
                    OR (newer.created_at = mr.created_at AND newer.run_id > mr.run_id))
         )
        LEFT JOIN match_attempts ma
          ON ma.run_id = mr.run_id AND ma.service = p.service AND ma.song_id = p.song_id
        JOIN platform_song_ids ps
          ON ps.service = p.service
         AND ps.song_id = p.song_id
        JOIN tracks t ON t.track_uid = ps.track_uid
        LEFT JOIN track_list tl
            ON LOWER(tl.service) = LOWER(p.service)
           AND tl.song_id = p.song_id
        WHERE t.canonical_yt_video_id IS NOT NULL
          AND t.canonical_yt_video_id != ''
          AND (? OR (ma.status IN ('matched', 'cached_match', 'proxy_matched', 'manual_override')
                     AND COALESCE(ma.video_id, '') != ''))
          AND NOT EXISTS (
              SELECT 1 FROM manual_overrides mo
              WHERE mo.service = p.service AND mo.song_id = p.song_id
                AND LOWER(mo.action) IN ('block', 'manual_blocked')
          )
        ORDER BY e.chart_date, p.service, p.job_name, p.source_variant, p.rank_order, p.song_id
        """,
        (*params, include_unmatched),
    ).fetchall()
    out: dict[str, list[Any]] = {date: [] for date in dates}
    for row in rows:
        out.setdefault(row["chart_date"], []).append(row)
    return out


def hype_report_for_date(
    conn: sqlite3.Connection,
    chart_date: str,
    *,
    previous_apple_videos: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows = fetch_hype_rows_for_dates(conn, [chart_date]).get(chart_date, [])
    return build_hype_report_from_rows(rows, previous_apple_videos=previous_apple_videos)


def _hype_grouped_rows(rows):
    """Keep contributor selection identical to the report's ordered grouping."""
    group_by_video, group_by_identity = {}, {}
    for row in rows:
        identity = hype_identity_key(row)
        video_id = row["video_id"] or ""
        uid = group_by_video.get(video_id) or group_by_identity.get(identity) or identity
        group_by_identity.setdefault(identity, uid)
        if video_id:
            group_by_video.setdefault(video_id, uid)
        yield uid, row


def _hype_input(row, input_config):
    job = str(row["job_name"] or "").strip()
    group = (input_config.get(job) or {}).get("hype_group", "")
    weight = float((input_config.get(job) or {}).get("hype_weight") or 0.0)
    if not group and normalized_service(row["service"]) == "melon" and job == "Gen-Z-Daily":
        return "melon_genz", DEFAULT_HYPE_WEIGHTS["melon_genz"]
    return group, weight


def contributing_hype_rows(rows):
    """Include every enrichment row of a scored group, including playback selectors."""
    grouped = list(_hype_grouped_rows(rows))
    config, scored = hype_inputs(), set()
    for uid, row in grouped:
        group, weight = _hype_input(row, config)
        if (group in {"apple", "ytmusic"} or
                (group == "melon_genz" and str(row["source_variant"] or "default").strip() == "combined")):
            if calculate_rank_score(row["rank_order"]) * weight > 0:
                scored.add(uid)
    return [row for uid, row in grouped if uid in scored]


def build_hype_report_from_rows(
    rows: Iterable[Any],
    *,
    previous_apple_videos: set[str] | None = None,
) -> list[dict[str, Any]]:
    previous_apple_videos = previous_apple_videos or set()
    input_config = hype_inputs()

    grouped: dict[str, dict[str, Any]] = {}
    for uid, row in _hype_grouped_rows(rows):
        item = grouped.setdefault(
            uid,
            {
                "video_id": row["video_id"],
                "title": "",
                "artist": "",
                "album": "",
                "yt_title": row["yt_title"] or "",
                "yt_artist": row["yt_artist"] or "",
                "yt_album": row["yt_album"] or "",
                "artwork_url": "",
                "apple_url": "",
                "melon_url": "",
                "spotify_url": "",
                "apple_rank": None,
                "melon_rank": None,
                "melon_genz_rank": None,
                "ytmusic_rank": None,
                "_score_parts": {},
                "_best_service_priority": 9999,
                "_artwork_by_service": {},
            },
        )
        service = normalized_service(row["service"])
        if row["artwork_url"] and service not in item["_artwork_by_service"]:
            item["_artwork_by_service"][service] = row["artwork_url"]
        job_name = str(row["job_name"] or "").strip()
        source_variant = str(row["source_variant"] or "default").strip()
        track_url = build_track_url(service, row["song_id"], row["album_id"])
        group, weight = _hype_input(row, input_config)
        if group == "apple":
            item["apple_rank"] = min(item["apple_rank"] or 9999, row["rank_order"])
            item["apple_url"] = track_url or item["apple_url"]
            item["_score_parts"]["apple"] = max(item["_score_parts"].get("apple", 0.0), calculate_rank_score(row["rank_order"]) * weight)
        elif group == "melon_genz":
            item["melon_url"] = track_url or item["melon_url"]
            if source_variant == "combined":
                item["melon_genz_rank"] = min(item["melon_genz_rank"] or 9999, row["rank_order"])
                item["_score_parts"]["melon_genz"] = max(
                    item["_score_parts"].get("melon_genz", 0.0),
                    calculate_rank_score(row["rank_order"]) * weight,
                )
        elif group == "ytmusic":
            item["ytmusic_rank"] = min(item["ytmusic_rank"] or 9999, row["rank_order"])
            item["_score_parts"]["ytmusic"] = max(item["_score_parts"].get("ytmusic", 0.0), calculate_rank_score(row["rank_order"]) * weight)
        elif service == "melon" and job_name == "Top-100-Daily":
            item["melon_rank"] = min(item["melon_rank"] or 9999, row["rank_order"])
            item["melon_url"] = track_url or item["melon_url"]
        elif service == "spotify":
            item["spotify_url"] = track_url or item["spotify_url"]
        service_priority = {"apple": 0, "melon": 1, "spotify": 2, "ytmusic": 3}.get(service, 4)
        best_rank = min(item.get("_best_rank", 9999), 9999)
        best_service_priority = min(item.get("_best_service_priority", 9999), 9999)
        if row["rank_order"] and (
            not item["title"]
            or row["rank_order"] < best_rank
            or (row["rank_order"] == best_rank and service_priority < best_service_priority)
        ):
            item["_best_rank"] = row["rank_order"]
            item["_best_service_priority"] = service_priority
            item["video_id"] = row["video_id"] or item["video_id"]
            item["yt_title"] = row["yt_title"] or item["yt_title"]
            item["yt_artist"] = row["yt_artist"] or item["yt_artist"]
            item["yt_album"] = row["yt_album"] or item["yt_album"]
            item["title"] = row["title"] or item["title"]
            item["artist"] = row["artist"] or item["artist"]
            item["album"] = row["album"] or item["album"]
            item["artwork_url"] = row["artwork_url"] or item["artwork_url"]

    report = []
    for item in grouped.values():
        artwork_by_service = item.pop("_artwork_by_service", {})
        score_parts = item.pop("_score_parts", {})
        hype_index = sum(score_parts.values())
        item.pop("_best_rank", None)
        item.pop("_best_service_priority", None)
        item["title"] = item["title"] or item["yt_title"] or ""
        item["artist"] = item["artist"] or item["yt_artist"] or ""
        item["album"] = item["album"] or item["yt_album"] or ""
        item["artwork_url"] = (
            artwork_by_service.get("apple")
            or artwork_by_service.get("melon")
            or artwork_by_service.get("spotify")
            or artwork_by_service.get("ytmusic")
            or item["artwork_url"]
        )
        if item["apple_rank"] in (9999, None):
            item["apple_rank"] = None
        if item["melon_rank"] in (9999, None):
            item["melon_rank"] = None
        if item["melon_genz_rank"] in (9999, None):
            item["melon_genz_rank"] = None
        if item["ytmusic_rank"] in (9999, None):
            item["ytmusic_rank"] = None
        item.update(
            {
                "hype_index": round(hype_index, 2)
            }
        )
        if hype_index > 0:
            report.append(item)

    report.sort(
        key=lambda row: (
            -float(row["hype_index"]),
            row["apple_rank"] or 9999,
            row["melon_genz_rank"] or 9999,
            row["melon_rank"] or 9999,
            row["ytmusic_rank"] or 9999,
            row["title"] or "",
            row["video_id"] or "",
        )
    )
    for index, row in enumerate(report, 1):
        row["hype_rank"] = index
    return report[:200]
