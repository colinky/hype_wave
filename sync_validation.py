"""Process-level database writer lock and explicit cache flushing."""
from __future__ import annotations
import os
from pathlib import Path
from contextlib import contextmanager
from hype_db_common import postgres_url


class PlaybackBlocked(RuntimeError):
    """An uncertain/unavailable selection must not become a completed snapshot."""


@contextmanager
def sync_run_lock(db_path):
    """One cooperative writer across scheduled runs, local runs and incident repair."""
    if os.environ.get("HYPE_SYNC_PARENT_PID") == str(os.getppid()):
        yield
        return
    if postgres_url():
        from hype_db_schema import connect
        with connect(db_path, read_only=True) as conn:
            # The hosted URL is a transaction pooler: a session lock could be
            # detached from this client at commit. Pin only this dedicated lock
            # transaction until the command exits; normal DB work stays short.
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '0'")
            acquired = conn.execute("SELECT pg_try_advisory_xact_lock(1847391027)").fetchone()[0]
            if not acquired:
                raise PlaybackBlocked("Another synchronization or incident repair owns the write lock")
            yield
    else:
        import fcntl
        lock_path = Path(str(db_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PlaybackBlocked("Another synchronization or repair owns the write lock") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def run_locked_cli(main):
    """Lock standalone writers while an actual sync_all child shares its parent's lock."""
    import argparse
    from ytmusic_playlist_sync import BILINGUAL_CACHE, load_dotenv

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db-path", default=str(Path(__file__).with_name("hype_wave_data.db")))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    args, _ = parser.parse_known_args()
    load_dotenv(args.env_file)
    if args.env_file == ".env":
        load_dotenv(str(Path(__file__).with_name(".secrets") / ".env"))
    if args.dry_run:
        return main()
    with sync_run_lock(args.db_path):
        result = main()
        # A failed save must affect the exit status, while the writer lock is held.
        BILINGUAL_CACHE.flush()
        return result

