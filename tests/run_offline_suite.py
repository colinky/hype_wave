"""Explicit offline regression suite; never discover opt-in operational scripts."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))
MODULES = (
    "test_apple_ingest", "test_apple_bilingual_localization",
    "test_cache_canonical_guards", "test_db_rank_slot_migration", "test_heal_split_tracks",
    "test_history_rebuild", "test_legacy_cache_publication", "test_matching_regressions",
    "test_playlist_update_retention", "test_playlist_verification", "test_snapshot_contracts",
    "test_ytmusic_chart_identity", "test_ytmusic_monday_schedule",
    "test_playlist_recovery_evidence_db", "test_sync_phase_readiness",
    "test_playlist_durable_recovery", "test_recovery_adversarial_verifier",
    "test_recovery_tail_adversarial",
    "test_playlist_partial_fallback",
    "test_ytmusic_playability", "test_playability_probe",
    "test_chart_canonical_preservation", "test_ytmusic_chart_playability",
    "test_canonical_decision_recovery", "test_history_identity_repair",
    "test_deferred_crawlers", "test_frozen_publication_snapshot", "test_sync_playability_contract",
    "test_playlist_playability_safety", "test_reconcile_playability", "test_release_adversarial",
    "test_history_identity_exclusions", "test_history_identity_exclusions_ui",
    "test_playlist_transition_visibility", "test_reconcile_observed_move",
    "test_recording_mix_versions",
    "test_recording_identity_cleanup",
    "test_observed_recording_identity", "test_publish_chart_repair",
    "test_recording_identity_cleanup_adversarial",
    "test_recording_release_identity", "test_spotify_album_provenance", "test_partial_artist_identity",
    "test_source_title_translation", "test_spotify_album_storage",
    "test_narrator_identity", "test_spotify_locale_refresh", "test_native_recording_preservation", "test_native_source_split",
    "test_observed_credit_aliases", "test_catalog_duration_pair",
)


def main():
    output = io.StringIO()
    with (patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
          patch.object(socket, "create_connection", side_effect=AssertionError("Offline suite forbids network")) as network,
          patch.object(socket.socket, "connect", side_effect=AssertionError("Offline suite forbids network")) as sockets,
          patch("psycopg2.connect", side_effect=AssertionError("Offline suite forbids PostgreSQL")) as postgres,
          redirect_stdout(output), redirect_stderr(output)):
        suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    report = {"captured_at": datetime.now(timezone.utc).isoformat(), "python": sys.version,
              "tests": result.testsRun, "successful": result.wasSuccessful(),
              "failures": [{"test": str(test), "traceback": trace} for test, trace in result.failures],
              "errors": [{"test": str(test), "traceback": trace} for test, trace in result.errors],
              "skipped": [(str(test), reason) for test, reason in result.skipped],
              "modules": list(MODULES), "network_and_postgres_forbidden": True,
              "blocked_external_attempts": {"network": network.call_count,
                                            "socket": sockets.call_count, "postgres": postgres.call_count}}
    directory = TESTS / "_artifacts/id_stability"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "offline_suite.log").write_text(output.getvalue())
    (directory / "offline_suite.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
