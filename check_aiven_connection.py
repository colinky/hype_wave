"""Read-only Actions preflight using the same settings as the application."""
from pathlib import Path

from hype_db_common import database_backend
from hype_db_schema import connect


def main() -> None:
    if database_backend() != 'aiven':
        raise RuntimeError('The deployment must explicitly select HYPE_DB_BACKEND=aiven')
    with connect(Path('unused.db'), read_only=True) as conn:
        role = conn.execute(
            'SELECT current_user AS name, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls '
            'FROM pg_roles WHERE rolname=current_user'
        ).fetchone()
        if role['name'] != 'hype_sync' or any(role[key] for key in ('rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolbypassrls')):
            raise RuntimeError('Use the restricted hype_sync URI, not the migration administrator')
        tls = conn.execute('SELECT ssl, version FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
        if not tls or not tls['ssl']:
            raise RuntimeError('Aiven connection is not using TLS')
        tables = conn.execute(
            "SELECT count(*) AS total, bool_and(c.relrowsecurity) AS secured "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p')"
        ).fetchone()
        if not tables['total'] or not tables['secured']:
            raise RuntimeError('Every application table must have RLS enabled')
        conn.execute('SELECT 1 FROM public.tracks LIMIT 1').fetchone()
        conn.execute('SELECT 1 FROM public.ytmusic_artist_translations LIMIT 1').fetchone()
        conn.execute('SELECT 1 FROM public.ytmusic_song_translations LIMIT 1').fetchone()
    print(f"Aiven preflight passed: role=hype_sync, TLS={tls['version']}, RLS tables={tables['total']}")


if __name__ == '__main__':
    main()
