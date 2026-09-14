"""Cached matches never suppress current Korean Spotify source observations."""
import os
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

import hype_db
from hype_db_schema import init_schema
import hype_db_store as store
import spotify_to_ytmusic_crawl as spotify
from ytmusic_playlist_sync import SourceTrack, localized_source_fields

SID = '40tqYMTWs6E1FKEDMTWRFB'


class SpotifyKoreanRefreshProposalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'SUPABASE_DB_URL': '', 'HYPE_DEFER_HISTORY_EXPORT': '1',
                                                'BYPASS_TRACK_COUNT_VAL': 'false'}))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No network')))
        self.enterContext(patch.object(spotify, 'load_dotenv'))
        self.enterContext(patch.object(spotify, 'make_ytmusic', return_value=Mock()))
        self.enterContext(patch.object(hype_db, 'get_expected_track_count', return_value=None))
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn, repair_source_bindings=False)
        self.addCleanup(self.conn.close)
        store.ensure_track(self.conn, track_uid='existing')
        store.upsert_track_list_metadata(self.conn, service='spotify', song_id=SID, track_uid='existing',
            row={'title_en': 'Song', 'artist_en': 'Artist', 'album_en': 'Old English Album',
                 'title_ko': '노래', 'artist_ko': '가수', 'album_ko': 'Old Korean Album'})
        self.conn.commit()
        self.cached = self.enterContext(patch.object(spotify, 'load_targeted_spotify_cache',
            return_value={SID: {'video_id': 'existing-video', 'album_ko': 'Old Korean Album'}}))
        self.calls, self.forwarded = [], []
        def persist_observed(**kwargs):
            self.forwarded.append(kwargs)
            rows = [{**vars(track), **localized_source_fields(track, kwargs['tracks_ko_map'].get(track.song_id))}
                    for track in kwargs['raw_tracks']]
            store.persist_crawled_tracks('unused.db', service='spotify', job_name='proposal-fixture',
                chart_date='2026-09-15', tracks=rows, conn=self.conn, commit=False)
            return ['existing-video']
        self.pipeline = self.enterContext(patch.object(spotify, 'process_matching_pipeline', side_effect=persist_observed))
        self.publisher = self.enterContext(patch.object(spotify, 'update_ytmusic_playlist'))
        with patch.object(sys, 'argv', ['proposal', '--spotify-playlist-urls', 'https://open.spotify.com/playlist/fixture',
              '--yt-auth', 'fixture', '--yt-playlist-id', 'fixture-target', '--job-name', 'proposal-fixture',
              '--playlist-name', 'Fixture', '--db-path', 'unused.db', '--defer-publish', '--spotify-track-limit', '1']):
            args = spotify.parse_args()
        self.enterContext(patch.object(spotify, 'parse_args', return_value=args))

    def fetch(self, url, *, market, official_album_cache, **kwargs):
        self.calls.append((market, official_album_cache))
        official_album_cache.setdefault(SID, 'Fresh Official Album')
        return ('Fixture', '', [SourceTrack(1, '노래' if market == 'KR' else 'Song',
            '가수' if market == 'KR' else 'Artist', service='spotify', song_id=SID,
            album=official_album_cache[SID], locale='ko' if market == 'KR' else 'en', source='spotify_embed_scrape')],
            'spotify_embed_scrape')

    def test_all_cached_still_collects_korean_and_persists_both_observed_albums(self):
        with patch.object(spotify, 'fetch_spotify_tracks_scraped', side_effect=self.fetch):
            self.assertEqual(spotify.main(), 0)
        self.assertEqual([call[0] for call in self.calls], ['US', 'KR'])
        self.assertIs(self.calls[0][1], self.calls[1][1])
        self.assertEqual(self.forwarded[0]['tracks_ko_map'][SID].album, 'Fresh Official Album')
        row = self.conn.execute('SELECT album_ko,album_en FROM track_list WHERE service=? AND song_id=?', ('spotify', SID)).fetchone()
        self.assertEqual(tuple(row), ('Fresh Official Album', 'Fresh Official Album'))
        self.publisher.assert_not_called()

    def test_korean_failure_is_optional_and_never_fabricates_a_korean_observation(self):
        def fetch(url, **kwargs):
            if kwargs['market'] == 'KR':
                self.calls.append(('KR', kwargs['official_album_cache']))
                raise RuntimeError('Fixture Korean endpoint unavailable')
            return self.fetch(url, **kwargs)
        with patch.object(spotify, 'fetch_spotify_tracks_scraped', side_effect=fetch):
            self.assertEqual(spotify.main(), 0)
        self.assertEqual([call[0] for call in self.calls], ['US', 'KR'])
        self.assertEqual(self.forwarded[0]['tracks_ko_map'], {})
        row = self.conn.execute('SELECT album_ko,album_en FROM track_list WHERE service=? AND song_id=?', ('spotify', SID)).fetchone()
        self.assertEqual(tuple(row), ('Old Korean Album', 'Fresh Official Album'))
        self.publisher.assert_not_called()


if __name__ == '__main__':
    unittest.main()
