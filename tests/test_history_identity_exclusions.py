"""A held recording version cannot merge through source URLs or replacement chains."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hype_db_reports as reports


OLD, NEW, ALIAS = "EmRFwlGlbb8", "YJhEsDa2o8w", "verified-alias"
EXCLUSION = {"video_ids": [OLD, NEW], "reason": "Japanese and standard album versions remain unverified",
             "evidence_ref": "offline-receipt:bloody-paradise"}


def row(video, day_rank=1, **changes):
    return {"video_id": video, "title": "Bloody Paradise", "artist": "ENHYPEN", "album": "THE SIN : BLISS",
            "yt_title": "Bloody Paradise", "yt_artist": "ENHYPEN", "yt_album": "THE SIN : BLISS",
            "apple_url": "", "melon_url": "https://www.melon.com/song/detail.htm?songId=602858445",
            "spotify_url": "https://open.spotify.com/track/0r2JVOjI7H1jhXzXBOorKu", "artwork_url": "",
            "hype_rank": day_rank, "hype_index": 20.74, "apple_rank": None, "melon_rank": None,
            "melon_genz_rank": None, "ytmusic_rank": 30, **changes}


def history():
    return reports.compact_frontend_history({"2026-09-13": [row(OLD, yt_album="THE SIN : BLISS (Japanese Ver.)")],
                                             "2026-09-14": [row(NEW)]})


class HistoryIdentityExclusionsTests(unittest.TestCase):
    def test_scoped_repair_keeps_held_versions_separate_despite_same_source_urls(self):
        original = history()
        fixed = reports.repair_frontend_history(original, {"2026-09-14": [row(NEW)]},
                                                identity_exclusions=[EXCLUSION])
        self.assertEqual(fixed["identity_exclusions"], [EXCLUSION])
        self.assertEqual(reports.inflate_frontend_history(fixed), reports.inflate_frontend_history(original))
        self.assertNotIn("identity_key", fixed["rankings"]["2026-09-14"][NEW])

    def test_explicit_and_transitive_links_cannot_override_exclusion(self):
        for links in ([{"old_video_id": OLD, "new_video_id": NEW, "evidence_ref": "pair"}],
                      [{"old_video_id": OLD, "new_video_id": ALIAS, "evidence_ref": "first"},
                       {"old_video_id": ALIAS, "new_video_id": NEW, "evidence_ref": "second"}]):
            with self.subTest(links=links), self.assertRaisesRegex(ValueError, "excluded"):
                reports.carry_history_identity([row(NEW)], history(), identity_exclusions=[EXCLUSION], identity_links=links)

    def test_verified_alias_of_one_version_does_not_bridge_the_other_version(self):
        fixed = reports.carry_history_identity([row(ALIAS)], history(), identity_exclusions=[EXCLUSION],
            identity_links=[{"old_video_id": OLD, "new_video_id": ALIAS, "evidence_ref": "same-version"}])
        self.assertEqual(fixed[0]["identity_key"], OLD)
        held = reports.carry_history_identity([row(NEW)], {"2026-09-14": fixed}, identity_exclusions=[EXCLUSION])
        self.assertNotIn("identity_key", held[0])

    def test_next_day_and_same_day_general_export_preserve_exclusions_without_db_reads(self):
        fixed = reports.repair_frontend_history(history(), {"2026-09-14": [row(NEW)]}, identity_exclusions=[EXCLUSION])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps(fixed))
            with patch.object(reports, "connect", side_effect=AssertionError("frozen export cannot read DB")):
                for date in ("2026-09-14", "2026-09-15"):
                    payload = reports.export_frontend_history("unused", path, reports_by_date={date: [row(NEW)]}, expected_date=date)
                    self.assertEqual(payload["identity_exclusions"], [EXCLUSION])
                    self.assertNotIn("identity_key", payload["rankings"][date][NEW])
                    self.assertEqual(payload["rankings"]["2026-09-13"], fixed["rankings"]["2026-09-13"])

    def test_compaction_preserves_metadata_and_hydrated_rows_roundtrip(self):
        payload = reports.compact_frontend_history(history(), identity_exclusions=[EXCLUSION], generated_at="fixed")
        hydrated = reports.inflate_frontend_history(payload)
        roundtrip = reports.compact_frontend_history(hydrated, identity_exclusions=payload["identity_exclusions"], generated_at="fixed")
        self.assertEqual(roundtrip, payload)
        self.assertEqual(reports.compact_frontend_history(payload, generated_at="fixed"), payload)

    def test_validator_rejects_shared_identity_even_after_old_version_rows_expire(self):
        for dates in ({"2026-09-14": [row(NEW, identity_key=OLD)]},
                      {"2026-09-13": [row(OLD, identity_key="common")],
                       "2026-09-14": [row(NEW, identity_key="common")]}):
            payload = reports.compact_frontend_history(dates)
            payload["identity_exclusions"] = [EXCLUSION]
            with self.assertRaisesRegex(ValueError, "Excluded recordings"):
                reports.validate_frontend_history(payload, "2026-09-14")

    def test_malformed_exclusions_and_silent_policy_replacement_are_rejected(self):
        values = [None, {}, [{**EXCLUSION, "video_ids": [OLD]}], [{**EXCLUSION, "video_ids": [OLD, OLD]}],
                  [{**EXCLUSION, "video_ids": [OLD, " " + NEW]}], [{**EXCLUSION, "reason": ""}],
                  [{**EXCLUSION, "evidence_ref": ""}], [{**EXCLUSION, "extra": True}]]
        for value in values:
            payload = history()
            payload["identity_exclusions"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                reports.validate_frontend_history(payload, "2026-09-14")
        payload = reports.compact_frontend_history(history(), identity_exclusions=[EXCLUSION])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            reports.compact_frontend_history(payload, identity_exclusions=[{**EXCLUSION, "reason": "silently changed"}])


if __name__ == "__main__":
    unittest.main()
