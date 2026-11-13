"""Metadata persistence skips physical UPDATEs only when merged values are unchanged."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hype_db_schema import init_schema
import hype_db_store as store
import hype_db
import melon_to_ytmusic_crawl as melon


FIELDS = ("album_id", "title_ko", "artist_ko", "album_ko",
          "title_en", "artist_en", "album_en", "artwork_url")
MODES = ("single", "raw", "matched")


class MetadataNoopUpdateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn, repair_source_bindings=False)
        self.row = {
            "service": "apple", "song_id": "123", "rank": 1, "locale": "en",
            "album_id": "456", "title_ko": "노래", "artist_ko": "가수", "album_ko": "앨범",
            "title_en": "Song", "artist_en": "Artist", "album_en": "Album",
            "artwork_url": "https://example.test/art.jpg",
        }
        self.run_number = 0
        self.save("raw", self.row)
        self.save("matched", self.row)
        self.track_uid = store.find_track_by_service_song(self.conn, "apple", "123")
        self.conn.execute("CREATE TABLE update_audit(table_name TEXT)")
        for table in ("platform_song_ids", "track_list"):
            self.conn.execute(f"""CREATE TRIGGER audit_{table} AFTER UPDATE ON {table}
                BEGIN INSERT INTO update_audit VALUES ('{table}'); END""")
        self.conn.commit()

    def save(self, mode, row):
        if mode == "single":
            store.upsert_track_list_metadata(self.conn, service="apple", song_id="123",
                                             track_uid=self.track_uid, row=row)
            return
        kwargs = dict(service="apple", job_name="Fixture-Daily", chart_date="2026-11-09",
                      tracks=[row], conn=self.conn)
        if mode == "raw":
            store.persist_crawled_tracks("unused.db", **kwargs)
        else:
            self.run_number += 1
            match = {**row, "video_id": "fixture-video", "status": "matched", "score": 1,
                     "yt_title": "Song", "yt_artist": "Artist", "yt_album": "Album"}
            store.persist_crawl_run("unused.db", **kwargs, matches=[match],
                                    started_at=f"2026-11-09T00:00:{self.run_number:02d}+00:00",
                                    skip_playlist_order=True)

    def metadata(self):
        row = self.conn.execute("SELECT * FROM track_list WHERE service='apple' AND song_id='123'").fetchone()
        return {field: row[field] for field in FIELDS}

    def updates(self):
        return [row[0] for row in self.conn.execute("SELECT table_name FROM update_audit")]

    def test_same_and_blank_values_do_not_update_in_all_paths(self):
        before = self.metadata()
        for mode in MODES:
            for blank in (False, "", "   ", None):
                with self.subTest(mode=mode, blank=blank):
                    row = self.row if blank is False else {**self.row, **dict.fromkeys(FIELDS, blank)}
                    self.save(mode, row)
                    self.assertEqual(self.metadata(), before)
                    self.assertEqual(self.updates(), [])

    def test_each_changed_field_updates_in_all_paths(self):
        for mode in MODES:
            for field in FIELDS:
                with self.subTest(mode=mode, field=field):
                    before = self.metadata()
                    changed = {**self.row, field: self.row[field] + " changed"}
                    self.save(mode, changed)
                    self.assertEqual(self.metadata(), {**before, field: changed[field]})
                    self.assertEqual(self.updates(), ["track_list"])
                    self.conn.execute(f"UPDATE track_list SET {field}=?", (before[field],))
                    self.conn.execute("DELETE FROM update_audit")

    def test_null_values_are_preserved_until_nonempty_values_arrive(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("UPDATE track_list SET " + ", ".join(f"{field}=NULL" for field in FIELDS))
                self.conn.execute("DELETE FROM update_audit")
                self.save(mode, {**self.row, **dict.fromkeys(FIELDS, None)})
                self.assertEqual(self.metadata(), dict.fromkeys(FIELDS, None))
                self.assertEqual(self.updates(), [])
                self.save(mode, self.row)
                self.assertEqual(self.metadata(), {field: self.row[field] for field in FIELDS})
                self.assertEqual(self.updates(), ["track_list"])

    def test_binding_changes_and_metadata_only_option_are_preserved(self):
        store.ensure_track(self.conn, track_uid="other", video_id=None, yt_title="", yt_artist="",
                           yt_album="", status="unmatched", score=0)
        store.upsert_track_list_metadata(self.conn, service="apple", song_id="123",
            track_uid="other", row={"title_en": "New title"}, bind_source_id=False)
        self.assertEqual(store.find_track_by_service_song(self.conn, "apple", "123"), self.track_uid)
        self.assertEqual(self.metadata()["title_en"], "New title")
        self.assertEqual(self.updates(), ["track_list"])
        self.conn.execute("DELETE FROM update_audit")
        store.upsert_track_list_metadata(self.conn, service="apple", song_id="123",
                                         track_uid="other", row={})
        self.assertEqual(store.find_track_by_service_song(self.conn, "apple", "123"), "other")
        self.assertEqual(self.updates(), ["platform_song_ids"])
        self.conn.execute("DELETE FROM update_audit")
        self.save("matched", {**self.row, "title_en": "New title"})
        self.assertEqual(store.find_track_by_service_song(self.conn, "apple", "123"), self.track_uid)
        self.assertEqual(self.updates(), ["platform_song_ids"])

    def test_rank_and_collection_times_still_advance_with_unchanged_metadata(self):
        row = {**self.row, "rank": 2}
        for mode, now in (("raw", "2026-11-09T01:00:00+00:00"),
                          ("matched", "2026-11-09T02:00:00+00:00")):
            with self.subTest(mode=mode), patch.object(store, "utc_now_iso", return_value=now):
                self.save(mode, row)
                track = self.conn.execute("SELECT updated_at FROM tracks WHERE track_uid=?",
                                          (self.track_uid,)).fetchone()
                self.assertEqual(track[0], now)
                self.assertEqual(self.conn.execute("SELECT rank_order FROM playlist_order").fetchone()[0], 2)
                self.assertEqual(self.updates(), [])
        run = self.conn.execute("SELECT status, completed_at FROM match_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(run), ("completed", "2026-11-09T02:00:00+00:00"))


class AlbumCacheNoopUpdateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db_path = Path(directory) / "cache.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        init_schema(self.conn, repair_source_bindings=False)
        self.enterContext(patch.dict(melon._ALBUM_NAME_CACHE, {
            "1": {"name": "Album", "created_at": "2000-01-01", "last_checked": "2000-01-01"},
            "2": {"name": "Untouched", "created_at": "2000-01-01", "last_checked": "2000-01-01"},
        }, clear=True))
        melon.save_album_cache(self.db_path)
        self.conn.executescript("""
            CREATE TABLE cache_updates(album_id TEXT);
            CREATE TRIGGER audit_album_update AFTER UPDATE ON album_metadata
            BEGIN INSERT INTO cache_updates VALUES (NEW.album_id); END;
        """)

    def rows(self):
        return [tuple(row) for row in self.conn.execute("""
            SELECT album_id, album_name, created_at, last_checked
            FROM album_metadata WHERE service='melon' ORDER BY album_id
        """)]

    def updates(self):
        return [row[0] for row in self.conn.execute("SELECT album_id FROM cache_updates")]

    def clear_updates(self):
        self.conn.execute("DELETE FROM cache_updates")
        self.conn.commit()

    def test_reloaded_unchanged_cache_does_not_update(self):
        before = self.rows()
        self.assertEqual(len(before), 2)
        melon._ALBUM_NAME_CACHE.clear()
        melon.load_album_cache(self.db_path)
        melon.save_album_cache(self.db_path)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.updates(), [])

    def test_each_changed_value_updates_only_its_album(self):
        for key, value in (("name", "Renamed"), ("created_at", "2026-11-13"),
                           ("last_checked", "2026-11-14")):
            with self.subTest(key=key):
                before_other = self.rows()[1]
                melon._ALBUM_NAME_CACHE["1"][key] = value
                melon.save_album_cache(self.db_path)
                item = melon._ALBUM_NAME_CACHE["1"]
                self.assertEqual(self.rows()[0], ("1", item["name"], item["created_at"], item["last_checked"]))
                self.assertEqual(self.rows()[1], before_other)
                self.assertEqual(self.updates(), ["1"])
                self.clear_updates()

    def test_new_album_is_stored_without_updating_existing_albums(self):
        before = self.rows()
        melon._ALBUM_NAME_CACHE["3"] = {"name": "New", "created_at": "2026-11-13", "last_checked": "2026-11-13"}
        melon.save_album_cache(self.db_path)
        self.assertEqual(self.rows(), before + [("3", "New", "2026-11-13", "2026-11-13")])
        self.assertEqual(self.updates(), [])

    def test_null_and_empty_names_keep_existing_storage_semantics(self):
        melon._ALBUM_NAME_CACHE["1"]["name"] = None
        melon.save_album_cache(self.db_path)
        self.assertIsNone(self.rows()[0][1])
        self.clear_updates()
        melon.save_album_cache(self.db_path)
        self.assertEqual(self.updates(), [])
        melon._ALBUM_NAME_CACHE["1"]["name"] = ""
        melon.save_album_cache(self.db_path)
        self.assertEqual(self.rows()[0][1], "")
        self.assertEqual(self.updates(), ["1"])

    def test_missing_dates_still_use_the_current_collection_time(self):
        melon._ALBUM_NAME_CACHE["1"] = {"name": "Album"}
        for now in ("2026-11-13T00:00:00+00:00", "2026-11-13T01:00:00+00:00"):
            with self.subTest(now=now), patch.object(hype_db, "utc_now_iso", return_value=now):
                melon.save_album_cache(self.db_path)
                self.assertEqual(self.rows()[0][2:], (now, now))
                self.assertEqual(self.updates(), ["1"])
                self.clear_updates()
                melon.save_album_cache(self.db_path)
                self.assertEqual(self.updates(), [])

    def test_expired_cache_remains_available_during_fetch_failure(self):
        melon._ALBUM_NAME_CACHE.clear()
        melon.load_album_cache(self.db_path)
        with patch.object(melon.http_session, "get", side_effect=RuntimeError("Fixture outage")) as fetch:
            self.assertEqual(melon.fetch_melon_album_name("1", ttl_days=0), "Album")
            fetch.assert_called_once()
        melon.save_album_cache(self.db_path)
        self.assertEqual(self.rows()[0][1], "Album")
        self.assertEqual(self.rows()[0][3], melon._ALBUM_NAME_CACHE["1"]["last_checked"])


if __name__ == "__main__":
    unittest.main()
