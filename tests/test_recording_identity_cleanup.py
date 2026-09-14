"""Earlier alias/source contamination needs a separate, reversible correction."""
import copy
import json
import sqlite3
import unittest

from hype_db_schema import init_schema
from hype_db_store import CanonicalDecisionError, ensure_track, upsert_track_list_metadata
from repair_ytmusic_chart_incident import apply_repair, plan_repair, verify_repair
from test_canonical_decision_recovery import evidence

ORIGINAL, FEATURE, MIX = "orig0000001", "feat0000001", "mix00000001"


def metadata(video, title="Song"):
    return {"video_id": video, "title": title, "artist": "Artist", "album": "Album", "verified": True}


def selection(uid, video, title="Song", *, new=False):
    return {"expected_track_uid": uid, "expected_video_id": None if new else video,
            "selected_video_id": video, "candidate_evidence": evidence(video),
            "metadata": metadata(video, title), "reason": "correct_exact_metadata"}


class IdentityCleanupTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn)
        for uid, video in (("original", ORIGINAL), ("feature", FEATURE)):
            ensure_track(self.conn, track_uid=uid, video_id=video, yt_title="Song", yt_artist="Artist",
                         yt_album="Album", status="matched", score=1)
        upsert_track_list_metadata(self.conn, service="apple", song_id="wrong-binding", track_uid="feature",
                                   row={"title": "Song", "artist": "Artist", "album": "Album"})
        self.conn.commit()

    def spec(self, case):
        return {"repair_id": "legacy-identity", "implementation_revision": "fixture", "cases": [case]}

    def binding_case(self):
        return {"case_id": "original-source", "service": "apple", "song_id": "wrong-binding",
                "action": "repair_recording_identity", "evidence_ref": "official-source:original",
                "decision": selection("original", ORIGINAL),
                "bindings": [{"service": "apple", "song_id": "wrong-binding", "expected_track_uid": "feature",
                              "target_track_uid": "original", "source_metadata_verified": True,
                              "evidence_ref": "official-source:original"}],
                "metadata_decisions": [selection("feature", FEATURE, "Song (feat. Other)")]}

    def test_original_source_moves_to_existing_original_without_changing_either_id(self):
        manifest = plan_repair(self.conn, self.spec(self.binding_case()))
        apply_repair(self.conn, manifest)
        self.assertEqual(self.conn.execute("SELECT track_uid FROM platform_song_ids").fetchone()[0], "original")
        actual = dict(self.conn.execute("SELECT track_uid,canonical_yt_video_id FROM tracks"))
        self.assertEqual(actual, {"original": ORIGINAL, "feature": FEATURE})
        self.assertEqual(self.conn.execute("SELECT yt_title FROM tracks WHERE track_uid='feature'").fetchone()[0],
                         "Song (feat. Other)")
        self.assertEqual(verify_repair(self.conn, manifest)["cases"], 1)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT track_uid FROM platform_song_ids").fetchone()[0], "feature")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM migration_reports").fetchone()[0], 0)

    def test_unproved_source_rebind_rolls_back(self):
        case = self.binding_case()
        case["bindings"][0]["source_metadata_verified"] = False
        manifest = plan_repair(self.conn, self.spec(case))
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertEqual(self.conn.execute("SELECT track_uid FROM platform_song_ids").fetchone()[0], "feature")

    def alias_case(self):
        self.conn.execute("INSERT INTO yt_video_ids(video_id,track_uid,is_canonical) VALUES (?,?,0)", (MIX, "original"))
        self.conn.commit()
        return {"case_id": "different-mix", "service": "ytmusic", "song_id": ORIGINAL,
                "action": "repair_recording_identity", "evidence_ref": "exact-metadata:mix",
                "decision": selection("original", ORIGINAL),
                "alias_splits": [{"video_id": MIX, "expected_track_uid": "original", "target_track_uid": "separate-mix",
                                  "evidence_ref": "exact-metadata:mix", "decision": selection("separate-mix", MIX, "Song - Summer Mix", new=True)}]}

    def test_alias_only_split_creates_no_fake_platform_source(self):
        manifest = plan_repair(self.conn, self.spec(self.alias_case()))
        before_sources = [tuple(row) for row in self.conn.execute("SELECT * FROM platform_song_ids")]
        apply_repair(self.conn, manifest)
        self.assertEqual(self.conn.execute("SELECT track_uid,is_canonical FROM yt_video_ids WHERE video_id=?", (MIX,)).fetchone()[:],
                         ("separate-mix", 1))
        self.assertEqual(before_sources, [tuple(row) for row in self.conn.execute("SELECT * FROM platform_song_ids")])
        self.assertEqual(self.conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid='original'").fetchone()[0], ORIGINAL)
        verify_repair(self.conn, manifest)
        self.conn.commit()
        self.assertEqual(apply_repair(self.conn, manifest)["status"], "already_applied")

    def test_equivalent_alias_cannot_be_split_with_different_recording_reason(self):
        case = self.alias_case()
        case["alias_splits"][0]["decision"]["metadata"]["title"] = "Song"
        manifest = plan_repair(self.conn, self.spec(case))
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertFalse(self.conn.execute("SELECT 1 FROM tracks WHERE track_uid='separate-mix'").fetchone())

    def test_alias_ownership_change_after_plan_is_not_overwritten(self):
        manifest = plan_repair(self.conn, self.spec(self.alias_case()))
        self.conn.execute("UPDATE yt_video_ids SET track_uid='feature' WHERE video_id=?", (MIX,))
        self.conn.commit()
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertEqual(self.conn.execute("SELECT track_uid FROM yt_video_ids WHERE video_id=?", (MIX,)).fetchone()[0], "feature")

    def test_cleanup_cannot_use_a_canonical_switch_as_metadata_correction(self):
        case = self.binding_case()
        case["metadata_decisions"][0]["selected_video_id"] = ORIGINAL
        manifest = plan_repair(self.conn, self.spec(case))
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertEqual(self.conn.execute("SELECT track_uid FROM platform_song_ids").fetchone()[0], "feature")


if __name__ == "__main__":
    unittest.main()
