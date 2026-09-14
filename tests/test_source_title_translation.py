"""A reviewed bilingual title is evidence for one exact source/candidate pair."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from sync_validation import proven_source_identity, recording_identity_matches, source_recording_matches


ROOT = Path(__file__).parents[1]
SOURCE = {
    "service": "melon", "song_id": "36386455",
    "title_ko": "사랑으로", "artist_ko": "wave to earth", "album_ko": "0.1 flaws and all.",
    "title_en": "", "artist_en": "", "album_en": "",
}
CANDIDATE = {
    "video_id": "QX2dqXr8mOU", "title": "love.", "artist": "wave to earth", "album": "0.1 flaws and all.",
    "title_ko": "love.", "artist_ko": "wave to earth", "album_ko": "0.1 flaws and all.",
    "title_en": "love.", "artist_en": "wave to earth", "album_en": "0.1 flaws and all.",
    "length_seconds": 308, "music_video_type": "MUSIC_VIDEO_TYPE_ATV",
}


class SourceTitleTranslationTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT / "matching_alias.json").read_text())

    def matches(self, source=None, candidate=None, policy=None):
        return source_recording_matches(source or SOURCE, candidate or CANDIDATE,
                                        policy=self.policy if policy is None else policy)

    def test_observed_same_apple_id_bilingual_title_allows_exact_308_second_candidate(self):
        proof = self.policy["source_identity_evidence"]["melon:36386455"]
        self.assertEqual(proof["kind"], "official_cross_service_title_translation")
        apple = [ref for ref in proof["references"] if ref.get("service") == "apple"]
        self.assertEqual({ref["song_id"] for ref in apple}, {"1770274550"})
        self.assertEqual({ref["title"] for ref in apple}, {"사랑으로", "love."})
        self.assertEqual({ref["isrc"] for ref in apple}, {"KRA252300813"})
        self.assertEqual({ref["duration_ms"] for ref in apple}, {307750})
        self.assertFalse(recording_identity_matches(SOURCE, CANDIDATE))
        self.assertTrue(self.matches())
        proven = proven_source_identity(SOURCE, "melon", self.policy)
        self.assertEqual(proven["title_en"], "love.")
        self.assertEqual(proven["title_ko"], "사랑으로")
        self.assertEqual(proven["length_seconds"], 307.75)

    def test_different_source_artist_song_album_version_or_service_cannot_use_translation(self):
        for change in ({"song_id": "another-source"}, {"service": "spotify"},
                       {"artist_ko": "Another Artist"}, {"album_ko": "Another Album"},
                       {"title_ko": "사랑으로 (Remix)"}, {"length_seconds": 3000}):
            with self.subTest(change=change):
                self.assertFalse(self.matches(source={**SOURCE, **change}))

    def test_conflicting_other_source_locale_is_not_hidden_by_the_translation(self):
        for title, artist, album in (("사랑으로 (Remix)", "wave to earth", "0.1 flaws and all."),
                                     ("사랑으로", "Another Artist", "0.1 flaws and all."),
                                     ("사랑으로", "wave to earth", "Another Album")):
            with self.subTest(title=title, artist=artist, album=album):
                self.assertFalse(self.matches(source={**SOURCE, "title": title, "artist": artist, "album": album}))

    def test_another_candidate_id_album_artist_or_version_is_not_approved(self):
        for change in ({"video_id": "another-video"}, {"album": "Another Album"},
                       {"album_ko": "Another Album"}, {"album_en": "Another Album"},
                       {"artist": "Another Artist"}, {"artist_en": "Another Artist"},
                       {"title": "love. (Remix)"}, {"title_en": "love. (Live)"},
                       {"title_ko": "love. (feat. Someone Else)"}, {"title_en": ""}):
            with self.subTest(change=change):
                self.assertFalse(self.matches(candidate={**CANDIDATE, **change}))

    def test_duration_rounding_is_accepted_but_short_long_or_unknown_candidates_are_not(self):
        for length in (307.75, 308):
            with self.subTest(accepted_length=length):
                self.assertTrue(self.matches(candidate={**CANDIDATE, "length_seconds": length}))
        for length in (25, 3000, 0, None, float("nan"), float("inf")):
            with self.subTest(rejected_length=length):
                self.assertFalse(self.matches(candidate={**CANDIDATE, "length_seconds": length}))

    def test_translation_requires_registered_proof_and_official_references(self):
        self.assertFalse(self.matches(policy={}))
        for change in ({"kind": "unreviewed_translation"}, {"references": []},
                       {"source_variants": []}, {"candidate": {}}):
            policy = deepcopy(self.policy)
            policy["source_identity_evidence"]["melon:36386455"].update(change)
            with self.subTest(change=change):
                self.assertFalse(self.matches(policy=policy))

    def test_identity_evidence_does_not_mutate_source_candidate_or_policy(self):
        source, candidate, policy = deepcopy(SOURCE), deepcopy(CANDIDATE), deepcopy(self.policy)
        before = deepcopy((source, candidate, policy))
        self.assertTrue(self.matches(source=source, candidate=candidate, policy=policy))
        self.assertEqual((source, candidate, policy), before)


if __name__ == "__main__":
    unittest.main()
