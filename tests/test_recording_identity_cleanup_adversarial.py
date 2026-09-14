"""Identity cleanup must verify nested outputs and honor later manual blocks."""
import json
import os
import unittest
from unittest.mock import patch

from hype_db_store import CanonicalDecisionError
from repair_ytmusic_chart_incident import apply_repair, plan_repair, verify_repair
import test_recording_identity_cleanup as fixtures


class IdentityCleanupAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for target in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("External access forbidden")))
        self.fixture = fixtures.IdentityCleanupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn

    def state(self):
        tables = [row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {table: sorted(json.dumps(dict(row), sort_keys=True) for row in self.conn.execute(
            'SELECT * FROM "' + table.replace('"', '""') + '"'
        )) for table in tables}

    def assert_failed_apply_preserves_all_rows(self, case):
        manifest = plan_repair(self.conn, self.fixture.spec(case))
        before = self.state()
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM migration_reports").fetchone()[0], 0)

    def test_direct_manual_block_for_source_less_split_alias_stops_verify_and_retry(self):
        manifest = plan_repair(self.conn, self.fixture.spec(self.fixture.alias_case()))
        apply_repair(self.conn, manifest)
        self.conn.commit()
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM platform_song_ids WHERE track_uid='separate-mix'"
        ).fetchone())
        self.conn.execute(
            "INSERT INTO manual_overrides(service,song_id,action,updated_at) VALUES ('ytmusic',?,'block',?)",
            (fixtures.MIX, "2026-09-15T00:00:00+09:00"),
        )
        self.conn.commit()
        before = self.state()
        with self.assertRaises(CanonicalDecisionError):
            verify_repair(self.conn, manifest)
        with self.assertRaises(CanonicalDecisionError):
            apply_repair(self.conn, manifest)
        self.assertEqual(self.state(), before)

    def test_ignored_nested_canonical_flag_write_cannot_be_verified(self):
        case = self.fixture.alias_case()
        self.conn.execute("""
            CREATE TRIGGER ignore_nested_canonical_flag
            BEFORE UPDATE OF is_canonical ON yt_video_ids
            WHEN NEW.video_id='mix00000001' AND NEW.is_canonical=1
            BEGIN SELECT RAISE(IGNORE); END
        """)
        self.conn.commit()
        self.assert_failed_apply_preserves_all_rows(case)

    def test_ignored_source_rebinding_cannot_be_verified_as_original(self):
        case = self.fixture.binding_case()
        self.conn.execute("""
            CREATE TRIGGER ignore_original_rebinding
            BEFORE UPDATE OF track_uid ON platform_song_ids
            WHEN OLD.service='apple' AND OLD.song_id='wrong-binding' AND NEW.track_uid='original'
            BEGIN SELECT RAISE(IGNORE); END
        """)
        self.conn.commit()
        self.assert_failed_apply_preserves_all_rows(case)

    def test_ignored_related_metadata_write_cannot_be_verified(self):
        case = self.fixture.binding_case()
        self.conn.execute("""
            CREATE TRIGGER ignore_feature_metadata
            BEFORE UPDATE OF yt_title ON tracks
            WHEN NEW.track_uid='feature' AND NEW.yt_title='Song (feat. Other)'
            BEGIN SELECT RAISE(IGNORE); END
        """)
        self.conn.commit()
        self.assert_failed_apply_preserves_all_rows(case)

    def test_ignored_explicit_empty_source_album_write_cannot_be_verified(self):
        case = self.fixture.binding_case()
        case["source_metadata_repairs"] = [{
            "service": "apple", "song_id": "wrong-binding", "source_metadata_verified": True,
            "evidence_ref": "exact-source:album-empty", "values": {"album_en": ""},
        }]
        self.assertEqual(self.conn.execute(
            "SELECT album_en FROM track_list WHERE service='apple' AND song_id='wrong-binding'"
        ).fetchone()[0], "Album")
        self.conn.execute("""
            CREATE TRIGGER ignore_explicit_empty_source_album
            BEFORE UPDATE OF album_en ON track_list
            WHEN OLD.service='apple' AND OLD.song_id='wrong-binding' AND NEW.album_en=''
            BEGIN SELECT RAISE(IGNORE); END
        """)
        self.conn.commit()
        self.assert_failed_apply_preserves_all_rows(case)


if __name__ == "__main__":
    unittest.main()
