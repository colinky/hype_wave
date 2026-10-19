"""Unchanged official relations use bulk proof; changed identity/policy stays fail-closed."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import hype_db
from hype_db_store import _yt_metadata_verified_key
import crawler_common
import sync_all
import ytmusic_to_ytmusic_crawl as weekly


class WeeklyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'HYPE_DB_BACKEND':'sqlite'}))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('offline test')))
        self.db = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'test.db'
        hype_db.init_db(self.db)
        self.conn = self.enterContext(hype_db.connect(self.db))
        self.row = {'song_id':'source00001', 'original_video_id':'source00001',
                    'atv_external_video_id':'target00001', 'source':'youtube_charts_weekly_browse_api',
                    'rank':1, 'title':'Song', 'artist':'Artist', 'title_en':'Song', 'artist_en':'Artist'}
        self.conn.execute("INSERT INTO tracks(track_uid,canonical_yt_video_id,yt_title,yt_artist,yt_album,yt_metadata_verified_key,match_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ('uid','target00001','Song','Artist','',_yt_metadata_verified_key('target00001','Song','Artist',''),'matched','now','now'))
        for vid, canonical in [('source00001',0),('target00001',1)]:
            self.conn.execute('INSERT INTO platform_song_ids VALUES(?,?,?)',('ytmusic',vid,'uid'))
            self.conn.execute('INSERT INTO yt_video_ids VALUES(?,?,?)',(vid,'uid',canonical))
        self.conn.execute('INSERT INTO track_list(service,song_id,title_en,artist_en) VALUES(?,?,?,?)',
                          ('ytmusic','source00001','Song','Artist'))
        self.conn.execute('INSERT INTO source_song_relations VALUES(?,?,?,?,?,?,?,?,?)',
            ('ytmusic','source00001','youtube_charts_atv_external_video_id','ytmusic','target00001',
             'youtube_charts_weekly_browse_api','2026-W41','2026-W41','now'))
        self.conn.commit()

    def test_unchanged_proven_relation_uses_no_youtube_requests(self):
        client = Mock()
        result = weekly.preflight_chart_relations(self.conn, [self.row], client, '2026-W42', dry_run=True)
        self.assertEqual(result['source00001']['video_id'], 'target00001')
        self.assertEqual(client.mock_calls, [])

    def test_writer_lock_is_reentrant_but_excludes_independent_writers(self):
        import fcntl
        from sync_validation import sync_run_lock
        with sync_run_lock(self.db):
            with sync_run_lock(self.db):
                pass
            with Path(str(self.db)+'.lock').open('a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_changed_metadata_target_binding_or_manual_policy_cannot_use_fast_path(self):
        for changes in ({'title':'Other','title_en':'Other'},
                        {'atv_external_video_id':'another0001'},
                        {'artist':'Other','artist_en':'Other'}):
            self.assertEqual(weekly.cached_chart_relations(self.conn, [{**self.row, **changes}]), {})
        self.conn.execute("INSERT INTO manual_overrides(service,song_id,action,target_track_uid,updated_at) VALUES(?,?,?,?,?)",
                          ('apple','policy','split','uid','now'))
        self.assertEqual(weekly.cached_chart_relations(self.conn, [self.row]), {})

    def test_cache_resolution_uses_callers_metadata_cache(self):
        shared = {}
        def load(*args, **kwargs):
            kwargs['metadata_resolver']('target00001')
            return {}
        with patch.object(hype_db, 'get_bulk_cached_matches', side_effect=load), \
             patch.object(crawler_common, 'get_verified_video_metadata', return_value={'video_id':'target00001'}) as verify:
            crawler_common.load_verified_matching_cache(self.conn, service='ytmusic', tracks=[self.row],
                                                        ytmusic=Mock(), read_only=True, metadata_cache=shared)
        self.assertIs(verify.call_args.kwargs['metadata_cache'], shared)

    def test_partial_summary_uses_verified_actual_count(self):
        import json
        from unittest.mock import patch
        partial = {'excluded_items':[{'position':2}], 'effective_video_ids':['a']}
        payload = {'outcomes':[{'actual':[{'video_id':'a'}]}]}
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {'status':'published','requested_count':2,
                                                          'recovery_payload_json':json.dumps(payload)}
        with patch('hype_db_store._latest_partial_publication', return_value=partial):
            result = sync_all.publication_result(conn, {'target_id':'fixture'}, 'start')
        self.assertEqual(result, {'mode':'partial','requested':2,'published':1,'excluded':[{'position':2}]})


if __name__ == '__main__':
    unittest.main()
