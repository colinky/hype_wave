"""Opt-in PostgreSQL repair rehearsal in a newly generated, disposable schema.

Public tables are read only and hashed before/after. External stage readbacks
are explicitly simulated; this script never publishes playlists or history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from hype_db_schema import PostgresConnectionWrapper
from hype_db_store import ensure_track, upsert_track_list_metadata
from repair_ytmusic_chart_incident import apply_repair, mark_repair_stage, plan_repair, verify_repair


def checkpoint(report, phase):
    report["phase"] = phase
    print(json.dumps({"phase": phase}), flush=True)


def digest_rows(rows):
    values = sorted(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str) for row in rows)
    return {"rows": len(values), "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest()}


def connect_schema(url, schema, *, read_only=False):
    if schema != "public" and not re.fullmatch(r"rehearsal_chart_[0-9a-f]{32}", schema):
        raise ValueError("Invalid isolated schema")
    conn = psycopg2.connect(url, connect_timeout=20)
    conn.set_session(readonly=read_only, isolation_level="SERIALIZABLE")
    select_schema(conn, schema)
    return conn


def select_schema(conn, schema):
    """Reassert isolation in every transaction, including transaction-pooler reconnects."""
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema)))
        cursor.execute("SET LOCAL lock_timeout='10s'")
        cursor.execute("SET LOCAL statement_timeout='120s'")
        cursor.execute("SELECT current_schemas(false)")
        if cursor.fetchone()[0] != [schema, "pg_catalog"]:
            conn.close()
            raise RuntimeError("Search path escaped the explicitly selected schema")


def hashes(conn, schema, tables):
    result = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        for table in sorted(tables):
            cursor.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
            result[table] = digest_rows(cursor.fetchall())
    return result


def public_hashes(url, tables):
    conn = connect_schema(url, "public", read_only=True)
    try:
        return hashes(conn, "public", tables)
    finally:
        conn.rollback()
        conn.close()


def restore_backup(conn, schema, backup):
    """Clone already-present data server-side after verifying backup/source equality."""
    with conn.cursor() as cursor:
        for table, rows in backup["tables"].items():
            definitions = []
            columns = backup["columns"][table]
            for name, udt, nullable, default in columns:
                if udt not in {"text", "int4", "float8", "timestamptz"}:
                    raise ValueError("Unexpected backup column type")
                definition = sql.SQL("{} pg_catalog.{}").format(sql.Identifier(name), sql.Identifier(udt))
                if nullable == "NO":
                    definition += sql.SQL(" NOT NULL")
                if default is not None:
                    if not re.fullmatch(r"(?:0|now\(\)|'(?:[^']|'')*'::text)", default):
                        raise ValueError("Unexpected backup column default")
                    definition += sql.SQL(" DEFAULT ") + sql.SQL(default)
                definitions.append(definition)
            cursor.execute(sql.SQL("CREATE TABLE {}.{} ({})").format(
                sql.Identifier(schema), sql.Identifier(table), sql.SQL(",").join(definitions)))
            names = sql.SQL(",").join(sql.Identifier(row[0]) for row in columns)
            cursor.execute(sql.SQL("INSERT INTO {}.{} ({}) SELECT {} FROM public.{}").format(
                sql.Identifier(schema), sql.Identifier(table), names, names, sql.Identifier(table)))
        for foreign_keys in (False, True):
            for table, name, definition in backup["constraints"]:
                if definition.startswith("FOREIGN KEY") != foreign_keys:
                    continue
                if ";" in definition or "public." in definition.lower():
                    raise ValueError("Constraint references an external schema")
                cursor.execute(sql.SQL("ALTER TABLE {}.{} ADD CONSTRAINT {} {}").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(name), sql.SQL(definition)))
        cursor.execute("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=%s", (schema,))
        if cursor.fetchone()[0] != len(backup["constraints"]):
            raise AssertionError("Constraint copy is incomplete")


def seed_fixtures(conn, fixtures):
    wrapped = PostgresConnectionWrapper(conn)
    for fixture in fixtures:
        for entry in [fixture, *fixture.get("additional_tracks", [])]:
            uid = entry["track_uid"]
            if not uid.startswith("rehearsal-"):
                raise ValueError("Synthetic fixture UID needs rehearsal- prefix")
            ensure_track(wrapped, track_uid=uid, video_id=entry["video_id"],
                         yt_title=entry["metadata"]["title"], yt_artist=entry["metadata"]["artist"],
                         yt_album=entry["metadata"].get("album", ""), status="matched", score=1)
            upsert_track_list_metadata(wrapped, service="ytmusic", song_id=entry["song_id"],
                                       track_uid=uid, row=entry["metadata"])


def simulated_stages(wrapped, manifest):
    outputs = manifest.get("outputs") or {}
    if "playlists" not in outputs or "history" not in outputs:
        return {"status": "not_run", "reason": "output scope is absent"}
    now = datetime.now(timezone.utc).isoformat()
    playlists = []
    for target in outputs["playlists"]:
        preserved = {item["video_id"]: item["set_video_id"] for item in target.get("preserved_items", [])}
        items = [{"video_id": vid, "set_video_id": preserved.get(vid, f"rehearsal-slot-{index}")}
                 for index, vid in enumerate(target["video_ids"], 1)]
        playlists.append({"playlist_id": target["playlist_id"], "video_ids": target["video_ids"],
                          "items": items, "observed_at": now, "readback_verified": True})
    publication = mark_repair_stage(wrapped, manifest, "publication", {
        "evidence_ref": "isolated-rehearsal:simulated-playlist-readback", "playlists": playlists,
    })
    history = mark_repair_stage(wrapped, manifest, "history", {
        "evidence_ref": "isolated-rehearsal:simulated-history-readback", "files": [
            {**target, "public_sha256": target["sha256"], "observed_at": now, "readback_verified": True}
            for target in outputs["history"]
        ],
    })
    return {"publication": publication, "history": history, "external_readbacks": "simulated_only"}


def run(backup_path, spec_path, report_path, *, origin_only=False, tasks_path=None, history_date=None):
    from sync_all import load_env_file
    load_env_file(Path(__file__).resolve().parents[1] / ".secrets" / ".env")
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("Database credentials are unavailable")
    backup, supplied = json.loads(backup_path.read_text()), json.loads(spec_path.read_text())
    fixtures = supplied.pop("fixtures", [])
    tables = sorted(backup["tables"])
    if len(tables) != 18 or set(tables) != set(backup["columns"]):
        raise ValueError("Expected the complete 18-table backup")
    schema = "rehearsal_chart_" + uuid.uuid4().hex
    report = {"schema": schema, "public_writes": False, "external_provider_calls": False,
              "backup_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
              "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(), "checks": {}, "phase": "public_before"}
    created, conn = False, None
    try:
        report["public_before"] = public_hashes(url, tables)
        expected = {table: digest_rows(backup["tables"][table]) for table in tables}
        if report["public_before"] != expected:
            raise AssertionError("Destination public data differs from the local backup; origin is not verified")
        report["checks"]["same_origin_backup_18_tables"] = True
        report["local_backup_rows_uploaded"] = False
        if origin_only:
            report.update(status="passed", phase="read_only_origin_verification")
            return 0
        checkpoint(report, "create_schema")
        conn = psycopg2.connect(url, connect_timeout=20)
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.commit()
        created = True
        conn.close()
        conn = connect_schema(url, schema)
        checkpoint(report, "restore_backup")
        restore_backup(conn, schema, backup)
        restored = hashes(conn, schema, tables)
        if restored != expected:
            raise AssertionError("Cloned table hashes differ from the complete backup")
        report["checks"]["backup_rows_and_constraints"] = True
        seed_fixtures(conn, fixtures)
        conn.commit()
        baseline = hashes(conn, schema, tables)
        conn.rollback()
        read = connect_schema(url, schema, read_only=True)
        try:
            checkpoint(report, "read_only_plan")
            manifest = plan_repair(PostgresConnectionWrapper(read), supplied)
            if hashes(read, schema, tables) != baseline:
                raise AssertionError("Read-only plan changed cloned data")
        finally:
            read.rollback()
            read.close()
        report["checks"]["read_only_plan"] = True
        wrapped = PostgresConnectionWrapper(conn)
        checkpoint(report, "injected_receipt_failure")
        select_schema(conn, schema)
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE FUNCTION {}.reject_rehearsal_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ "
                                   "BEGIN IF NEW.source='ytmusic_chart_incident' THEN RAISE EXCEPTION 'rehearsal_receipt_failure'; "
                                   "END IF; RETURN NEW; END $$").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("CREATE TRIGGER reject_rehearsal_receipt BEFORE INSERT ON {}.migration_reports "
                                   "FOR EACH ROW EXECUTE FUNCTION {}.reject_rehearsal_receipt()").format(
                                       sql.Identifier(schema), sql.Identifier(schema)))
        try:
            apply_repair(wrapped, manifest)
        except psycopg2.DatabaseError as exc:
            if "rehearsal_receipt_failure" not in str(exc):
                raise
        else:
            raise AssertionError("Injected receipt failure did not abort the repair")
        finally:
            conn.rollback()
        if hashes(conn, schema, tables) != baseline:
            raise AssertionError("Receipt failure left partial database changes")
        conn.rollback()
        report["checks"]["receipt_failure_atomic"] = True
        checkpoint(report, "apply_and_rollback")
        select_schema(conn, schema)
        apply_repair(wrapped, manifest)
        conn.rollback()
        if hashes(conn, schema, tables) != baseline:
            raise AssertionError("Caller rollback did not restore every table")
        conn.rollback()
        report["checks"]["caller_owned_rollback"] = True
        checkpoint(report, "apply_and_commit")
        select_schema(conn, schema)
        apply_repair(wrapped, manifest)
        conn.commit()
        committed = hashes(conn, schema, tables)
        conn.close()
        conn = connect_schema(url, schema)
        wrapped = PostgresConnectionWrapper(conn)
        checkpoint(report, "reconnect_resume")
        if apply_repair(wrapped, manifest)["status"] != "already_applied" or hashes(conn, schema, tables) != committed:
            raise AssertionError("Reconnect/resume mutated the committed repair")
        report["checks"]["commit_reconnect_resume"] = True
        @contextmanager
        def isolated_connection(*args, **kwargs):
            raw = connect_schema(url, schema, read_only=kwargs.get("read_only", False))
            try:
                yield PostgresConnectionWrapper(raw)
            finally:
                raw.rollback()
                raw.close()
        from heal_split_tracks import heal
        from sync_validation import PlaybackBlocked
        with patch("hype_db.connect", isolated_connection), patch("sync_validation.sync_run_lock", lambda *args: nullcontext()):
            try:
                heal(Path("isolated-rehearsal"), dry_run=False)
            except PlaybackBlocked:
                report["checks"]["active_incident_blocks_heal"] = True
            else:
                raise AssertionError("Active incident did not block ordinary heal")
        checkpoint(report, "simulated_stages")
        report["stages"] = simulated_stages(wrapped, manifest)
        conn.commit()
        select_schema(conn, schema)
        report["verification"] = verify_repair(wrapped, manifest)
        if tasks_path:
            from sync_validation import freeze_outputs
            tasks = json.loads(tasks_path.read_text())
            tasks = tasks["tasks"] if isinstance(tasks, dict) else tasks
            tasks = [task for task in tasks if task.get("enabled", True) not in (False, "false", "False", "0", 0)]
            before_freeze = hashes(conn, schema, tables)
            checkpoint(report, "freeze_outputs_read_only")
            frozen = freeze_outputs(wrapped, tasks, history_date=history_date)
            if hashes(conn, schema, tables) != before_freeze:
                raise AssertionError("Output freeze changed isolated database rows")
            report["freeze"] = {"fingerprint": frozen["fingerprint"], "history_date": frozen["history_date"],
                                "inputs": len(frozen["inputs"]), "outputs": len(frozen["outputs"]),
                                "unique_video_ids": len({vid for output in frozen["outputs"] for vid in output["video_ids"]})}
            report["checks"]["freeze_read_only"] = True
        report["manifest_hash"] = manifest["manifest_hash"]
        report["case_count"] = len(manifest["cases"])
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc).splitlines()[0][:300].replace(url, "[redacted]"))
    finally:
        if conn is not None and not conn.closed:
            conn.rollback()
            conn.close()
        try:
            report["public_after"] = public_hashes(url, tables)
            report["checks"]["public_18_tables_unchanged"] = report.get("public_before") == report["public_after"]
            if not report["checks"]["public_18_tables_unchanged"]:
                report["status"] = "failed"
        except Exception as exc:
            report.update(status="failed", public_verification_error_type=type(exc).__name__)
        finally:
            if created:
                try:
                    cleanup = psycopg2.connect(url, connect_timeout=20)
                    try:
                        with cleanup.cursor() as cursor:
                            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                        cleanup.commit()
                        report["isolated_schema_removed"] = True
                    finally:
                        cleanup.close()
                except Exception as exc:
                    report.update(status="failed", cleanup_error_type=type(exc).__name__, isolated_schema_removed=False)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": report["status"], "phase": report["phase"], "checks": report["checks"],
                      "isolated_schema_removed": report.get("isolated_schema_removed", False)}))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-isolated-schema", action="store_true", help="Explicitly allow creating and dropping one generated rehearsal schema")
    parser.add_argument("--verify-origin-only", action="store_true", help="Only read public table hashes and compare the local backup; no schema creation or writes")
    parser.add_argument("--tasks", type=Path, help="Optional enabled output tasks for read-only snapshot verification")
    parser.add_argument("--history-date")
    args = parser.parse_args()
    if not args.allow_isolated_schema and not args.verify_origin_only:
        parser.error("Rehearsal requires --allow-isolated-schema")
    if args.report.resolve() in {args.backup.resolve(), args.spec.resolve()}:
        parser.error("Report must not overwrite the backup or specification")
    return run(args.backup, args.spec, args.report, origin_only=args.verify_origin_only,
               tasks_path=args.tasks, history_date=args.history_date)


if __name__ == "__main__":
    raise SystemExit(main())
