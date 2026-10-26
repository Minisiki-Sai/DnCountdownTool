import test from 'node:test';
import assert from 'node:assert/strict';
import { VOICE_RULES, normalizeVoiceSettings, voiceRule, hpAnnouncement } from '../src/utils/voiceAnnouncements.ts';
import { voicePreviews, voiceGroups, toggleVoiceGroup } from '../src/utils/voicePreviews.ts';

test('boss groups are complete, ordered and group toggles preserve other nests', () => {
  const groups = voiceGroups('sea-nightmare');
  assert.deepEqual(groups.map(g => [g.name,g.rows.length]),[['狮蝎',5],['天启',6],['海龙',7]]);
  assert.equal(new Set(groups.flatMap(g => g.rows.map(r => r.key))).size,18);
  assert.ok(groups.every(g => g.rows.every(r => !r.label.startsWith(`${g.name} · `))));
  const keys = groups[0].rows.map(r => r.key);
  const enabled = toggleVoiceGroup(['classic-claw','sea-ice-bind'],keys,true);
  assert.deepEqual(toggleVoiceGroup(enabled,keys,false),['classic-claw','sea-ice-bind']);
  assert.deepEqual(toggleVoiceGroup(enabled,keys,true),enabled);
});

test('black and sand groups include every rule once and preserve phase labels and selection',()=>{
  const expected:Record<string,string[]> = {
    classic:['食人魔','牛头','惩罚者','千夫长','黑龙'],
    hard:['萝丝琳德','炎之使徒','雷之使徒','佩达'],
    'sand-hard':['陌生的少女','忠实的崇拜者卡基夫','峡谷的监视者阿卜杜拉','暴风支配者朱武','沙漠龙'],
  };
  for(const [nest,names] of Object.entries(expected)){
    const groups=voiceGroups(nest),keys=groups.flatMap(g=>g.rows.map(r=>r.key));
    assert.deepEqual(groups.map(g=>g.name),names);
    assert.equal(new Set(keys).size,keys.length);
    assert.deepEqual([...keys].sort(),VOICE_RULES.filter(r=>r.nest===nest).map(r=>r.key).sort());
    const selected=['sea-ice-bind',...keys];
    const groupKeys=groups[0].rows.map(r=>r.key);
    assert.deepEqual(toggleVoiceGroup(selected,groupKeys,false),selected.filter(k=>!groupKeys.includes(k)));
    assert.deepEqual(normalizeVoiceSettings({skills:selected,clawMerged:true,bombMerged:true}).skills,selected);
  }
  const hard=voiceGroups('hard');
  assert.equal(hard.find(g=>g.name==='炎之使徒')!.rows[0].key,'hard-roar');
  assert.equal(hard.find(g=>g.name==='雷之使徒')!.rows[0].key,'hard-slam');
  assert.ok(hard.find(g=>g.name==='佩达')!.rows.some(r=>r.key==='hard-claw'));
  assert.equal(hard.flatMap(g=>g.rows).find(r=>r.key==='hard-gravity')!.label,'二阶段 · 上升重力');
  assert.equal(voiceGroups('sand-hard').flatMap(g=>g.rows).find(r=>r.key==='sand-output')!.label,'一阶段 · 输出窗口');
  assert.ok(voiceGroups('classic').find(g=>g.name==='黑龙')!.rows.some(r=>r.key==='classic-claw'));
});

test('merged ice preference migrates both legacy switches and controls both triggers', () => {
  for (const skills of [['sea-hp-ice'],['sea-breath'],['sea-hp-ice','sea-breath']]) {
    const settings = normalizeVoiceSettings({enabled:true,skills});
    assert.deepEqual(settings.skills,['sea-breath']);
    assert.equal(hpAnnouncement(4237023,62,'梦魇海龙',settings),'马上拉冰');
    assert.equal(voiceRule(325430,'梦魇海龙',settings)?.key,'sea-breath');
  }
  const off = normalizeVoiceSettings({enabled:true,skills:[]});
  assert.equal(hpAnnouncement(4237023,62,'梦魇海龙',off),'');
  assert.equal(voiceRule(325431,'梦魇海龙',off),undefined);
});

test('every row has nonempty selectable speech including every ice and laser variant', () => {
  for (const rule of VOICE_RULES) {
    const options = voicePreviews(rule);
    assert.ok(options.length);
    assert.ok(options.every(o => o.label.includes(o.text) && o.text.length > 0));
    assert.equal(new Set(options.map(o => o.label)).size,options.length);
    if (rule.key === 'sea-breath') assert.equal(options.length,5);
    if (rule.key === 'sea-apocalypse-rotating-laser') assert.equal(options.length,3);
  }
});

test('phase three preview side labels match corrected live speech', () => {
  const options = voicePreviews(VOICE_RULES.find(r => r.key === 'sea-breath')!);
  assert.equal(options.find(o => o.label.startsWith('三阶段·左侧'))?.text, '注意拉冰，前往龙左侧安全区');
  assert.equal(options.find(o => o.label.startsWith('三阶段·右侧'))?.text, '注意拉冰，前往龙右侧安全区');
});

test('shadow explosion preview only contains the spoken phrase', () => {
  assert.deepEqual(voicePreviews(VOICE_RULES.find(r => r.key === 'sea-shadow-explosion')!),
    [{ label: '黑影即将爆炸', text: '黑影即将爆炸' }]);
});
