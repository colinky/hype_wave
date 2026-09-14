"""An unregistered native source may be split only to that exact recording."""
import copy
from datetime import datetime, timedelta, timezone
import os
import sqlite3
import unittest
from unittest.mock import patch

import hype_db_store as store
from hype_db_schema import init_schema
from repair_ytmusic_chart_incident import apply_repair, plan_repair, verify_repair

SOURCE, ORIGINAL, OTHER = 'native00001', 'studio00001', 'foreign0001'


class NativeSourceSplitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SUPABASE_DB_URL': ''}))
        for method in ('socket.socket.connect', 'requests.sessions.Session.request', 'psycopg2.connect'):
            self.enterContext(patch(method, side_effect=AssertionError('No external access')))
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn, repair_source_bindings=False)
        store.ensure_track(self.conn, track_uid='original', video_id=ORIGINAL,
                           yt_title='Song', yt_artist='Artist', yt_album='Studio', status='matched', score=1)
        for service, song_id, title in [('ytmusic', SOURCE, 'Song (Live)'), ('apple', 'kept-source', 'Song')]:
            store.upsert_track_list_metadata(self.conn, service=service, song_id=song_id, track_uid='original',
                                            row={'title': title, 'artist': 'Artist', 'album': 'Album'})
        self.conn.commit()

    def dump(self):
        return list(self.conn.iterdump())

    def case(self):
        now = datetime.now(timezone.utc)
        metadata = {'video_id': SOURCE, 'title': 'Song (Live)', 'artist': 'Artist', 'album': 'Album',
                    'title_ko': 'Song (Live)', 'artist_ko': 'Artist', 'album_ko': 'Album',
                    'title_en': 'Song (Live)', 'artist_en': 'Artist', 'album_en': 'Album',
                    'verified': True, 'length_seconds': 277, 'artist_identity_complete': True,
                    'artist_ids': ['UC-explicit-artist'], 'artist_names_by_id': {'UC-explicit-artist':['Artist']}, 'music_video_type': 'MUSIC_VIDEO_TYPE_OMV'}
        return {'case_id': 'native-live', 'action': 'split_binding', 'service': 'ytmusic', 'song_id': SOURCE,
                'evidence_ref': 'fixture:exact-native-source',
                'decision': {'expected_track_uid': 'live', 'expected_video_id': None, 'selected_video_id': SOURCE,
                             'reason': 'new_recording_split', 'metadata': metadata,
                             'candidate_evidence': {'video_id': SOURCE, 'state': 'playable', 'exact_id': True,
                                'has_audio': True, 'auth_state': 'authenticated', 'run_health': 'healthy',
                                'environment': 'offline-native-split', 'observed_at': now.isoformat(),
                                'expires_at': (now + timedelta(minutes=10)).isoformat(),
                                'title': 'Song (Live)', 'artist': 'Artist'}},
                'bindings': [{'service': 'ytmusic', 'song_id': SOURCE, 'expected_track_uid': 'original',
                              'source_metadata_verified': True, 'evidence_ref': 'fixture:exact-native-source'}],
                'aliases': [{'video_id': SOURCE, 'expected_track_uid': None, 'expected_absent': True,
                             'evidence_ref': 'fixture:exact-native-source'}]}

    def manifest(self, case=None):
        return plan_repair(self.conn, {'repair_id': 'native-split', 'implementation_revision': 'offline-fixture',
                                      'cases': [case or self.case()]})

    def assert_rejected_unchanged(self, case):
        before = self.dump()
        with self.assertRaises(store.CanonicalDecisionError):
            apply_repair(self.conn, self.manifest(case))
        self.assertEqual(self.dump(), before)

    def test_native_source_creates_one_alias_and_preserves_the_shared_original(self):
        old = dict(self.conn.execute("SELECT * FROM tracks WHERE track_uid='original'").fetchone())
        source_rows = [tuple(r) for r in self.conn.execute('SELECT * FROM track_list ORDER BY service,song_id')]
        before = self.dump()
        manifest = self.manifest()
        self.assertEqual(self.dump(), before)
        apply_repair(self.conn, manifest)
        self.assertEqual(dict(self.conn.execute("SELECT * FROM tracks WHERE track_uid='original'").fetchone()), old)
        self.assertEqual(store.find_track_by_service_song(self.conn, 'apple', 'kept-source'), 'original')
        self.assertEqual(store.find_track_by_service_song(self.conn, 'ytmusic', SOURCE), 'live')
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM track_list ORDER BY service,song_id')], source_rows)
        self.assertEqual(tuple(self.conn.execute('SELECT track_uid,is_canonical FROM yt_video_ids WHERE video_id=?',
                                               (SOURCE,)).fetchone()), ('live', 1))
        self.assertEqual(verify_repair(self.conn, manifest)['status'], 'db_verified')
        self.conn.rollback()
        self.assertEqual(self.dump(), before)
        apply_repair(self.conn, manifest)
        self.conn.commit()
        after = self.dump()
        self.assertEqual(apply_repair(self.conn, manifest)['status'], 'already_applied')
        self.assertEqual(self.dump(), after)

    def test_absence_is_explicit_and_only_the_one_native_source_is_allowed(self):
        variants = []
        case = self.case(); case['aliases'][0].pop('expected_absent'); variants.append(case)
        case = self.case(); case['aliases'][0].pop('expected_track_uid'); variants.append(case)
        case = self.case(); case['aliases'][0]['expected_track_uid'] = 'original'; variants.append(case)
        case = self.case(); case['aliases'][0].pop('evidence_ref'); variants.append(case)
        case = self.case(); case['bindings'][0].pop('source_metadata_verified'); variants.append(case)
        case = self.case(); case['bindings'][0].pop('evidence_ref'); variants.append(case)
        case = self.case(); case['service'] = 'apple'; variants.append(case)
        case = self.case(); case['song_id'] = OTHER; variants.append(case)
        case = self.case(); case['bindings'].append({'service':'apple','song_id':'kept-source','expected_track_uid':'original'}); variants.append(case)
        for case in variants:
            with self.subTest(case=variants.index(case)):
                self.assert_rejected_unchanged(case)

    def test_exact_native_identity_preserves_the_caption_for_new_or_existing_alias(self):
        for existing_alias in (False, True):
            with self.subTest(existing_alias=existing_alias):
                case = self.case()
                title = "Artist - TOUR REPORT 'Song' IN BANGKOK"
                for key in ('title', 'title_ko', 'title_en'):
                    case['decision']['metadata'][key] = title
                case['decision']['candidate_evidence']['title'] = title
                case['source_row'] = {'title':'Song (Live)','artist':'Artist','album':'Album'}
                if existing_alias:
                    self.conn.execute('INSERT INTO yt_video_ids VALUES (?,?,0)', (SOURCE,'original'))
                    case['aliases'][0] = {'video_id':SOURCE,'expected_track_uid':'original'}
                source_before = [tuple(r) for r in self.conn.execute('SELECT * FROM track_list')]
                apply_repair(self.conn, self.manifest(case))
                self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM track_list')], source_before)
                self.assertEqual(store.find_track_by_service_song(self.conn, 'ytmusic', SOURCE), 'live')
                self.conn.rollback()

    def test_native_album_correction_cannot_change_caption_or_invent_an_album(self):
        for values in ({'title_en':'New caption'}, {'album_en':'An unobserved album'}):
            with self.subTest(values=values):
                case = self.case()
                case['source_metadata_repairs'] = [{'service':'ytmusic','song_id':SOURCE,'values':values,
                    'source_metadata_verified':True,'evidence_ref':'fixture:exact-source'}]
                self.assert_rejected_unchanged(case)

    def test_partial_unknown_expired_wrong_person_or_version_cannot_create_alias(self):
        for target, key, value in [('metadata','artist_identity_complete',False), ('metadata','artist_ids',[]),
                                  ('metadata','title_en',''), ('metadata','length_seconds',None),
                                  ('metadata','length_seconds',float('nan')), ('metadata','length_seconds',-1),
                                  ('metadata','artist','Wrong Default Artist'),
                                  ('metadata','title','Entirely Different Upload (Live)'),
                                  ('metadata','album','Wrong Default Album'),
                                  ('metadata','title_en','Song (Acoustic)'), ('metadata','artist_en','Someone Else'),
                                  ('candidate_evidence','state','unknown'), ('candidate_evidence','auth_state','unknown'),
                                  ('candidate_evidence','expires_at','2000-01-01T00:00:00+00:00'),
                                  ('candidate_evidence','title','Song (Acoustic)'),
                                  ('candidate_evidence','video_id',OTHER)]:
            with self.subTest(target=target, key=key, value=value):
                case = self.case(); case['decision'][target][key] = value
                self.assert_rejected_unchanged(case)

    def test_existing_alias_or_canonical_owner_is_never_stolen(self):
        for canonical in (False, True):
            with self.subTest(canonical=canonical):
                if canonical:
                    store.ensure_track(self.conn, track_uid='foreign', video_id=SOURCE,
                                       yt_title='Foreign', yt_artist='Someone', yt_album='Elsewhere')
                else:
                    self.conn.execute('INSERT INTO yt_video_ids VALUES (?,?,0)', (SOURCE,'original'))
                self.assert_rejected_unchanged(self.case())
                self.conn.rollback()

    def test_native_split_cannot_carry_another_existing_alias_with_it(self):
        self.conn.executemany('INSERT INTO yt_video_ids VALUES (?,?,0)', [(SOURCE,'original'), (OTHER,'original')])
        self.conn.commit()
        case = self.case()
        case['aliases'] = [{'video_id':video,'expected_track_uid':'original'} for video in (SOURCE, OTHER)]
        self.assert_rejected_unchanged(case)

    def test_native_split_rejects_a_contradictory_canonical_alias_flag(self):
        self.conn.execute('INSERT INTO yt_video_ids VALUES (?,?,1)', (SOURCE,'original'))
        self.conn.commit()
        case = self.case()
        case['aliases'] = [{'video_id':SOURCE,'expected_track_uid':'original'}]
        self.assert_rejected_unchanged(case)

    def test_owner_appearing_after_plan_and_changed_source_binding_fail_closed(self):
        for change in ('alias','binding'):
            with self.subTest(change=change):
                manifest = self.manifest()
                if change == 'alias':
                    self.conn.execute('INSERT INTO yt_video_ids VALUES (?,?,0)', (SOURCE,'original'))
                else:
                    store.ensure_track(self.conn, track_uid='foreign')
                    self.conn.execute('UPDATE platform_song_ids SET track_uid=? WHERE service=? AND song_id=?',
                                      ('foreign','ytmusic',SOURCE))
                before = self.dump()
                with self.assertRaises(store.CanonicalDecisionError): apply_repair(self.conn, manifest)
                self.assertEqual(self.dump(), before)
                self.conn.rollback()

    def test_source_and_destination_manual_policies_block_without_changes(self):
        for service,song_id,target in [('ytmusic',SOURCE,None),('apple','kept-source',None),('ytmusic','manual-extra','live')]:
            with self.subTest(service=service,song_id=song_id):
                self.conn.execute('INSERT INTO manual_overrides(service,song_id,action,target_track_uid,updated_at) VALUES (?,?,?,?,?)',
                                  (service,song_id,'block',target,'2026-09-15'))
                self.assert_rejected_unchanged(self.case())
                self.conn.rollback()

    def test_source_album_repair_and_split_roll_back_together_when_receipt_fails(self):
        self.conn.execute("UPDATE track_list SET album_en='Wrong Remix' WHERE service='ytmusic' AND song_id=?", (SOURCE,))
        self.conn.execute("CREATE TRIGGER deny_receipt BEFORE INSERT ON migration_reports BEGIN SELECT RAISE(ABORT,'receipt failure'); END")
        self.conn.commit()
        case = self.case()
        case['source_metadata_repairs'] = [{'service':'ytmusic','song_id':SOURCE,'values':{'album_en':'Album'},
                                           'source_metadata_verified':True,'evidence_ref':'fixture:upstream-album'}]
        before = self.dump()
        with self.assertRaises(sqlite3.IntegrityError): apply_repair(self.conn,self.manifest(case))
        self.assertEqual(self.dump(),before)

    def uploader_case(self):
        case = self.case()
        channel, title, author = 'UCaaaaaaaaaaaaaaaaaaaaaa', 'An exact native upload', 'Uploader'
        metadata = case['decision']['metadata']
        metadata.update(identity_basis='exact_native_uploader', artist_identity_complete=False,
                        artist_ids=[], artist_names_by_id={}, creator_channel_id=channel,
                        length_seconds=151, music_video_type='MUSIC_VIDEO_TYPE_UGC')
        for suffix in ('','_ko','_en'):
            metadata.update({f'title{suffix}':title, f'artist{suffix}':author, f'album{suffix}':''})
        case['decision']['candidate_evidence'].update(title=title, artist=author, music_video_type='MUSIC_VIDEO_TYPE_UGC')
        row = {'videoId':SOURCE,'title':title,'artists':[{'name':author,'id':channel}],
               'album':None,'isAvailable':None,'videoType':'MUSIC_VIDEO_TYPE_UGC'}
        case['native_watch_evidence'] = {
            'evidence_ref':'fixture:actual-native-watch', 'observed_at':datetime.now(timezone.utc).isoformat(),
            'player_details':{'videoId':SOURCE,'title':title,'author':author,'channelId':channel,
                              'lengthSeconds':'151','musicVideoType':'MUSIC_VIDEO_TYPE_UGC'},
            'watch':{'ko':[copy.deepcopy(row)],'en':[copy.deepcopy(row)]}}
        return case

    def test_native_uploader_proof_keeps_the_exact_entity_without_claiming_a_musical_artist(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                case = self.uploader_case()
                if existing:
                    self.conn.execute('INSERT INTO yt_video_ids VALUES (?,?,0)', (SOURCE,'original'))
                    case['aliases'] = [{'video_id':SOURCE,'expected_track_uid':'original'}]
                sources = [tuple(r) for r in self.conn.execute('SELECT * FROM track_list')]
                manifest = self.manifest(case)
                apply_repair(self.conn, manifest)
                self.assertFalse(case['decision']['metadata']['artist_identity_complete'])
                self.assertEqual(store.find_track_by_service_song(self.conn,'ytmusic',SOURCE),'live')
                self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM track_list')],sources)
                self.assertEqual(verify_repair(self.conn,manifest)['status'],'db_verified')
                self.conn.rollback()

    def test_uploader_proof_requires_literal_complete_fresh_exact_channel_observations(self):
        variants = []
        for field,value in [('evidence_ref',''),('observed_at','2000-01-01T00:00:00+00:00'),
                            ('observed_at','2100-01-01T00:00:00+00:00')]:
            case = self.uploader_case(); case['native_watch_evidence'][field] = value; variants.append(case)
        for field,value in [('videoId',OTHER),('channelId','different-channel'),('lengthSeconds','0'),('musicVideoType','MUSIC_VIDEO_TYPE_OMV')]:
            case = self.uploader_case(); case['native_watch_evidence']['player_details'][field] = value; variants.append(case)
        for field,value in [('videoId',OTHER),('title','Different upload'),('artists',[{'name':'Uploader','id':None}]),
                            ('artists',[{'name':'Other','id':'UCaaaaaaaaaaaaaaaaaaaaaa'}]),('isAvailable',False)]:
            case = self.uploader_case(); case['native_watch_evidence']['watch']['en'][0][field] = value; variants.append(case)
        case = self.uploader_case(); case['native_watch_evidence']['watch']['ko'] *= 2; variants.append(case)
        for field,value in [('artist_identity_complete',True),('creator_channel_id','wrong'),('length_seconds',152),
                            ('artist_ids',['UCaaaaaaaaaaaaaaaaaaaaaa']),('title_en','Different upload')]:
            case = self.uploader_case(); case['decision']['metadata'][field] = value; variants.append(case)
        case = self.uploader_case(); del case['decision']['metadata']['artist_en']; variants.append(case)
        case = self.uploader_case(); case['service'] = 'apple'; variants.append(case)
        case = self.uploader_case(); case['song_id'] = OTHER; variants.append(case)
        for index,case in enumerate(variants):
            with self.subTest(index=index): self.assert_rejected_unchanged(case)

    def test_numeric_watch_display_tokens_are_not_creators_but_unknown_names_remain_blocked(self):
        case = self.uploader_case()
        case['native_watch_evidence']['watch']['ko'][0]['artists'] += [
            {'name':'조회수 1768만회','id':None}, {'name':'좋아요 3.5만개','id':None}]
        original = copy.deepcopy(case)
        apply_repair(self.conn, self.manifest(case))
        self.assertEqual(case, original)
        self.conn.rollback()
        for extra in ({'name':'Guest Creator','id':None},
                      {'name':'1768 views','id':'UCbbbbbbbbbbbbbbbbbbbbbb'},
                      {'name':'2026 Band','id':None}):
            with self.subTest(extra=extra):
                changed = copy.deepcopy(case)
                changed['native_watch_evidence']['watch']['ko'][0]['artists'].append(extra)
                self.assert_rejected_unchanged(changed)


if __name__ == '__main__':
    unittest.main()
