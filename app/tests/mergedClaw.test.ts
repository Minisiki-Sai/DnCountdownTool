import test from 'node:test';
import assert from 'node:assert/strict';
import {VOICE_RULES,normalizeVoiceSettings,voiceModes,voiceRule} from '../src/utils/voiceAnnouncements.ts';
import {voiceGroups,voicePreviews} from '../src/utils/voicePreviews.ts';
import {ClassicMechanics} from '../src/utils/classicMechanics.ts';
import {HardMechanics} from '../src/utils/hardMechanics.ts';

test('one selectable claw row per nest with both modes and both previews',()=>{
  for(const [nest,key,old] of [['classic','classic-claw','classic-claw-ready'],['hard','hard-claw','hard-mark-ready']]){
    const rows=voiceGroups(nest).flatMap(g=>g.rows);
    assert.equal(rows.filter(r=>r.key===key).length,1);
    assert.ok(!rows.some(r=>r.key===old));
    const rule=VOICE_RULES.find(r=>r.key===key)!;
    assert.deepEqual(voiceModes(rule),{release:true,warning:true});
    assert.deepEqual(voicePreviews(rule).map(p=>p.text),['注意影舞者点名','点名已好']);
  }
});

test('all legacy selected and mode combinations retain effective release/warning choices',()=>{
  for(const [key,old] of [['classic-claw','classic-claw-ready'],['hard-claw','hard-mark-ready']]){
    for(const selectedStatus of [false,true])for(const selectedReady of [false,true])
    for(const release of [false,true])for(const warning of [false,true]){
      const raw={enabled:true,skills:['sea-ice-bind',...(selectedStatus?[key]:[]),...(selectedReady?[old]:[])],
        releaseSkills:release?[key]:[],warningSkills:warning?[old]:[]};
      const s=normalizeVoiceSettings(raw);
      assert.equal(s.skills.includes(key),(selectedStatus&&release)||(selectedReady&&warning));
      assert.equal(s.releaseSkills.includes(key),selectedStatus&&release);
      assert.equal(s.warningSkills.includes(key),selectedReady&&warning);
      assert.ok(!s.skills.includes(old));assert.ok(s.skills.includes('sea-ice-bind'));
      assert.deepEqual(normalizeVoiceSettings(JSON.parse(JSON.stringify(s))),s);
    }
    const legacy=normalizeVoiceSettings({skills:[old]});
    assert.ok(legacy.warningSkills.includes(key));assert.ok(!legacy.releaseSkills.includes(key));
  }
});

test('merged modes gate clocks independently; existing status skill routing remains',()=>{
  for(const [key,old,t,id,delay,nest,Clock] of [
    ['classic-claw','classic-claw-ready',503682,34849,57,'黑龙经典',ClassicMechanics],
    ['hard-claw','hard-mark-ready',4181009,734017,115,'黑龙硬核',HardMechanics],
  ] as const){
    const releaseOnly=normalizeVoiceSettings({enabled:true,skills:[key]});
    const warningOnly=normalizeVoiceSettings({enabled:true,skills:[old]});
    for(const [s,expected] of [[releaseOnly,false],[warningOnly,true]] as const){
      const clock=new Clock();clock.observe({source:'actor',templateId:t,skill:id,timestamp:0},nest,s);
      assert.equal(clock.due(delay*1000,s).includes('点名已好'),expected);
    }
    const both=normalizeVoiceSettings({enabled:true});
    assert.equal(voiceRule(t===503682?34849:734017,nest,both)?.key,key);
  }
});
