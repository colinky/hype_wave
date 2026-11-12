"""Unchanged official relations use bulk proof; changed identity/policy stays fail-closed."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, call, patch

import hype_db
from hype_db_store import _yt_metadata_verified_key
import crawler_common
import sync_all
import sync_validation
import ytmusic_to_ytmusic_crawl as weekly


class PostgresWriterLockTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'HYPE_DB_BACKEND': 'aiven', 'AIVEN_DB_URI': 'postgresql://fixture/test',
            'SUPABASE_DB_URL': 'postgresql://fixture/test', 'HYPE_SYNC_PARENT_PID': '',
        }))
        self.raw = MagicMock()
        self.cursor = self.raw.cursor.return_value
        self.cursor.description = [('acquired',)]
        self.cursor.fetchone.return_value = (True,)
        self.open_connection = self.enterContext(patch('psycopg2.connect', return_value=self.raw))
        self.enterContext(patch('hype_db_schema.postgres_connect_kwargs', return_value={}))
        self.enterContext(patch('hype_db_schema.verify_postgres_schema'))

    def test_aiven_commits_before_work_and_closes_after_nested_lock(self):
        with sync_validation.sync_run_lock('unused.db'):
            self.cursor.execute.assert_called_with('SELECT pg_try_advisory_lock(1847391027)', ())
            self.assertEqual(self.raw.mock_calls[-1], call.commit())
            self.raw.close.assert_not_called()
            with sync_validation.sync_run_lock('unused.db'):
                self.open_connection.assert_called_once()
        self.raw.close.assert_called_once()
        self.raw.rollback.assert_not_called()
        self.assertFalse(sync_validation._writer_lock_held.get())

    def test_aiven_exception_closes_lock_connection_and_resets_ownership(self):
        with self.assertRaisesRegex(RuntimeError, 'worker failed'):
            with sync_validation.sync_run_lock('unused.db'):
                self.assertEqual(self.raw.mock_calls[-1], call.commit())
                raise RuntimeError('worker failed')
        self.raw.rollback.assert_called_once()
        self.raw.close.assert_called_once()
        self.assertFalse(sync_validation._writer_lock_held.get())

    def test_supabase_keeps_transaction_lock_until_work_finishes(self):
        with patch.dict(os.environ, {'HYPE_DB_BACKEND': 'supabase'}):
            with sync_validation.sync_run_lock('unused.db'):
                self.assertEqual(self.cursor.execute.call_args_list, [
                    call("SET LOCAL idle_in_transaction_session_timeout = '0'", ()),
                    call('SELECT pg_try_advisory_xact_lock(1847391027)', ()),
                ])
                self.assertEqual(self.raw.mock_calls[-1], call.cursor().fetchone())
                self.raw.close.assert_not_called()
        self.raw.close.assert_called_once()

    def test_busy_lock_blocks_work_and_closes_connection(self):
        for backend in ('aiven', 'supabase'):
            with self.subTest(backend=backend), patch.dict(os.environ, {'HYPE_DB_BACKEND': backend}):
                self.raw.reset_mock()
                self.cursor.fetchone.return_value = (False,)
                with self.assertRaises(sync_validation.PlaybackBlocked):
                    with sync_validation.sync_run_lock('unused.db'):
                        self.fail('A competing writer must not start')
                self.raw.close.assert_called_once()
                self.assertFalse(sync_validation._writer_lock_held.get())

    def test_child_uses_parent_lock_without_opening_a_connection(self):
        with patch.dict(os.environ, {'HYPE_SYNC_PARENT_PID': str(os.getppid())}):
            with sync_validation.sync_run_lock('unused.db'):
                self.open_connection.assert_not_called()
        self.assertFalse(sync_validation._writer_lock_held.get())


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
        with patch('hype_db_store.get_bulk_cached_matches', return_value={
            'source00001':{'video_id':'target00001','track_uid':'other-uid','status':'cached_match',
                           'yt_title':'Song','yt_artist':'Artist'}
        }):
            self.assertEqual(weekly.cached_chart_relations(self.conn, [self.row]), {})
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

    def test_duplicate_source_ids_preserve_100_raw_slots_and_99_effective_matches(self):
        from test_youtube_charts_browser import chart_payload
        from hype_db_common import dedupe_source_tracks
        payload = chart_payload()
        payload['chart']['trackViews'][32]['encryptedVideoId'] = payload['chart']['trackViews'][21]['encryptedVideoId']
        raw = weekly.extract_chart_entries_from_browser_json(payload)
        effective = dedupe_source_tracks(raw, 'ytmusic')
        started = '2026-10-12T07:05:00+00:00'
        hype_db.persist_crawled_tracks(self.db, service='ytmusic', job_name='Weekly-Hot-100',
            chart_date='2026-10-08', reference_period='2026-W41', tracks=raw, conn=self.conn)
        hype_db.persist_crawl_run(self.db, service='ytmusic', job_name='Weekly-Hot-100',
            chart_date='2026-10-08', reference_period='2026-W41', started_at=started,
            tracks=effective, matches=[{**row, 'status':'matched', 'yt_title':row['title'],
                'yt_artist':row['artist']} for row in effective], conn=self.conn, skip_playlist_order=True)
        saved = self.conn.execute("SELECT song_id,rank_order FROM playlist_order ORDER BY rank_order").fetchall()
        self.assertEqual([row['rank_order'] for row in saved], list(range(1, 101)))
        self.assertEqual(saved[21]['song_id'], saved[32]['song_id'])
        self.assertEqual(len(effective), 99)
        self.assertNotIn(33, [row['rank'] for row in effective])
        self.assertTrue(sync_all.data_snapshot_ready(self.conn, service='ytmusic',
            job_name='Weekly-Hot-100', started_at=started))
        from hype_db_reports import fetch_hype_rows_for_dates, build_hype_report_from_rows
        report = build_hype_report_from_rows(fetch_hype_rows_for_dates(self.conn, ['2026-10-12'])['2026-10-12'])
        duplicate = next(row for row in report if row['video_id'] == raw[21]['video_id'])
        self.assertEqual(duplicate['ytmusic_rank'], 22)
        from hype_scoring import calculate_rank_score
        self.assertEqual(duplicate['hype_index'], round(calculate_rank_score(22) * 0.2, 2))

    def test_history_check_rebuilds_latest_completed_date_using_read_only_connections(self):
        import json
        from datetime import datetime, timezone
        from contextlib import contextmanager
        import check_history_rebuild as check
        import hype_db_reports as reports
        rows = [{'song_id':str(i), 'rank':i, 'title':f'Song {i}', 'artist':'Artist'}
                for i in range(1, 101)]
        hype_db.persist_crawled_tracks(self.db, service='apple', job_name='KR-Top-100',
            chart_date='2026-11-10', reference_period='2026-11-10', tracks=rows, conn=self.conn)
        matches = [{**row, 'video_id':f"video{row['rank']:06d}", 'status':'matched',
                    'yt_title':row['title'], 'yt_artist':'Artist'} for row in rows]
        hype_db.persist_crawl_run(self.db, service='apple', job_name='KR-Top-100',
            chart_date='2026-11-10', reference_period='2026-11-10', started_at='2026-11-11T07:05:00+00:00',
            tracks=rows, matches=matches, conn=self.conn, skip_playlist_order=True)
        output = self.db.with_name('history.json')
        connections = []
        @contextmanager
        def read_only_connect(path, *, read_only=False):
            self.assertTrue(read_only)
            with hype_db.connect(path, read_only=read_only) as conn:
                connections.append(conn.execute('PRAGMA query_only').fetchone()[0])
                yield conn
        with patch.object(check, 'connect', side_effect=read_only_connect), \
             patch.object(reports, 'connect', side_effect=read_only_connect), \
             patch.object(check, 'datetime', wraps=datetime) as clock, patch('builtins.print'):
            clock.now.return_value = datetime(2026, 11, 12, tzinfo=timezone.utc)
            result = check.main(output, self.db)
        self.assertEqual(connections, [1, 1])
        self.assertTrue(result['database_connected'])
        self.assertFalse(result['ready_for_today'])
        self.assertEqual(result['latest_history_date'], '2026-11-11')
        self.assertEqual(result['latest_chart_tracks'], 100)
        self.assertEqual(json.loads(output.read_text())['dates'], ['2026-11-11'])
        self.assertEqual(json.loads(output.with_suffix('.summary.json').read_text()), result)

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
