import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeVoiceSettings} from '../src/utils/voiceAnnouncements.ts';
import {voiceGroups,voicePreviews,selectVoiceGroup} from '../src/utils/voicePreviews.ts';
import {ClassicMechanics} from '../src/utils/classicMechanics.ts';
test('bomb is a single row with two previews and old independent choices migrate',()=>{
  const rows=voiceGroups('classic').flatMap(g=>g.rows);
  const rule=rows.find(r=>r.key==='classic-bomb-add')!;
  assert.equal(rows.filter(r=>r.name.includes('爆炸兵')).length,1);
  assert.deepEqual(voicePreviews(rule).map(p=>p.text),['爆炸兵出现','所有爆炸兵消失']);
  for(const appear of [false,true])for(const disappear of [false,true]){
    const s=normalizeVoiceSettings({enabled:true,skills:[...(appear?['classic-red-add-spawn']:[]),...(disappear?['classic-red-add-exit']:[])]});
    assert.equal(s.skills.includes(rule.key),appear||disappear);
    assert.equal(s.releaseSkills.includes(rule.key),appear);
    assert.equal(s.warningSkills.includes(rule.key),disappear);
    assert.deepEqual(normalizeVoiceSettings(JSON.parse(JSON.stringify(s))),s);
    const c=new ClassicMechanics();
    c.observe({source:'boss',templateId:503682,skill:34876,timestamp:0},'黑龙经典',s);
    const born=c.observe({source:'bomb',templateId:503693,skill:0,kind:'monster_spawn',timestamp:1000},'黑龙经典',s);
    const gone=c.observe({source:'bomb',templateId:503693,skill:34928,kind:'classic_add_exit',timestamp:2000},'黑龙经典',s);
    assert.deepEqual(born,appear?['爆炸兵出现']:[]);
    assert.deepEqual(gone,disappear?['所有爆炸兵消失']:[]);
    const all=selectVoiceGroup(s,[rule.key],true);
    assert.ok(all.releaseSkills.includes(rule.key)&&all.warningSkills.includes(rule.key));
  }
});
test('disabled legacy modes remain disabled and claw migration still works',()=>{
  const s=normalizeVoiceSettings({skills:['classic-red-add-spawn','classic-red-add-exit'],releaseSkills:[]});
  assert.ok(!s.skills.includes('classic-bomb-add'));
  const mixed=normalizeVoiceSettings({skills:['classic-red-add-exit','classic-claw-ready','hard-mark-ready']});
  for(const key of ['classic-bomb-add','classic-claw','hard-claw'])assert.ok(mixed.warningSkills.includes(key));
  assert.deepEqual(normalizeVoiceSettings(mixed),mixed);
});
