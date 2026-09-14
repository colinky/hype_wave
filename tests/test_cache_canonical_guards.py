from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from hype_db import connect, init_db
import hype_db_store as store


CANONICAL = "canonVid001"
INCOMING = "newVideo001"
SOURCE_ID = "source-song-1"
UID = "uid-canonical"


def source_row(**overrides):
    row = {
        "rank": 1,
        "service": "apple",
        "song_id": SOURCE_ID,
        "title": "The Adult I Dreamed Of",
        "artist": "meomuru",
        "album": "The Adult I Dreamed Of",
        "locale": "en",
    }
    row.update(overrides)
    return row


def match_row(video_id=INCOMING, **overrides):
    row = {
        **source_row(),
        "video_id": video_id,
        "yt_title": "Candidate Title",
        "yt_artist": "Candidate Artist",
        "yt_album": "Candidate Album",
        "status": "matched",
        "score": 0.91,
        "query": "search fixture",
    }
    row.update(overrides)
    return row


def seed_canonical(conn, *, yt_title="Canonical Title", yt_artist="Canonical Artist", yt_album="Canonical Album"):
    store.ensure_track(
        conn,
        track_uid=UID,
        video_id=CANONICAL,
        yt_title=yt_title,
        yt_artist=yt_artist,
        yt_album=yt_album,
        status="matched",
        score=1.0,
    )
    store.upsert_track_list_metadata(
        conn,
        service="apple",
        song_id=SOURCE_ID,
        track_uid=UID,
        row=source_row(),
    )
    conn.commit()


def canonical_state(conn):
    track = tuple(conn.execute(
        "SELECT canonical_yt_video_id, yt_title, yt_artist, yt_album FROM tracks WHERE track_uid=?",
        (UID,),
    ).fetchone())
    aliases = [tuple(row) for row in conn.execute(
        "SELECT video_id, is_canonical FROM yt_video_ids WHERE track_uid=? ORDER BY video_id",
        (UID,),
    ).fetchall()]
    return track, aliases


