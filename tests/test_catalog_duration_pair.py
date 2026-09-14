"""Known official durations cannot disappear from a reviewed release decision."""
import copy
import json
from pathlib import Path
import unittest

import sync_validation as v

ROOT = Path(__file__).resolve().parents[1]
case = next(c for c in json.loads((ROOT / 'tests/fixtures/observed_release_identity.json').read_text())['cases'] if c['title'] == 'Blue')
fixture = {'source': case['sources'][0], 'metadata': case['metadata']}
base = json.loads((ROOT / 'matching_alias.json').read_text())
blue = next(p for p in base['recording_release_evidence'] if p['title'] == 'Blue')
base['recording_release_evidence'] = [p for p in base['recording_release_evidence'] if p['title'] != 'Blue']

def check(name, value):
    if not value:
        raise AssertionError(name)

def match(source=None, metadata=None, proof=None):
    policy = copy.deepcopy(base)
    policy['recording_release_evidence'].append(copy.deepcopy(blue if proof is None else proof))
    return v.source_recording_matches(fixture['source'] if source is None else source,
        fixture['metadata'] if metadata is None else metadata, policy=policy)

def fields(row, name, value):
    result = copy.deepcopy(row)
    for suffix in ('', '_ko', '_en'):
        result[name + suffix] = value
    return result

class BlueDurationTests(unittest.TestCase):
 def test_known_durations_are_consumed(self):
  check('source_no_duration_uses_known_catalog_pair',match())
  check('exact_known_source235_107',match(source={**fixture['source'],'length_seconds':235.107}))
  for value in (235.108,235.106,233,0,float('nan'),float('inf'),'not-duration'):
   check('wrong_provided_source:'+str(value),not match(source={**fixture['source'],'length_seconds':value}))
  proof=copy.deepcopy(blue);proof.pop('catalog_duration_pair')
  check('pair_removed_source_omits_duration_still_blocked',not match(proof=proof))
  check('pair_removed_known_source_still_blocked',not match(source={**fixture['source'],'length_seconds':235.107},proof=proof))
 def test_exact_pair_binding(self):
  for field,value in {'source_service':'spotify','source_song_id':'another-source','source_url':'https://music.apple.com/kr/song/unknown/123','source_duration_ms':235108,'recording_song_id':'another-standard','recording_reference_url':'https://music.apple.com/kr/song/unknown/456','recording_duration_ms':233098,'recording_video_id':'another-video'}.items():
   proof=copy.deepcopy(blue);proof['catalog_duration_pair'][field]=value
   check('pair_change:'+field,not match(proof=proof))
  for field in blue['catalog_duration_pair']:
   proof=copy.deepcopy(blue);proof['catalog_duration_pair'].pop(field)
   check('pair_missing:'+field,not match(proof=proof))
  for value in (None,{},[],0,'invalid'):
   proof=copy.deepcopy(blue);proof['catalog_duration_pair']=value
   check('malformed_pair:'+repr(value),not match(proof=proof))
 def test_references_are_required(self):
  for index in (0,1):
   for field,value in [('url','https://music.apple.com/kr/song/wrong/123'),('catalog_song_id','wrong'),('duration_ms',1),('isrc','WRONGISRC')]:
    proof=copy.deepcopy(blue);proof['references'][index][field]=value
    check('ref'+str(index)+':'+field,not match(proof=proof))
   proof=copy.deepcopy(blue);proof['references'].pop(index)
   check('ref_missing:'+str(index),not match(proof=proof))
  proof=copy.deepcopy(blue);proof['references'][1]['duration_ms']=240000;proof['catalog_duration_pair']['recording_duration_ms']=240000
  check('standard_to_exact_candidate_unchanged2s_bound',not match(proof=proof))
 def test_identity_and_exact_target_stay_strict(self):
  for field,value in [('title','Blue (Live)'),('title','Blue (Remix)'),('title','Blue (Instrumental)'),('title','Blue (feat. Unrelated)'),('artist','Another Performer'),('album','Another Special Edition')]:
   check('source_change:'+field+':'+value,not match(source=fields(fixture['source'],field,value)))
  check('another_source_id',not match(source={**fixture['source'],'song_id':'1337452975'}))
  check('another_source_service',not match(source={**fixture['source'],'service':'spotify'}))
  check('another_candidate_id',not match(metadata={**fixture['metadata'],'video_id':'another'}))
  for field,value in [('title','Blue (Live)'),('artist','Another Performer'),('album','Another Standard Album')]:
   check('target_change:'+field,not match(metadata=fields(fixture['metadata'],field,value)))
  for value in (232,234,233.099,235.107,None):
   check('target_exact_length:'+str(value),not match(metadata={**fixture['metadata'],'length_seconds':value}))
 def test_general_duration_rule_is_unchanged(self):
  left={'title':'Example','artist':'Someone','length_seconds':100}
  check('general_exact2s_allowed',v.recording_identity_matches(left,{**left,'length_seconds':102}))
  check('general_over2s_blocked',not v.recording_identity_matches(left,{**left,'length_seconds':102.001}))

if __name__ == '__main__':
    unittest.main()
