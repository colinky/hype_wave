"""Observed Radio Mix must not share a recording with the original release."""
import unittest

from hype_db_common import version_signature, has_version_mismatch
from hype_db_store import _metadata_rows_equivalent
from ytmusic_playlist_sync import _has_recording_version_mismatch


class RecordingMixVersionTests(unittest.TestCase):
    def test_observed_bisbetic_radio_mix_cannot_match_original(self):
        original = 'Beauty and a Beat (feat. Nicki Minaj)'
        remix = 'Beauty and a Beat (Bisbetic Radio Mix) (feat. Nicki Minaj)'
        self.assertTrue(has_version_mismatch(original, remix))
        self.assertTrue(_has_recording_version_mismatch([original], [remix]))
        self.assertFalse(_metadata_rows_equivalent(
            {'title': original, 'artist': 'Justin Bieber', 'album': 'Believe'},
            {'title': remix, 'artist': 'Justin Bieber', 'album': 'Beauty and a Beat (Remixes)'},
        ))

    def test_named_mix_identity_is_preserved(self):
        for title in ('Song (Radio Mix)', 'Song - Summer Mix', 'Song (Extended Mix)', 'Song (Club Mix)'):
            with self.subTest(title=title):
                self.assertTrue(has_version_mismatch('Song', title))
                self.assertFalse(has_version_mismatch(title, title))
        self.assertTrue(has_version_mismatch('Song (Bisbetic Radio Mix)', 'Song (Club Mix)'))

    def test_mix_in_an_ordinary_title_is_not_a_version_marker(self):
        for title in ('Mix It Up', 'The Mix', 'Little Mix', 'Song (feat. Little Mix)'):
            with self.subTest(title=title):
                self.assertEqual(version_signature(title), '')


if __name__ == '__main__':
    unittest.main()
