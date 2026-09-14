"""Exact reviewed releases do not authorize different recordings or source inputs."""
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import unittest
from sync_validation import source_recording_matches, recording_identity_matches

ROOT=Path(__file__).parents[1]
CASES=json.loads((ROOT/'tests/fixtures/observed_release_identity.json').read_text())['cases']
POLICY=json.loads((ROOT/'matching_alias.json').read_text())

class ReleaseIdentityTests(unittest.TestCase):
    def test_default_display_fields_may_use_either_exact_observed_locale(self):
        self.assertEqual(len(CASES), 8)
        for case in CASES:
            for locales in product(("ko", "en"), repeat=3):
                metadata = {**case["metadata"], **{
                    field: case["metadata"][field + "_" + locale]
                    for field, locale in zip(("title", "artist", "album"), locales)}}
                for source in case["sources"]:
                    with self.subTest(title=case["title"], source=source["song_id"], locales=locales):
                        self.assertTrue(source_recording_matches(source, metadata, policy=POLICY))

    def test_release_exception_rejects_unobserved_default_or_changed_locale(self):
        for case in CASES:
            # Exercise the reviewed exception, rather than the ordinary match
            # path for a source that already names the standard release.
            source = next(row for row in case["sources"]
                          if not recording_identity_matches(row, case["metadata"]))
            for field in ("title", "artist", "album"):
                for key in (field, field + "_ko", field + "_en"):
                    for value in ("Unobserved recording", "", None):
                        with self.subTest(title=case["title"], field=key, value=value):
                            self.assertFalse(source_recording_matches(
                                source, {**case["metadata"], key: value}, policy=POLICY))

    def test_observed_originals_across_reviewed_release_editions(self):
        for case in CASES:
            for source in case['sources']:
                with self.subTest(title=case['title'],source=source['song_id']):
                    self.assertTrue(source_recording_matches(source,case['metadata'],policy=POLICY))

    def blocked_pair(self):
        case=next(c for c in CASES if c['title']=='Ice Cream')
        source=next(s for s in case['sources'] if s['service']=='spotify')
        self.assertFalse(recording_identity_matches(source,case['metadata']))
        return source,case['metadata']

    def test_no_generic_album_version_stripping(self):
        source,meta=self.blocked_pair()
        for changes in ({'song_id':'another-source'}, {'album_en':'Unreviewed Edition'},
                        {'title_en':'Ice Cream (Earl Grey Ver.)'}, {'artist_en':'Another Singer'},
                        {'length_seconds':114}):
            with self.subTest(changes=changes):
                self.assertFalse(source_recording_matches({**source,**changes},meta,policy=POLICY))

    def test_candidate_substitution_missing_or_changed_metadata_is_not_approved(self):
        source,meta=self.blocked_pair()
        for changes in ({'video_id':'other-video'}, {'album_ko':'Another Edition'},
                        {'title_en':'Ice Cream (Earl Grey Ver.)'}, {'title_ko':'Ice Cream (feat. Other)'},
                        {'title_en':'Ice Cream (Clean)'}, {'artist_en':'Another Singer'},
                        {'length_seconds':114}, {'length_seconds':None}, {'title_en':''}):
            with self.subTest(changes=changes):
                self.assertFalse(source_recording_matches(source,{**meta,**changes},policy=POLICY))

    def test_missing_or_contradictory_catalog_evidence_does_not_authorize_release(self):
        source,meta=self.blocked_pair()
        for mutate in (lambda p:p.update(references=[]), lambda p:p.pop('catalog_isrc'),
                       lambda p:p['references'][0].update(isrc='DIFFERENT'),
                       lambda p:p.update(source_variants=[])):
            policy=deepcopy(POLICY)
            proof=next(p for p in policy['recording_release_evidence'] if p['title']=='Ice Cream')
            mutate(proof)
            self.assertFalse(source_recording_matches(source,meta,policy=policy))

    def test_proof_cannot_suppress_conflicting_feature_rating_or_version_in_other_locale(self):
        source,meta=self.blocked_pair()
        for value in ('Ice Cream (feat. Another Singer)','Ice Cream (Explicit)','Ice Cream (Live)'):
            modified={**source,'title_ko':value}
            policy=deepcopy(POLICY)
            proof=next(p for p in policy['recording_release_evidence'] if p['title']=='Ice Cream')
            proof['source_variants'].append([value,source['artist_ko'],source['album_ko']])
            self.assertFalse(source_recording_matches(modified,meta,policy=policy))

if __name__=='__main__':unittest.main()
