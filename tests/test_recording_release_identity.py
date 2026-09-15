"""Exact reviewed releases do not authorize different recordings or source inputs."""
from copy import deepcopy
from itertools import product
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from sync_validation import source_recording_matches, recording_identity_matches

ROOT=Path(__file__).parents[1]
CASES=json.loads((ROOT/'tests/fixtures/observed_release_identity.json').read_text())['cases']
POLICY=json.loads((ROOT/'matching_alias.json').read_text())

class ReleaseIdentityTests(unittest.TestCase):
    def test_default_display_fields_may_use_either_exact_observed_locale(self):
        self.assertEqual(len(CASES), 8)
        for case in CASES:
            for locales in product(("ko", "en"), repeat=3):
                metadata = {**case["metadata"], **{
                    field: case["metadata"][field + "_" + locale]
                    for field, locale in zip(("title", "artist", "album"), locales)}}
                for source in case["sources"]:
                    with self.subTest(title=case["title"], source=source["song_id"], locales=locales):
                        self.assertTrue(source_recording_matches(source, metadata, policy=POLICY))

    def test_release_exception_rejects_unobserved_default_or_changed_locale(self):
        for case in CASES:
            # Exercise the reviewed exception, rather than the ordinary match
            # path for a source that already names the standard release.
            source = next(row for row in case["sources"]
                          if not recording_identity_matches(row, case["metadata"]))
            for field in ("title", "artist", "album"):
                for key in (field, field + "_ko", field + "_en"):
                    for value in ("Unobserved recording", "", None):
                        with self.subTest(title=case["title"], field=key, value=value):
                            self.assertFalse(source_recording_matches(
                                source, {**case["metadata"], key: value}, policy=POLICY))

    def test_observed_originals_across_reviewed_release_editions(self):
        for case in CASES:
            for source in case['sources']:
                with self.subTest(title=case['title'],source=source['song_id']):
                    self.assertTrue(source_recording_matches(source,case['metadata'],policy=POLICY))

    def blocked_pair(self):
        case=next(c for c in CASES if c['title']=='Ice Cream')
        source=next(s for s in case['sources'] if s['service']=='spotify')
        self.assertFalse(recording_identity_matches(source,case['metadata']))
        return source,case['metadata']

    def test_no_generic_album_version_stripping(self):
        source,meta=self.blocked_pair()
        for changes in ({'song_id':'another-source'}, {'album_en':'Unreviewed Edition'},
                        {'title_en':'Ice Cream (Earl Grey Ver.)'}, {'artist_en':'Another Singer'},
                        {'length_seconds':114}):
            with self.subTest(changes=changes):
                self.assertFalse(source_recording_matches({**source,**changes},meta,policy=POLICY))

    def test_candidate_substitution_missing_or_changed_metadata_is_not_approved(self):
        source,meta=self.blocked_pair()
        for changes in ({'video_id':'other-video'}, {'album_ko':'Another Edition'},
                        {'title_en':'Ice Cream (Earl Grey Ver.)'}, {'title_ko':'Ice Cream (feat. Other)'},
                        {'title_en':'Ice Cream (Clean)'}, {'artist_en':'Another Singer'},
                        {'length_seconds':114}, {'length_seconds':None}, {'title_en':''}):
            with self.subTest(changes=changes):
                self.assertFalse(source_recording_matches(source,{**meta,**changes},policy=POLICY))

    def test_missing_or_contradictory_catalog_evidence_does_not_authorize_release(self):
        source,meta=self.blocked_pair()
        for mutate in (lambda p:p.update(references=[]), lambda p:p.pop('catalog_isrc'),
                       lambda p:p['references'][0].update(isrc='DIFFERENT'),
                       lambda p:p.update(source_variants=[])):
            policy=deepcopy(POLICY)
            proof=next(p for p in policy['recording_release_evidence'] if p['title']=='Ice Cream')
            mutate(proof)
            self.assertFalse(source_recording_matches(source,meta,policy=policy))

    def test_proof_cannot_suppress_conflicting_feature_rating_or_version_in_other_locale(self):
        source,meta=self.blocked_pair()
        for value in ('Ice Cream (feat. Another Singer)','Ice Cream (Explicit)','Ice Cream (Live)'):
            modified={**source,'title_ko':value}
            policy=deepcopy(POLICY)
            proof=next(p for p in policy['recording_release_evidence'] if p['title']=='Ice Cream')
            proof['source_variants'].append([value,source['artist_ko'],source['album_ko']])
            self.assertFalse(source_recording_matches(modified,meta,policy=policy))

