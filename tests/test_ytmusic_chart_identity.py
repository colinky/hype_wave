from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hype_db import connect, init_db
from hype_db_common import compact_metadata_key, metadata_key
import hype_db_store as store
import ytmusic_to_ytmusic_crawl as chart_crawl
from ytmusic_to_ytmusic_crawl import preflight_chart_relations


PHCI = "pHciG9_2xXM"
F7 = "f7-z0FDBpZo"
INVITATION_SOURCE = "Yyfv2R97dHA"
INVITATION_TARGET = "Jit6zG2_Xho"
ATV_RELATION = store.YOUTUBE_ATV_RELATION


def seed_grls(conn) -> None:
    store.ensure_track(
        conn,
        track_uid="uid-phci",
        video_id=PHCI,
        yt_title="Meet my GRLS",
        yt_artist="FKA twigs",
        status="matched",
        score=0.668,
    )
    store.ensure_track(
        conn,
        track_uid="uid-f7",
        video_id=F7,
        yt_title="GRLS",
        yt_artist="TUIDE",
        yt_album="TUNE & PLAY",
        status="matched",
        score=1.0,
    )
    store.upsert_track_list_metadata(
        conn,
        service="ytmusic",
        song_id=PHCI,
        track_uid="uid-phci",
        row={"title_en": "GRLS", "artist_en": "TUIDE", "locale": "en"},
    )
    store.upsert_track_list_metadata(
        conn,
        service="ytmusic",
        song_id=F7,
        track_uid="uid-f7",
        row={
            "title_en": "GRLS",
            "artist_en": "TUIDE",
            "album_en": "TUNE & PLAY",
            "locale": "en",
        },
    )
    for service, song_id in (("apple", "6795782863"), ("melon", "602872574")):
        store.upsert_track_list_metadata(
            conn,
            service=service,
            song_id=song_id,
            track_uid="uid-f7",
            row={
                "title_en": "GRLS",
                "artist_en": "TUIDE",
                "album_en": "TUNE & PLAY",
                "locale": "en",
            },
        )
    conn.execute(
        "INSERT INTO metadata_lookup_index VALUES (?, 'uid-phci', 'polluted', 1.0)",
        (compact_metadata_key("GRLS", "FKA twigs"),),
    )
    store.ensure_track(
        conn,
        track_uid="uid-real-fka",
        video_id="real-fka-video",
        yt_title="Two Weeks",
        yt_artist="FKA twigs",
        status="matched",
        score=1.0,
    )
    real_fka_key = compact_metadata_key("Two Weeks", "FKA twigs")
    conn.execute(
        "INSERT INTO metadata_lookup_index VALUES (?, 'uid-real-fka', 'real', 1.0)",
        (real_fka_key,),
    )
    conn.executemany(
        """
        INSERT INTO playlist_order(
            service, job_name, source_variant, reference_period, song_id, rank_order
        ) VALUES ('ytmusic', 'Weekly-Hot-100', 'default', ?, ?, ?)
        """,
        [("2026-W33", PHCI, 95), ("2026-W35", PHCI, 62), ("2026-W36", PHCI, 40)],
    )
    conn.commit()


def invitation_source(*, album: str = "") -> dict:
    return {
        "source": "youtube_charts_weekly_browse_api",
        "rank": 75,
        "song_id": INVITATION_SOURCE,
        "original_video_id": INVITATION_SOURCE,
        "atv_external_video_id": INVITATION_TARGET,
        "title": "Invitation",
        "artist": "Nam You Joung, LEE MINHYUK (HUTA)",
        "album": album,
        "title_en": "Invitation",
        "artist_en": "Nam You Joung, LEE MINHYUK (HUTA)",
        "album_en": album,
        "title_ko": "Invitation",
        "artist_ko": "Nam You Joung, 이민혁 (허타)",
        "album_ko": album,
    }


def invitation_target() -> dict:
    return {
        "title": "Invitation (feat. HUTA)",
        "artist": "Nam You Joung",
        "album": "Invitation",
        "title_en": "Invitation (feat. HUTA)",
        "artist_en": "Nam You Joung",
        "album_en": "Invitation",
        "title_ko": "초대",
        "artist_ko": "남유정",
        "album_ko": "Invitation",
    }


def seed_polluted_invitation(conn) -> None:
    polluted = {
        **invitation_source(album="샤먼 OST"),
        "album_id": "legacy-shaman-album-id",
    }
    store.ensure_track(
        conn,
        track_uid="uid-invitation-source",
        video_id=INVITATION_SOURCE,
        yt_title=polluted["title"],
        yt_artist=polluted["artist"],
        yt_album=polluted["album"],
        status="matched",
        score=1.0,
    )
    store.ensure_track(
        conn,
        track_uid="uid-invitation-target",
        video_id=INVITATION_TARGET,
        yt_title="Invitation (feat. HUTA)",
        yt_artist="Nam You Joung",
        yt_album="Invitation",
        status="matched",
        score=1.0,
    )
    store.upsert_track_list_metadata(
        conn,
        service="ytmusic",
        song_id=INVITATION_SOURCE,
        track_uid="uid-invitation-source",
        row=polluted,
    )
    store.upsert_track_list_metadata(
        conn,
        service="ytmusic",
        song_id=INVITATION_TARGET,
        track_uid="uid-invitation-target",
        row=invitation_target(),
    )
    store.upsert_metadata_lookup(
        conn,
        track_uid="uid-invitation-source",
        row=polluted,
        source="legacy",
        score=1.0,
    )
    conn.execute(
        """
        INSERT INTO playlist_order(
            service, job_name, source_variant, reference_period, song_id, rank_order
        ) VALUES ('ytmusic', 'Weekly-Hot-100', 'default', '2026-W36', ?, 75)
        """,
        (INVITATION_SOURCE,),
    )
    conn.commit()


