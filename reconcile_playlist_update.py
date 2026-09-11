"""Explicit, scoped playlist recovery. Observation-only unless --apply is supplied.

Stop scheduled/manual workers before applying. A database claim prevents stale
writers from committing further intents; it cannot cancel an in-flight API call.
Only playlist items/order are recovered, not title or description.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ytmusic_playlist_sync import (
    PlaylistMutationUncertain,
    _addition_receipts,
    _audit_playlist_items,
    _compare_playlist_video_ids,
    _identity_review_required,
    _recoverable_playlist_items,
    _replace_playlist_contents,
    _same_owned_slots,
    bilingual_cache_read_only,
    get_existing_playlist_items,
    make_ytmusic,
)


def reconcile_playlist_update(
    ytmusic: Any, db_path: str | Path, run_id: str, playlist_id: str, *,
    apply: bool = False, workers_quiescent: bool = False, reclaim_recovery: bool = False,
    append_missing_last: bool = False,
) -> dict[str, Any]:
    from hype_db import get_playlist_update_run

    run = get_playlist_update_run(db_path, run_id, read_only=True)
    if not run or run.get("playlist_id") != playlist_id:
        raise ValueError("Requested run and playlist do not identify the same audit")
    requested = run.get("requested_video_ids", [])
    existing = run.get("existing_video_ids", [])
    if (len(requested) != run.get("requested_count") or not requested
            or len(existing) != run.get("existing_count")):
        raise RuntimeError("Audit snapshots are incomplete; refusing reconciliation")
    original_requested = list(requested)
    partial_policy = {}
    if run.get("publication_mode") == "partial":
        requested = run.get("effective_video_ids") or []
        if not requested or [item for item in original_requested if item in set(requested)] != requested:
            raise RuntimeError("Partial publication is missing a valid ordered effective target")
        partial_policy = {"publication_mode": "partial", "effective_video_ids": requested,
                          "excluded_items": run.get("excluded_items") or []}
    current = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
    actual_ids = [item["videoId"] for item in current]
    requested_comparison = _compare_playlist_video_ids(ytmusic, requested, actual_ids)
    existing_comparison = _compare_playlist_video_ids(ytmusic, existing, actual_ids)
    payload = run.get("recovery_payload") or {}
    tail_attempted = any(event.get("phase") == "reconcile_tail" and event.get("operation") == "add"
                         for event in payload.get("events", []))
    mutations = [event for event in payload.get("events", [])
                 if event.get("operation") in {"add", "remove"}]
    acknowledged_intents = {event.get("intent_seq") for event in mutations if event.get("state") == "ack"}
    unresolved_mutation = any(
        event.get("state") == "ambiguous"
        or (event.get("state") == "intent" and event.get("seq") not in acknowledged_intents)
        for event in mutations
    )
    differences = run.get("differences", [])
    rejected_events = [event for event in payload.get("events", [])
                       if event.get("identity_review_required") and event.get("differences")]
    if rejected_events:
        differences = rejected_events[-1]["differences"]
    review_required = bool(run.get("identity_review_required")) or _identity_review_required(differences) or any(
        event.get("identity_review_required") for event in payload.get("events", [])
    )
    review_checks = []
    if review_required and existing_comparison["matches"] and not requested_comparison["matches"]:
        rejected = [row for row in differences if _identity_review_required([row])]
        for row in rejected:
            review_checks.append(_compare_playlist_video_ids(
                ytmusic, [row["expected_id"]], [row["actual_id"]],
            ))
        review_required = not review_checks or not all(row["matches"] for row in review_checks)

    # Reconstruct ownership before selecting an observation-only outcome. A
    # rejected new slot may expose the same video ID as the original snapshot.
    owned, ownership_error = None, "Ownership or identity evidence is insufficient"
    newly_observed_rejection = False
    try:
        candidate = _recoverable_playlist_items(run)
        if _same_owned_slots(current, candidate):
            owned = candidate
            mutations = [event for event in payload.get("events", [])
                         if event.get("operation") in {"add", "remove"}]
            last = mutations[-1] if mutations else {}
            owned_ids = [item["videoId"] for item in owned]
            newly_observed_rejection = bool(
                owned_ids and owned_ids == requested[:len(owned_ids)]
                and last.get("phase") == "publish" and last.get("operation") == "add"
                and last.get("state") == "ack"
                and _identity_review_required(requested_comparison["differences"])
            )
            review_required = review_required or newly_observed_rejection
    except (ValueError, KeyError, RuntimeError) as exc:
        ownership_error = str(exc)

    # Version-0 audits never gain historical ownership. Their explicitly
    # authorized tail append can still prove that single new operation, including
    # a crash after its ACK and before the new substitution was compared.
    tail_acks = [event for event in payload.get("events", [])
                 if event.get("phase") == "reconcile_tail"
                 and event.get("operation") == "add" and event.get("state") == "ack"]
    if tail_acks:
        try:
            tail_after = _audit_playlist_items(tail_acks[-1].get("after_items"))
            if len(tail_after) == len(requested) and _same_owned_slots(current, tail_after):
                newly_observed_rejection = newly_observed_rejection or _identity_review_required(
                    requested_comparison["differences"]
                )
                review_required = review_required or newly_observed_rejection
        except (ValueError, KeyError, RuntimeError):
            pass  # Incomplete legacy append evidence never grants ownership.

    action, reason = "blocked", ownership_error
    prefix_comparison = None
    if requested_comparison["matches"]:
        action, reason = "finalize_requested", "Current playlist verifies against the original request"
        review_required = False  # Fresh requested-state proof resolves the old rejection, not its history.
    elif unresolved_mutation and existing_comparison["matches"]:
        reason = "An unresolved mutation may still take effect; matching the old snapshot cannot finalize recovery"
    elif append_missing_last:
        if run.get("evidence_version") != 0:
            reason = "Append-last recovery is limited to an explicitly scoped legacy audit"
        elif tail_attempted:
            reason = "A tail append was already attempted; do not retry an unresolved provider operation"
        elif len(requested) < 2 or len(current) != len(requested) - 1 or len(set(requested)) != len(requested):
            reason = "Append-last requires exactly one missing trailing item from a unique original request"
        else:
            prefix_comparison = _compare_playlist_video_ids(ytmusic, requested[:-1], actual_ids)
            if prefix_comparison["matches"]:
                action, reason = "append_missing_last", "Verified complete prefix; append only the one requested tail item"
            else:
                reason = "Current playlist is not the verified original request prefix; no append is permitted"
    elif existing_comparison["matches"] and not review_required:
        action, reason = "finalize_restored", "Current playlist verifies against the original item snapshot"
    elif review_required and existing_comparison["matches"]:
        reason = "Items may be restored, but rejected substitution still requires identity review"
    elif owned is not None:
        action, reason = "restore_owned_items", "Complete durable receipts cover the current slots"
    report = {
        "update_run_id": run_id, "playlist_id": playlist_id, "status": run["status"],
        "action": action, "reason": reason, "applied": False,
        "requested_comparison": requested_comparison, "existing_comparison": existing_comparison,
        "prefix_comparison": prefix_comparison,
        "identity_review_required": review_required, "review_checks": review_checks,
        "actual_items": current, "item_mutations": 0,
        "publication_mode": "partial" if partial_policy else "full",
        "original_requested_count": len(original_requested), "effective_count": len(requested),
    }
    if not apply:
        return report
    if not workers_quiescent:
        raise RuntimeError("Applying reconciliation requires confirmation that all playlist workers are stopped")
    if run["status"] not in {"running", "mutation_failed", "recovery_required"}:
        raise RuntimeError("Audit is no longer pending; no reconciliation is necessary")
    if action == "blocked":
        raise RuntimeError(reason)

    from hype_db import (
        append_playlist_update_evidence, claim_playlist_update_recovery, finish_playlist_update,
    )
    claimed = claim_playlist_update_recovery(
        db_path, run_id, expected_statuses=(run["status"],),
        expected_claim_token=run.get("claim_token") or "",
        expected_state_fingerprint=run.get("state_fingerprint") or "",
        workers_quiescent=workers_quiescent,
        attestation="Operator confirmed all scheduled/manual playlist workers are stopped",
        allow_reclaim=reclaim_recovery,
        append_missing_last=action == "append_missing_last",
    )
    token = claimed["claim_token"]
    fingerprint = claimed.get("state_fingerprint") or ""

    def evidence(event):
        nonlocal fingerprint
        stored = append_playlist_update_evidence(
            db_path, run_id, event, claim_token=token,
            expected_state_fingerprint=fingerprint,
        )
        fingerprint = stored.get("state_fingerprint") or fingerprint
        return stored

    def finish(status, items, comparison, error="", complete=True, *, review=None, restored=None):
        return finish_playlist_update(
            db_path, run_id, status=status, actual_video_ids=[item["videoId"] for item in items],
            actual_items=items, observation_complete=complete, differences=comparison["differences"],
            error=error, claim_token=token,
            expected_state_fingerprint=fingerprint,
            identity_review_required=review, restore_verified=restored,
        )

    comparison = requested_comparison
    try:
        fresh = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
        if fresh != current:
            raise RuntimeError("Playlist changed after recovery planning; refusing reconciliation")
        if newly_observed_rejection:
            evidence({
                "phase": "reconcile", "operation": "observe", "state": "verified",
                "chunk_order": 0, "attempt": 1, "items": fresh,
                "verification_matches": False, "identity_review_required": True,
                "differences": requested_comparison["differences"],
            })
        if action == "restore_owned_items":
            expected = _replace_playlist_contents(
                ytmusic, playlist_id, fresh, existing, evidence=evidence,
                phase="restore", allow_duplicates=True,
            )
            fresh = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(fresh, expected):
                raise RuntimeError("Restored playlist differs from acknowledged item ownership")
            report["item_mutations"] = 1
        elif action == "append_missing_last":
            # This new before-snapshot proves only this append's ownership.
            # It does not manufacture receipts for the legacy failed update.
            intent = evidence({
                "phase": "reconcile_tail", "operation": "add", "state": "intent",
                "chunk_order": 1, "attempt": 1,
                "items": [{"videoId": requested[-1]}], "before_items": fresh,
                "verification_matches": True, "differences": prefix_comparison["differences"],
            })
            try:
                result = ytmusic.add_playlist_items(playlist_id, [requested[-1]], duplicates=False)
                acknowledged = _addition_receipts(result, [requested[-1]])
                expected = _audit_playlist_items(fresh + acknowledged)
                evidence({
                    "phase": "reconcile_tail", "operation": "add", "state": "ack",
                    "chunk_order": 1, "attempt": 1, "intent_seq": intent["seq"],
                    "items": acknowledged, "after_items": expected,
                })
            except Exception as exc:
                try:
                    evidence({
                        "phase": "reconcile_tail", "operation": "add", "state": "ambiguous",
                        "chunk_order": 1, "attempt": 1, "intent_seq": intent["seq"],
                        "items": [{"videoId": requested[-1]}], "error": f"{type(exc).__name__}: {exc}",
                    })
                except Exception:
                    pass  # The durable unresolved intent remains a no-retry barrier.
                raise PlaylistMutationUncertain("Tail append acknowledgement is uncertain; retry is forbidden") from exc
            fresh = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
            if not _same_owned_slots(fresh, expected):
                raise RuntimeError("Tail append changed existing slots/order or lacks the acknowledged final slot")
            report["item_mutations"] = 1
        target = requested if action in {"finalize_requested", "append_missing_last"} else existing
        comparison = _compare_playlist_video_ids(ytmusic, target, [item["videoId"] for item in fresh])
        if not comparison["matches"]:
            if _identity_review_required(comparison["differences"]):
                evidence({
                    "phase": "reconcile_tail" if action == "append_missing_last" else "reconcile",
                    "operation": "observe", "state": "verified",
                    "chunk_order": 0, "attempt": 1, "items": fresh,
                    "observation_complete": True, "verification_matches": False,
                    "identity_review_required": True, "differences": comparison["differences"],
                })
            raise RuntimeError("Playlist no longer verifies against the approved recovery target")
        evidence({
            "phase": "reconcile_tail" if action == "append_missing_last" or tail_attempted else "reconcile",
            "operation": "observe", "state": "verified",
            "chunk_order": 0, "attempt": 1, "items": fresh,
            "reconciliation_action": action, "review_checks": review_checks,
            "observation_complete": True, "verification_matches": True,
            **(partial_policy if action == "finalize_requested" else {}),
        })
        needs_review = review_required and action == "restore_owned_items"
        if needs_review:
            evidence({
                "phase": "restore", "operation": "observe", "state": "verified",
                "chunk_order": 0, "attempt": 1, "items": fresh,
                "restore_verified": True, "identity_review_required": True,
            })
        status = "recovery_required" if needs_review else (
            "published" if action in {"finalize_requested", "append_missing_last"} else "restored"
        )
        finish(status, fresh, requested_comparison if needs_review else comparison,
               review=needs_review, restored=action in {"finalize_restored", "restore_owned_items"})
        report.update(applied=True, actual_items=fresh, status=status, identity_review_required=needs_review)
        return report
    except Exception as exc:
        try:
            actual = get_existing_playlist_items(ytmusic, playlist_id)
            complete = True
        except Exception:
            actual, complete = [], False
        finish("recovery_required", actual, comparison, str(exc), complete,
               review=True if _identity_review_required(comparison["differences"]) else None)
        raise


def main() -> int:
    from sync_all import load_env_file

    root = Path(__file__).resolve().parent
    load_env_file(root / ".env")
    load_env_file(root / ".secrets" / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(root / "hype_wave_data.db"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--playlist-id", required=True)
    parser.add_argument("--yt-auth", default=str(root / ".secrets" / "browser.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--workers-quiescent", action="store_true")
    parser.add_argument("--reclaim-recovery", action="store_true",
                        help="Explicitly take over an interrupted recovery after stopping its worker")
    parser.add_argument("--append-missing-last", action="store_true",
                        help="Explicitly append one missing legacy request tail; never remove existing items")
    args = parser.parse_args()
    if args.apply and not args.workers_quiescent:
        parser.error("--apply requires --workers-quiescent after stopping all workers")
    with bilingual_cache_read_only():
        result = reconcile_playlist_update(
            make_ytmusic(args.yt_auth), args.db_path, args.run_id, args.playlist_id,
            apply=args.apply, workers_quiescent=args.workers_quiescent,
            reclaim_recovery=args.reclaim_recovery,
            append_missing_last=args.append_missing_last,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
