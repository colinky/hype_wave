"""Observed exact watch/album responses; synthetic playback, no network or external DB."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from hype_db_schema import init_schema
import hype_db_store as store
import sync_validation as validation
import ytmusic_playlist_sync as matching
from tests.playlist_playability_fixture import PlaylistVerifier

CASES = json.loads(Path(__file__).with_name('fixtures').joinpath('observed_partial_artist_identity.json').read_text())['cases']


class ObservedClient:
    def __init__(self, case, language='ko'):
        self.case, self.language = case, language
        self.album_calls = 0
        self._auth_headers = {'Cookie': 'offline-browser-fixture'}
        self._session = object()
        self.headers = {}

    def get_song(self, video_id):
        p = self.case['player']
        return {'videoDetails': {'videoId': video_id, 'title': p['title'], 'author': p['artist'],
                                'lengthSeconds': str(self.case['length_seconds']), 'musicVideoType': p['music_video_type']}}

    def get_watch_playlist(self, videoId, **kwargs):
        return {'tracks': [deepcopy(self.case['watch'][self.language])]}

    def get_artist(self, artist_id):
        return {'name': next(a['name'] for a in self.case['watch'][self.language]['artists'] if a['id'] == artist_id)}

    def get_album(self, album_id):
        self.album_calls += 1
        return deepcopy(self.case['album'][self.language])


class PartialArtistIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SUPABASE_DB_URL': ''}))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('No network')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No network')))
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.case = deepcopy(CASES[0])
        self.clients = {}
        self.enterContext(patch.object(matching, 'make_ytmusic', side_effect=self.client))
        self.enterContext(patch.object(matching, 'YTMusic', side_effect=self.authenticated_clone))
        self.enterContext(matching.bilingual_cache_read_only())

    def authenticated_clone(self, headers, *, language, requests_session):
        self.assertEqual(headers, self.client()._auth_headers)
        self.assertIsNot(headers, self.client()._auth_headers)
        self.assertIs(requests_session, self.client()._session)
        return self.client(language=language)

    def client(self, _auth=None, *, language='ko', **kwargs):
        if language not in self.clients:
            self.clients[language] = ObservedClient(self.case, language)
        return self.clients[language]

    def metadata(self, partial=True, memo=None):
        return matching.get_verified_video_metadata(self.client(), self.case['video_id'],
                   metadata_cache=memo, allow_partial_artist_ids=partial)

    def seed(self, video_id=None):
        source = self.case['source']
        store.ensure_track(self.conn, track_uid='existing', video_id=video_id or self.case['video_id'],
                           yt_title=source['title_ko'], yt_artist=source['artist_ko'], yt_album=source['album_ko'],
                           status='matched', score=1)
        store.upsert_track_list_metadata(self.conn, service=source['service'], song_id=source['song_id'],
                                         track_uid='existing', row=source)
        self.conn.commit()

    def validate(self, *, source=None, states=None, selected=None):
        vid = self.case['video_id']
        verifier = PlaylistVerifier(states=states, metadata={vid: self.case['player'], 'old-video': self.case['player']})
        source = source or self.case['source']
        return validation.validate_matches(self.conn, service=source['service'], sources=[source],
                   matches=[{'song_id': source['song_id'], 'video_id': selected or vid, 'status': 'matched', 'score': 1}],
                   client=self.client(), verifier=verifier)

    def test_recorded_two_songs_keep_every_name_only_for_healthy_existing_id(self):
        for case in CASES:
            with self.subTest(video_id=case['video_id']):
                self.case, self.clients = deepcopy(case), {}
                self.conn.execute('DELETE FROM platform_song_ids')
                self.conn.execute('DELETE FROM yt_video_ids')
                self.conn.execute('DELETE FROM tracks')
                self.seed()
                before = '\n'.join(self.conn.iterdump())
                metadata = self.metadata()
                self.assertIsNotNone(metadata)
                self.assertFalse(metadata['artist_identity_complete'])
                for name in metadata['unlinked_artist_names']:
                    self.assertIn(name, metadata['artist_ko'].lower())
                    self.assertIn(name, metadata['artist_en'].lower())
                result = self.validate()[0]
                self.assertEqual(result['video_id'], case['video_id'])
                self.assertNotIn('canonical_decision', result)
                self.assertEqual('\n'.join(self.conn.iterdump()), before)

    def test_default_strict_and_shared_memo_never_reuse_partial_for_substitution(self):
        memo = {}
        self.assertIsNone(self.metadata(partial=False, memo=memo))
        self.assertIsNotNone(self.metadata(memo=memo))
        self.assertIsNone(self.metadata(partial=False, memo=memo))
        other = 'other-video'
        self.assertFalse(matching._compare_playlist_video_ids(
            self.client(), [self.case['video_id']], [other], metadata_cache=memo)['matches'])

    def test_melon_whenever_with_the_same_explicit_credits_keeps_its_existing_id(self):
        self.case, self.clients = deepcopy(CASES[1]), {}
        self.case['source'].update(service='melon', song_id='602137421', title_en='', artist_en='', album_en='',
                                   artist_ko='dress, Raf Sandou', album_ko='MOHO')
        self.seed()
        self.assertEqual(self.validate()[0]['video_id'], self.case['video_id'])

    def test_all_null_public_row_uses_same_account_clone_only_for_partial_keep(self):
        public = ObservedClient(self.case)
        public.get_watch_playlist = lambda **kwargs: {'tracks': [{**self.case['watch']['ko'],
            'artists': [{'id': None, 'name': '김하온 (HAON), Nosun, Raf Sandou, Marv 및 정준혁'}]}]}
        with patch.object(matching, 'make_ytmusic', return_value=public), patch.object(
                matching, 'YTMusic', side_effect=self.authenticated_clone) as cloned:
            self.assertIsNone(self.metadata(partial=False))
            cloned.assert_not_called()
            self.assertIsNotNone(self.metadata())
            self.assertEqual(cloned.call_count, 2)

    def test_partial_cannot_create_an_unauthenticated_or_unbounded_locale_client(self):
        for field, value in (('_auth_headers', {}), ('_session', None)):
            with self.subTest(field=field), patch.object(self.client(), field, value):
                self.assertIsNone(self.metadata())

    def test_new_selection_and_unavailable_replacement_cannot_use_partial(self):
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate()
        self.seed('old-video')
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(states={'old-video': 'unavailable'})

    def test_even_injected_partial_result_cannot_authorize_a_new_id(self):
        metadata = self.metadata()
        with patch.object(matching, 'get_verified_video_metadata', return_value=metadata):
            with self.assertRaises(validation.PlaybackBlocked):
                self.validate()
        self.seed('old-video')
        old = {**metadata, 'video_id': 'old-video', 'artist_identity_complete': True}
        calls = []
        def observed(_client, video_id, **kwargs):
            calls.append((video_id, kwargs.get('allow_partial_artist_ids', False)))
            return old if video_id == 'old-video' else metadata
        with patch.object(matching, 'get_verified_video_metadata', side_effect=observed):
            with self.assertRaises(validation.PlaybackBlocked):
                self.validate(states={'old-video': 'unavailable'})
        self.assertIn((self.case['video_id'], False), calls)

    def test_a_new_candidate_cannot_displace_the_healthy_partial_existing_recording(self):
        self.seed()
        result = self.validate(selected='different-candidate')[0]
        self.assertEqual(result['video_id'], self.case['video_id'])
        self.assertNotIn('canonical_decision', result)

    def test_each_source_locale_must_explicitly_credit_unlinked_performers(self):
        self.seed()
        for field in ('artist_ko', 'artist_en'):
            source = {**self.case['source'], field: self.case['source'][field].replace('Marv', 'Different Person')}
            with self.subTest(field=field), self.assertRaises(validation.PlaybackBlocked):
                self.validate(source=source)

    def test_source_cannot_omit_linked_collaborators_while_retaining_lead_and_marv(self):
        self.seed()
        source = {**self.case['source'], 'artist_ko': '김하온, Marv', 'artist_en': 'HAON, Marv'}
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(source=source)

    def test_same_short_name_cannot_hide_a_different_unlinked_affiliation(self):
        for language in ('ko', 'en'):
            self.case['watch'][language]['artists'][-1]['name'] = 'Marv (Band A)'
            self.case['album'][language]['tracks'][0]['artists'][-1]['name'] = 'Marv (Band A)'
        self.seed()
        source = {**self.case['source'], **{field: self.case['source'][field].replace('Marv', 'Marv (Band B)')
                                          for field in ('artist_ko', 'artist_en')}}
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(source=source)

    def test_partial_cannot_hide_explicit_feature_or_album_version_conflict(self):
        self.seed()
        for change in ({'title_ko': 'TICK TOCK (feat. Different Person)'},
                       {'album_en': 'Show Me The Money 12 Episode 1 (Radio Mix)'}):
            with self.subTest(change=change), self.assertRaises(validation.PlaybackBlocked):
                self.validate(source={**self.case['source'], **change})

    def test_missing_or_changed_lead_id_and_different_unlinked_locale_are_inconclusive(self):
        for location, change in ((('ko', 0), {'id': None}), (('en', 0), {'id': 'another-lead'}),
                                 (('en', -1), {'name': 'Different Person'})):
            self.case, self.clients = deepcopy(CASES[0]), {}
            language, index = location
            self.case['watch'][language]['artists'][index].update(change)
            self.case['album'][language]['tracks'][0]['artists'][index].update(change)
            with self.subTest(change=change):
                self.assertIsNone(self.metadata())

    def test_exact_album_corroboration_is_required_and_cannot_contradict_watch(self):
        for change in ({'title': 'Different Song'}, {'videoId': 'wrong-id'},
                       {'duration': '0:17'}, {'isAvailable': False},
                       {'artists': [{'name': 'Different Person', 'id': None}]}):
            self.case, self.clients = deepcopy(CASES[0]), {}
            self.case['album']['en']['tracks'][0].update(change)
            with self.subTest(change=change):
                self.assertIsNone(self.metadata())

    def test_two_missing_album_names_cannot_corroborate_each_other(self):
        for value in (None, '', '   '):
            self.case, self.clients = deepcopy(CASES[0]), {}
            self.case['watch']['ko']['album']['name'] = value
            self.case['album']['ko']['title'] = value
            with self.subTest(value=value):
                self.assertIsNone(self.metadata())

    def test_explicit_watch_unavailable_is_not_overridden_by_album_available(self):
        self.case['watch']['ko']['isAvailable'] = False
        self.assertTrue(self.case['album']['ko']['tracks'][0]['isAvailable'])
        self.assertIsNone(self.metadata())
        for language in ('ko', 'en'):
            self.case['watch'][language]['artists'] = self.case['watch'][language]['artists'][:1]
        self.assertIsNone(self.metadata(partial=False))

    def test_partial_cannot_bypass_unavailable_unknown_or_bad_auth(self):
        self.seed()
        for state in ('unknown', 'unavailable'):
            with self.subTest(state=state), self.assertRaises(validation.PlaybackBlocked):
                self.validate(states={self.case['video_id']: state})
        verifier = PlaylistVerifier(health='unknown')
        with self.assertRaises(validation.PlaybackBlocked):
            validation.validate_matches(self.conn, service='spotify', sources=[self.case['source']], matches=[],
                                        client=self.client(), verifier=verifier)

    def test_reviewed_names_do_not_merge_a_different_performer_or_affiliation(self):
        pairs = [('악동뮤지션', 'AKMU'), ('도경수', '디오'), ('디오', 'D.O.'),
                 ('제이홉', 'j-hope of BTS'), ('선우(THE BOYZ)', 'SUNWOO'), ('SOLE (쏠)', 'SOLE')]
        for left, right in pairs:
            with self.subTest(left=left):
                self.assertTrue(validation.recording_identity_matches({'title': 'Song', 'artist': left},
                                                                      {'title': 'Song', 'artist': right}, player=True))
                self.assertFalse(validation.recording_identity_matches({'title': 'Song (feat. '+left+')', 'artist': 'Lead'},
                                         {'title': 'Song (feat. Different Person)', 'artist': 'Lead'}))
        self.assertFalse(validation.recording_identity_matches({'title': 'Song (feat. John of One Band)', 'artist': 'Lead'},
                             {'title': 'Song (feat. John of Another Band)', 'artist': 'Lead'}))


if __name__ == '__main__':
    unittest.main()