class LocalizedChartMergeTests(unittest.TestCase):
    def test_same_rank_with_different_song_ids_does_not_mix_locales(self) -> None:
        primary = [{
            "rank": 40,
            "video_id": PHCI,
            "atv_external_video_id": F7,
            "title": "GRLS",
            "artist": "TUIDE",
        }]
        unrelated = [{
            "rank": 40,
            "video_id": "wrongVid001",
            "atv_external_video_id": "wrongAtv001",
            "title": "Meet my GRLS",
            "artist": "FKA twigs",
            "album": "MAGDALENE",
        }]

        merged = chart_crawl.merge_localized_chart_entries(primary, unrelated, "ko")

        self.assertEqual(merged[0]["title_ko"], "")
        self.assertEqual(merged[0]["artist_ko"], "")
        self.assertEqual(merged[0]["album_ko"], "")

    def test_exact_video_or_atv_identity_combines_locales(self) -> None:
        cases = (
            ({"video_id": PHCI, "atv_external_video_id": F7},
             {"video_id": PHCI, "atv_external_video_id": "otherAtv001"}),
            ({"video_id": PHCI, "atv_external_video_id": F7},
             {"video_id": "otherVid001", "atv_external_video_id": F7}),
        )
        for primary_ids, localized_ids in cases:
            with self.subTest(localized_ids=localized_ids):
                primary = [{"rank": 40, "title": "GRLS", **primary_ids}]
                localized = [{
                    "rank": 99,
                    "title": "GRLS",
                    "artist": "튜이드",
                    "album": "TUNE & PLAY",
                    **localized_ids,
                }]

                merged = chart_crawl.merge_localized_chart_entries(
                    primary, localized, "ko"
                )

                self.assertEqual(
                    (merged[0]["title_ko"], merged[0]["artist_ko"], merged[0]["album_ko"]),
                    ("GRLS", "튜이드", "TUNE & PLAY"),
                )


