import test from 'node:test';
import assert from 'node:assert/strict';
import {selectVoiceGroup,voiceGroupState,voiceGroups} from '../src/utils/voicePreviews.ts';
import {voiceModes,normalizeVoiceSettings} from '../src/utils/voiceAnnouncements.ts';
test('select all enables every supported mode for every Boss, including merged claw',()=>{
  for(const nest of ['classic','hard','sand-hard','sea-nightmare'])for(const group of voiceGroups(nest)){
    const keys=group.rows.map(r=>r.key);
    const initial={skills:['unrelated'],releaseSkills:['unrelated'],warningSkills:['unrelated']};
    const next=selectVoiceGroup(initial,keys,true);
    for(const r of group.rows){
      assert.ok(next.skills.includes(r.key));
      assert.equal(next.releaseSkills.includes(r.key),voiceModes(r).release);
      assert.equal(next.warningSkills.includes(r.key),voiceModes(r).warning);
    }
    assert.deepEqual(voiceGroupState(next,keys),{checked:true,indeterminate:false});
    assert.deepEqual(selectVoiceGroup(next,keys,true),next);
    assert.deepEqual(initial.skills,['unrelated']);
    const off=selectVoiceGroup(next,keys,false);
    assert.deepEqual(off.skills,['unrelated']);
    assert.deepEqual(off.releaseSkills,next.releaseSkills);
    assert.deepEqual(off.warningSkills,next.warningSkills);
    assert.deepEqual(voiceGroupState(off,keys),{checked:false,indeterminate:false});
  }
});
test('all main rows with missing submode are partial; select all repairs and survives saving',()=>{
  const keys=['classic-claw'];
  const s=normalizeVoiceSettings({enabled:true,clawMerged:true,skills:keys,releaseSkills:keys,warningSkills:[]});
  assert.deepEqual(voiceGroupState(s,keys),{checked:false,indeterminate:true});
  const all={...s,...selectVoiceGroup(s,keys,true)};
  assert.deepEqual(voiceGroupState(all,keys),{checked:true,indeterminate:false});
  assert.deepEqual(normalizeVoiceSettings(JSON.parse(JSON.stringify(all))),all);
  assert.deepEqual(voiceGroupState(all,[]),{checked:false,indeterminate:false});
});
