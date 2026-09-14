"""Exact Spotify source albums, without network or matching-cache provenance."""
import html
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
import spotify_to_ytmusic_crawl as spotify
from ytmusic_playlist_sync import SourceTrack

SID, OTHER, ALBUM = "40tqYMTWs6E1FKEDMTWRFB", "59QIYdXAL9XeNtM0j8vN0k", "3dUrT5Fo4xOTALDH7vX05C"


def page(*, song_id=SID, album="Official Album", kind="Song"):
    values = {"og:url": "https://open.spotify.com/track/" + song_id,
              "og:description": "Artist · " + album + " · " + kind + " · 2026",
              "music:album": "https://open.spotify.com/album/" + ALBUM}
    return "".join('<meta property="' + key + '" content="' + html.escape(value, quote=True) + '">' for key, value in values.items())


def response(body, *, url="https://open.spotify.com/track/" + SID):
    return Mock(text=body, content=body.encode(), url=url, raise_for_status=Mock())


def playlist(item):
    data = {"props": {"pageProps": {"state": {"data": {"entity": {"title": "Fixture", "trackList": [item]}}}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'


class SpotifyAlbumProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"SUPABASE_DB_URL": ""}))
        for name in ("socket.create_connection", "socket.socket.connect", "psycopg2.connect"):
            self.enterContext(patch(name, side_effect=AssertionError("External access forbidden")))

    def test_embed_album_requires_the_exact_track_and_ignores_unrelated_nested_releases(self):
        item = {"uri": "spotify:track:" + SID, "title": "Song",
                "artist": {"album": {"name": "Wrong artist release"}}, "albumArtUrl": "https://wrong.invalid/art"}
        data = {"other": {"id": OTHER, "album": {"name": "Other track album"}}}
        self.assertEqual(spotify.album_name_from_page_data(item, data, SID), "")
        item["album"] = {"name": "Exact Album"}
        self.assertEqual(spotify.album_name_from_page_data(item, data, SID), "Exact Album")
        self.assertEqual(spotify.album_name_from_page_data(item, data, OTHER), "Other track album")
        self.assertEqual(spotify.album_name_from_page_data({"albumName": "Unbound Album"}, {}, SID), "")

    def test_track_ids_are_exact_and_contradictory_identity_is_rejected(self):
        for raw in ("prefix" + SID, "https://evil.invalid/track/" + SID,
                    "https://open.spotify.com/track/" + SID + "/extra", "spotify:album:" + SID):
            self.assertFalse(spotify.mapping_has_track_id({"url": raw}, SID))
        self.assertFalse(spotify.mapping_has_track_id({"id": OTHER, "uri": "spotify:track:" + SID}, SID))
        self.assertTrue(spotify.mapping_has_track_id({"uri": "spotify:track:" + SID}, SID))
        self.assertTrue(spotify.mapping_has_track_id({"url": "https://open.spotify.com/intl-ko/track/" + SID + "?si=fixture"}, SID))

    def test_public_page_album_is_bound_to_track_url_and_song_description(self):
        self.assertEqual(spotify.official_spotify_album_from_html(page(album="Album & Version"), SID), "Album & Version")
        self.assertEqual(spotify.official_spotify_album_from_html(page(song_id=OTHER), SID), "")
        self.assertEqual(spotify.official_spotify_album_from_html(page(kind="Album"), SID), "")
        self.assertEqual(spotify.official_spotify_album_from_html('<div data-album="Unrelated Album"></div>', SID), "")
        contradictory = page(song_id=OTHER) + '<script type="application/json">' + json.dumps({"id": SID, "albumName": "Wrong page"}) + '</script>'
        self.assertEqual(spotify.official_spotify_album_from_html(contradictory, SID), "")

    def test_official_fetch_rejects_redirected_track_and_invalid_ids(self):
        with patch.object(spotify.http_session, "get", return_value=response(page(), url="https://open.spotify.com/track/" + OTHER)) as get:
            self.assertEqual(spotify.fetch_official_spotify_album(SID), "")
            get.assert_called_once()
        with patch.object(spotify.http_session, "get", side_effect=AssertionError("Invalid ID must not fetch")):
            for sid in ("", "bad", "https://evil.invalid/track/" + SID):
                self.assertEqual(spotify.fetch_official_spotify_album(sid), "")

    def test_official_unavailable_stays_empty_and_is_cached_only_for_this_run(self):
        cache = {}
        with patch.object(spotify.http_session, "get", side_effect=requests.HTTPError("unavailable")) as get:
            self.assertEqual(spotify.fetch_official_spotify_album(SID, album_cache=cache), "")
            self.assertEqual(spotify.fetch_official_spotify_album(SID, album_cache=cache), "")
            get.assert_called_once()
        with patch.object(spotify.http_session, "get", return_value=response(page())):
            self.assertEqual(spotify.fetch_official_spotify_album(SID, album_cache={}), "Official Album")

    def test_playlist_embed_album_is_preserved_without_extra_fetch_or_cache_substitution(self):
        item = {"uri": "spotify:track:" + SID, "title": "Song", "subtitle": "Artist", "albumName": "Exact Embed Album"}
        with patch.object(spotify.http_session, "get", return_value=response(playlist(item))) as get, \
                patch.object(spotify, "get_album_from_musicbrainz", side_effect=AssertionError("No MusicBrainz source enrichment")):
            _, _, tracks, source = spotify.fetch_spotify_tracks_scraped("fixture", use_musicbrainz=True,
                match_cache={"song|artist": {"album": "Poisoned Cache"}})
        self.assertEqual(tracks[0].album, "Exact Embed Album")
        self.assertEqual(source, "spotify_embed_scrape")
        get.assert_called_once()

    def test_us_and_kr_share_only_official_album_cache_and_never_reinject_matching_albums(self):
        item = {"uri": "spotify:track:" + SID, "title": "Song", "subtitle": "Artist"}
        calls, cache, collected = [], {}, []
        def get(url, **kwargs):
            calls.append(url)
            return response(playlist(item) if "/embed/playlist/" in url else page(), url=url)
        with patch.object(spotify.http_session, "get", side_effect=get), \
                patch.object(spotify, "get_album_from_musicbrainz", side_effect=AssertionError("No MusicBrainz source enrichment")):
            for market in ("US", "KR"):
                _, _, tracks, _ = spotify.fetch_spotify_tracks_scraped("fixture", market=market, official_album_cache=cache,
                    use_musicbrainz=True, match_cache={"song|artist": {"album": "Poisoned Cache"}})
                collected.extend(tracks)
        self.assertEqual([track.album for track in collected], ["Official Album", "Official Album"])
        self.assertEqual(sum("/embed/" not in url for url in calls), 1)
        blank = [SourceTrack(1, "Song", "Artist", service="spotify", song_id=SID, album="", locale=locale) for locale in ("en", "ko")]
        self.assertEqual(spotify.apply_spotify_album_cache(blank, {SID: {"album": "Wrong Source", "yt_album": "Wrong YT"}}), 0)
        with patch.object(spotify, "get_album_from_musicbrainz", side_effect=AssertionError("No MusicBrainz source enrichment")):
            self.assertEqual(spotify.enrich_spotify_albums_with_musicbrainz(blank, use_musicbrainz=True), 0)
        self.assertEqual([track.album for track in blank], ["", ""])


if __name__ == "__main__":
    unittest.main()
