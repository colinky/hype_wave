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
    _compare_exact_playlist_video_ids,
    _identity_review_required,
    _preserve_playlist_slots,
    _playlist_item_keys,
    _recoverable_playlist_items,
    _same_owned_slots,
    bilingual_cache_read_only,
    get_existing_playlist_items,
    make_ytmusic,
)


def reconcile_playlist_update(
    ytmusic: Any, db_path: str | Path, run_id: str, playlist_id: str, *,
    apply: bool = False, workers_quiescent: bool = False, reclaim_recovery: bool = False,
    append_missing_last: bool = False, confirm_observed_move: bool = False,
    complete_requested: bool = False,
    playability_verifier: Any = None, before_mutation: Any = None,
) -> dict[str, Any]:
    from hype_db import get_playlist_update_run
    from sync_validation import PlaybackBlocked, require_playable, verifier_for

    if confirm_observed_move and (not workers_quiescent or append_missing_last):
        raise ValueError("Observed-move confirmation requires stopped workers and cannot combine with tail append")
    if complete_requested and (not workers_quiescent or append_missing_last):
        raise ValueError("Completing the original request requires stopped workers and cannot combine with tail append")
    run = get_playlist_update_run(db_path, run_id, read_only=True)
    if not run or run.get("playlist_id") != playlist_id:
        raise ValueError("Requested run and playlist do not identify the same audit")
    requested = run.get("requested_video_ids", [])
    existing = run.get("existing_video_ids", [])
    if (len(requested) != run.get("requested_count") or not requested
            or len(existing) != run.get("existing_count")):
        raise RuntimeError("Audit snapshots are incomplete; refusing reconciliation")
    # Historical partial-publication policy never changes the original target.
    verifier = playability_verifier if playability_verifier is not None else verifier_for(ytmusic)
    current = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
    actual_ids = [item["videoId"] for item in current]
    requested_comparison = _compare_exact_playlist_video_ids(requested, actual_ids)
    existing_comparison = _compare_exact_playlist_video_ids(existing, actual_ids)
    payload = run.get("recovery_payload") or {}
    tail_attempted = any(event.get("phase") == "reconcile_tail" and event.get("operation") == "add"
                         for event in payload.get("events", []))
    mutations = [event for event in payload.get("events", [])
                 if event.get("operation") in {"add", "remove", "move"}]
    move_confirmation, ownership_run = None, run
    if confirm_observed_move:
        last = mutations[-1] if mutations else {}
        if last.get("operation") != "move" or last.get("state") not in {"intent", "ambiguous"}:
            raise RuntimeError("Only the final unresolved move may be explicitly confirmed")
        intent_seq = last.get("seq") if last["state"] == "intent" else last.get("intent_seq")
        intent = next((event for event in mutations if event.get("seq") == intent_seq
                       and event.get("state") == "intent" and event.get("operation") == "move"), None)
        if not intent:
            raise RuntimeError("Observed move has no complete original intent")
        second = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
        slots = lambda rows: [(row["videoId"], row["setVideoId"]) for row in _audit_playlist_items(rows)]
        expected = slots(intent.get("after_items"))
        if slots(current) != expected or slots(second) != expected:
            raise RuntimeError("Observed move needs two exact observations of the intended complete after-state")
        move_confirmation = {
            "phase": "reconcile", "operation": "observe", "state": "verified",
            "chunk_order": 0, "attempt": 1, "reconciliation_action": "confirm_observed_move",
            "items": second, "before_items": intent.get("before_items"), "after_items": intent.get("after_items"),
            "observation_complete": True, "verification_matches": True,
            "attestation": "workers_quiescent=true; explicit_confirmation=true",
            "review_checks": [{"intent_seq": intent_seq, "items": observed} for observed in (current, second)],
        }
        events = payload.get("events", [])
        ownership_run = {**run, "recovery_payload": {**payload, "events": [*events, {
            **move_confirmation, "seq": max((event["seq"] for event in events), default=0) + 1,
        }]}}
        # Replay validates all earlier ownership, the exact intended move, and
        # that no other unresolved mutation can borrow this observation.
        _recoverable_playlist_items(ownership_run)
    acknowledged_intents = {event.get("intent_seq") for event in mutations if event.get("state") == "ack"}
    unresolved_mutation = move_confirmation is None and any(
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
            review_checks.append(_compare_exact_playlist_video_ids(
                [row["expected_id"]], [row["actual_id"]],
            ))
        review_required = not review_checks or not all(row["matches"] for row in review_checks)

    # Reconstruct ownership before selecting an observation-only outcome. A
    # rejected new slot may expose the same video ID as the original snapshot.
    owned, ownership_error = None, "Ownership or identity evidence is insufficient"
    newly_observed_rejection = False
    try:
        candidate = _recoverable_playlist_items(ownership_run)
        unresolved_mutation = False  # Explicitly confirmed moves keep their historical ambiguous event.
        if _same_owned_slots(current, candidate):
            owned = candidate
            mutations = [event for event in payload.get("events", [])
                         if event.get("operation") in {"add", "remove", "move"}]
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
    if complete_requested and (unresolved_mutation or owned is None):
        reason = "Completing the original request requires complete owned slots and no unresolved mutation"
    elif requested_comparison["matches"]:
        action, reason = "finalize_requested", "Current playlist verifies against the original request"
        review_required = False  # Fresh requested-state proof resolves the old rejection, not its history.
    elif complete_requested:
        action, reason = "complete_requested", "Complete owned slots permit finishing the immutable original request"
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
            prefix_comparison = _compare_exact_playlist_video_ids(requested[:-1], actual_ids)
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
    target = requested if action in {"finalize_requested", "append_missing_last", "complete_requested"} else existing
    if action == "restore_owned_items" and len(set(target)) != len(target):
        action, reason = "blocked", "Restoring duplicate original IDs requires separate reviewed slot ownership; no mutation is permitted"
    if action != "blocked":
        try:
            require_playable(verifier, target, items=current)
            if move_confirmation is not None:
                require_playable(verifier, target, items=second)
        except PlaybackBlocked as exc:
            action, reason = "blocked", str(exc)
    report = {
        "update_run_id": run_id, "playlist_id": playlist_id, "status": run["status"],
        "action": action, "reason": reason, "applied": False,
        "requested_comparison": requested_comparison, "existing_comparison": existing_comparison,
        "prefix_comparison": prefix_comparison,
        "identity_review_required": review_required, "review_checks": review_checks,
        "actual_items": current, "item_mutations": 0,
        "publication_mode": "full",
        "original_requested_count": len(requested), "effective_count": len(requested),
        **({"confirmed_move_intent_seq": intent_seq} if move_confirmation is not None else {}),
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

    def verify_target(items):
        require_playable(verifier, target, items=items, force=True)
        if before_mutation is not None:
            before_mutation()
        observed = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
        if _playlist_item_keys(observed) != _playlist_item_keys(_audit_playlist_items(items)):
            raise RuntimeError("Playlist changed during recovery verification; refusing reconciliation")
        require_playable(verifier, target, items=observed)
        return observed

    def before_change():
        verify_target(get_existing_playlist_items(ytmusic, playlist_id))

    comparison = requested_comparison
    try:
        fresh = _audit_playlist_items(get_existing_playlist_items(ytmusic, playlist_id))
        if _playlist_item_keys(fresh) != _playlist_item_keys(current):
            raise RuntimeError("Playlist changed after recovery planning; refusing reconciliation")
        observed = verify_target(fresh)
        if move_confirmation is not None:
            evidence({**move_confirmation, "items": observed,
                      "review_checks": [{"intent_seq": intent_seq, "items": items} for items in (fresh, observed)]})
        if newly_observed_rejection:
            evidence({
                "phase": "reconcile", "operation": "observe", "state": "verified",
                "chunk_order": 0, "attempt": 1, "items": fresh,
                "verification_matches": False, "identity_review_required": True,
                "differences": requested_comparison["differences"],
            })
        if action == "complete_requested":
            expected = _preserve_playlist_slots(
                ytmusic, playlist_id, fresh, requested, evidence=evidence, before_mutation=before_change,
                playability_verifier=verifier,
            )
            fresh = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(fresh, expected):
                raise RuntimeError("Completed request differs from acknowledged item ownership")
            report["item_mutations"] = 1
        elif action == "restore_owned_items":
            expected = _preserve_playlist_slots(
                ytmusic, playlist_id, fresh, existing, evidence=evidence,
                phase="restore", before_mutation=before_change,
                playability_verifier=verifier,
            )
            fresh = get_existing_playlist_items(ytmusic, playlist_id)
            if not _same_owned_slots(fresh, expected):
                raise RuntimeError("Restored playlist differs from acknowledged item ownership")
            report["item_mutations"] = 1
        elif action == "append_missing_last":
            # This new before-snapshot proves only this append's ownership.
            # It does not manufacture receipts for the legacy failed update.
            verify_target(fresh)
            intent = evidence({
                "phase": "reconcile_tail", "operation": "add", "state": "intent",
                "chunk_order": 1, "attempt": 1,
                "items": [{"videoId": requested[-1]}], "before_items": fresh,
                "verification_matches": True, "differences": prefix_comparison["differences"],
            })
            require_playable(verifier, target, items=fresh)
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
        comparison = _compare_exact_playlist_video_ids(target, [item["videoId"] for item in fresh])
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
        fresh = verify_target(fresh)
        evidence({
            "phase": "reconcile_tail" if action == "append_missing_last" or tail_attempted else "reconcile",
            "operation": "observe", "state": "verified",
            "chunk_order": 0, "attempt": 1, "items": fresh,
            "reconciliation_action": action, "review_checks": review_checks,
            "observation_complete": True, "verification_matches": True,
        })
        needs_review = review_required and action == "restore_owned_items"
        if needs_review:
            evidence({
                "phase": "restore", "operation": "observe", "state": "verified",
                "chunk_order": 0, "attempt": 1, "items": fresh,
                "restore_verified": True, "identity_review_required": True,
            })
        status = "recovery_required" if needs_review else (
            "published" if action in {"finalize_requested", "append_missing_last", "complete_requested"} else "restored"
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
    parser.add_argument("--confirm-observed-move", action="store_true",
                        help="Explicitly confirm a final unresolved move from two exact observations; never replay the move")
    parser.add_argument("--complete-requested", action="store_true",
                        help="Explicitly finish the immutable original request using only proven owned slots")
    args = parser.parse_args()
    if (args.apply or args.confirm_observed_move or args.complete_requested) and not args.workers_quiescent:
        parser.error("Applying or confirming recovery requires --workers-quiescent after stopping all workers")
    with bilingual_cache_read_only():
        result = reconcile_playlist_update(
            make_ytmusic(args.yt_auth), args.db_path, args.run_id, args.playlist_id,
            apply=args.apply, workers_quiescent=args.workers_quiescent,
            reclaim_recovery=args.reclaim_recovery,
            append_missing_last=args.append_missing_last,
            confirm_observed_move=args.confirm_observed_move,
            complete_requested=args.complete_requested,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
