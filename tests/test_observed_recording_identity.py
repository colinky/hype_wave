"""Recorded public metadata, synthetic healthy evidence, isolated SQLite only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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

FIXTURE = json.loads(Path(__file__).with_name('fixtures').joinpath('observed_recording_identity.json').read_text())['cases']
POLICY = json.loads(Path(__file__).parents[1].joinpath('matching_alias.json').read_text())


class VerifiedPlayerArtistTests(unittest.TestCase):
    def setUp(self):
        # Exact LZWlP4kjEtw watch/artist/player observations, 2026-09-14.
        # The player omitted duration; synthetic lengths below test its guard.
        self.metadata = {
            'video_id': 'LZWlP4kjEtw', 'title': 'Akrapovic', 'artist': 'hamo', 'album': 'Akrapovic',
            'title_ko': '아크라포빅', 'artist_ko': '하모 (hamo)', 'album_ko': '아크라포빅',
            'title_en': 'Akrapovic', 'artist_en': 'hamo', 'album_en': 'Akrapovic',
            'length_seconds': 136, 'music_video_type': 'MUSIC_VIDEO_TYPE_ATV',
            'artist_identity_complete': True, 'artist_ids': ['UCuAp8osZtq3RFRHj4-YakGQ'],
            'artist_names_by_id': {'UCuAp8osZtq3RFRHj4-YakGQ': ['하모', '하모 (hamo)', 'hamo']},
        }
        self.player = {'video_id': 'LZWlP4kjEtw', 'title': 'Akrapovic', 'artist': '하모',
                       'exact_id': True, 'music_video_type': 'MUSIC_VIDEO_TYPE_ATV'}

    def test_observed_spelling_uses_exact_artist_proof_only_for_the_player(self):
        before = deepcopy((self.metadata, self.player))
        self.assertTrue(validation.recording_identity_matches(self.metadata, self.player, player=True))
        self.assertFalse(validation.recording_identity_matches(self.metadata, self.player))
        self.assertEqual((self.metadata, self.player), before)

    def test_one_valid_locale_cannot_hide_another_artist_or_extra_credit(self):
        for field in ('artist', 'artist_ko', 'artist_en'):
            for value in ('Unrelated Other Artist', 'hamo, Unrelated Other Artist', ['hamo'], ''):
                with self.subTest(field=field, value=value):
                    self.assertFalse(validation.recording_identity_matches(
                        {**self.metadata, field: value}, self.player, player=True))
        self.assertFalse(validation.recording_identity_matches(self.metadata,
            {**self.player, 'artist': 'Unrelated Other Artist', 'artist_en': '하모',
             'title_en': 'Akrapovic'}, player=True))

    def test_incomplete_or_other_video_artist_proof_cannot_authorize_the_spelling(self):
        artist_id = self.metadata['artist_ids'][0]
        other = 'UC' + 'x' * 22
        for changes in ({'artist_identity_complete': False}, {'artist_names_by_id': {}},
                        {'artist_ids': [artist_id, other]},
                        {'artist_names_by_id': {artist_id: ['hamo'], other: ['하모']}},
                        {'artist_names_by_id': {artist_id: ['hamo', '하모 (hamo)']}},
                        {'video_id': 'otherVID123'}):
            with self.subTest(changes=changes):
                self.assertFalse(validation.recording_identity_matches(
                    {**self.metadata, **changes}, self.player, player=True))
        for changes in ({'video_id': 'otherVID123'}, {'exact_id': False}, {'exact_id': 1}):
            with self.subTest(changes=changes):
                self.assertFalse(validation.recording_identity_matches(
                    self.metadata, {**self.player, **changes}, player=True))

    def test_artist_proof_never_bypasses_recording_checks(self):
        for changes in ({'title': 'Entirely Different Recording'}, {'title': 'Akrapovic (Live)'},
                        {'title': 'Akrapovic (feat. Other Guest)'}, {'length_seconds': 139}):
            with self.subTest(changes=changes):
                self.assertFalse(validation.recording_identity_matches(
                    self.metadata, {**self.player, **changes}, player=True))
        self.assertTrue(validation.recording_identity_matches(
            self.metadata, {**self.player, 'length_seconds': 136}, player=True))


class RecordedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SUPABASE_DB_URL': ''}))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No network')))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('No network')))
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def validate(self, case, source, *, state='playable', health='healthy', metadata=None, states=None):
        now = datetime.now(timezone.utc)
        class Verifier:
            environment = 'offline-observed-identity'
            def check_health(self):
                return {'auth_state': 'authenticated', 'run_health': health}
            def verify(self, video_id, **kwargs):
                observed = (states or {}).get(video_id, state)
                return {**case['player'], 'video_id': video_id, 'state': observed, 'exact_id': True,
                        'has_audio': observed == 'playable', 'confirmed_unavailable': observed == 'unavailable',
                        'environment': self.environment,
                        'auth_state': 'authenticated', 'run_health': health,
                        'observed_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat()}
        selected = {'song_id': source['song_id'], 'video_id': case['video_id'], 'status': 'matched', 'score': 1.0}
        def exact(_client, video_id, **kwargs):
            return case['old_metadata'] if video_id == case['old_metadata']['video_id'] else (metadata or case['metadata'])
        with patch.object(matching, 'get_verified_video_metadata', side_effect=exact):
            return validation.validate_matches(self.conn, service=source['service'], sources=[source],
                                               matches=[selected], client=object(), verifier=Verifier())

    def test_observed_normal_bindings_pass_but_wrong_feature_and_album_stay_blocked(self):
        accepted, blocked = [], []
        for case in FIXTURE:
            for source in case['sources']:
                expected = case['title'] != 'Dirty Work' and source['song_id'] != 'qdr0fZbuffY'
                before = '\n'.join(self.conn.iterdump())
                with self.subTest(title=case['title'], service=source['service'], expected=expected):
                    if expected:
                        self.assertEqual(self.validate(case, source)[0]['video_id'], case['video_id'])
                        accepted.append(source['song_id'])
                    else:
                        with self.assertRaises(validation.PlaybackBlocked):
                            self.validate(case, source)
                        blocked.append(source['song_id'])
                    self.assertEqual('\n'.join(self.conn.iterdump()), before)
        self.assertEqual((len(accepted), len(blocked)), (10, 2))

    def test_player_omission_does_not_allow_explicit_conflicting_credits_or_versions(self):
        case = deepcopy(FIXTURE[1])
        for title in ('Beauty and a Beat (feat. Flo Milli)', 'Beauty and a Beat (Radio Mix)',
                      'Beauty and a Beat (Japanese Ver.)'):
            with self.subTest(title=title):
                case['player']['title'] = title
                with self.assertRaises(validation.PlaybackBlocked):
                    self.validate(case, case['sources'][0])

    def test_one_matching_locale_cannot_hide_a_conflicting_feature_mix_or_album(self):
        case = FIXTURE[1]
        for changes in ({'title_ko': 'Beauty and a Beat (feat. Flo Milli)'},
                        {'title_ko': 'Beauty and a Beat (Radio Mix) (feat. Nicki Minaj)'},
                        {'album_ko': 'Believe (Remixed)'}):
            with self.subTest(changes=changes), self.assertRaises(validation.PlaybackBlocked):
                self.validate(case, case['sources'][0], metadata={**case['metadata'], **changes})

    def test_explicit_clean_and_summer_mix_are_not_interchangeable(self):
        case = deepcopy(FIXTURE[2])
        for title in ('Seven - Clean Ver. (feat. Latto)', 'Seven - Summer Mix (feat. Latto)'):
            metadata = {**case['metadata'], 'title': title, 'title_ko': title, 'title_en': title}
            with self.subTest(title=title), self.assertRaises(validation.PlaybackBlocked):
                self.validate(case, case['sources'][0], metadata=metadata)

    def test_missing_source_feature_does_not_use_player_omission_permission(self):
        case = FIXTURE[0]
        self.assertTrue(validation.recording_identity_matches(case['metadata'], case['player'], player=True))
        self.assertFalse(validation.recording_identity_matches(case['sources'][0], case['metadata']))

    def test_a_shared_featured_person_does_not_make_different_lead_artists_equivalent(self):
        left = {'title': 'Same Title (feat. Guest)', 'artist': 'First Lead, Guest'}
        right = {'title': 'Same Title (feat. Guest)', 'artist': 'Different Singer, Guest'}
        self.assertFalse(validation.recording_identity_matches(left, right))
        self.assertFalse(validation.recording_identity_matches(
            {'title': 'Song (feat. John of One Band)', 'artist': 'Lead'},
            {'title': 'Song (feat. John of Another Band)', 'artist': 'Lead'}))

    def test_official_source_proof_does_not_survive_changed_source_id_title_or_album(self):
        case = FIXTURE[3]
        source = next(s for s in case['sources'] if s['service'] == 'spotify')
        for changes in ({'song_id': 'different'}, {'title_en': 'Love Love Loveless'},
                        {'title_ko': 'Love Love Love (feat. Another Person)'},
                        {'album_en': 'Remixing The Human Soul'}):
            changed = {**source, **changes}
            with self.subTest(changes=changes):
                self.assertEqual(validation.proven_source_identity(changed, 'spotify', POLICY), changed)
                with self.assertRaises(validation.PlaybackBlocked):
                    self.validate(case, changed)

    def test_corrected_dirty_original_and_observed_blank_love_source_album_pass(self):
        dirty = deepcopy(FIXTURE[0])
        original = dirty['old_metadata']
        dirty.update(video_id=original['video_id'], metadata=original)
        dirty['player'] = {k: original[k] for k in ('video_id', 'title', 'artist', 'music_video_type')}
        self.assertEqual(self.validate(dirty, dirty['sources'][0])[0]['video_id'], original['video_id'])
        love = FIXTURE[3]
        source = next(s for s in love['sources'] if s['service'] == 'ytmusic')
        corrected = {**source, 'album_ko': '', 'album_en': ''}
        self.assertEqual(self.validate(love, corrected)[0]['video_id'], love['video_id'])

    def test_changed_remix_alias_cannot_be_approved_by_original_source_proof(self):
        case = deepcopy(FIXTURE[3])
        source = next(s for s in case['sources'] if s['service'] == 'spotify')
        remix = case['other_recordings'][0]
        case['video_id'] = remix['video_id']
        case['player'] = {k: remix[k] for k in ('video_id', 'title', 'artist', 'music_video_type')}
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(case, source, metadata=remix)

    def test_official_duration_proof_rejects_a_different_length_with_the_same_display_title(self):
        case = FIXTURE[3]
        source = next(s for s in case['sources'] if s['service'] == 'spotify')
        with self.assertRaises(validation.PlaybackBlocked):
            self.validate(case, source, metadata={**case['metadata'], 'length_seconds': 77})

    def localized_decision(self):
        case, source = FIXTURE[4], FIXTURE[4]['sources'][0]
        old = case['old_metadata']
        store.ensure_track(self.conn, track_uid='localized', video_id=old['video_id'],
                           yt_title=old['title'], yt_artist=old['artist'], yt_album=old['album'], status='matched', score=1)
        store.upsert_track_list_metadata(self.conn, service=source['service'], song_id=source['song_id'],
                                         track_uid='localized', row=source)
        self.conn.commit()
        result = self.validate(case, source, states={old['video_id']: 'unavailable'})[0]
        return case, source, result['canonical_decision']

    def test_localized_unavailable_replacement_saves_then_keeps_the_current_recording(self):
        case, source, decision = self.localized_decision()
        self.assertIn('expected_identity_policy_hash', decision)
        result = store.apply_canonical_decision(self.conn, track_uid='localized', decision=decision, source_row=source)
        self.conn.commit()
        self.assertEqual(result['video_id'], case['video_id'])
        next_result = self.validate(case, source)[0]
        self.assertEqual(next_result['video_id'], case['video_id'])
        self.assertNotIn('canonical_decision', next_result)
        self.assertEqual(dict(self.conn.execute('SELECT video_id,is_canonical FROM yt_video_ids')),
                         {case['old_metadata']['video_id']: 0, case['video_id']: 1})

    def test_typed_store_rejects_changed_identity_policy_and_explicit_wrong_recording(self):
        case, source, decision = self.localized_decision()
        before = '\n'.join(self.conn.iterdump())
        with patch.object(Path, 'read_bytes', return_value=b'{}'), self.assertRaisesRegex(store.CanonicalDecisionError, 'policy changed'):
            store.apply_canonical_decision(self.conn, track_uid='localized', decision=decision, source_row=source)
        self.assertEqual('\n'.join(self.conn.iterdump()), before)
        wrong = deepcopy(decision)
        for key in ('title', 'title_ko', 'title_en'):
            wrong['metadata'][key] += ' (Radio Mix)'
        with self.assertRaises(store.CanonicalDecisionError):
            store.apply_canonical_decision(self.conn, track_uid='localized', decision=wrong, source_row=source)
        self.assertEqual('\n'.join(self.conn.iterdump()), before)

    def test_unknown_unavailable_and_unhealthy_still_block_all_normal_sources(self):
        case, source = FIXTURE[1], FIXTURE[1]['sources'][0]
        for values in ({'state': 'unknown'}, {'state': 'unavailable'}, {'health': 'unknown'}):
            with self.subTest(values=values), self.assertRaises(validation.PlaybackBlocked):
                self.validate(case, source, **values)


if __name__ == '__main__':
    unittest.main()
