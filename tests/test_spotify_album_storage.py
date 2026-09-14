"""Fresh Spotify album observations survive raw and cached-match persistence."""
import os
import sqlite3
import unittest
from unittest.mock import patch

from hype_db_schema import init_schema
import hype_db_store as store


SID = "40tqYMTWs6E1FKEDMTWRFB"
SOURCE = {"rank": 1, "service": "spotify", "song_id": SID, "title": "Song",
          "artist": "Artist", "album": "", "locale": "en", "source": "spotify_embed_scrape"}


class SpotifyAlbumStorageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.addCleanup(self.conn.close)
        store.ensure_track(self.conn, track_uid="existing", video_id="existing-video",
                           yt_title="Song", yt_artist="Artist", yt_album="YouTube Display Album",
                           status="matched", score=1)
        store.upsert_track_list_metadata(self.conn, service="spotify", song_id=SID, track_uid="existing",
            row={"title_en": "Song", "artist_en": "Artist", "album_en": "Poisoned English Album",
                 "title_ko": "노래", "artist_ko": "가수", "album_ko": "Observed Korean Album"})
        self.conn.commit()

    def albums(self, service="spotify"):
        row = self.conn.execute("SELECT album_ko, album_en FROM track_list WHERE service=? AND song_id=?",
                                (service, SID)).fetchone()
        return tuple(row)

    def raw(self):
        store.persist_crawled_tracks("unused.db", service="spotify", job_name="album-provenance-fixture",
            chart_date="2026-09-14", tracks=[SOURCE], conn=self.conn, commit=False)

    def test_fresh_official_blank_clears_only_observed_locale_and_remains_caller_owned(self):
        before = "\n".join(self.conn.iterdump())
        self.raw()
        self.assertEqual(self.albums(), ("Observed Korean Album", ""))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual("\n".join(self.conn.iterdump()), before)

    def test_manual_partial_and_other_platform_updates_preserve_unspecified_albums(self):
        store.upsert_track_list_metadata(self.conn, service="spotify", song_id=SID, track_uid="existing",
                                         row={"album_en": ""})
        self.assertEqual(self.albums(), ("Observed Korean Album", "Poisoned English Album"))
        store.upsert_track_list_metadata(self.conn, service="apple", song_id=SID, track_uid="existing",
                                         row={"album_en": "Apple Album"})
        blank = {**SOURCE, "service": "apple"}
        store.upsert_track_list_metadata(self.conn, service="apple", song_id=SID, track_uid="existing",
                                         row=blank, source_album_row=blank)
        self.assertEqual(self.albums("apple"), ("", "Apple Album"))
        store.upsert_track_list_metadata(self.conn, service="spotify", song_id=SID, track_uid="existing",
                                         row={"title_en": "Updated Title"},
                                         source_album_row={"title_en": "Updated Title"})
        self.assertEqual(self.albums(), ("Observed Korean Album", "Poisoned English Album"))

    def test_single_and_bulk_cached_matches_cannot_reintroduce_display_albums(self):
        self.conn.execute("UPDATE track_list SET album_ko='' WHERE service='spotify' AND song_id=?", (SID,))
        self.conn.commit()
        match = {**SOURCE, "video_id": "existing-video", "status": "cached_match", "score": 1,
                 "yt_title": "Song", "yt_artist": "Artist", "yt_album": "YouTube Display Album",
                 "album": "Cached Display Album", "album_en": "Cached English Album",
                 "album_ko": "Cached Korean Album"}
        for mode in ("single", "bulk"):
            with self.subTest(mode=mode):
                self.raw()
                if mode == "single":
                    store.upsert_track_match(self.conn, service="spotify", source_row=SOURCE, match_row=match)
                else:
                    store.persist_crawl_run("unused.db", service="spotify", job_name="album-provenance-fixture",
                        chart_date="2026-09-14", started_at="20260914T160000Z", tracks=[SOURCE],
                        matches=[match], conn=self.conn, commit=False, skip_playlist_order=True)
                self.assertEqual(self.albums(), ("", ""))
                canonical = self.conn.execute("SELECT canonical_yt_video_id, yt_album FROM tracks WHERE track_uid='existing'").fetchone()
                self.assertEqual(tuple(canonical), ("existing-video", "YouTube Display Album"))
                self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
