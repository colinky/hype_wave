"""Offline tests for backend isolation and restricted Aiven connections."""
import os
import stat
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hype_db_common import database_backend, postgres_url, postgres_connect_kwargs
from hype_db_schema import connect, ensure_postgres_indexes
from ytmusic_playlist_sync import PostgresBilingualCache

AIVEN = 'postgresql://hype_sync:secret@db.example:1234/defaultdb?sslmode=require'
SETTINGS = {'HYPE_DB_BACKEND': 'aiven', 'AIVEN_DB_URI': AIVEN,
            'AIVEN_DB_HOST': 'db.example', 'AIVEN_DB_CA_CERTIFICATE': '-----BEGIN CERTIFICATE-----\\nfixture\\n-----END CERTIFICATE-----',
            'SUPABASE_DB_URL': 'postgresql://legacy:secret@old.example/postgres'}


class AivenBackendTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, SETTINGS, clear=True))

    def test_aiven_presence_does_not_implicitly_switch_legacy_backend(self):
        os.environ.pop('HYPE_DB_BACKEND')
        self.assertEqual(database_backend(), 'supabase')
        os.environ.pop('SUPABASE_DB_URL')
        self.assertEqual(database_backend(), 'sqlite')

    def test_explicit_sqlite_never_connects_to_remote_database(self):
        os.environ['HYPE_DB_BACKEND'] = 'sqlite'
        with tempfile.TemporaryDirectory() as directory, patch('psycopg2.connect') as remote:
            with connect(Path(directory) / 'local.db') as conn:
                self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)
            remote.assert_not_called()
        self.assertIsNone(postgres_url())

    def test_missing_selected_uri_cannot_fall_back(self):
        for backend, key in [('aiven', 'AIVEN_DB_URI'), ('supabase', 'SUPABASE_DB_URL')]:
            with self.subTest(backend=backend), patch.dict(os.environ, {'HYPE_DB_BACKEND': backend, key: ''}):
                with self.assertRaisesRegex(ValueError, key):
                    postgres_url()

    def test_unknown_backend_is_an_error(self):
        os.environ['HYPE_DB_BACKEND'] = 'typo'
        with self.assertRaises(ValueError):
            postgres_url()

    def test_mismatched_host_stops_before_network(self):
        os.environ['AIVEN_DB_HOST'] = 'different.example'
        with patch('psycopg2.connect') as remote, self.assertRaisesRegex(ValueError, 'must match'):
            with connect('unused.db'):
                pass
        remote.assert_not_called()

    def test_require_mode_is_required_and_not_overridden(self):
        os.environ['AIVEN_DB_URI'] = AIVEN.replace('require', 'disable')
        with self.assertRaisesRegex(ValueError, 'sslmode=require'):
            postgres_connect_kwargs()

    def test_ca_missing_or_malformed_is_an_error(self):
        for certificate in ['', 'not a certificate']:
            with self.subTest(certificate=certificate), patch.dict(os.environ, {'AIVEN_DB_CA_CERTIFICATE': certificate}):
                with self.assertRaisesRegex(ValueError, 'AIVEN_DB_CA_CERTIFICATE'):
                    postgres_connect_kwargs()

    def test_escaped_and_multiline_pem_use_one_private_file(self):
        with patch('hype_db_common.ssl.SSLContext') as context:
            escaped = postgres_connect_kwargs()
            os.environ['AIVEN_DB_CA_CERTIFICATE'] = SETTINGS['AIVEN_DB_CA_CERTIFICATE'].replace('\\n', '\n')
            multiline = postgres_connect_kwargs()
        self.assertEqual(escaped, multiline)
        self.assertNotIn('sslmode', escaped)  # URI's require remains untouched.
        self.assertEqual(escaped['host'], 'db.example')
        file = Path(escaped['sslrootcert'])
        self.assertEqual(file.read_text(), os.environ['AIVEN_DB_CA_CERTIFICATE'] + '\n')
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        context.return_value.load_verify_locations.assert_called_once()

    def test_explicit_other_database_url_is_not_retargeted(self):
        kwargs = postgres_connect_kwargs('postgresql://utility:secret@chosen.example/db')
        self.assertNotIn('host', kwargs)
        self.assertNotIn('sslrootcert', kwargs)

    def test_runtime_does_not_attempt_schema_creation(self):
        from ytmusic_to_ytmusic_crawl import ensure_chart_source_audit_table
        conn = MagicMock()
        ensure_postgres_indexes(conn)
        PostgresBilingualCache(AIVEN, Path('unused.db'))._init_db(conn)
        ensure_chart_source_audit_table(conn)
        conn.cursor.assert_not_called()
        conn.execute.assert_not_called()

    def test_cache_load_failure_is_not_a_success_or_sqlite_fallback(self):
        cache = PostgresBilingualCache(AIVEN, Path('unused.db'))
        with patch.object(cache, '_connect', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'could not be loaded'):
                cache.get_artist('x')
        self.assertFalse(cache.loaded)
        self.assertFalse(cache.fallback_active)
        self.assertIsNone(cache.fallback)
        # Even if a matching caller catches the lookup error, the CLI flush fails.
        with self.assertRaisesRegex(RuntimeError, 'could not be loaded'):
            cache.flush()

    def test_cache_save_failure_preserves_pending_data_and_raises(self):
        cache = PostgresBilingualCache(AIVEN, Path('unused.db'))
        cache.dirty_artists['x'] = (['name'], '2026-09-19T00:00:00Z')
        with patch.object(cache, '_connect', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'could not be saved'):
                cache.flush()
        self.assertIn('x', cache.dirty_artists)

    def test_cli_save_failure_cannot_exit_successfully(self):
        from sync_validation import run_locked_cli
        with (patch.object(sys, 'argv', ['crawler']), patch('ytmusic_playlist_sync.load_dotenv'),
              patch('sync_validation.sync_run_lock', return_value=nullcontext()),
              patch('ytmusic_playlist_sync.BILINGUAL_CACHE.flush', side_effect=RuntimeError('save failed'))):
            with self.assertRaisesRegex(RuntimeError, 'save failed'):
                run_locked_cli(lambda: 0)


    def test_contending_writer_is_rejected(self):
        from sync_validation import sync_run_lock, PlaybackBlocked
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (False,)
        with patch('hype_db_schema.connect', return_value=nullcontext(conn)), self.assertRaises(PlaybackBlocked):
            with sync_run_lock('unused.db'):
                pass

    def test_child_shares_the_live_parent_lock(self):
        from sync_validation import sync_run_lock
        os.environ['HYPE_SYNC_PARENT_PID'] = str(os.getppid())
        with patch('hype_db_schema.connect') as remote:
            with sync_run_lock('unused.db'):
                pass
            remote.assert_not_called()

    def test_preflight_rejects_an_administrator_uri(self):
        from check_aiven_connection import main
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            'name': 'avnadmin', 'rolsuper': False, 'rolcreatedb': True,
            'rolcreaterole': True, 'rolbypassrls': True}
        with patch('check_aiven_connection.connect', return_value=nullcontext(conn)), self.assertRaisesRegex(RuntimeError, 'restricted hype_sync'):
            main()

    def test_preflight_requires_tls_and_rls_in_read_only_connection(self):
        from check_aiven_connection import main
        role = {'name': 'hype_sync', 'rolsuper': False, 'rolcreatedb': False,
                'rolcreaterole': False, 'rolbypassrls': False}
        for tls, secured, error in [(False, True, 'TLS'), (True, False, 'RLS')]:
            with self.subTest(tls=tls, secured=secured):
                conn = MagicMock()
                conn.execute.return_value.fetchone.side_effect = [
                    role, {'ssl': tls, 'version': 'TLSv1.3'}, {'total': 20, 'secured': secured}]
                with patch('check_aiven_connection.connect', return_value=nullcontext(conn)) as connect_mock:
                    with self.assertRaisesRegex(RuntimeError, error):
                        main()
                self.assertTrue(connect_mock.call_args.kwargs['read_only'])


if __name__ == '__main__':
    unittest.main()