VIDEO_CASES = json.loads((ROOT/'tests/fixtures/observed_music_video_recording.json').read_text())['cases']


class MusicVideoRecordingProofTests(unittest.TestCase):
    def test_exact_pairs_use_reviewed_identity_without_rewriting_source(self):
        for case in VIDEO_CASES:
            source = deepcopy(case['source'])
            with self.subTest(source=source['song_id']):
                self.assertTrue(source_recording_matches(source, case['candidate'], policy=POLICY))
                self.assertTrue(recording_identity_matches(case['candidate'], case['player'], player=True))
                self.assertEqual(source, case['source'])
                if source['song_id'] != 'GxChUrrY4bc':
                    self.assertFalse(source_recording_matches(source, case['candidate'], policy={}))

    def test_changed_source_literals_or_unobserved_duration_do_not_use_proof(self):
        from sync_validation import proven_source_identity
        for case in VIDEO_CASES:
            source = case['source']
            for changes in ({'song_id': 'OtherSrc111'}, {'service': 'spotify'},
                            {'title_en': source['title_en'] + ' (Live)'},
                            {'title_ko': source['title_ko'] + ' (feat. Someone Else)'},
                            {'artist_en': 'Another Performer'}, {'album_en': 'Other Album'},
                            {'title_en': '', 'artist_en': 'Another Performer'},
                            {'length_seconds': 999}, {'length_seconds': True}, {'length_seconds': float('nan')}):
                row = {**source, **changes}
                with self.subTest(source=source['song_id'], changes=changes):
                    self.assertIs(proven_source_identity(row, row['service'], POLICY), row)
                    if changes.get('song_id') is None and changes.get('service') is None:
                        self.assertFalse(source_recording_matches(row, case['candidate'], policy=POLICY))

    def test_candidate_id_locale_credits_duration_and_artist_identity_are_exact(self):
        for case in VIDEO_CASES:
            meta = case['candidate']
            for changes in ({'video_id': 'OtherVideo1'}, {'title_en': meta['title_en'] + ' (Instrumental)'},
                            {'title_ko': meta['title_ko'] + ' (feat. Other)'}, {'artist_en': 'Another Artist'},
                            {'album_en': 'Other Album'}, {'title': 'Unobserved display'},
                            {'length_seconds': meta['length_seconds'] + 1}, {'length_seconds': None},
                            {'artist_ids': ['UC0000000000000000000000']}, {'artist_names_by_id': {}},
                            {'music_video_type': 'MUSIC_VIDEO_TYPE_OMV'}):
                if all(meta.get(k) == v for k, v in changes.items()):
                    continue
                with self.subTest(source=case['source']['song_id'], changes=changes):
                    self.assertFalse(source_recording_matches(case['source'], {**meta, **changes}, policy=POLICY))

    def test_missing_or_contradictory_pair_evidence_is_not_accepted(self):
        for case in VIDEO_CASES:
            key = 'ytmusic:' + case['source']['song_id']
            for field, value in (('references', []), ('limit', ''), ('source_lengths_seconds', []),
                                 ('max_duration_difference_seconds', -1), ('candidate', {}),
                                 ('source_video_id', 'OtherSrc111'), ('recording', {})):
                policy = deepcopy(POLICY)
                policy['source_identity_evidence'][key][field] = value
                with self.subTest(key=key, field=field):
                    self.assertFalse(source_recording_matches(case['source'], case['candidate'], policy=policy))


class VideoRecordingClient:
    """Saved exact watch/album/player fields; artist names from verified metadata."""
    def __init__(self, case, language='ko'):
        self.case, self.language = case, language
        self._auth_headers = {'Cookie': 'offline-fixture'}
        self._session, self.headers = object(), {}

    def get_song(self, video_id):
        row = self.case['player']
        return {'videoDetails': {'videoId': video_id, 'title': row['title'], 'author': row['artist'],
                                 'lengthSeconds': str(row['length_seconds']), 'musicVideoType': row['music_video_type']}}

    def get_watch_playlist(self, videoId, **kwargs):
        return {'tracks': [deepcopy(self.case['watch'][self.language])]}

    def get_artist(self, artist_id):
        assert artist_id in self.case['candidate']['artist_ids']
        return {'name': self.case['candidate']['artist_' + self.language]}

    def get_album(self, album_id):
        return deepcopy(self.case['album'][self.language])


