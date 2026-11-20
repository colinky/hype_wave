import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ytmusic_playlist_sync as matching
from artist_alias_candidates import extract_candidates


class VerifiedArtistTests(unittest.TestCase):
    def setUp(self):
        self.source = matching.SourceTrack(1, "Higher Love", "투모로우바이투게더", album="PERFECT STORM")
        self.credit = {"name": "TOMORROW X TOGETHER", "id": "UCdeb5b3bAxn1OXXFFeuCaHg"}
        self.song = {
            "resultType": "song", "videoId": "4FSvEWTOYGY", "title": self.source.title,
            "artists": [self.credit], "album": {"name": self.source.album},
        }

    def test_real_solo_group_is_one_artist_even_with_x_in_its_name(self):
        score = matching.score_result(self.source, self.song)
        self.assertEqual(score, (1.0, 1.0, 1.0, 1.0))
        with patch.dict(matching.ALIASES.solo_artists, clear=True):
            self.assertLess(matching.score_result(self.source, self.song)[0], 0.6)

    def test_alias_does_not_rescue_wrong_or_incomplete_credits_or_versions(self):
        for changes in (
            {"artists": [{**self.credit, "id": "another-channel"}]},
            {"artists": [{**self.credit, "id": None}]},
            {"artists": [self.credit, {"name": "Guest"}]},
            {"album_track_credits": None},
            {"album_track_credits": [self.credit, {"name": "Guest"}]},
            {"title": "Higher Love (feat. Guest)"},
            {"title": "Higher Love (Cover)"},
        ):
            with self.subTest(changes=changes):
                score = matching.score_result(self.source, {**self.song, **changes}, require_verified_artist=True)
                self.assertLess(score[0], 0.6)

    def test_unknown_young_b_and_collaborations_do_not_activate_the_rule(self):
        for name in ("Young B", "Panda Gomm, 양홍원", "TXT", "투모로우바이투게더 (feat. Guest)"):
            with self.subTest(name=name):
                self.assertIsNone(matching.ALIASES.solo_artist(matching.SourceTrack(1, "Song", name), None))
        young = matching.SourceTrack(1, "Thread", "Young B", service="apple", song_id="1573192794")
        self.assertIsNotNone(matching.ALIASES.solo_artist(young, None))
        self.assertIsNone(matching.ALIASES.solo_artist(young, matching.SourceTrack(1, "실", "Other")))

    def test_last_query_requires_a_verified_channel_even_with_an_exact_display_name(self):
        alias_query = "Higher Love TOMORROW X TOGETHER"
        for channel, expected in ((None, None), ("another-channel", None), (self.credit["id"], "4FSvEWTOYGY")):
            client = Mock()
            result = {**self.song, "artists": [{**self.credit, "id": channel}]}
            client.search.side_effect = lambda q, **kwargs: [result] if q == alias_query else []
            with self.subTest(channel=channel), patch.object(matching.time, "sleep"), \
                    patch.object(matching, "resolve_bilingual_song", return_value={}), \
                    patch.object(matching, "resolve_bilingual_artist", return_value=[]):
                match = matching.search_youtube_music(client, self.source, None, min_score=.6,
                                                      min_title_score=.65, min_artist_score=.55, limit=25)
                self.assertEqual(match.video_id, expected)
                queries = list(dict.fromkeys(call.args[0] for call in client.search.call_args_list))
                self.assertEqual(queries, matching.search_queries_for_track(self.source, None) + [alias_query])

    def test_reviewed_configuration_rejects_collisions_but_preserves_overrides(self):
        policy = json.loads((Path(matching.__file__).parent / "matching_alias.json").read_text())
        policy["verified_single_artists"].append(policy["verified_single_artists"][0])
        policy["overrides"] = {"Test|Artist": "manual"}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "policy.json"
            config.write_text(json.dumps(policy))
            with self.assertLogs(matching.LOG, level="WARNING"):
                aliases = matching.AliasManager(config)
        self.assertFalse(aliases.solo_artists)
        self.assertEqual(aliases.overrides["test|artist"], "manual")

    def test_later_search_can_supply_a_missing_channel_for_the_same_video(self):
        first = {**self.song, "artists": [{**self.credit, "id": None}]}
        def lookup(_client, query, _limit, **kwargs):
            return [self.song if query.endswith(self.credit["name"]) else first]

        with patch.object(matching, "search_ytmusic_songs", side_effect=lookup), \
                patch.object(matching, "resolve_bilingual_song", return_value={}), \
                patch.object(matching, "resolve_bilingual_artist", return_value=[]), patch.object(matching.time, "sleep"):
            result = matching.search_youtube_music(Mock(), self.source, None, min_score=.6,
                                                   min_title_score=.65, min_artist_score=.55, limit=25)
        self.assertEqual(result.video_id, self.song["videoId"])

    def test_database_candidates_are_review_only_and_preserve_punctuation(self):
        row = {"service": "apple", "song_id": "1", "title_ko": "곡", "title_en": "Song",
               "artist_ko": "가수", "artist_en": "Artist+"}
        groups = extract_candidates([row, {**row, "song_id": "2", "artist_en": "ARTIST+"},
                                    {**row, "song_id": "3", "artist_en": "Artist"}])
        self.assertEqual(sorted(len(g["observations"]) for g in groups), [1, 2])
        self.assertNotIn("artist+", matching.ALIASES.solo_artists)


if __name__ == "__main__":
    unittest.main()
