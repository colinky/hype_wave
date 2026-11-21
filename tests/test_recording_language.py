import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, call, patch

import crawler_common as crawler
import ytmusic_playlist_sync as sync


def song(video, title, artist, album):
    return {"videoId": video, "resultType": "song", "title": title,
            "artists": [{"name": artist}], "album": {"name": album}}


class RecordingLanguageTests(unittest.TestCase):
    def setUp(self):
        self.en = sync.SourceTrack(1, "The First Snow", "EXO", service="apple", song_id="854908536",
                                   album="Miracles in December - EP", locale="en")
        self.ko = sync.SourceTrack(1, "첫 눈", "EXO", album="12월의 기적 - EP", locale="ko")
        self.original = song("h0b4nw7NDhA", "첫 눈 The First Snow", "EXO", "12월의 기적 Miracles in December")
        self.chinese = song("ZrPE-8vPPRQ", "初雪 The First Snow", "EXO", "Miracles in December")
        self.client = Mock()
        self.enterContext(patch.object(sync.time, "sleep"))
        self.resolve = self.enterContext(patch.object(sync, "resolve_bilingual_song", return_value={}))

    def search(self, first, second=(), *, source=None, local=None):
        primary = source or self.en
        secondary = self.ko if source is None else local
        first_query = f"{primary.title} {primary.artist}"
        def respond(query, **options):
            if query == first_query:
                return first
            if isinstance(second, Exception):
                raise second
            return second
        self.client.search.reset_mock()
        self.client.search.side_effect = respond
        return sync.search_youtube_music(self.client, primary, secondary, min_score=.6,
                                         min_title_score=.65, min_artist_score=.55, limit=25)

    def test_snow_resolves_without_another_search_in_either_order(self):
        for first in ([self.chinese, self.original], [self.original, self.chinese], [self.original]):
            with self.subTest(first=first):
                self.assertEqual(self.search(first).video_id, self.original["videoId"])
                self.client.search.assert_called_once_with("The First Snow EXO", filter="songs", limit=25)

    def test_snow_conflict_uses_one_local_song_lookup_and_requires_positive_evidence(self):
        for second, expected in [
            ([self.original], self.original["videoId"]),
            ([{**self.original, "title": "첫 눈"}], self.original["videoId"]),
            ([], None), ([self.chinese], None),
            ([{**self.original, "artists": [{"name": "Someone Else"}]}], None),
            ([self.original, {**self.original, "videoId": "second-recording"}], None),
            ([{**self.original, "title": "The First Snow"}], None),
            ([{**self.original, "title": "첫 눈 Another Song"}], None),
            ([{**self.original, "resultType": "video"}], None),
        ]:
            with self.subTest(second=second):
                self.assertEqual(self.search([self.chinese], second).video_id, expected)
                self.assertEqual(self.client.search.call_args_list, [
                    call("The First Snow EXO", filter="songs", limit=25),
                    call("첫 눈 EXO", filter="songs", limit=25),
                ])

    def test_resolved_titles_are_available_even_if_search_only_displays_english(self):
        localized = {s["videoId"]: {"title_ko": s["title"]} for s in (self.original, self.chinese)}
        self.resolve.side_effect = lambda _, vid: localized[vid]
        first = [{**s, "title": "The First Snow"} for s in (self.chinese, self.original)]
        self.assertEqual(self.search(first).video_id, self.original["videoId"])
        self.assertEqual(self.client.search.call_count, 1)

    def test_explicit_wrong_edition_skips_mixed_fallback_and_confirms_original_once(self):
        original = song("original", "Darling", "D.O.", "Darling")
        english = song("english", "Darling (English Ver.)", "D.O.", "Darling")
        primary = sync.SourceTrack(1, "Darling", "D.O.", album="Darling")
        local = sync.SourceTrack(1, "달링", "도경수", album="Darling")
        self.assertEqual(self.search([english], [original], source=primary, local=local).video_id, "original")
        self.assertEqual(self.client.search.call_args_list, [
            call("Darling D.O.", filter="songs", limit=25), call("달링 도경수", filter="songs", limit=25)])
        self.assertIsNone(self.search([english], source=primary).video_id)
        self.client.search.assert_called_once_with("Darling D.O.", filter="songs", limit=25)

    def test_failed_language_lookup_preserves_the_search_error(self):
        with self.assertRaisesRegex(RuntimeError, "search incomplete"):
            self.search([self.chinese], TimeoutError("unavailable"))
        self.assertEqual(self.client.search.call_count, 2)

    def test_same_english_title_without_a_source_language_hint_stays_unresolved(self):
        result = self.search([self.chinese, self.original], source=self.en)
        self.assertIsNone(result.video_id)
        self.assertEqual(self.client.search.call_count, 1)

    def test_missing_translation_and_japanese_spelling_do_not_trigger_confirmation(self):
        for title, local, candidate, artist, source_album, yt_album, expected_calls in [
            ("Storm Is...", "지금까지와는 다른 무언가가 필요해 (Storm is...)", "Storm is...", "TOMORROW X TOGETHER",
             "PERFECT STORM - EP", "PERFECT STORM", 1),
            ("サムライハ-ト / Some Like It Hot!! Samurai Heart (애니메이션 `은혼` 엔딩 테마곡)",
             None, "サムライハート(Some Like It Hot!!)", "SPYAIR", "Rockin' The World", "Samurai Heart (Some Like It Hot!!)", 2),
        ]:
            with self.subTest(title=title):
                source = sync.SourceTrack(1, title, artist, album=source_album)
                secondary = sync.SourceTrack(1, local, artist, album=source_album) if local else None
                result = self.search([song("correct", candidate, artist, yt_album)], source=source, local=secondary)
                self.assertEqual(result.video_id, "correct")
                self.assertEqual(self.client.search.call_count, expected_calls)
                self.assertEqual({c.args[0] for c in self.client.search.call_args_list}, {f"{title} {artist}"})

    def test_version_aliases_and_other_editions(self):
        for label in ("English Ver.", "ENG Version", "영어 버전"):
            with self.subTest(label=label):
                source = sync.SourceTrack(1, "Darling (English Version)", "D.O.", album="Darling")
                correct = song("english", f"Darling ({label})", "D.O.", "Darling")
                wrong = song("japanese", "Darling (Japanese Version)", "D.O.", "Darling")
                self.assertEqual(self.search([wrong, correct], source=source).video_id, "english")
                self.assertEqual(self.client.search.call_count, 1)

    def test_cache_uses_service_policy_and_checks_conflicting_name_lookup_once(self):
        for origin, cached_song, expected_calls in (
            ("service_song_id", self.original, 0), ("manual", self.original, 0),
            ("metadata_lookup", self.original, 0), ("metadata_lookup", self.chinese, 1),
        ):
            with self.subTest(origin=origin, song=cached_song):
                self.client.search.reset_mock()
                self.client.search.return_value = [self.original]
                cached = {"video_id": cached_song["videoId"], "yt_title": cached_song["title"], "yt_artist": "EXO",
                          "cache_origin": origin, "status": "manual_override" if origin == "manual" else "cached_match"}
                with patch("hype_db.connect", return_value=nullcontext(Mock())), patch.object(
                    crawler, "load_verified_matching_cache", return_value={self.en.song_id: cached}
                ):
                    ids = crawler.process_matching_pipeline(
                        all_tracks=[self.en], tracks_ko_map={self.en.song_id: self.ko}, ytmusic=self.client,
                        db_path=Path("unused"), service="apple", job_name="KR-Top-Songs", update_date_str="2026-11-21",
                        started_at="2026-11-21", dry_run=True,
                    )
                self.assertEqual(ids, [self.original["videoId"]])
                self.assertEqual(self.client.search.call_count, expected_calls)
                if expected_calls:
                    self.client.search.assert_called_once_with("첫 눈 EXO", filter="songs", limit=25)
