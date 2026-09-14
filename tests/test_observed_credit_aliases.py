"""Reviewed literal artist credits keep different people and roles separate."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from sync_validation import recording_identity_matches, source_recording_matches
from ytmusic_playlist_sync import ALIASES

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'tests/fixtures/observed_credit_identity.json').read_text())['cases']


class ObservedCreditAliasesTests(unittest.TestCase):
    def test_observed_bilingual_source_and_player_credits_are_consistent(self):
        for case in CASES:
            with self.subTest(case=case['name']):
                self.assertTrue(source_recording_matches(case['source'], case['metadata']))
                self.assertTrue(recording_identity_matches(case['metadata'], case['player'], player=True))

    def test_other_performer_or_recording_version_is_not_approved(self):
        for case in CASES:
            for changes in ({'artist': 'An unrelated performer', 'artist_ko': 'An unrelated performer',
                             'artist_en': 'An unrelated performer'},
                            {'title_en': case['metadata']['title_en'] + ' (Live)'},
                            {'title_en': case['metadata']['title_en'] + ' (feat. Another Guest)'}):
                with self.subTest(case=case['name'], changes=changes):
                    self.assertFalse(source_recording_matches(case['source'], {**case['metadata'], **changes}))

    def test_minjeong_does_not_identify_winter_or_kim_minjeong(self):
        variants = {name.casefold() for name in ALIASES.get_variants('Minjeong', 'artist')}
        self.assertIn('강민정', variants)
        self.assertFalse(variants & {'winter', '윈터', '김민정', 'kim minjeong'})
        left = {'title': 'Time Stop Button (feat. Minjeong)', 'artist': 'RawRabbit'}
        for name in ('Winter', '윈터', '김민정', 'Kim Minjeong'):
            self.assertFalse(recording_identity_matches(left,
                {'title': f'Time Stop Button (feat. {name})', 'artist': 'RawRabbit'}))

    def test_night_night_guest_rename_does_not_remove_or_replace_the_guest(self):
        case = next(row for row in CASES if row['name'] == 'night_night')
        self.assertIn('Whys Young', ALIASES.get_variants('윤지영', 'artist'))
        for artist in ('Andr', 'Andr, Another Guest'):
            source = {**case['source'], **{
                field: artist for field in ('artist', 'artist_ko', 'artist_en')}}
            with self.subTest(artist=artist):
                self.assertFalse(source_recording_matches(source, case['metadata']))

    def test_hannah_jang_alias_preserves_the_named_guest_and_role(self):
        case = next(row for row in CASES if row['name'] == 'lay_down_on_the_grass')
        self.assertIn('Hannah Jang', ALIASES.get_variants('장한나', 'artist'))
        for artist in ('CRUCiAL STAR', 'CRUCiAL STAR, Another Guest'):
            source = {**case['source'], **{
                field: artist for field in ('artist', 'artist_ko', 'artist_en')}}
            with self.subTest(artist=artist):
                self.assertFalse(source_recording_matches(source, case['metadata']))
        self.assertFalse(recording_identity_matches(
            {'title': 'Lay Down On The Grass (feat. 장한나)', 'artist': 'CRUCiAL STAR'},
            {'title': 'Lay Down On The Grass (Narr. Hannah Jang)', 'artist': 'CRUCiAL STAR'}))

    def test_reviewed_affiliation_does_not_strip_other_people_or_roles(self):
        left = {'title': 'Question Mark (feat. 최자)', 'artist': 'Primary'}
        self.assertTrue(recording_identity_matches(left,
            {'title': 'Question Mark (feat. CHOIZA Of Dynamicduo)', 'artist': 'Primary'}))
        for title in ('Question Mark (feat. Another Person Of Dynamicduo)',
                      'Question Mark (Narr. CHOIZA Of Dynamicduo)'):
            self.assertFalse(recording_identity_matches(left, {'title': title, 'artist': 'Primary'}))

    def test_identity_checks_do_not_rewrite_source_or_observed_metadata(self):
        cases = deepcopy(CASES)
        for case in cases:
            source_recording_matches(case['source'], case['metadata'])
            recording_identity_matches(case['metadata'], case['player'], player=True)
        self.assertEqual(cases, CASES)


if __name__ == '__main__':
    unittest.main()
