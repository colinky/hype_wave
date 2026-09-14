from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hype_db import connect, init_db
import hype_db_store as store


SOURCE = "src00000001"
CURRENT = "old00000001"
TARGET = "new00000001"
SOURCE_UID = "existing-song"
TARGET_UID = "chart-atv"
METADATA = {"title": "REDRED", "artist": "CORTIS", "album": "GREENGREEN"}
CHART_ROW = {
    "source": "youtube_charts_weekly_browse_api",
    "original_video_id": SOURCE,
    "original_title": METADATA["title"],
    "original_artist_or_channel": METADATA["artist"],
    "atv_external_video_id": TARGET,
    "album": "",
}


class ChartCanonicalPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "chart.db"
        init_db(self.db_path)

    def seed(self, conn, *, independent: bool = True, same_uid: bool = False,
             canonical: str = CURRENT) -> None:
        store.ensure_track(
            conn, track_uid=SOURCE_UID, video_id=canonical,
            yt_title=METADATA["title"], yt_artist=METADATA["artist"],
            yt_album=METADATA["album"], status="matched", score=1.0,
        )
        store.upsert_track_list_metadata(
            conn, service="ytmusic", song_id=SOURCE,
            track_uid=SOURCE_UID, row=METADATA,
        )
        if canonical != SOURCE:
            conn.execute(
                "INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES (?, ?, 0)",
                (SOURCE, SOURCE_UID),
            )
        if independent:
            for service, song_id in (("melon", "123456"), ("apple", "987654")):
                store.upsert_track_list_metadata(
                    conn, service=service, song_id=song_id,
                    track_uid=SOURCE_UID, row=METADATA,
                )
        if same_uid:
            conn.execute(
                "INSERT INTO platform_song_ids(service,song_id,track_uid) VALUES ('ytmusic', ?, ?)",
                (TARGET, SOURCE_UID),
            )
            conn.execute(
                "INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES (?, ?, 0)",
                (TARGET, SOURCE_UID),
            )
        else:
            store.ensure_track(
                conn, track_uid=TARGET_UID, video_id=TARGET,
                yt_title=METADATA["title"], yt_artist=METADATA["artist"],
                yt_album=METADATA["album"], status="matched", score=1.0,
            )
            store.upsert_track_list_metadata(
                conn, service="ytmusic", song_id=TARGET,
                track_uid=TARGET_UID, row=METADATA,
            )
        store.record_source_song_relation(
            conn, service="ytmusic", source_song_id=SOURCE,
            relation_type=store.YOUTUBE_ATV_RELATION,
            related_service="ytmusic", related_song_id=TARGET,
            evidence_source="youtube_charts_weekly_browse_api",
            reference_period="2026-W37", payload=CHART_ROW,
        )
        conn.commit()

    def consolidate(self, conn, **kwargs):
        return store.consolidate_source_song_relation(
            conn, service="ytmusic", source_song_id=SOURCE,
            relation_type=store.YOUTUBE_ATV_RELATION,
            related_service="ytmusic", related_song_id=TARGET,
            source_row=CHART_ROW, related_row=METADATA, **kwargs,
        )

    def replacement_decision(self):
        now = datetime.now(timezone.utc)
        common = {"exact_id": True, "auth_state": "authenticated", "run_health": "healthy", "environment": "test",
                  "observed_at": (now - timedelta(seconds=1)).isoformat(), "expires_at": (now + timedelta(minutes=20)).isoformat()}
        return {"expected_track_uid": SOURCE_UID, "expected_video_id": CURRENT, "selected_video_id": TARGET,
                "reason": "replace_unavailable_recording", "same_recording": True,
                "current_evidence": {**common, "video_id": CURRENT, "state": "unavailable", "confirmed_unavailable": True},
                "candidate_evidence": {**common, "video_id": TARGET, "state": "playable", "has_audio": True},
                "metadata": {**METADATA, "video_id": TARGET, "verified": True}}

    def test_existing_cross_service_canonical_and_bindings_are_unchanged(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn)
        before = self.db_path.read_bytes()
        with connect(self.db_path) as conn:
            for _ in range(2):
                result = self.consolidate(conn)
                self.assertEqual(result["status"], "preserved_canonical")
                self.assertEqual(result["match"]["video_id"], CURRENT)
                self.assertEqual(result["winner_uid"], SOURCE_UID)
            self.assertEqual(
                dict(conn.execute(
                    "SELECT service,track_uid FROM platform_song_ids WHERE song_id IN (?, '123456', '987654')",
                    (SOURCE,),
                )),
                {"ytmusic": SOURCE_UID, "melon": SOURCE_UID, "apple": SOURCE_UID},
            )
            conn.commit()
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_chart_source_already_canonical_stays_stable_across_retries(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn, canonical=SOURCE)
        before = self.db_path.read_bytes()
        for _ in range(2):
            with connect(self.db_path) as conn:
                result = self.consolidate(conn)
                self.assertEqual(result["status"], "preserved_canonical")
                self.assertEqual(result["match"]["video_id"], SOURCE)
                self.assertEqual(result["winner_uid"], SOURCE_UID)
                conn.commit()
            self.assertEqual(self.db_path.read_bytes(), before)

    def test_offline_relation_repair_also_preserves_existing_canonical(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn)
        before = self.db_path.read_bytes()
        with connect(self.db_path) as conn:
            self.assertEqual(
                store.repair_source_song_relations(conn),
                {"scanned": 1, "preserved_canonical": 1},
            )
            conn.commit()
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_dry_run_preserves_database_bytes(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn)
        before = self.db_path.read_bytes()
        with connect(self.db_path, read_only=True) as conn:
            result = self.consolidate(conn, dry_run=True)
            self.assertEqual(result["status"], "preserved_canonical")
            self.assertEqual(result["match"]["video_id"], CURRENT)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_manual_block_precedes_existing_canonical_preservation(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn)
            conn.execute(
                "INSERT INTO manual_overrides(service,song_id,action,reason,updated_at) "
                "VALUES ('ytmusic', ?, 'block', 'test', '2026-09-14')",
                (SOURCE,),
            )
            conn.commit()
        before = self.db_path.read_bytes()
        with connect(self.db_path) as conn:
            self.assertEqual(self.consolidate(conn)["status"], "manual_blocked")
            conn.commit()
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_source_only_identity_repair_requires_verified_replacement(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn, independent=False)
            self.assertEqual(self.consolidate(conn)["status"], "identity_review_required")
            proposal = {**self.replacement_decision(), "expected_target_uid": TARGET_UID}
            self.assertEqual(self.consolidate(conn, canonical_decision=proposal)["status"], "linked")
            self.assertEqual(
                store.find_track_by_service_song(conn, "ytmusic", SOURCE), SOURCE_UID,
            )
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM tracks WHERE track_uid=?", (TARGET_UID,),
            ).fetchone())

    def test_alias_promotion_leaves_exactly_one_canonical_video_flag(self) -> None:
        with connect(self.db_path) as conn:
            self.seed(conn, independent=False, same_uid=True)
            self.consolidate(conn, canonical_decision=self.replacement_decision())
            self.assertEqual(
                dict(conn.execute("SELECT video_id,is_canonical FROM yt_video_ids")),
                {SOURCE: 0, CURRENT: 0, TARGET: 1},
            )
            self.assertEqual(conn.execute(
                "SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (SOURCE_UID,),
            ).fetchone()[0], TARGET)


if __name__ == "__main__":
    unittest.main()
