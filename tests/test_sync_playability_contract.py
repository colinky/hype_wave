"""Independent sync acceptance: temporary SQLite, fake reads, no live services."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import crawler_common
import hype_db
from hype_db_schema import init_schema
import hype_db_store as store
import sync_validation as validation
import ytmusic_playlist_sync as matching
import ytmusic_to_ytmusic_crawl as chart


OLD, NEW, SOURCE, CHART_SOURCE = "QbsbqekMkCU", "cQPXraDIvS4", "1234", "UsbRoaH6y-Q"
UID, DAY, JOB = "existing-recording", "2026-09-14", "Fixture-Sync"
IDENTITY = {"title": "Pop Off Pop Off", "artist": "KiiiKiii", "album": "WhyKiiiKiii"}


def evidence(video_id, state="playable"):
    now = datetime.now(timezone.utc)
    return {"video_id": video_id, "state": state, "exact_id": True, "reason_code": "fixture",
            "observed_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat(), "environment": "offline-test",
            "auth_state": "authenticated", "run_health": "healthy", "has_audio": state == "playable",
            "confirmed_unavailable": state == "unavailable", **IDENTITY}


class Verifier:
    def __init__(self, states=None, *, health="healthy"):
        self.states = states or {}
        self.environment = "offline-test"
        self.health = {"run_health": health, "auth_state": "authenticated" if health == "healthy" else "unknown"}
        self.calls = []
        self.after_verify = None

    def check_health(self, **kwargs):
        return dict(self.health)

    def verify(self, video_id, **kwargs):
        self.calls.append(video_id)
        result = evidence(video_id, self.states.get(video_id, "playable"))
        if self.after_verify:
            self.after_verify(video_id, result)
        return result


class SyncPlayabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1"}))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live network forbidden")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Live network forbidden")))
        self.enterContext(matching.bilingual_cache_read_only())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "isolated.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn)
        self.conn.commit()
        self.verifier = Verifier()
        self.metadata = {}
        self.enterContext(patch.object(validation, "verifier_for", return_value=self.verifier))
        self.enterContext(patch.object(matching, "get_verified_video_metadata", side_effect=self.exact_metadata))
        self.enterContext(patch.object(crawler_common, "get_verified_video_metadata", side_effect=self.exact_metadata))
        self.enterContext(patch.object(matching.time, "sleep"))

    def exact_metadata(self, _client, video_id, **kwargs):
        return deepcopy(self.metadata.get(video_id, {"video_id": video_id, "verified": True,
                                                   "music_video_type": "MUSIC_VIDEO_TYPE_ATV", **IDENTITY}))

    def source(self, **changes):
        return {"song_id": SOURCE, "service": "apple", "rank": 1, "source": "track_lockup",
                **IDENTITY, "title_en": IDENTITY["title"], "artist_en": IDENTITY["artist"],
                "album_en": IDENTITY["album"], **changes}

    def selected(self, video_id=NEW, **changes):
        return {**self.source(), "video_id": video_id, "yt_title": IDENTITY["title"],
                "yt_artist": IDENTITY["artist"], "yt_album": IDENTITY["album"],
                "status": "matched", "score": 1.0, **changes}

    def seed(self, *, service="apple", song_id=SOURCE):
        store.ensure_track(self.conn, track_uid=UID, video_id=OLD, yt_title=IDENTITY["title"],
                           yt_artist=IDENTITY["artist"], yt_album=IDENTITY["album"], status="matched", score=1)
        for binding, identifier in ((service, song_id), ("melon", "9876")):
            store.upsert_track_list_metadata(self.conn, service=binding, song_id=identifier,
                                             track_uid=UID, row=self.source())
        self.conn.commit()

    def manual(self, action, video_id=OLD, *, service="apple", song_id=SOURCE):
        self.conn.execute(
            "INSERT OR REPLACE INTO manual_overrides(service,song_id,action,target_track_uid,canonical_yt_video_id,updated_at) VALUES (?,?,?,?,?,?)",
            (service, song_id, action, UID, video_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def snapshot(self):
        self.conn.commit()
        return {table: sorted(json.dumps(dict(row), sort_keys=True) for row in self.conn.execute("SELECT * FROM " + table))
                for table in ("tracks", "platform_song_ids", "track_list", "playlist_order", "match_runs",
                              "match_attempts", "metadata_lookup_index", "manual_overrides")}

    def validate(self, matches=None, source=None):
        return validation.validate_matches(self.conn, service="apple", sources=[source or self.source()],
                                           matches=matches or [self.selected()], client=object(), verifier=self.verifier)

    def test_playable_prior_beats_a_new_chart_recording_without_writes(self):
        self.seed()
        before = self.snapshot()
        result = self.validate()
        self.assertEqual(result[0]["video_id"], OLD)
        self.assertNotIn("canonical_decision", result[0])
        self.assertEqual(self.snapshot(), before)

    def test_confirmed_unavailable_can_select_and_store_a_same_recording(self):
        self.seed()
        self.verifier.states[OLD] = "unavailable"
        result = self.validate()
        self.assertEqual(result[0]["video_id"], NEW)
        self.assertEqual(result[0]["canonical_decision"]["reason"], "replace_unavailable_recording")
        store.apply_canonical_decision(self.conn, track_uid=UID, decision=result[0]["canonical_decision"], source_row=self.source())
        self.assertEqual(self.conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (UID,)).fetchone()[0], NEW)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (UID,)).fetchone()[0], OLD)

    def test_unknown_prior_blocks_replacement_without_changes(self):
        self.seed()
        self.verifier.states[OLD] = "unknown"
        before = self.snapshot()
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.assertEqual(self.snapshot(), before)

    def test_cached_unavailable_cannot_reenter_through_final_selection(self):
        self.seed()
        self.verifier.states[OLD] = "unavailable"
        cache = validation.playable_cache({SOURCE: {"video_id": OLD, "status": "cached_match"}}, self.verifier)
        self.assertEqual(cache[SOURCE]["excluded_video_ids"], [OLD])
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(OLD, status="cached_match")])

    def test_unknown_manual_override_cannot_fall_back_to_search(self):
        self.seed()
        self.manual("set_canonical")
        self.verifier.states[OLD] = "unknown"
        track = matching.SourceTrack(rank=1, service="apple", song_id=SOURCE, **IDENTITY)
        with patch.object(crawler_common, "search_youtube_music") as search, self.assertRaises(RuntimeError):
            self.pipeline(track, no_db_cache=True)
        search.assert_not_called()

    def test_no_db_cache_preserves_a_manual_block(self):
        self.seed()
        self.manual("block")
        track = matching.SourceTrack(rank=1, service="apple", song_id=SOURCE, **IDENTITY)
        with patch.object(crawler_common, "search_youtube_music") as search:
            self.assertEqual(self.pipeline(track, no_db_cache=True), [])
        search.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT action FROM manual_overrides").fetchone()[0], "block")

    def test_final_gate_rejects_a_different_song_even_when_player_is_ok(self):
        self.metadata[NEW] = {"video_id": NEW, "title": "Unrelated song", "artist": "A different performer",
                              "album": "Another album", "music_video_type": "MUSIC_VIDEO_TYPE_ATV"}
        before = self.snapshot()
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.assertEqual(self.snapshot(), before)

    def test_final_gate_rejects_version_change_before_authorizing_same_recording(self):
        self.seed()
        self.verifier.states[OLD] = "unavailable"
        self.metadata[NEW] = {"video_id": NEW, **IDENTITY, "title": "Pop Off Pop Off (Live)",
                              "music_video_type": "MUSIC_VIDEO_TYPE_ATV"}
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()

    def test_same_old_id_does_not_bypass_changed_source_identity(self):
        self.seed()
        changed = self.source(title="A completely different song", title_en="A completely different song", artist="Another artist", artist_en="Another artist")
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(OLD)], source=changed)

    def test_trusted_source_cache_does_not_hide_conflicting_live_video_identity(self):
        self.seed()
        self.metadata[OLD] = {"video_id": OLD, "title": "Entirely unrelated recording", "artist": "Other performer",
                              "album": "Other album", "music_video_type": "MUSIC_VIDEO_TYPE_ATV"}
        def live_identity(video_id, result):
            if video_id == OLD:
                result.update(title="Entirely unrelated recording", artist="Other performer")
        self.verifier.after_verify = live_identity
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(OLD, status="cached_match")])

    def test_expired_playable_evidence_is_not_accepted(self):
        def expire(_video_id, result):
            result["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.verifier.after_verify = expire
        with self.assertRaises(validation.PlaybackBlocked):
            validation.require_playable(self.verifier, [NEW])

    def test_final_run_health_cannot_disagree_with_successful_candidate(self):
        self.verifier.after_verify = lambda _id, _row: self.verifier.health.update(run_health="unknown")
        with self.assertRaises(validation.PlaybackBlocked):
            validation.require_playable(self.verifier, [NEW])

    def test_manual_override_status_cannot_authorize_the_wrong_target(self):
        self.seed()
        self.manual("set_canonical", OLD)
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(NEW, status="manual_override")])

    def test_manual_policy_added_during_network_validation_is_rechecked(self):
        def change(_id, _row):
            self.manual("block", None)
        self.verifier.after_verify = change
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.assertEqual(self.conn.execute("SELECT action FROM manual_overrides").fetchone()[0], "block")

    def test_manual_target_uid_cannot_claim_another_tracks_video(self):
        self.seed()
        store.ensure_track(self.conn, track_uid="other-recording", video_id=NEW, **{
            "yt_title": IDENTITY["title"], "yt_artist": IDENTITY["artist"], "yt_album": IDENTITY["album"]})
        self.manual("set_canonical", NEW)
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(NEW, status="manual_override")])

    def test_target_uid_only_manual_policy_uses_its_current_canonical(self):
        self.seed()
        self.manual("set_canonical", None)
        valid = self.validate([self.selected(OLD, status="manual_override")])
        self.assertEqual(valid[0]["video_id"], OLD)
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(NEW, status="manual_override")])

    def test_alias_manual_target_cannot_be_hidden_by_a_playable_cache_hit(self):
        self.seed()
        key = "|".join(matching.normalize_text(IDENTITY[field]) for field in ("title", "artist"))
        with patch.dict(matching.ALIASES.overrides, {key: NEW}, clear=True):
            with self.assertRaises(validation.PlaybackBlocked):
                self.validate([self.selected(OLD, status="cached_match")])
            valid = self.validate([self.selected(NEW, status="manual_override")])
        self.assertEqual(valid[0]["video_id"], NEW)
        self.assertEqual(valid[0]["status"], "manual_override")

    def test_sql_manual_policy_takes_precedence_over_a_conflicting_alias(self):
        self.seed()
        self.manual("set_canonical", OLD)
        key = "|".join(matching.normalize_text(IDENTITY[field]) for field in ("title", "artist"))
        with patch.dict(matching.ALIASES.overrides, {key: NEW}, clear=True):
            valid = self.validate([self.selected(OLD, status="manual_override")])
        self.assertEqual(valid[0]["video_id"], OLD)

    def test_alias_changed_during_validation_blocks_completion(self):
        key = "|".join(matching.normalize_text(IDENTITY[field]) for field in ("title", "artist"))
        with patch.dict(matching.ALIASES.overrides, {}, clear=True):
            self.verifier.after_verify = lambda _id, _row: matching.ALIASES.overrides.update({key: OLD})
            with self.assertRaises(validation.PlaybackBlocked):
                self.validate()

    def test_unrecognized_manual_block_status_cannot_remove_a_playable_prior(self):
        self.seed()
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(None, status="manual_blocked")])

    def test_live_identity_conflict_cannot_be_cleared_by_stale_exact_metadata(self):
        self.seed()
        self.verifier.after_verify = lambda _id, row: row.update(title="Unrelated recording", artist="Other performer")
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate([self.selected(OLD, status="cached_match")])

    def test_identity_change_on_final_recheck_blocks_completion(self):
        def change(video_id, row):
            if self.verifier.calls.count(video_id) > 1:
                row.update(title="Unrelated recording", artist="Other performer")
        self.verifier.after_verify = change
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()

    def test_lost_auth_on_final_health_check_cannot_report_success(self):
        self.verifier.after_verify = lambda _id, _row: self.verifier.health.update(auth_state="unknown")
        with self.assertRaises(validation.PlaybackBlocked):
            validation.require_playable(self.verifier, [NEW])

    def pipeline(self, track, **kwargs):
        return crawler_common.process_matching_pipeline(
            all_tracks=[track], ytmusic=object(), db_path=self.db, service="apple", job_name=JOB,
            update_date_str=DAY, started_at="20260914T100000Z", reference_period=DAY, **kwargs)

    def test_unknown_health_with_no_selection_cannot_commit_raw_or_completed_run(self):
        self.verifier.health.update(run_health="unknown", auth_state="unknown")
        track = matching.SourceTrack(rank=1, service="apple", song_id=SOURCE, **IDENTITY)
        failed = matching.MatchResult(rank=1, service="apple", song_id=SOURCE, status="failed", **IDENTITY)
        before = self.snapshot()
        with patch.object(crawler_common, "search_youtube_music", return_value=failed), self.assertRaises(validation.PlaybackBlocked):
            self.pipeline(track)
        self.assertEqual(self.snapshot(), before)

    def test_extra_raw_snapshots_roll_back_if_final_matching_write_fails(self):
        track = matching.SourceTrack(rank=1, service="apple", song_id=SOURCE, **IDENTITY)
        matched = matching.MatchResult(rank=1, service="apple", song_id=SOURCE, status="matched", video_id=NEW,
                                       yt_title=IDENTITY["title"], yt_artist=IDENTITY["artist"], yt_album=IDENTITY["album"], score=1, **IDENTITY)
        before = self.snapshot()
        extra = [{"source_variant": "alternate", "chart_date": DAY, "reference_period": DAY,
                  "tracks": [self.source()]}]
        with patch.object(crawler_common, "search_youtube_music", return_value=matched), patch.object(
                hype_db, "persist_crawl_run", side_effect=RuntimeError("Injected final write failure")), self.assertRaises(RuntimeError):
            self.pipeline(track, extra_raw_snapshots=extra)
        self.assertEqual(self.snapshot(), before)

    def test_youtube_chart_keeps_a_valid_manual_override_through_final_validation(self):
        self.seed(service="ytmusic", song_id=CHART_SOURCE)
        self.manual("set_canonical", OLD, service="ytmusic", song_id=CHART_SOURCE)
        entries = [{"rank": 1, "video_id": CHART_SOURCE, "atv_external_video_id": NEW, **IDENTITY,
                    "source": "youtube_charts_weekly_browse_api", "chart_period_start": "2026-09-04", "chart_period_end": "2026-09-10"}]
        argv = ["chart", "--youtube-charts-csv", "fixture.csv", "--yt-auth", "unused", "--yt-playlist-id", "unused-target",
                "--chart-period-end", "2026-09-10", "--db-path", str(self.db), "--defer-publish", "--job-name", "Fixture-Chart"]
        with patch("sys.argv", argv), patch.object(chart, "load_dotenv"), patch.object(chart, "make_ytmusic", return_value=object()), patch.object(
                chart, "extract_chart_entries_from_csv", return_value=entries), patch.object(chart, "update_ytmusic_playlist") as publish:
            self.assertEqual(chart.main(), 0)
        publish.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (UID,)).fetchone()[0], OLD)


if __name__ == "__main__":
    unittest.main()