class MusicVideoRecordingRoundtripTests(unittest.TestCase):
    def setUp(self):
        from hype_db_schema import init_schema
        import ytmusic_playlist_sync as matching
        self.enterContext(patch.dict(os.environ, {'SUPABASE_DB_URL': ''}))
        for name in ('socket.socket.connect', 'requests.sessions.Session.request', 'psycopg2.connect'):
            self.enterContext(patch(name, side_effect=AssertionError('No external access')))
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.case, self.clients = deepcopy(VIDEO_CASES[0]), {}
        self.enterContext(matching.bilingual_cache_read_only())
        self.enterContext(patch.object(matching, 'make_ytmusic', side_effect=self.client))
        self.enterContext(patch.object(matching, 'YTMusic', side_effect=lambda *args, language, **kwargs: self.client(language=language)))

    def client(self, _auth=None, *, language='ko', **kwargs):
        if language not in self.clients:
            self.clients[language] = VideoRecordingClient(self.case, language)
        return self.clients[language]

    def seed(self):
        import hype_db_store as store
        source, meta = self.case['source'], self.case['candidate']
        self.conn.execute('DELETE FROM platform_song_ids')
        self.conn.execute('DELETE FROM yt_video_ids')
        self.conn.execute('DELETE FROM tracks')
        store.ensure_track(self.conn, track_uid='reviewed-existing', video_id=meta['video_id'],
                           yt_title=meta['title'], yt_artist=meta['artist'], yt_album=meta['album'], status='matched', score=.9)
        store.upsert_track_list_metadata(self.conn, service='ytmusic', song_id=source['song_id'],
                                         track_uid='reviewed-existing', row=source)
        self.conn.commit()

    def verifier(self, state='playable'):
        from tests.playlist_playability_fixture import PlaylistVerifier
        vid = self.case['candidate']['video_id']
        return PlaylistVerifier(states={vid: state}, metadata={vid: self.case['player']})

    def test_existing_cache_roundtrip_reads_exact_metadata_and_keeps_candidate(self):
        import crawler_common
        import sync_validation as validation
        for case in VIDEO_CASES:
            self.case, self.clients = deepcopy(case), {}
            self.seed()
            before = list(self.conn.iterdump())
            verifier = self.verifier()
            self.client()._hype_playability_verifier = verifier
            with self.subTest(source=case['source']['song_id']):
                cache = crawler_common.load_verified_matching_cache(self.conn, service='ytmusic',
                    tracks=[self.case['source']], ytmusic=self.client(), read_only=True)
                result = validation.validate_matches(self.conn, service='ytmusic', sources=[self.case['source']],
                    matches=[{**cache[self.case['source']['song_id']], 'song_id':self.case['source']['song_id']}],
                    client=self.client(), verifier=verifier)[0]
                self.assertEqual(result['video_id'], case['candidate']['video_id'])
                self.assertNotIn('canonical_decision', result)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_yuri_all_null_performer_reader_is_partial_exact_and_never_fabricates_ids(self):
        import ytmusic_playlist_sync as matching
        self.case, self.clients = deepcopy(VIDEO_CASES[-1]), {}
        vid = self.case['candidate']['video_id']
        self.assertIsNone(matching.get_verified_video_metadata(self.client(), vid))
        result = matching.get_verified_video_metadata(self.client(), vid, allow_partial_artist_ids=True)
        self.assertEqual(result, self.case['candidate'])
        self.assertFalse(result['artist_identity_complete'])
        self.assertEqual(result['artist_ids'], [])
        for field, value in (('title', 'Other Episode'), ('artists', [{'name':'Other Guest', 'id':None}]),
                             ('artists', [{'name':'', 'id':None}]), ('album', {'name':'Other Album', 'id':'OtherAlbum'})):
            self.case, self.clients = deepcopy(VIDEO_CASES[-1]), {}
            self.case['watch']['en'][field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(matching.get_verified_video_metadata(self.client(), vid, allow_partial_artist_ids=True))

    def test_partial_pair_does_not_enable_unbound_selection_or_unknown_playback(self):
        import sync_validation as validation
        self.case, self.clients = deepcopy(VIDEO_CASES[-1]), {}
        source, meta = self.case['source'], self.case['candidate']
        def run(state):
            return validation.validate_matches(self.conn, service='ytmusic', sources=[source],
                matches=[{'song_id':source['song_id'], 'video_id':meta['video_id'], 'status':'cached_match'}],
                client=self.client(), verifier=self.verifier(state))
        with self.assertRaises(validation.PlaybackBlocked):
            run('playable')
        self.seed()
        for state in ('unknown', 'unavailable'):
            with self.subTest(state=state), self.assertRaises(validation.PlaybackBlocked):
                run(state)


if __name__=='__main__':unittest.main()