class ResolverAssistedCacheTests(unittest.TestCase):
    def setUp(self):
        self.environment = unittest.mock.patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    @staticmethod
    def resolved(video_id=CANONICAL):
        return {
            "video_id": video_id,
            "title": "The Adult I Dreamed Of",
            "artist": "meomuru",
            "album": "The Adult I Dreamed Of",
            "title_en": "The Adult I Dreamed Of",
            "artist_en": "meomuru",
            "album_en": "The Adult I Dreamed Of",
            "title_ko": "꿈꾸던 어른이 되었나요?",
            "artist_ko": "머무르",
            "album_ko": "꿈꾸던 어른이 되었나요?",
        }

    def test_resolver_validates_self_only_cache_once_and_refreshes_canonical_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "resolver.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn, yt_title="", yt_artist="", yt_album="")
                resolver = Mock(return_value=self.resolved())

                cached = store.get_bulk_cached_matches(
                    conn,
                    "apple",
                    [source_row(), source_row()],
                    metadata_resolver=resolver,
                )
                conn.commit()

                self.assertEqual(cached[SOURCE_ID]["video_id"], CANONICAL)
                self.assertEqual(cached[SOURCE_ID]["cache_origin"], "service_song_id")
                resolver.assert_called_once_with(CANONICAL)
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT canonical_yt_video_id, yt_title, yt_artist, yt_album "
                        "FROM tracks WHERE track_uid=?",
                        (UID,),
                    ).fetchone()),
                    (CANONICAL, "The Adult I Dreamed Of", "meomuru", "The Adult I Dreamed Of"),
                )

    def test_resolver_rejections_are_cache_misses_and_read_only(self):
        cases = {
            "foreign_id": Mock(return_value=self.resolved(INCOMING)),
            "empty": Mock(return_value=None),
            "exception": Mock(side_effect=RuntimeError("metadata provider unavailable")),
        }
        for name, resolver in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                path = Path(directory) / f"{name}.db"
                init_db(path)
                with connect(path) as conn:
                    seed_canonical(conn, yt_title="", yt_artist="", yt_album="")
                before = path.read_bytes()

                with connect(path, read_only=True) as conn:
                    if name == "exception":
                        with self.assertRaisesRegex(RuntimeError, "metadata provider unavailable"):
                            store.get_bulk_cached_matches(
                                conn,
                                "apple",
                                [source_row()],
                                metadata_resolver=resolver,
                                read_only=True,
                            )
                    else:
                        self.assertNotIn(
                            SOURCE_ID,
                            store.get_bulk_cached_matches(
                                conn,
                                "apple",
                                [source_row()],
                                metadata_resolver=resolver,
                                read_only=True,
                            ),
                        )

                self.assertEqual(path.read_bytes(), before)

    def test_independent_metadata_conflict_does_not_call_resolver(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "independent-conflict.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn, yt_title="Different Song", yt_artist="Different Artist", yt_album="")
                store.upsert_track_list_metadata(
                    conn,
                    service="ytmusic",
                    song_id=CANONICAL,
                    track_uid=UID,
                    row={"title_en": "Different Song", "artist_en": "Different Artist"},
                )
                conn.commit()
                resolver = Mock(return_value=self.resolved())

                self.assertNotIn(
                    SOURCE_ID,
                    store.get_bulk_cached_matches(
                        conn,
                        "apple",
                        [source_row()],
                        metadata_resolver=resolver,
                    ),
                )
                resolver.assert_not_called()


class CanonicalPersistenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.environment = unittest.mock.patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def assert_guarded_state(self, conn):
        self.assertEqual(
            canonical_state(conn),
            (
                (CANONICAL, "Canonical Title", "Canonical Artist", "Canonical Album"),
                [(CANONICAL, 1)],
            ),
        )
        conflict = conn.execute(
            "SELECT existing_video_id, incoming_video_id FROM review_conflicts "
            "WHERE service='apple' AND song_id=?",
            (SOURCE_ID,),
        ).fetchone()
        self.assertEqual(tuple(conflict), (CANONICAL, INCOMING))

    def test_direct_upsert_keeps_existing_canonical_and_metadata_on_unsupported_change(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "direct.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn)

                self.assertEqual(
                    store.upsert_track_match(
                        conn,
                        service="apple",
                        source_row=source_row(),
                        match_row=match_row(),
                    ),
                    UID,
                )
                conn.commit()
                self.assert_guarded_state(conn)

    def test_bulk_persist_rejects_selection_disagreement_without_completing(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "bulk.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn)
                store.persist_crawled_tracks(
                    path,
                    service="apple",
                    job_name="Canonical-Guard-Fixture",
                    chart_date="2026-09-08",
                    reference_period="2026-09-08",
                    tracks=[source_row()],
                    conn=conn,
                    commit=False,
                )
                before = list(conn.iterdump())
                with self.assertRaisesRegex(store.CanonicalDecisionError, "cannot complete"):
                    store.persist_crawl_run(
                        path, service="apple", job_name="Canonical-Guard-Fixture", chart_date="2026-09-08",
                        reference_period="2026-09-08", started_at="2026-09-09T00:00:00.000001+00:00",
                        tracks=[source_row()], matches=[match_row()], conn=conn, skip_playlist_order=True,
                    )
                self.assertEqual(list(conn.iterdump()), before)

    def test_bulk_same_canonical_refresh_remains_allowed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "bulk-refresh.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn)
                refreshed = match_row(
                    CANONICAL,
                    yt_title="Refreshed Title",
                    yt_artist="Refreshed Artist",
                    yt_album="Refreshed Album",
                )
                store.persist_crawled_tracks(
                    path,
                    service="apple",
                    job_name="Canonical-Refresh-Fixture",
                    chart_date="2026-09-08",
                    reference_period="2026-09-08",
                    tracks=[source_row()],
                    conn=conn,
                    commit=False,
                )
                store.persist_crawl_run(
                    path,
                    service="apple",
                    job_name="Canonical-Refresh-Fixture",
                    chart_date="2026-09-08",
                    reference_period="2026-09-08",
                    started_at="2026-09-09T00:00:00.000002+00:00",
                    tracks=[source_row()],
                    matches=[refreshed],
                    conn=conn,
                    skip_playlist_order=True,
                )

                self.assertEqual(
                    canonical_state(conn),
                    (
                        (CANONICAL, "Refreshed Title", "Refreshed Artist", "Refreshed Album"),
                        [(CANONICAL, 1)],
                    ),
                )

    def test_same_canonical_refresh_and_manual_override_remain_allowed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "allowed.db"
            init_db(path)
            with connect(path) as conn:
                seed_canonical(conn)
                store.upsert_track_match(
                    conn,
                    service="apple",
                    source_row=source_row(),
                    match_row=match_row(
                        CANONICAL,
                        yt_title="Refreshed Title",
                        yt_artist="Refreshed Artist",
                        yt_album="Refreshed Album",
                    ),
                )
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT canonical_yt_video_id, yt_title, yt_artist, yt_album "
                        "FROM tracks WHERE track_uid=?",
                        (UID,),
                    ).fetchone()),
                    (CANONICAL, "Refreshed Title", "Refreshed Artist", "Refreshed Album"),
                )

                conn.execute(
                    "INSERT INTO manual_overrides(service, song_id, action, canonical_yt_video_id, reason, updated_at) "
                    "VALUES ('apple', ?, 'set_canonical', ?, 'fixture', '2026-09-09T00:00:00Z')",
                    (SOURCE_ID, INCOMING),
                )
                store.upsert_track_match(
                    conn,
                    service="apple",
                    source_row=source_row(),
                    match_row=match_row(),
                )
                bound_uid = conn.execute(
                    "SELECT track_uid FROM platform_song_ids WHERE service='apple' AND song_id=?",
                    (SOURCE_ID,),
                ).fetchone()[0]
                self.assertEqual(
                    conn.execute(
                        "SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?",
                        (bound_uid,),
                    ).fetchone()[0],
                    INCOMING,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM yt_video_ids WHERE track_uid=? AND is_canonical=1",
                        (bound_uid,),
                    ).fetchone()[0],
                    1,
                )

    def test_manual_video_switch_does_not_copy_the_original_videos_metadata(self):
        for mode in ("direct", "bulk"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                path = Path(directory) / "manual-metadata.db"
                init_db(path)
                with connect(path) as conn:
                    seed_canonical(conn)
                    conn.execute(
                        "INSERT INTO manual_overrides(service,song_id,action,canonical_yt_video_id,reason,updated_at) "
                        "VALUES ('apple',?,'set_canonical',?,'fixture','2026-09-09')",
                        (SOURCE_ID, INCOMING),
                    )
                    observed = match_row(CANONICAL, yt_title="Original Video Title",
                                         yt_artist="Original Video Artist", yt_album="Original Video Album")
                    if mode == "direct":
                        store.upsert_track_match(conn, service="apple", source_row=source_row(), match_row=observed)
                    else:
                        store.persist_crawled_tracks(path, service="apple", job_name="Manual-Metadata-Fixture",
                                                     chart_date="2026-09-09", reference_period="2026-09-09",
                                                     tracks=[source_row()], conn=conn, commit=False)
                        before = list(conn.iterdump())
                        with self.assertRaisesRegex(store.CanonicalDecisionError, "cannot complete"):
                            store.persist_crawl_run(path, service="apple", job_name="Manual-Metadata-Fixture",
                                                    chart_date="2026-09-09", reference_period="2026-09-09",
                                                    started_at="2026-09-09T00:00:00+00:00", tracks=[source_row()],
                                                    matches=[observed], conn=conn, skip_playlist_order=True)
                        self.assertEqual(list(conn.iterdump()), before)
                        continue
                    row = conn.execute(
                        "SELECT t.canonical_yt_video_id,t.yt_title,t.yt_artist,t.yt_album "
                        "FROM platform_song_ids p JOIN tracks t ON t.track_uid=p.track_uid "
                        "WHERE p.service='apple' AND p.song_id=?", (SOURCE_ID,),
                    ).fetchone()
                    self.assertEqual(tuple(value or "" for value in row), (INCOMING, "", "", ""))


if __name__ == "__main__":
    unittest.main()
