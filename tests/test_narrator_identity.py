"""Role-aware guest credits and exact regional catalog evidence."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import sync_validation as v
import ytmusic_playlist_sync as matching

ROOT = Path(__file__).resolve().parents[1]
policy = json.loads((ROOT / 'matching_alias.json').read_text())
fixture = json.loads((ROOT / 'tests/fixtures/observed_narrator_identity.json').read_text())
class NarratorIdentityTests(unittest.TestCase):
 def setUp(self):
  self.enterContext(patch.dict(os.environ,{'SUPABASE_DB_URL':''}))
  for name in ['socket.create_connection','socket.socket.connect','requests.sessions.Session.request','psycopg2.connect']:self.enterContext(patch(name,side_effect=AssertionError('No external access')))
  aliases=copy.deepcopy(matching.ALIASES.artist_map);self.addCleanup(lambda:setattr(matching.ALIASES,'artist_map',aliases))
  matching.ALIASES._build_map(policy['artists'],matching.ALIASES.artist_map)
 def row(self,title,artist='Lead'):return {'title':title,'artist':artist,'length_seconds':212}
 def test_current_narrator_translation_and_narration_spelling_pass(self):
  self.assertTrue(v.recording_identity_matches(self.row('Song (Narr. 기안84)'),self.row('Song (Narration Kian84)')))
 def test_other_narrator_or_role_does_not_pass(self):
  left=self.row('Song (Narr. Kian84)')
  for title in ['Song (Narr. Other)','Song (feat. Kian84)','Song (Narr. Kian85)','Song']:
   with self.subTest(title=title):self.assertFalse(v.recording_identity_matches(left,self.row(title)))
 def test_mixed_locale_role_and_swapped_guest_roles_reject(self):
  self.assertFalse(v.recording_identity_matches({**self.row('Song (Narr. Kian84)'),'title_en':'Song (feat. Kian84)','artist_en':'Lead'},self.row('Song (Narr. Kian84)')))
  self.assertFalse(v.recording_identity_matches(self.row('Song (feat. First) (Narr. Second)'),self.row('Song (Narr. First) (feat. Second)')))
 def test_old_missing_feature_artist_credit_contract_is_kept(self):
  self.assertTrue(v.recording_identity_matches(self.row('Song','Lead, Guest'),self.row('Song (feat. Guest)','Lead')))
  self.assertFalse(v.recording_identity_matches(self.row('Song','Lead, Guest'),self.row('Song (Narr. Guest)','Lead')))
 def test_player_may_omit_but_not_contradict_narrator_role(self):
  source=self.row('Song (Narr. Kian84)')
  self.assertTrue(v.recording_identity_matches(source,self.row('Song'),player=True))
  for title in ['Song (feat. Kian84)','Song (Narr. Other)']:
   self.assertFalse(v.recording_identity_matches(source,self.row(title),player=True))
 def test_missing_locale_narrator_is_rejected_but_credited_missing_feature_is_kept(self):
  good={**self.row('Song (Narr. Kian84)'),'title_en':'Song (Narr. Kian84)','artist_en':'Lead'}
  self.assertFalse(v.recording_identity_matches({**good,'title_en':'Song'},good))
  self.assertFalse(v.recording_identity_matches(good,{**good,'title_en':'Song'}))
  self.assertTrue(v.recording_identity_matches(good,{**good,'title_en':'Song'},player=True))
  source={**self.row('Song (feat. Guest)','Lead'),'title_en':'Song','artist_en':'Lead, Guest'}
  self.assertTrue(v.recording_identity_matches(source,self.row('Song (feat. Guest)','Lead')))
 def test_punctuation_delimiters_require_the_same_named_guest_and_role(self):
  for title in ['Song (Narr.Kian84)','Song (Narration: Kian84)','Song (Narr:Kian84)']:
   self.assertTrue(v.recording_identity_matches(self.row(title),self.row('Song (Narr. 기안84)')))
   self.assertFalse(v.recording_identity_matches(self.row(title),self.row('Song (Narr. Other)')))
   self.assertFalse(v.recording_identity_matches(self.row(title),self.row('Song (feat.Kian84)')))
  self.assertTrue(v.recording_identity_matches(self.row('Song (feat.Guest)'),self.row('Song (feat. Guest)')))
  self.assertFalse(v.recording_identity_matches(self.row('Song (feat.Guest)'),self.row('Song (feat.Other)')))
  self.assertEqual(v._guest_credits('Feather'),set())
  self.assertEqual(v._guest_credits('Narrative'),set())
  self.assertFalse(v.recording_identity_matches(self.row('Song (Narr.)'),self.row('Song (Narr.)')))
 def test_unrecognized_or_empty_named_credit_fails_closed(self):
  for left,right in [('Song (Narr. !!!)','Song'),('Song (feat. !!!)','Song'),('Song (Narr. Алексей)','Song (Narr. Борис)'),('Song (Narr. Kian84 & ???)','Song (Narr. Kian84)')]:
   with self.subTest(left=left,right=right):
    self.assertFalse(v.recording_identity_matches(self.row(left),self.row(right)))
 def test_exact_observed_two_ids_pass_only_through_narrow_proof(self):
  for source in fixture['sources']:
   before=copy.deepcopy(source)
   self.assertFalse(v.source_recording_matches(source,fixture['candidate'],policy={}))
   self.assertTrue(v.source_recording_matches(source,fixture['candidate'],policy=policy))
   self.assertEqual(source,before)
 def test_source_scope_change_rejects_and_source_is_not_rewritten(self):
  for source in fixture['sources']:
   for change in [{'song_id':'unreviewed'},{'album_en':'Unknown Album'},{'title_en':'I’m gonna TOESA (feat. Someone Else)'}]:
    self.assertFalse(v.source_recording_matches({**source,**change},fixture['candidate'],policy=policy))
 def test_exact_candidate_guard_blocks_metadata_and_id_drift(self):
  source=fixture['sources'][0]
  for changes in [{'video_id':'OtherVideo1'},{'title_en':'I’m gonna TOESA (Narr. Someone Else)'},{'title_en':'I’m gonna TOESA (feat. Kian84)'},{'length_seconds':None},{'length_seconds':250},{'album_en':'Unreviewed Album'}]:
   self.assertFalse(v.source_recording_matches(source,{**fixture['candidate'],**changes},policy=policy))
 def test_candidate_absence_cannot_enable_a_scoped_credit_proof(self):
  p=copy.deepcopy(policy)
  for key in ('apple:6802901371', 'apple:6804744599'):p['source_identity_evidence'][key]['candidate']={}
  for source in fixture['sources']:self.assertFalse(v.source_recording_matches(source,fixture['candidate'],policy=p))
if __name__=='__main__':unittest.main()