class SourceSongRelationTests(unittest.TestCase):
    def decision(self, conn, source_id, target_id, metadata):
        uid = store.find_track_by_service_song(conn, "ytmusic", source_id)
        old = conn.execute("SELECT canonical_yt_video_id FROM tracks WHERE track_uid=?", (uid,)).fetchone()[0]
        now = datetime.now(timezone.utc)
        common = {"exact_id": True, "auth_state": "authenticated", "run_health": "healthy", "environment": "fixture",
                  "observed_at": (now - timedelta(seconds=1)).isoformat(), "expires_at": (now + timedelta(minutes=20)).isoformat(),
                  "state": "playable", "has_audio": True}
        return {"expected_track_uid": uid, "expected_video_id": old, "selected_video_id": target_id,
                "expected_target_uid": store.find_track_by_video(conn, target_id), "same_recording": True,
                "reason": "restore_unintended_chart_switch",
                "current_evidence": {**common, "video_id": old}, "candidate_evidence": {**common, "video_id": target_id},
                "metadata": {**metadata, "video_id": target_id, "verified": True},
                "incident": {"repair_id": "fixture", "case_id": source_id, "evidence_ref": "fixture-before-image",
                             "original_video_id": target_id, "changed_video_id": old}}

    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_relation_periods_are_monotonic_and_counterpart_change_conflicts(self) -> None:
        record = getattr(store, "record_source_song_relation", None)
        self.assertIsNotNone(record, "source relation recorder is required")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "relations.db"
            init_db(db_path)
            with connect(db_path) as conn:
                self.assertEqual(
                    record(
                        conn,
                        service="ytmusic",
                        source_song_id=PHCI,
                        relation_type=ATV_RELATION,
                        related_service="ytmusic",
                        related_song_id=F7,
                        evidence_source="youtube_charts",
                        reference_period="2026-W36",
                    ),
                    "inserted",
                )
                for period in ("2026-W35", "2026-W37"):
                    self.assertEqual(
                        record(
                            conn,
                            service="ytmusic",
                            source_song_id=PHCI,
                            relation_type=ATV_RELATION,
                            related_service="ytmusic",
                            related_song_id=F7,
                            evidence_source="youtube_charts",
                            reference_period=period,
                        ),
                        "confirmed",
                    )
                self.assertEqual(
                    record(
                        conn,
                        service="ytmusic",
                        source_song_id=PHCI,
                        relation_type=ATV_RELATION,
                        related_service="ytmusic",
                        related_song_id="otherVid123",
                        evidence_source="youtube_charts",
                        reference_period="2026-W38",
                    ),
                    "conflict",
                )
                row = conn.execute(
                    "SELECT * FROM source_song_relations WHERE source_song_id = ?",
                    (PHCI,),
                ).fetchone()
                self.assertEqual(row["related_song_id"], F7)
                self.assertEqual(
                    (
                        row["first_seen_reference_period"],
                        row["last_seen_reference_period"],
                    ),
                    ("2026-W35", "2026-W37"),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM review_conflicts WHERE song_id = ?",
                        (PHCI,),
                    ).fetchone()[0],
                    1,
                )

    def test_relation_metadata_allows_exact_feature_credit_moved_to_artist(self) -> None:
        source = {
            "title": "Invitation",
            "artist": "Nam You Joung, LEE MINHYUK (HUTA)",
            "album": "",
        }
        related = {
            "title": "Invitation (feat. HUTA)",
            "artist": "Nam You Joung",
            "album": "Invitation",
        }
        self.assertTrue(
            store._relation_metadata_matches(
                Mock(), None, None, source, related
            )
        )

    def test_relation_feature_exception_stays_narrow(self) -> None:
        related = {
            "title": "Invitation (feat. HUTA)",
            "artist": "Nam You Joung",
            "album": "Invitation",
        }
        rejected_sources = [
            {
                "title": "Invitation",
                "artist": "Nam You Joung, HUT",
                "album": "Invitation",
            },
            {
                "title": "Invitation (feat. OTHER)",
                "artist": "Nam You Joung",
                "album": "Invitation",
            },
            {
                "title": "Invitation",
                "artist": "Different Artist, HUTA",
                "album": "Invitation",
            },
            {
                "title": "Invitation (Live)",
                "artist": "Nam You Joung, HUTA",
                "album": "Invitation",
            },
            {
                "title": "Invitation (Remix)",
                "artist": "Nam You Joung, HUTA",
                "album": "Invitation",
            },
        ]
        for source in rejected_sources:
            with self.subTest(source=source):
                self.assertFalse(
                    store._relation_metadata_matches(
                        Mock(), None, None, source, related
                    )
                )

    def test_trusted_chart_relation_repairs_blank_source_album_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "invitation.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_polluted_invitation(conn)
                store.record_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    evidence_source="youtube_charts_weekly_browse_api",
                    reference_period="2026-W36",
                    payload=invitation_source(),
                )
                first = store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    source_row=invitation_source(),
                    related_row=invitation_target(),
                    canonical_decision=self.decision(conn, INVITATION_SOURCE, INVITATION_TARGET, invitation_target()),
                )
                conn.commit()

                self.assertEqual(first["status"], "linked")
                source_metadata = conn.execute(
                    "SELECT album_id, album_ko, album_en FROM track_list "
                    "WHERE service='ytmusic' AND song_id=?",
                    (INVITATION_SOURCE,),
                ).fetchone()
                self.assertEqual(tuple(source_metadata), ("", "", ""))
                canonical = conn.execute(
                    "SELECT canonical_yt_video_id, yt_album FROM tracks "
                    "WHERE track_uid='uid-invitation-source'",
                ).fetchone()
                self.assertEqual(tuple(canonical), (INVITATION_TARGET, "Invitation"))
                self.assertEqual(
                    dict(conn.execute(
                        "SELECT song_id, track_uid FROM platform_song_ids "
                        "WHERE service='ytmusic' AND song_id IN (?, ?)",
                        (INVITATION_SOURCE, INVITATION_TARGET),
                    ).fetchall()),
                    {
                        INVITATION_SOURCE: "uid-invitation-source",
                        INVITATION_TARGET: "uid-invitation-source",
                    },
                )
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM metadata_lookup_index WHERE lookup_key=?",
                    (metadata_key(
                        "Invitation",
                        "Nam You Joung, LEE MINHYUK (HUTA)",
                        "샤먼 OST",
                    ),),
                ).fetchone())
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT rank_order, song_id FROM playlist_order "
                        "WHERE reference_period='2026-W36'",
                    ).fetchone()),
                    (75, INVITATION_SOURCE),
                )

            before_second = db_path.read_bytes()
            with connect(db_path) as conn:
                second = store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    source_row=invitation_source(),
                    related_row=invitation_target(),
                )
                conn.commit()
            self.assertEqual(second["status"], "already_linked")
            self.assertEqual(db_path.read_bytes(), before_second)

    def test_already_linked_relation_repairs_reintroduced_source_album_once(self) -> None:
        polluted_key = metadata_key(
            "Invitation",
            "Nam You Joung, LEE MINHYUK (HUTA)",
            "샤먼 OST",
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "invitation-already-linked.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_polluted_invitation(conn)
                store.record_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    evidence_source="youtube_charts_weekly_browse_api",
                    reference_period="2026-W36",
                )
                store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    source_row=invitation_source(),
                    related_row=invitation_target(),
                    canonical_decision=self.decision(conn, INVITATION_SOURCE, INVITATION_TARGET, invitation_target()),
                )
                conn.execute(
                    "UPDATE track_list SET album_id='legacy-shaman-album-id', "
                    "album_ko='샤먼 OST', album_en='샤먼 OST' "
                    "WHERE service='ytmusic' AND song_id=?",
                    (INVITATION_SOURCE,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata_lookup_index "
                    "VALUES (?, 'uid-invitation-source', 'legacy', 1.0)",
                    (polluted_key,),
                )
                conn.commit()

                repaired = store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    source_row=invitation_source(),
                    related_row=invitation_target(),
                )
                conn.commit()
                source_metadata = conn.execute(
                    "SELECT album_id, album_ko, album_en FROM track_list "
                    "WHERE service='ytmusic' AND song_id=?",
                    (INVITATION_SOURCE,),
                ).fetchone()
                self.assertEqual(repaired["status"], "already_linked")
                self.assertEqual(tuple(source_metadata), ("", "", ""))
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM metadata_lookup_index WHERE lookup_key=?",
                    (polluted_key,),
                ).fetchone())

            before_noop = db_path.read_bytes()
            with connect(db_path) as conn:
                noop = store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=INVITATION_SOURCE,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=INVITATION_TARGET,
                    source_row=invitation_source(),
                    related_row=invitation_target(),
                )
                conn.commit()
            self.assertEqual(noop["status"], "already_linked")
            self.assertEqual(db_path.read_bytes(), before_noop)

    def test_manual_block_and_dry_run_do_not_repair_polluted_source_album(self) -> None:
        for mode in ("manual_block", "dry_run"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                db_path = Path(directory) / f"invitation-{mode}.db"
                init_db(db_path)
                with connect(db_path) as conn:
                    seed_polluted_invitation(conn)
                    store.record_source_song_relation(
                        conn,
                        service="ytmusic",
                        source_song_id=INVITATION_SOURCE,
                        relation_type=ATV_RELATION,
                        related_service="ytmusic",
                        related_song_id=INVITATION_TARGET,
                        evidence_source="youtube_charts_weekly_browse_api",
                        reference_period="2026-W36",
                    )
                    if mode == "manual_block":
                        conn.execute(
                            """
                            INSERT INTO manual_overrides(
                                service, song_id, action, reason, updated_at
                            ) VALUES ('ytmusic', ?, 'block', 'qa fixture',
                                      '2026-09-07T00:00:00Z')
                            """,
                            (INVITATION_SOURCE,),
                        )
                    conn.commit()
                before = db_path.read_bytes()

                with connect(db_path, read_only=(mode == "dry_run")) as conn:
                    result = store.consolidate_source_song_relation(
                        conn,
                        service="ytmusic",
                        source_song_id=INVITATION_SOURCE,
                        relation_type=ATV_RELATION,
                        related_service="ytmusic",
                        related_song_id=INVITATION_TARGET,
                        source_row=invitation_source(),
                        related_row=invitation_target(),
                        dry_run=(mode == "dry_run"),
                    )
                    source_metadata = conn.execute(
                        "SELECT album_id, album_ko, album_en FROM track_list "
                        "WHERE service='ytmusic' AND song_id=?",
                        (INVITATION_SOURCE,),
                    ).fetchone()

                self.assertEqual(
                    result["status"],
                    # A read-only relationship without a typed decision cannot
                    # replace the already bound exact native recording.
                    "manual_blocked" if mode == "manual_block" else "preserved_canonical",
                )
                self.assertEqual(
                    tuple(source_metadata),
                    ("legacy-shaman-album-id", "샤먼 OST", "샤먼 OST"),
                )
                self.assertEqual(db_path.read_bytes(), before)

    def test_grls_consolidation_cleans_pollution_and_preserves_raw_ranks(self) -> None:
        record = getattr(store, "record_source_song_relation", None)
        consolidate = getattr(store, "consolidate_source_song_relation", None)
        self.assertIsNotNone(record)
        self.assertIsNotNone(consolidate, "safe relation consolidation is required")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "grls.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                record(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    evidence_source="youtube_charts",
                    reference_period="2026-W36",
                )
                first = consolidate(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    source_row={"title": "GRLS", "artist": "TUIDE", "album": ""},
                    canonical_decision=self.decision(conn, PHCI, F7, {"title": "GRLS", "artist": "TUIDE", "album": "TUNE & PLAY"}),
                )
                conn.commit()

                self.assertEqual(first["status"], "linked")
                bindings = dict(
                    conn.execute(
                        "SELECT song_id, track_uid FROM platform_song_ids WHERE service='ytmusic' AND song_id IN (?, ?)",
                        (PHCI, F7),
                    ).fetchall()
                )
                self.assertEqual(bindings, {PHCI: "uid-phci", F7: "uid-phci"})
                videos = {
                    row["video_id"]: (row["track_uid"], row["is_canonical"])
                    for row in conn.execute(
                        "SELECT * FROM yt_video_ids WHERE video_id IN (?, ?)",
                        (PHCI, F7),
                    )
                }
                self.assertEqual(videos, {PHCI: ("uid-phci", 0), F7: ("uid-phci", 1)})
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM metadata_lookup_index WHERE lookup_key = ?",
                        (compact_metadata_key("GRLS", "FKA twigs"),),
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT track_uid FROM metadata_lookup_index WHERE lookup_key = ?",
                        (compact_metadata_key("Two Weeks", "FKA twigs"),),
                    ).fetchone()[0],
                    "uid-real-fka",
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT reference_period, rank_order, song_id FROM playlist_order ORDER BY reference_period"
                        ).fetchall()
                    ],
                    [
                        ("2026-W33", 95, PHCI),
                        ("2026-W35", 62, PHCI),
                        ("2026-W36", 40, PHCI),
                    ],
                )

                second = consolidate(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    source_row={"title": "GRLS", "artist": "TUIDE", "album": ""},
                )
                self.assertEqual(second["status"], "already_linked")

    def test_manual_block_prevents_relation_consolidation(self) -> None:
        record = getattr(store, "record_source_song_relation", None)
        consolidate = getattr(store, "consolidate_source_song_relation", None)
        self.assertIsNotNone(record)
        self.assertIsNotNone(consolidate)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "blocked.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                conn.execute(
                    """
                    INSERT INTO manual_overrides(
                        service, song_id, action, reason, updated_at
                    ) VALUES ('ytmusic', ?, 'block', 'qa fixture', '2026-09-07T00:00:00Z')
                    """,
                    (PHCI,),
                )
                record(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    evidence_source="youtube_charts",
                    reference_period="2026-W36",
                )
                result = consolidate(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                )
                self.assertEqual(result["status"], "manual_blocked")
                bindings = dict(
                    conn.execute(
                        "SELECT song_id, track_uid FROM platform_song_ids WHERE service='ytmusic' AND song_id IN (?, ?)",
                        (PHCI, F7),
                    ).fetchall()
                )
                self.assertEqual(bindings, {PHCI: "uid-phci", F7: "uid-f7"})

    def test_related_side_manual_canonical_conflict_prevents_merge(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "related-override.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                conn.execute(
                    """
                    INSERT INTO manual_overrides(
                        service, song_id, action, canonical_yt_video_id,
                        reason, updated_at
                    ) VALUES ('ytmusic', ?, 'set_canonical', ?, 'qa fixture',
                              '2026-09-07T00:00:00Z')
                    """,
                    (F7, "otherVid123"),
                )
                store.record_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    evidence_source="youtube_charts",
                    reference_period="2026-W36",
                )
                result = store.consolidate_source_song_relation(
                    conn,
                    service="ytmusic",
                    source_song_id=PHCI,
                    relation_type=ATV_RELATION,
                    related_service="ytmusic",
                    related_song_id=F7,
                    source_row={"title": "GRLS", "artist": "TUIDE"},
                    related_row={"title": "GRLS", "artist": "TUIDE"},
                )

                self.assertEqual(result["status"], "manual_override_conflict")
                self.assertEqual(
                    dict(conn.execute(
                        "SELECT song_id, track_uid FROM platform_song_ids "
                        "WHERE service='ytmusic' AND song_id IN (?, ?)",
                        (PHCI, F7),
                    ).fetchall()),
                    {PHCI: "uid-phci", F7: "uid-f7"},
                )

    def test_manual_blocked_upserts_never_bind_or_create_a_video(self) -> None:
        source = {
            "rank": 40,
            "service": "ytmusic",
            "song_id": PHCI,
            "title": "GRLS",
            "artist": "TUIDE",
            "album": "TUNE & PLAY",
        }
        matched = {
            **source,
            "video_id": F7,
            "yt_title": "GRLS",
            "yt_artist": "TUIDE",
            "status": "matched",
            "score": 1.0,
        }
        for path_name in ("direct", "bulk"):
            with self.subTest(path=path_name), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                db_path = Path(directory) / f"blocked-{path_name}.db"
                init_db(db_path)
                with connect(db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO manual_overrides(
                            service, song_id, action, reason, updated_at
                        ) VALUES ('ytmusic', ?, 'block', 'qa fixture',
                                  '2026-09-07T00:00:00Z')
                        """,
                        (PHCI,),
                    )
                    if path_name == "direct":
                        blocked_uid = store.upsert_track_match(
                            conn,
                            service="ytmusic",
                            source_row=source,
                            match_row=matched,
                        )
                        conn.commit()
                    else:
                        store.persist_crawled_tracks(
                            db_path,
                            service="ytmusic",
                            job_name="Fixture-Blocked",
                            source_variant="default",
                            chart_date="2026-09-05",
                            reference_period="2026-W36",
                            tracks=[source],
                            conn=conn,
                            commit=False,
                        )
                        store.persist_crawl_run(
                            db_path,
                            service="ytmusic",
                            job_name="Fixture-Blocked",
                            chart_date="2026-09-05",
                            reference_period="2026-W36",
                            started_at="2026-09-07T00:00:00.000001+00:00",
                            tracks=[source],
                            matches=[matched],
                            conn=conn,
                            skip_playlist_order=True,
                        )
                        blocked_uid = conn.execute(
                            "SELECT track_uid FROM match_attempts WHERE song_id=?",
                            (PHCI,),
                        ).fetchone()[0]

                    blocked = conn.execute(
                        "SELECT canonical_yt_video_id, match_status FROM tracks "
                        "WHERE track_uid=?",
                        (blocked_uid,),
                    ).fetchone()
                    self.assertEqual(tuple(blocked), (None, "manual_blocked"))
                    self.assertIsNone(conn.execute(
                        "SELECT 1 FROM platform_song_ids "
                        "WHERE service='ytmusic' AND song_id=?",
                        (PHCI,),
                    ).fetchone())
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM yt_video_ids "
                            "WHERE video_id IN (?, ?)",
                            (PHCI, F7),
                        ).fetchone()[0],
                        0,
                    )

    def test_metadata_lookup_cannot_validate_itself_from_the_same_source_binding(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "self-cache.db"
            init_db(db_path)
            incoming = {
                "song_id": PHCI,
                "title": "GRLS",
                "artist": "TUIDE",
                "album": "TUNE & PLAY",
            }
            with connect(db_path) as conn:
                store.ensure_track(
                    conn,
                    track_uid="uid-self",
                    video_id=PHCI,
                    status="matched",
                    score=1.0,
                )
                store.upsert_track_list_metadata(
                    conn,
                    service="ytmusic",
                    song_id=PHCI,
                    track_uid="uid-self",
                    row={
                        "title_en": "GRLS",
                        "artist_en": "TUIDE",
                        "album_en": "TUNE & PLAY",
                    },
                )
                conn.execute(
                    "INSERT INTO metadata_lookup_index VALUES (?, 'uid-self', 'self', 1.0)",
                    (compact_metadata_key("GRLS", "TUIDE"),),
                )
                conn.commit()

                self.assertNotIn(
                    PHCI,
                    store.get_bulk_cached_matches(
                        conn, service="ytmusic", tracks=[incoming]
                    ),
                )

    def test_manual_canonical_alias_returns_the_exact_overridden_video(self) -> None:
        overridden_video = "override001"
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "manual-alias.db"
            init_db(db_path)
            with connect(db_path) as conn:
                ensure_uid = "uid-alias-target"
                store.ensure_track(
                    conn,
                    track_uid=ensure_uid,
                    video_id="canonical01",
                    yt_title="GRLS",
                    yt_artist="TUIDE",
                    status="matched",
                    score=1.0,
                )
                conn.execute(
                    "INSERT INTO yt_video_ids(video_id, track_uid, is_canonical) "
                    "VALUES (?, ?, 0)",
                    (overridden_video, ensure_uid),
                )
                conn.execute(
                    """
                    INSERT INTO manual_overrides(
                        service, song_id, action, canonical_yt_video_id,
                        reason, updated_at
                    ) VALUES ('ytmusic', ?, 'set_canonical', ?, 'qa fixture',
                              '2026-09-07T00:00:00Z')
                    """,
                    (PHCI, overridden_video),
                )
                conn.commit()

                cached = store.get_bulk_cached_matches(
                    conn,
                    service="ytmusic",
                    tracks=[{"song_id": PHCI, "title": "GRLS", "artist": "TUIDE"}],
                )[PHCI]
                self.assertEqual(cached["status"], "manual_override")
                self.assertEqual(cached["video_id"], overridden_video)
                self.assertEqual(cached["track_uid"], ensure_uid)
                self.assertEqual(
                    tuple(cached.get(key) or "" for key in ("yt_title", "yt_artist", "yt_album")),
                    ("", "", ""),
                    "An overridden alias cannot borrow metadata from a different canonical video",
                )


class ChartRelationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        from playlist_playability_fixture import PlaylistVerifier
        self.environment = patch.dict(os.environ, {"SUPABASE_DB_URL": ""})
        self.environment.start()
        # These cases inspect a replacement candidate after the original is
        # unavailable. Final source/old/target identity validation is separate.
        self.verifier = PlaylistVerifier(states={PHCI: "unavailable"},
                                        metadata={F7: {"title": "GRLS", "artist": "TUIDE"}})
        self.enterContext(patch("sync_validation.verifier_for", return_value=self.verifier))
        self.metadata = self.enterContext(patch("ytmusic_playlist_sync.get_verified_video_metadata", return_value={
            "video_id": F7, "title": "GRLS", "artist": "TUIDE", "album": "TUNE & PLAY",
            "music_video_type": "MUSIC_VIDEO_TYPE_ATV", **self.localized(),
        }))

    def tearDown(self) -> None:
        self.environment.stop()

    @staticmethod
    def entry() -> dict:
        return {
            "source": "youtube_charts_weekly_browse_api",
            "rank": 40,
            # The real caller normalizes effective_entries before preflight.
            "song_id": PHCI,
            "title": "GRLS",
            "artist": "TUIDE",
            "original_video_id": PHCI,
            "atv_external_video_id": F7,
            "original_title": "GRLS",
            "original_artist_or_channel": "TUIDE",
            "album": "TUNE & PLAY",
        }

    @staticmethod
    def verified_ytmusic() -> Mock:
        ytmusic = Mock()
        ytmusic.get_song.return_value = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "videoId": F7,
                "musicVideoType": "MUSIC_VIDEO_TYPE_ATV",
                "title": "GRLS",
                "author": "TUIDE",
                "lengthSeconds": "169",
            },
        }
        return ytmusic

    @staticmethod
    def localized() -> dict:
        return {
            "title_en": "GRLS",
            "artist_en": "TUIDE",
            "album_en": "TUNE & PLAY",
            "title_ko": "GRLS",
            "artist_ko": "튜이드",
            "album_ko": "TUNE & PLAY",
        }

    def test_unavailable_native_relation_observes_atv_without_rewriting_raw_chart_rank(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "preflight.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                with patch(
                    "ytmusic_to_ytmusic_crawl.resolve_bilingual_song",
                    return_value=self.localized(),
                ):
                    result = preflight_chart_relations(
                        conn,
                        [self.entry()],
                        self.verified_ytmusic(),
                        "2026-W36",
                    )
                conn.commit()

                self.assertEqual(result[PHCI]["video_id"], F7)
                self.assertEqual(
                    conn.execute(
                        "SELECT track_uid FROM platform_song_ids "
                        "WHERE service='ytmusic' AND song_id=?",
                        (PHCI,),
                    ).fetchone()[0],
                    "uid-phci",
                )
                self.assertEqual(
                    tuple(conn.execute(
                        "SELECT rank_order, song_id FROM playlist_order "
                        "WHERE reference_period='2026-W36'",
                    ).fetchone()),
                    (40, PHCI),
                )

    def test_healthy_native_relation_keeps_its_id_without_using_target_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "healthy-native-preflight.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                conn.commit()
                before = list(conn.iterdump())
                self.verifier.states[PHCI] = "playable"
                result = preflight_chart_relations(conn, [self.entry()], self.verified_ytmusic(), "2026-W36")
                self.assertEqual(result[PHCI]["video_id"], PHCI)
                self.metadata.assert_not_called()
                self.assertEqual(list(conn.iterdump()), before)

    def test_manual_block_is_terminal_before_atv_metadata_request(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "blocked-preflight.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                conn.execute(
                    """
                    INSERT INTO manual_overrides(
                        service, song_id, action, reason, updated_at
                    ) VALUES ('ytmusic', ?, 'block', 'qa fixture', '2026-09-07T00:00:00Z')
                    """,
                    (PHCI,),
                )
                ytmusic = self.verified_ytmusic()
                result = preflight_chart_relations(
                    conn, [self.entry()], ytmusic, "2026-W36"
                )

                self.assertEqual(result, {PHCI: {"status": "manual_blocked"}})
                ytmusic.get_song.assert_not_called()

    def test_atv_api_error_fails_closed_instead_of_using_old_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "api-error.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                ytmusic = Mock()
                ytmusic.get_song.side_effect = RuntimeError("provider unavailable")
                self.metadata.side_effect = RuntimeError("provider unavailable")

                with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                    preflight_chart_relations(
                        conn, [self.entry()], ytmusic, "2026-W36"
                    )

    def test_restricted_atv_identity_does_not_make_it_a_playback_candidate(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "premium-atv.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
                ytmusic = self.verified_ytmusic()
                ytmusic.get_song.return_value["playabilityStatus"] = {
                    "status": "UNPLAYABLE",
                    "reason": "This video is available with Music Premium",
                }
                self.verifier.states[F7] = "unknown"
                with patch(
                    "ytmusic_to_ytmusic_crawl.resolve_bilingual_song",
                    return_value=self.localized(),
                ):
                    with self.assertRaisesRegex(RuntimeError, "playback is uncertain"):
                        preflight_chart_relations(conn, [self.entry()], ytmusic, "2026-W36")

    def test_private_or_wrong_id_atv_metadata_is_rejected(self) -> None:
        cases = ("private", "wrong_id")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                db_path = Path(directory) / f"rejected-{case}.db"
                init_db(db_path)
                with connect(db_path) as conn:
                    seed_grls(conn)
                    ytmusic = self.verified_ytmusic()
                    if case == "private":
                        ytmusic.get_song.return_value["videoDetails"]["isPrivate"] = True
                    else:
                        ytmusic.get_song.return_value["videoDetails"]["videoId"] = PHCI
                    self.verifier.states[F7] = "unknown"
                    with patch(
                        "ytmusic_to_ytmusic_crawl.resolve_bilingual_song"
                    ) as resolve:
                        with self.assertRaisesRegex(RuntimeError, "playback is uncertain"):
                            preflight_chart_relations(conn, [self.entry()], ytmusic, "2026-W36")
                    resolve.assert_not_called()
                    self.assertEqual(
                        dict(conn.execute(
                            "SELECT song_id, track_uid FROM platform_song_ids "
                            "WHERE service='ytmusic' AND song_id IN (?, ?)",
                            (PHCI, F7),
                        ).fetchall()),
                        {PHCI: "uid-phci", F7: "uid-f7"},
                    )

    def test_dry_run_preflight_does_not_change_database_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            db_path = Path(directory) / "dry-preflight.db"
            init_db(db_path)
            with connect(db_path) as conn:
                seed_grls(conn)
            before = db_path.read_bytes()

            with connect(db_path, read_only=True) as conn, patch(
                "ytmusic_to_ytmusic_crawl.resolve_bilingual_song",
                return_value=self.localized(),
            ):
                result = preflight_chart_relations(
                    conn,
                    [self.entry()],
                    self.verified_ytmusic(),
                    "2026-W36",
                    dry_run=True,
                )

            self.assertEqual(result[PHCI]["video_id"], F7)
            self.assertEqual(db_path.read_bytes(), before)


class WeeklyChartRetryTests(unittest.TestCase):
    def setUp(self):
        from playlist_playability_fixture import PlaylistVerifier
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.verifier = PlaylistVerifier(metadata={"targetVid01": {"title": "Fixture Song", "artist": "Fixture Artist"}})
        self.enterContext(patch("sync_validation.verifier_for", return_value=self.verifier))
        self.enterContext(patch("ytmusic_playlist_sync.get_verified_video_metadata", return_value={
            "video_id": "targetVid01", "title": "Fixture Song", "artist": "Fixture Artist",
            "album": "Target Album", "music_video_type": "MUSIC_VIDEO_TYPE_ATV",
        }))

    @staticmethod
    def source_gate_args() -> SimpleNamespace:
        return SimpleNamespace(
            env_file="",
            yt_auth="",
            yt_oauth_client_id="",
            yt_oauth_client_secret="",
            yt_playlist_id="",
            db_path="unused.db",
            history_json="unused-history.json",
            source_playlist_url="unused-source",
            youtube_charts_url="unused-charts",
            youtube_charts_csv="fixture.csv",
            compare_csv=None,
            source_report_json=None,
            chart_period_start="",
            chart_period_end="",
            prefer_youtube_charts=True,
            use_source_playlist=False,
            job_name="Weekly-Hot-100",
            playlist_name="Weekly Hot 100",
            track_limit=100,
            search_limit=10,
            resolve_threshold=0.86,
            db_only=True,
            defer_publish=False,
            no_resolve=True,
            dry_run=False,
        )

    def test_unusable_chart_fails_but_verified_older_period_carries_forward(self) -> None:
        cases = (
            ("no_entries", [], "source_error", 1),
            (
                "missing_period",
                [{
                    "rank": 1,
                    "video_id": "sourceVid01",
                    "title": "Fixture Song",
                    "artist": "Fixture Artist",
                    "source": "youtube_charts_weekly_browse_api",
                }],
                "validation_failed",
                1,
            ),
            (
                "verified_older",
                [{
                    "rank": 1,
                    "video_id": "sourceVid01",
                    "title": "Fixture Song",
                    "artist": "Fixture Artist",
                    "source": "youtube_charts_weekly_browse_api",
                    "chart_period_start": "2026-08-23",
                    "chart_period_end": "2026-08-29",
                }],
                "stale_source",
                0,
            ),
        )
        for name, entries, expected_status, expected_exit in cases:
            with self.subTest(case=name):
                conn = Mock()

                @contextmanager
                def fake_connect(_path, *, read_only=False):
                    yield conn

                with (
                    patch.dict(os.environ, {"SUPABASE_DB_URL": ""}),
                    patch.object(
                        chart_crawl, "parse_args", return_value=self.source_gate_args()
                    ),
                    patch.object(chart_crawl, "load_dotenv"),
                    patch.object(
                        chart_crawl,
                        "extract_chart_entries_from_csv",
                        return_value=entries,
                    ),
                    patch.object(
                        chart_crawl,
                        "latest_ytmusic_reference_period",
                        return_value="2026-W36",
                    ),
                    patch.object(chart_crawl, "record_chart_source_audit") as audit,
                    patch("hype_db.connect", side_effect=fake_connect),
                    patch("hype_db.persist_crawled_tracks") as persist,
                ):
                    self.assertEqual(chart_crawl.main(), expected_exit)

                self.assertEqual(audit.call_args.kwargs["status"], expected_status)
                persist.assert_not_called()

    def _run_same_period_retry(self, *, cache_status="cached_match", relation_cache=None):
        args = SimpleNamespace(
            env_file="",
            yt_auth="unused-auth",
            yt_oauth_client_id="",
            yt_oauth_client_secret="",
            yt_playlist_id="target-playlist",
            db_path="unused.db",
            history_json="unused-history.json",
            source_playlist_url="unused-source",
            youtube_charts_url="unused-charts",
            youtube_charts_csv="fixture.csv",
            compare_csv=None,
            source_report_json=None,
            chart_period_start="2026-08-30",
            chart_period_end="2026-09-05",
            prefer_youtube_charts=True,
            use_source_playlist=False,
            job_name="Weekly-Hot-100",
            playlist_name="Weekly Hot 100",
            track_limit=1,
            search_limit=10,
            resolve_threshold=0.86,
            db_only=False,
            defer_publish=False,
            no_resolve=False,
            dry_run=False,
        )
        entries = [{
            "rank": 1,
            "video_id": "sourceVid01",
            "atv_external_video_id": "",
            "title": "Fixture Song",
            "artist": "Fixture Artist",
            "album": "",
            "source": "youtube_charts_weekly_browse_api",
            "chart_period_start": "2026-08-30",
            "chart_period_end": "2026-09-05",
        }]
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db_path = Path(directory) / "retry.db"
        init_db(db_path)
        conn = self.enterContext(connect(db_path))
        import hype_db_store as store
        store.ensure_track(conn, track_uid="retry-target", video_id="targetVid01", yt_title="Fixture Song",
                           yt_artist="Fixture Artist", yt_album="Target Album", status="matched", score=1)
        store.upsert_track_list_metadata(conn, service="ytmusic", song_id="sourceVid01", track_uid="retry-target",
                                         row={"title": "Fixture Song", "artist": "Fixture Artist"})
        if cache_status in {"manual_blocked", "manual_override"}:
            conn.execute("INSERT INTO manual_overrides(service,song_id,action,canonical_yt_video_id,updated_at) "
                         "VALUES ('ytmusic','sourceVid01',?,?, '2026-09-05T00:00:00Z')",
                         ("block" if cache_status == "manual_blocked" else "set_canonical",
                          None if cache_status == "manual_blocked" else "targetVid01"))
        conn.commit()

        @contextmanager
        def fake_connect(_path, *, read_only=False):
            yield conn

        publish = Mock()
        with (
            patch.dict(
                os.environ,
                {"SUPABASE_DB_URL": "", "HYPE_DEFER_HISTORY_EXPORT": "1"},
            ),
            patch.object(chart_crawl, "parse_args", return_value=args),
            patch.object(chart_crawl, "load_dotenv"),
            patch.object(chart_crawl, "make_ytmusic", return_value=Mock()),
            patch.object(
                chart_crawl,
                "extract_chart_entries_from_csv",
                return_value=entries,
            ),
            patch.object(
                chart_crawl,
                "latest_ytmusic_reference_period",
                return_value="2026-W36",
            ),
            patch.object(chart_crawl, "record_chart_source_audit"),
            patch.object(
                chart_crawl, "preflight_chart_relations", return_value=relation_cache or {}
            ),
            patch.object(chart_crawl, "update_ytmusic_playlist", publish),
            patch("hype_db.connect", side_effect=fake_connect),
            patch("hype_db.persist_crawled_tracks"),
            patch("hype_db.persist_crawl_run") as persist_run,
            patch("hype_db.export_frontend_history"),
            patch(
                "hype_db.get_bulk_cached_matches",
                return_value={
                    "sourceVid01": {
                        "video_id": "targetVid01",
                        "yt_title": "Fixture Song",
                        "yt_artist": "Fixture Artist",
                        "yt_album": "Target Album",
                        "status": cache_status,
                        "score": 1.0,
                    }
                },
            ),
            patch.object(chart_crawl.time, "sleep"),
        ):
            self.assertEqual(chart_crawl.main(), 0)

        publish.assert_called_once()
        return publish, persist_run.call_args.kwargs

    def test_same_period_retries_publish_without_copying_target_album_to_source(self) -> None:
        publish, persisted = self._run_same_period_retry()
        self.assertEqual(publish.call_args.args[2], ["targetVid01"])
        self.assertEqual(persisted["tracks"][0]["album"], "")
        self.assertEqual(persisted["matches"][0]["album"], "")
        self.assertEqual(persisted["matches"][0]["yt_album"], "Target Album")

    def test_fresh_manual_policy_is_not_overwritten_by_earlier_relation_cache(self) -> None:
        for status, expected_ids in (
            ("manual_blocked", []),
            ("manual_override", ["targetVid01"]),
        ):
            with self.subTest(status=status):
                publish, persisted = self._run_same_period_retry(
                    cache_status=status,
                    relation_cache={
                        "sourceVid01": {
                            "video_id": "staleRelation",
                            "status": "cached_match",
                            "score": 1.0,
                        }
                    },
                )
                self.assertEqual(publish.call_args.args[2], expected_ids)
                self.assertEqual(
                    persisted["matches"][0]["video_id"],
                    expected_ids[0] if expected_ids else "",
                )
                if status == "manual_blocked":
                    self.assertEqual(persisted["matches"][0]["status"], status)


if __name__ == "__main__":
    unittest.main()
