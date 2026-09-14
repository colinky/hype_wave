"""Run the actual browser hydrator with exclusion counterexamples; no network or DB."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest


EXCLUSION = {"video_ids": ["old", "new"], "reason": "recording version differs",
             "evidence_ref": "fixture:version-review"}


@unittest.skipUnless(shutil.which("node"), "Node is needed for real browser helper checks")
class HistoryIdentityExclusionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = (Path(__file__).resolve().parents[1] / "docs/index.html").read_text()
        cls.script = html[html.index("        function historyIdentity"):html.index("        async function loadChart")] + """
            const payloads = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            process.stdout.write(JSON.stringify(payloads.map(payload => {
                try { return {history: hydrateHistory(payload)}; }
                catch (error) { return {error: error.message}; }
            })));
        """

    def run_payloads(self, *payloads):
        result = subprocess.run([shutil.which("node"), "-e", self.script],
                                input=json.dumps(payloads), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    @staticmethod
    def payload():
        return {"schema_version": 2, "dates": ["2026-09-14", "2026-09-13"],
                "tracks": {"old": {"title": "Song", "artist": "Artist", "album": "Original"},
                           "new": {"title": "Song", "artist": "Artist", "album": "Different version"}},
                "rankings": {"2026-09-14": {"new": {"hype_rank": 1, "hype_index": 80, "album": ""}},
                             "2026-09-13": {"old": {"hype_rank": 1, "hype_index": 70}}}}

    def test_valid_optional_exclusions_leave_all_hydrated_rows_unchanged(self):
        original = self.payload()
        excluded = {**copy.deepcopy(original), "identity_exclusions": [EXCLUSION]}
        first, second = self.run_payloads(original, excluded)
        self.assertEqual(second, first)
        self.assertEqual(second["history"]["2026-09-14"][0]["album"], "")

    def test_legacy_without_exclusions_and_valid_exclusions_preserve_fallback_rows(self):
        original = {"2026-09-13": [{"video_id": "old", "title": "Song"}],
                    "2026-09-14": [{"video_id": "new", "title": "Song"}]}
        excluded = {**copy.deepcopy(original), "identity_exclusions": [EXCLUSION]}
        first, second = self.run_payloads(original, excluded)
        self.assertEqual(first, {"history": original})
        self.assertEqual(second, first)

    def test_malformed_exclusion_metadata_is_rejected_even_without_affected_rows(self):
        malformed = [None, {}, "invalid", [None], [[]], [{}], [{**EXCLUSION, "extra": "field"}]]
        for key, values in {
            "video_ids": [None, "old,new", [], ["old"], ["old", "old"], ["old", " "],
                          ["old", 1], ["old", " new"], ["old", "new "]],
            "reason": [None, "", " ", 1], "evidence_ref": [None, "", " ", {}],
        }.items():
            malformed.extend([{**EXCLUSION, key: value}] for value in values)
        payloads = [{"2026-09-14": [], "identity_exclusions": value} for value in malformed]
        for value, result in zip(malformed, self.run_payloads(*payloads)):
            with self.subTest(value=value):
                self.assertIn("invalid identity exclusions", result.get("error", ""))

    def test_cross_date_alias_into_excluded_legacy_fallback_identity_is_rejected(self):
        payload = self.payload()
        payload["identity_exclusions"] = [EXCLUSION]
        payload["rankings"]["2026-09-14"]["new"]["identity_key"] = "old"
        result, = self.run_payloads(payload)
        self.assertIn("joins recordings", result.get("error", ""))

    def test_shared_third_identity_across_any_excluded_pair_is_rejected(self):
        payloads = []
        for left, right in (("old", "new"), ("old", "third"), ("new", "third")):
            payloads.append({
                "identity_exclusions": [{**EXCLUSION, "video_ids": ["old", "new", "third"]}],
                "2026-09-13": [{"video_id": left, "identity_key": "shared-key"}],
                "2026-09-14": [{"video_id": right, "identity_key": "shared-key"}],
            })
        for result in self.run_payloads(*payloads):
            self.assertIn("joins recordings", result.get("error", ""))

    def test_excluded_identity_stays_protected_after_old_rows_leave_history_window(self):
        payload = {"identity_exclusions": [EXCLUSION],
                   "2026-09-14": [{"video_id": "new", "identity_key": "old"}]}
        result, = self.run_payloads(payload)
        self.assertIn("joins recordings", result.get("error", ""))

    def test_duplicate_exclusion_evidence_must_agree(self):
        original = self.payload()
        same = {**copy.deepcopy(original), "identity_exclusions": [EXCLUSION, EXCLUSION]}
        conflicts = [{**copy.deepcopy(original), "identity_exclusions": [EXCLUSION, {**EXCLUSION, **change}]}
                     for change in ({"reason": "different reason"}, {"evidence_ref": "different evidence"},
                                    {"video_ids": ["new", "old"]})]
        plain, accepted, *rejected = self.run_payloads(original, same, *conflicts)
        self.assertEqual(accepted, plain)
        for result in rejected:
            self.assertIn("conflicting identity exclusions", result.get("error", ""))

    def test_same_video_on_multiple_dates_and_unrelated_aliases_remain_valid(self):
        original = {"2026-09-13": [{"video_id": "old", "identity_key": "same-recording"}],
                    "2026-09-14": [{"video_id": "old", "identity_key": "same-recording"},
                                   {"video_id": "unrelated", "identity_key": "same-recording"}]}
        result, = self.run_payloads({**original, "identity_exclusions": [EXCLUSION]})
        self.assertEqual(result, {"history": original})


if __name__ == "__main__":
    unittest.main()
