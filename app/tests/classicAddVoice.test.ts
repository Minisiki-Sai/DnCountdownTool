import test from 'node:test';
import assert from 'node:assert/strict';
import { classicAddAnnouncement as text, normalizeVoiceSettings, CLASSIC_ADD_TEXTS } from '../src/utils/voiceAnnouncements.ts';

test('classic red add uses model-verified identity, not bishop or Puni', () => {
  const s = normalizeVoiceSettings({enabled:true});
  assert.equal(text('monster_spawn',503693,0,'黑龙经典',s),CLASSIC_ADD_TEXTS.red);
  assert.equal(text('monster_spawn',503691,0,'黑龙经典',s),CLASSIC_ADD_TEXTS.paladin);
  assert.equal(text('monster_spawn',503693,0,'黑龙3',s),CLASSIC_ADD_TEXTS.red);
  for (const id of [503689,503690,503695]) assert.equal(text('monster_spawn',id,0,'黑龙经典',s),'');
  for (const nest of ['黑龙硬核','梦魇海龙','']) assert.equal(text('monster_spawn',503693,0,nest,s),'');
});
test('normal retirement and early explosion are different observed actions, never spawn timers', () => {
  const s = normalizeVoiceSettings({enabled:true});
  assert.equal(text('classic_add_exit',503693,34928,'黑龙经典',s),CLASSIC_ADD_TEXTS.exit);
  assert.equal(text('classic_add_exit',503693,34926,'黑龙经典',s),CLASSIC_ADD_TEXTS.explosion);
  assert.equal(text('classic_add_exit',503693,34927,'黑龙经典',s),'');
  assert.equal(text('classic_add_exit',503689,34928,'黑龙经典',s),'');
  assert.equal(text('boss_hp_state',503693,34928,'黑龙经典',s),'');
  for (const raw of [{enabled:false},{enabled:true,skills:[]},{enabled:true,warningSkills:[]},{enabled:true,nests:[]}])
    assert.equal(text('classic_add_exit',503693,34928,'黑龙经典',normalizeVoiceSettings(raw)),'');
});
