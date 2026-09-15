from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import hype_db_store as store
from sync_validation import fingerprint
from test_canonical_decision_recovery import StorageFixture, OLD, NEW, UID, canonical, decision


class PublicationCounterpartDecisionTests(StorageFixture):
    def setUp(self):
        super().setUp()
        self.source = {"service": "ytmusic", "song_id": OLD, "title": "REDRED",
                       "artist": "CORTIS", "album": "", "length_seconds": 204}
        store.upsert_track_list_metadata(self.conn, service="ytmusic", song_id=OLD, track_uid=UID, row=self.source)
        metadata = {"video_id": NEW, "title": "REDRED", "artist": "CORTIS", "album": "GREENGREEN",
                    "length_seconds": 207, "music_video_type": "MUSIC_VIDEO_TYPE_ATV",
                    "artist_ids": ["UC" + "a" * 22], "artist_names_by_id": {"UC" + "a" * 22: ["CORTIS"]},
                    "artist_identity_complete": True, "verified": True}
        for locale in ("ko", "en"):
            metadata.update({key + "_" + locale: metadata[key] for key in ("title", "artist", "album")})
        proof = {"kind": "official_music_video_recording", "source_video_id": OLD,
                 "source_variants": [["REDRED", "CORTIS", ""]], "source_lengths_seconds": [204],
                 "max_duration_difference_seconds": 3, "recording": metadata, "candidate": metadata,
                 "references": [{"url": "https://example.test/official-release"}],
                 "limit": "This fixture's exact MV and official audio pair only."}
        policy = {"source_identity_evidence": {"ytmusic:" + OLD: proof}}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy_path = Path(directory.name) / "matching_alias.json"
        policy_path.write_text(json.dumps(policy))
        self.policy_patch = patch("hype_db_store.Path")
        mocked_path = self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        mocked_path.return_value.with_name.return_value = policy_path
        parent = {"repair_id": "parent", "manifest_hash": "a" * 64, "stages": {"db": "applied"},
                  "after": {"parent-case": {"state": {
                      "tracks": [{"track_uid": UID, "canonical_yt_video_id": OLD}],
                      "platform_song_ids": [{"service": "ytmusic", "song_id": OLD, "track_uid": UID}]}}}}
        self.conn.execute("INSERT INTO migration_reports(report_id,source,payload_json,created_at) VALUES (?,?,?,'2026-09-15')",
                          ("chart-repair:parent", "ytmusic_chart_incident", json.dumps(parent)))
        self.conn.commit()
        self.proposal = decision(incident=True)
        self.proposal.update(
            reason="adopt_verified_publication_counterpart", metadata=metadata,
            source_identity_key="ytmusic:" + OLD,
            expected_identity_policy_hash=hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            incident={"repair_id": "child", "case_id": "child-case", "parent_repair_id": "parent",
                      "parent_case_id": "parent-case", "parent_manifest_hash": parent["manifest_hash"],
                      "parent_receipt_fingerprint": fingerprint(parent), "evidence_ref": "sha256:reviewed",
                      "source_video_id": OLD, "selected_video_id": NEW})

    def apply(self, proposal=None):
        return store.apply_canonical_decision(self.conn, track_uid=UID,
                                              decision=proposal or self.proposal, source_row=self.source)

    def test_reviewed_counterpart_uses_parent_and_preserves_transaction_ownership(self):
        self.assertEqual(self.apply()["video_id"], NEW)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(canonical(self.conn), OLD)

    def test_missing_or_changed_parent_and_policy_refuse_without_writes(self):
        for section, field, value in (("incident", "parent_receipt_fingerprint", "b" * 64),
                                      ("incident", "parent_case_id", "unknown-case"),
                                      ("incident", "source_video_id", NEW),
                                      (None, "expected_identity_policy_hash", "c" * 64),
                                      (None, "source_identity_key", "ytmusic:" + NEW)):
            with self.subTest(field=field):
                proposal = copy.deepcopy(self.proposal)
                (proposal[section] if section else proposal)[field] = value
                before = list(self.conn.iterdump())
                with self.assertRaises(store.CanonicalDecisionError):
                    self.apply(proposal)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_orphan_owner_needs_exact_snapshot_single_alias_and_no_binding(self):
        owner = "orphan-owner"
        store.ensure_track(self.conn, track_uid=owner, video_id=NEW, yt_title="REDRED", yt_artist="CORTIS")
        self.conn.commit()
        self.proposal["expected_target_uid"] = owner
        before = list(self.conn.iterdump())
        with self.assertRaises(store.CanonicalDecisionError):
            self.apply()
        self.assertEqual(list(self.conn.iterdump()), before)

        self.proposal["expected_orphan_owner"] = {
            "track": dict(self.conn.execute("SELECT * FROM tracks WHERE track_uid=?", (owner,)).fetchone()),
            "bindings": [],
            "aliases": [dict(row) for row in self.conn.execute(
                "SELECT * FROM yt_video_ids WHERE track_uid=? ORDER BY video_id", (owner,))]}
        self.assertEqual(self.apply()["video_id"], NEW)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tracks WHERE track_uid=?", (owner,)).fetchone())
        self.conn.rollback()
        self.conn.execute("INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES ('extra000001',?,0)", (owner,))
        self.proposal["expected_orphan_owner"]["aliases"] = [dict(row) for row in self.conn.execute(
            "SELECT * FROM yt_video_ids WHERE track_uid=? ORDER BY video_id", (owner,))]
        before = list(self.conn.iterdump())
        with self.assertRaises(store.CanonicalDecisionError):
            self.apply()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_fabricated_source_row_cannot_replace_a_missing_actual_binding(self):
        self.conn.execute("DELETE FROM platform_song_ids WHERE service='ytmusic' AND song_id=?", (OLD,))
        before = list(self.conn.iterdump())
        with self.assertRaises(store.CanonicalDecisionError):
            self.apply()
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_unattributed_owner_can_keep_only_its_exact_self_video_binding(self):
        owner = "unattributed-owner"
        store.ensure_track(self.conn, track_uid=owner, video_id=NEW, yt_title="REDRED", yt_artist="CORTIS")
        self.conn.commit()
        self.proposal["expected_target_uid"] = owner
        for service, song, allowed in (("ytmusic", NEW, True), ("apple", NEW, False),
                                        ("ytmusic", "other000001", False)):
            with self.subTest(service=service, song=song):
                self.conn.execute("INSERT INTO platform_song_ids(service,song_id,track_uid) VALUES (?,?,?)",
                                  (service, song, owner))
                self.proposal["expected_orphan_owner"] = {
                    "track": dict(self.conn.execute("SELECT * FROM tracks WHERE track_uid=?", (owner,)).fetchone()),
                    "aliases": [dict(row) for row in self.conn.execute(
                        "SELECT * FROM yt_video_ids WHERE track_uid=? ORDER BY video_id", (owner,))],
                    "bindings": [dict(row) for row in self.conn.execute(
                        "SELECT * FROM platform_song_ids WHERE track_uid=? ORDER BY service,song_id", (owner,))]}
                before = list(self.conn.iterdump())
                if allowed:
                    self.assertEqual(self.apply()["video_id"], NEW)
                    self.assertEqual(store.find_track_by_service_song(self.conn, service, song), UID)
                else:
                    with self.assertRaises(store.CanonicalDecisionError):
                        self.apply()
                    self.assertEqual(list(self.conn.iterdump()), before)
                self.conn.rollback()
