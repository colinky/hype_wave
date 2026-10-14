"""Artist profile layout changes cannot weaken exact-video performer identity."""
from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

import requests
import ytmusic_playlist_sync as sync


LEAD, TOPIC = "UC-lead", "UC-topic"


def header(kind, name="A2O LITE - 주제", channel=TOPIC):
    return {"header": {kind: {"title": {"runs": [{"text": name}]},
        "subscriptionButton": {"subscribeButtonRenderer": {"channelId": channel}}}},
        "contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"title": "음악"}}]}}}


class Client:
    def __init__(self, language="en"):
        self.language = language
        self.requests = []
        self.artist_error = None
        self.ids = [LEAD, TOPIC]

    def get_song(self, video_id):
        return {"videoDetails": {"videoId": video_id, "title": "NEON", "author": "Shared uploader",
                                 "lengthSeconds": "208", "musicVideoType": "MUSIC_VIDEO_TYPE_ATV"}}

    def get_watch_playlist(self, videoId):
        return {"tracks": [{"videoId": videoId, "title": "NEON", "album": {"name": "NEON"},
                            "artists": [{"id": artist_id, "name": name} for artist_id, name in zip(self.ids, ["A2O", "A2O LITE"])]}]}

    def get_artist(self, artist_id):
        if self.artist_error:
            raise self.artist_error
        if artist_id in (LEAD, "UC-different-lead"):
            return {"name": "A2O"}
        raise KeyError("musicImmersiveHeaderRenderer" if self.language == "ko" else "twoColumnBrowseResultsRenderer")

    def _send_request(self, endpoint, body):
        assert endpoint == "browse" and body == {"browseId": TOPIC}
        self.requests.append(deepcopy(body))
        return header("musicVisualHeaderRenderer") if self.language == "ko" else header("musicImmersiveHeaderRenderer", "A2O LITE")


class ArtistMetadataLayoutTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"HYPE_DB_BACKEND": "sqlite"}))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("psycopg2.connect", side_effect=AssertionError("production DB forbidden")))
        self.enterContext(sync.bilingual_cache_read_only())
        self.clients = {language: Client(language) for language in ("ko", "en")}
        self.enterContext(patch.object(sync, "make_ytmusic", side_effect=lambda _auth, language: self.clients[language]))

    def test_normal_artist_name_uses_existing_sdk_without_fallback(self):
        client = Mock()
        client.get_artist.return_value = {"name": " Artist "}
        self.assertEqual(sync._artist_name_for_metadata(client, LEAD), "Artist")
        client._send_request.assert_not_called()

    def test_topic_headers_require_same_channel_and_remove_only_terminal_label(self):
        for name, expected in (("A2O LITE - Topic", "A2O LITE"), ("A2O LITE - 주제", "A2O LITE"),
                               ("Topic Band", "Topic Band"), ("Artist - Topic Remix", "Artist - Topic Remix")):
            client = Mock()
            client.get_artist.side_effect = KeyError("musicImmersiveHeaderRenderer")
            client._send_request.return_value = header("musicVisualHeaderRenderer", name)
            self.assertEqual(sync._artist_name_for_metadata(client, TOPIC), expected)
            client._send_request.assert_called_once_with("browse", {"browseId": TOPIC})

    def test_wrong_or_missing_topic_channel_id_cannot_supply_a_verified_name(self):
        for channel in ("UC-foreign", None):
            client = Mock()
            client.get_artist.side_effect = KeyError("musicImmersiveHeaderRenderer")
            client._send_request.return_value = header("musicVisualHeaderRenderer", channel=channel)
            with self.assertRaisesRegex(ValueError, "requested artist ID"):
                sync._artist_name_for_metadata(client, TOPIC)

    def test_empty_music_tab_does_not_prevent_reading_immersive_header(self):
        client = Mock()
        client.get_artist.side_effect = KeyError("twoColumnBrowseResultsRenderer")
        client._send_request.return_value = header("musicImmersiveHeaderRenderer", "Artist", "UC-official-channel-alias")
        self.assertEqual(sync._artist_name_for_metadata(client, LEAD), "Artist")

    def test_missing_name_is_inconclusive_and_unknown_layout_still_raises(self):
        client = Mock()
        client.get_artist.side_effect = KeyError("original-parser-error")
        client._send_request.return_value = header("musicImmersiveHeaderRenderer", "")
        self.assertIsNone(sync._artist_name_for_metadata(client, TOPIC))
        for payload in ({}, {"error": {"code": 429}}, header("unknownHeaderRenderer")):
            client._send_request.return_value = payload
            with self.assertRaisesRegex(KeyError, "original-parser-error"):
                sync._artist_name_for_metadata(client, TOPIC)

    def test_http_and_timeout_errors_are_not_treated_as_layout_changes(self):
        for error in (requests.HTTPError("429"), TimeoutError("timeout")):
            client = Mock()
            client.get_artist.side_effect = error
            with self.assertRaises(type(error)):
                sync._artist_name_for_metadata(client, TOPIC)
            client._send_request.assert_not_called()

    def test_exact_bilingual_video_keeps_both_performer_ids_and_clean_names(self):
        memo = {}
        result = sync.get_verified_video_metadata(Client(), "video-one", metadata_cache=memo)
        self.assertEqual(result["artist_ids"], [LEAD, TOPIC])
        self.assertEqual((result["artist_ko"], result["artist_en"]), ("A2O, A2O LITE", "A2O, A2O LITE"))
        sync.get_verified_video_metadata(Client(), "video-two", metadata_cache=memo)
        self.assertTrue(all(len(client.requests) == 1 for client in self.clients.values()))

    def test_different_or_unlinked_locale_performer_is_still_inconclusive(self):
        for ids in ([LEAD, "UC-other"], [LEAD, None]):
            self.clients["en"].ids = ids
            self.assertIsNone(sync.get_verified_video_metadata(Client(), "video-one"))

    def test_same_names_with_different_performer_ids_do_not_authorize_substitution(self):
        original = Client.get_watch_playlist
        def tracks(client, videoId):
            rows = original(client, videoId)
            if videoId == "other-video":
                rows["tracks"][0]["artists"][0]["id"] = "UC-different-lead"
            return rows
        with patch.object(Client, "get_watch_playlist", tracks):
            result = sync._compare_playlist_video_ids(Client(), ["video-one"], ["other-video"])
        self.assertFalse(result["matches"])
        self.assertFalse(result["differences"][0]["checks"]["author"])
        self.assertEqual(result["differences"][0]["reason"], "author_mismatch")

    def test_artist_api_outage_still_propagates_through_verified_metadata(self):
        self.clients["ko"].artist_error = requests.HTTPError("503 unavailable")
        with self.assertRaisesRegex(RuntimeError, "503 unavailable"):
            sync.get_verified_video_metadata(Client(), "video-one")


if __name__ == "__main__":
    unittest.main()
