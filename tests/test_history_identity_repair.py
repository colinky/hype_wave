"""Offline history repair: real ID regressions, temporal isolation and browser parity."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hype_db_reports as reports


STAMP = "2026-09-14T13:00:00+00:00"
CASES = (
    ("REDRED", "CORTIS", "5sOgJQ3N03Q", "DBOyG7y_FQg"),
    ("Pop Off Pop Off", "KiiiKiii", "QbsbqekMkCU", "cQPXraDIvS4"),
    ("RUDE!", "Hearts2Hearts", "Q4AE3ub4nBM", "EsU7ng0KIy0"),
    ("404 (New Era)", "KiiiKiii", "MUny_GDYIDM", "KL7km3PxilQ"),
    ("Less than a Lover", "JENNIE", "z9ifbheDGFM", "1PVUTUFxq8g"),
    ("BANG BANG", "IVE", "TfAiFxoXVlE", "1moSPsMlDBA"),
    ("FALLEN ANGEL", "JENNIE", "cOARkf5ZmtI", "SCl9CL9vqa4"),
    ("ICONIC HEART", "Hearts2Hearts", "A9EpZWrQ3dM", "hl7_dxuytRQ"),
    ("Candy Pink Magic Hole Flip Phone", "KiiiKiii", "DbI-M_jZukE", "QJmPjIwxzhc"),
    ("Animal", "KATSEYE", "vOfyxPRunFM", "kDsRsv3DsHg"),
    ("Bloody Paradise", "ENHYPEN", "EmRFwlGlbb8", "YJhEsDa2o8w"),
    ("Heroine", "back number", "XNc6BJlvczs", "107UoVP2fAA"),
    ("꿈꾸던 어른이 되었나요?", "MEOMURU", "GxChUrrY4bc", "JJx_WQXOeK0"),
)


def row(video="5sOgJQ3N03Q", title="REDRED", artist="CORTIS", rank=1, **fields):
    return {
        "video_id": video, "title": title, "artist": artist, "album": "",
        "yt_title": title, "yt_artist": artist, "yt_album": "",
        "artwork_url": "", "apple_url": "", "melon_url": "", "spotify_url": "",
        "hype_rank": rank, "hype_index": 100.0 - rank, "apple_rank": rank,
        "melon_rank": None, "melon_genz_rank": None, "ytmusic_rank": None,
        **fields,
    }


def compact(history):
    return reports.compact_frontend_history(history, generated_at=STAMP)


class HistoryIdentityRepairTests(unittest.TestCase):
    def test_rank_only_changes_do_not_rewrite_existing_identity_fields(self):
        source = "https://music.apple.com/album/1?i=2"
        older = row("old-video11", apple_url=source)
        current = row("current1111", apple_url=source)
        neighbor = row("neighbor111", rank=2)
        history = {"2026-09-12": [older], "2026-09-13": [current, neighbor]}
        incoming = [{**neighbor, "hype_rank": 1}, {**current, "hype_rank": 2}]
        result = reports.repair_frontend_history(compact(history), {"2026-09-13": incoming}, generated_at=STAMP)
        self.assertEqual(reports.inflate_frontend_history(result)["2026-09-13"], incoming)
        self.assertEqual(reports.inflate_frontend_history(result)["2026-09-12"], [older])

    def test_sparse_metadata_preserves_old_text_and_explicit_empty_values(self):
        first = row(title="Original display", album="", apple_url="")
        second = row(title="Localized display", album="New album", apple_url="https://music.apple.com/album/1?i=2")
        history = {"2026-09-13": [first], "2026-09-14": [second]}
        payload = compact(history)
        self.assertEqual(payload["rankings"]["2026-09-13"][first["video_id"]]["album"], "")
        self.assertEqual(payload["tracks"][first["video_id"]]["title"], "Localized display")
        self.assertEqual(reports.inflate_frontend_history(payload), dict(reversed(list(history.items()))))
        self.assertEqual(compact(reports.inflate_frontend_history(payload)), payload)
        self.assertNotIn("video_id", payload["rankings"]["2026-09-13"][first["video_id"]])

    def test_validation_checks_hydrated_overrides_and_duplicate_identity(self):
        base = compact({"2026-09-14": [row()]})
        for field, value in (("artist", ""), ("album", None), ("video_id", "other"),
                             ("unknown_field", "value"), ("identity_key", "")):
            candidate = copy.deepcopy(base)
            candidate["rankings"]["2026-09-14"]["5sOgJQ3N03Q"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                reports.validate_frontend_history(candidate, "2026-09-14")
        duplicates = compact({"2026-09-14": [row(identity_key="song"), row("DBOyG7y_FQg", rank=2, identity_key="song")]})
        with self.assertRaises(ValueError):
            reports.validate_frontend_history(duplicates, "2026-09-14")
        invalid_id = copy.deepcopy(base)
        invalid_id["rankings"]["2026-09-14"]["5sOgJQ3N03Q"]["video_id"] = "changed"
        with self.assertRaises(ValueError):
            reports.inflate_frontend_history(invalid_id)

    def test_all_thirteen_corrections_preserve_scores_and_unaffected_dates(self):
        old_rows = [row(old, title, artist, index) for index, (title, artist, old, new) in enumerate(CASES, 1)]
        wrong_rows = [row(new, title, artist, index) for index, (title, artist, old, new) in enumerate(CASES, 1)]
        payload = compact({"2026-09-12": old_rows, "2026-09-13": wrong_rows, "2026-09-14": old_rows})
        before = copy.deepcopy(payload)
        repaired = reports.repair_frontend_history(payload, {"2026-09-13": old_rows}, generated_at=STAMP)
        inflated = reports.inflate_frontend_history(repaired)
        self.assertEqual(inflated["2026-09-13"], old_rows)
        self.assertEqual(repaired["rankings"]["2026-09-14"], before["rankings"]["2026-09-14"])
        self.assertEqual(inflated["2026-09-12"], old_rows)
        self.assertEqual(payload, before)
        self.assertEqual(reports.repair_frontend_history(repaired, {"2026-09-13": old_rows}, generated_at=STAMP), repaired)

    def test_frozen_raw_and_manifest_mapping_rebuild_without_live_joins(self):
        raw = [
            {"service": "apple", "job_name": "KR-Top-100", "source_variant": "default",
             "reference_period": "2026-09-13", "song_id": "1887671067", "rank_order": 5},
            {"service": "ytmusic", "job_name": "Weekly-Hot-100", "source_variant": "default",
             "reference_period": "2026-W37", "song_id": "U6BDbXIah-Y", "rank_order": 6},
        ]
        source_before = copy.deepcopy(raw)
        metadata = {"album_id": "1887671065", "track_uid": "verified-recording",
                    "title": "REDRED", "artist": "CORTIS", "album": "GREENGREEN",
                    "yt_title": "REDRED", "yt_artist": "CORTIS", "yt_album": "REDRED", "artwork_url": ""}
        def frozen_report(video_id):
            # This captured source-to-recording mapping is the repair input.
            mapping = {(item["service"], item["song_id"]): {**metadata, "video_id": video_id} for item in raw}
            joined = [{**item, **mapping[(item["service"], item["song_id"]) ]} for item in raw]
            return reports.build_hype_report_from_rows(joined)
        with patch.object(reports, "connect", side_effect=AssertionError("live mapping read")):
            wrong = frozen_report("DBOyG7y_FQg")
            corrected = frozen_report("5sOgJQ3N03Q")
            before = compact({"2026-09-14": wrong})
            after = reports.repair_frontend_history(before, {"2026-09-14": corrected})
        self.assertEqual(raw, source_before)
        for field in reports.DAILY_RANKING_FIELDS:
            self.assertEqual(wrong[0][field], corrected[0][field])
        self.assertEqual(list(after["rankings"]["2026-09-14"]), ["5sOgJQ3N03Q"])

    def test_exact_source_carries_key_without_title_or_uid_guessing(self):
        source_url = "https://www.melon.com/song/detail.htm?songId=601807965"
        before = {"2026-09-13": [row(melon_url=source_url)]}
        carried = reports.carry_history_identity([row("DBOyG7y_FQg", melon_url=source_url)], before)
        self.assertEqual(carried[0]["identity_key"], "5sOgJQ3N03Q")
        other_version = reports.carry_history_identity([row("otherrecord", track_uid="same-current-uid")], before)
        self.assertNotIn("identity_key", other_version[0])
        separate = {"2026-09-13": [row(apple_url="apple-one"), row("different11", rank=2, melon_url="melon-two")]}
        with self.assertRaises(ValueError):
            reports.carry_history_identity([row("new-video11", apple_url="apple-one", melon_url="melon-two")], separate)

    def test_explicit_receipt_links_versions_without_shared_source_and_rejects_conflicts(self):
        link = {"old_video_id": "5sOgJQ3N03Q", "new_video_id": "DBOyG7y_FQg", "evidence_ref": "repair:case-1"}
        carried = reports.carry_history_identity([row("DBOyG7y_FQg")], {"2026-09-13": [row()]}, identity_links=[link])
        self.assertEqual(carried[0]["identity_key"], "5sOgJQ3N03Q")
        for links in ([{**link, "evidence_ref": ""}], [link, {**link, "old_video_id": "different"}],
                      [link, {**link, "old_video_id": link["new_video_id"], "new_video_id": link["old_video_id"]}]):
            with self.subTest(links=links), self.assertRaises(ValueError):
                reports.carry_history_identity([row()], {}, identity_links=links)
        history = {"2026-09-13": [row(identity_key="one", apple_url="source")],
                   "2026-09-14": [row("DBOyG7y_FQg", identity_key="two", apple_url="source")]}
        with self.assertRaises(ValueError):
            reports.carry_history_identity([row(apple_url="source")], history)

    def test_repair_then_same_day_and_next_day_export_carry_key_without_db(self):
        link = {"old_video_id": "5sOgJQ3N03Q", "new_video_id": "DBOyG7y_FQg", "evidence_ref": "repair:verified-same-recording"}
        before = compact({"2026-09-13": [row()], "2026-09-14": [row("DBOyG7y_FQg")]})
        corrected = reports.repair_frontend_history(before,
            {"2026-09-14": [row("DBOyG7y_FQg", identity_key="5sOgJQ3N03Q")]}, identity_links=[link])
        with tempfile.TemporaryDirectory() as directory, patch.object(reports, "connect", side_effect=AssertionError("DB accessed")), patch.object(reports, "init_db", side_effect=AssertionError("DB initialized")):
            path = Path(directory) / "history.json"
            path.write_text(json.dumps(corrected))
            for date in ("2026-09-14", "2026-09-15"):
                exported = reports.export_frontend_history(Path(directory) / "absent.db", path,
                    reports_by_date={date: [row("DBOyG7y_FQg")]}, expected_date=date)
                self.assertEqual(exported["rankings"][date]["DBOyG7y_FQg"]["identity_key"], "5sOgJQ3N03Q")
                self.assertEqual(reports.inflate_frontend_history(exported)["2026-09-13"], [row()])

    def test_past_repair_preserves_unaffected_rows_despite_newer_identity_receipts(self):
        unrelated = row(apple_url="apple-unrelated")
        wrong = row("clsPwXUeZFk", "Dirty Work", "aespa", 2, apple_url="apple-original")
        original = row("DxVbg_RbczA", "Dirty Work", "aespa", 2, apple_url="apple-original")
        history = {"2026-09-12": [unrelated, wrong], "2026-09-13": [unrelated, wrong],
                   "2026-09-14": [{**unrelated, "identity_key": unrelated["video_id"]}, original]}
        payload = compact(history)
        exclusions = [{"video_ids": ["DxVbg_RbczA", "clsPwXUeZFk"],
                       "reason": "Original and featured recordings differ", "evidence_ref": "fixture:exact-source"}]
        corrected = {**original, "identity_key": "DxVbg_RbczA"}
        changes = {date: [unrelated, corrected] for date in ("2026-09-12", "2026-09-13")}
        saved_payload, saved_changes = copy.deepcopy(payload), copy.deepcopy(changes)
        result = reports.repair_frontend_history(payload, changes, identity_exclusions=exclusions, generated_at=STAMP)
        self.assertEqual(reports.inflate_frontend_history(result), {**history, **changes})
        self.assertEqual(payload, saved_payload)
        self.assertEqual(changes, saved_changes)
        self.assertNotIn("identity_key", reports.inflate_frontend_history(result)["2026-09-12"][0])
        self.assertEqual(result["identity_exclusions"], exclusions)
        self.assertEqual(reports.repair_frontend_history(result, changes, generated_at=STAMP), result)

    def test_scoped_repair_does_not_prune_or_accept_undeclared_empty_dates(self):
        first, last = "2026-08-15", "2026-09-14"
        before = compact({first: [row()], last: [row()]})
        result = reports.repair_frontend_history(before, {first: [row(album="Corrected")]})
        self.assertEqual(result["dates"], [last, first])
        self.assertEqual(reports.inflate_frontend_history(result)[last], [row()])
        for changes in ({first: []}, {"invalid": [row()]}, {"2026-09-15": [row()]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reports.repair_frontend_history(before, changes)

    def test_atomic_write_refuses_stale_or_invalid_history_and_cleans_temporary_file(self):
        before = compact({"2026-09-13": [row()]})
        after = compact({"2026-09-13": [row()], "2026-09-14": [row()]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            raw = json.dumps(before).encode()
            path.write_bytes(raw)
            with self.assertRaises(RuntimeError):
                reports.write_frontend_history(after, path, expected_date="2026-09-14", expected_file_sha256="stale")
            self.assertEqual(path.read_bytes(), raw)
            invalid = copy.deepcopy(after)
            invalid["rankings"]["2026-09-13"]["5sOgJQ3N03Q"]["title"] = ""
            with self.assertRaises(ValueError):
                reports.write_frontend_history(invalid, path, expected_date="2026-09-14")
            self.assertEqual(path.read_bytes(), raw)
            reports.write_frontend_history(after, path, expected_date="2026-09-14", expected_file_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(json.loads(path.read_text()), after)
            self.assertFalse(list(Path(directory).glob(".history.json.*.tmp")))

    @unittest.skipUnless(shutil.which("node"), "Node is needed for real browser helper parity")
    def test_actual_browser_hydrate_and_identity_match_python(self):
        payload = compact({"2026-09-13": [row(album="")],
                           "2026-09-14": [row("DBOyG7y_FQg", album="New", identity_key="5sOgJQ3N03Q")]})
        html = (Path(__file__).resolve().parents[1] / "docs/index.html").read_text()
        functions = html[html.index("        function historyIdentity"):html.index("        async function loadChart")]
        script = functions + """
            const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
            const history = hydrateHistory(payload);
            const keys = Object.fromEntries(Object.entries(history).map(([date, rows]) => [date, rows.map(historyIdentity)]));
            payload.rankings['2026-09-14']['DBOyG7y_FQg'].video_id = 'injected';
            let blocked = false;
            try { hydrateHistory(payload); } catch (error) { blocked = true; }
            process.stdout.write(JSON.stringify({history, keys, blocked}));
        """
        result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(payload), text=True, capture_output=True, check=True)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["history"], reports.inflate_frontend_history(payload))
        self.assertEqual(actual["keys"]["2026-09-13"], actual["keys"]["2026-09-14"])
        self.assertTrue(actual["blocked"])


if __name__ == "__main__":
    unittest.main()
