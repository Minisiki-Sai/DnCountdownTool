import test from 'node:test';
import assert from 'node:assert/strict';
import { immediateMechanicText } from '../src/utils/voiceAnnouncements.ts';
import { hpAnnouncement } from '../src/utils/voiceAnnouncements.ts';
import { voiceModes } from '../src/utils/voiceAnnouncements.ts';

test('release and warning preferences are independent and old settings retain both', () => {
  const old = normalizeVoiceSettings({enabled:true});
  assert.deepEqual(old.releaseSkills, old.skills);
  assert.deepEqual(old.warningSkills, old.skills);
  const releaseOnly = normalizeVoiceSettings({enabled:true,warningSkills:[]});
  assert.equal(monsterAppearanceText('monster_spawn',4237036,'梦魇海龙',releaseOnly),'速击杀牛头怪');
  for (const threshold of [85,65,45,25]) assert.equal(hpAnnouncement(4237012,threshold,'梦魇海龙',releaseOnly),'');
  const warningOnly = normalizeVoiceSettings({enabled:true,releaseSkills:[]});
  assert.equal(monsterAppearanceText('monster_spawn',4237036,'梦魇海龙',warningOnly),'');
  for (const threshold of [85,65,45,25]) assert.equal(hpAnnouncement(4237012,threshold,'梦魇海龙',warningOnly),'牛头怪即将出现');
  for (const threshold of [68,58,48,38,28,18,8]) assert.equal(hpAnnouncement(4237023,threshold,'梦魇海龙',old),'');
  assert.equal(hpAnnouncement(4237012,85,'黑龙经典',old),'');
  const modes = voiceModes({key:'sea-hp-flight',nest:'sea-nightmare',name:'海龙 · 飞天预警',ids:[]});
  assert.deepEqual(modes,{release:false,warning:true});
  assert.deepEqual(normalizeVoiceSettings(JSON.parse(JSON.stringify(warningOnly))),warningOnly);
});
import { VOICE_RULES } from '../src/utils/voiceAnnouncements.ts';

test('both laser directions share selection, speech and one actor-scoped cooldown', () => {
  const settings = normalizeVoiceSettings({enabled:true,skills:['sea-apocalypse-rotating-laser']});
  assert.equal(voiceRule(50409,'梦魇海龙',settings)?.key,'sea-apocalypse-rotating-laser');
  assert.equal(seaMechanicText(50408),'注意旋转激光顺时针');
  assert.equal(seaMechanicText(50409),'注意旋转激光逆时针');
  assert.equal(seaMechanicText(50509),'');
  assert.equal(seaMechanicText(50409,true),'旋转激光已好');
  const clock = new SeaReminderClock();
  clock.started(50408,0,'boss');
  clock.started(50409,20000,'boss');
  assert.deepEqual(clock.due(95000),[]);
  assert.deepEqual(clock.due(114999),[]);
  assert.deepEqual(clock.due(115000),['旋转激光已好']);
  assert.deepEqual(clock.due(115100),[]);
  clock.started(50409,200000,'boss');
  clock.started(50408,220000,'boss');
  assert.deepEqual(clock.due(295000),[]);
  assert.deepEqual(clock.due(315000),['旋转激光已好']);
  clock.started(50409,400000,'boss');
  clock.health(4235447,0,410000,'boss');
  assert.deepEqual(clock.due(495000),[]);
});

test('nightmare announcements are grouped by boss with stable preference keys', () => {
  const rows = VOICE_RULES.filter(r => r.nest === 'sea-nightmare');
  assert.deepEqual(rows.map(r => r.name.split(' · ')[0]), [
    ...Array(5).fill('狮蝎'), ...Array(6).fill('天启'), ...Array(7).fill('海龙'),
  ]);
  assert.equal(rows.find(r => r.key === 'sea-breath')?.name, '海龙 · 拉冰');
  assert.equal(rows.find(r => r.key === 'sea-hp-flight')?.name, '海龙 · 飞天预警');
  assert.equal(new Set(rows.map(r => r.key)).size, rows.length);
  const saved = normalizeVoiceSettings({skills:['sea-minotaur','sea-hp-ice']});
  assert.deepEqual(saved.skills, ['sea-minotaur','sea-breath']);
});

test('HP shortening uses cast time and immediately reminds once, scoped to actor', () => {
  for (const [skill,template,hp,text] of [[50456,4235447,19,'召唤触手已好'],
    [50411,4235447,19,'六芒星已好'],[325456,4237023,30,'冰捆已好']] as const) {
    const clock = new SeaReminderClock();
    clock.started(skill,300000,'a'); clock.started(skill,300000,'b');
    clock.health(template,hp,365000,'a');
    assert.deepEqual(clock.due(365000),[text]);
    clock.health(template,hp,365100,'a');
    assert.deepEqual(clock.due(365100),[]);
    assert.deepEqual(clock.due(skill === 325456 ? 415000 : 375000),[text]);
    clock.started(skill,500000,'a');
    assert.deepEqual(clock.due(555000),[text]);
  }
});

test('ground laser variants share timer and tentacles have 60-percent selectable notice', () => {
  const settings = normalizeVoiceSettings({enabled:true});
  const clock = new SeaReminderClock();
  for (const skill of [50457,50458,50459]) {
    assert.equal(voiceRule(skill,'梦魇海龙',settings)?.key,'sea-apocalypse-ground-laser');
    assert.equal(seaMechanicText(skill),'注意地面激光');
    clock.started(skill,1000,'a');
  }
  assert.deepEqual(clock.due(36000),['地面激光已好']);
  assert.deepEqual(clock.due(36100),[]);
  assert.equal(hpAnnouncement(4235447,60,'梦魇海龙',settings),'召唤触手已好');
  assert.equal(hpAnnouncement(4235447,60,'梦魇海龙',normalizeVoiceSettings({enabled:true,skills:[]})),'');
  clock.health(4235447,20,100000,'a'); clock.started(50456,100000,'a');
  assert.deepEqual(clock.due(155000),[]);
  clock.health(4235447,19.99,160000,'a');
  assert.deepEqual(clock.due(160000),['召唤触手已好']);
});

test('1018 ice adept Black Dragon 3 status announcements survive nest filtering', () => {
  const settings = normalizeVoiceSettings({enabled:true, nests:['classic'], skills:['classic-claw']});
  // Replay confirmed these events before the frontend rejected the exact map label.
  const rows = [
    {skill:34849, timestamp:451746, job:'月之领主'},
    {skill:34849, timestamp:530558, job:'影舞者'},
  ];
  assert.deepEqual(rows.filter(r => voiceRule(r.skill,'黑龙巢穴（三）',settings))
    .map(r => `注意${r.job}点名`), ['注意月之领主点名','注意影舞者点名']);
  for (const nest of ['黑龙巢穴（一）','黑龙巢穴（二）','黑龙巢穴（三）','黑龙3','黑龙123','黑龙巢穴(三)','黑龙巢穴（三）（练习模式）'])
    assert.equal(voiceRule(34849,nest,settings)?.key,'classic-claw',nest);
  for (const nest of ['黑龙未知模式','沙龙巢穴（三）','黑龙硬核','梦魇海龙'])
    assert.equal(voiceRule(34849,nest,settings),undefined,nest);
  assert.equal(voiceRule(34849,'黑龙巢穴（三）',{...settings,skills:[]}),undefined);
  assert.equal(voiceRule(34849,'黑龙巢穴（三）',{...settings,enabled:false}),undefined);
  assert.equal(voiceRule(734017,'黑龙巢穴（三）',normalizeVoiceSettings({enabled:true})),undefined);
});

test('all requested HP thresholds are template-scoped and selectable', () => {
  const s=normalizeVoiceSettings({enabled:true});
  for (const t of [62,52,42,32,22]) assert.equal(hpAnnouncement(4237023,t,'梦魇海龙',s),'马上拉冰');
  for (const t of [66,56]) assert.equal(hpAnnouncement(4237023,t,'梦魇海龙',s),'马上飞天、注意躲避龙息消灭暗影');
  for (const t of [46,36,26,16,6]) assert.equal(hpAnnouncement(4237023,t,'梦魇海龙',s),'马上飞天、注意上炮');
  assert.equal(hpAnnouncement(4237023,61,'梦魇海龙',s),'冰捆已好');
  assert.equal(hpAnnouncement(4235447,42,'梦魇海龙',s),'六芒星已好');
  assert.equal(hpAnnouncement(4237003,62,'梦魇海龙',s),'');
  assert.equal(hpAnnouncement(4237023,62,'黑龙经典',s),'');
  assert.equal(hpAnnouncement(4237023,62,'梦魇海龙',{...s,skills:[]}),'');
});

test('low HP reschedules from original cast and reminds once', () => {
  const clock=new SeaReminderClock();
  clock.started(50411,0);
  clock.health(4235447,20,40000);
  assert.deepEqual(clock.due(55000),[]);
  clock.health(4235447,19,60000);
  assert.deepEqual(clock.due(60000),['六芒星已好']);
  assert.deepEqual(clock.due(75000),[]);
  clock.started(325456,100000);
  clock.health(4237023,30,120000);
  assert.deepEqual(clock.due(154999),[]);
  assert.deepEqual(clock.due(155000),['冰捆已好']);
  assert.deepEqual(clock.due(215000),[]);
});

test('new ice bind normal countdown, death and reset', () => {
  const clock=new SeaReminderClock();
  assert.equal(seaMechanicText(325456),'注意冰捆');
  clock.started(325456,0);
  assert.deepEqual(clock.due(114999),[]);
  assert.deepEqual(clock.due(115000),['冰捆已好']);
  clock.started(325456,200000);
  clock.health(4237023,0,210000);
  assert.deepEqual(clock.due(315000),[]);
  clock.health(4237023,29,320000);
  clock.clear(); clock.started(325456,0);
  assert.deepEqual(clock.due(55000),[]);
});

test('guardian announcements are removed including saved settings', () => {
  const s = normalizeVoiceSettings({ enabled: true });
  assert.equal(voiceRule(34023, '守卫者巢穴', s), undefined);
  assert.equal(voiceRule(34023, '守卫者巢穴（练习模式）', s), undefined);
  assert.equal(immediateMechanicText(34023), '');
  assert.deepEqual(normalizeVoiceSettings({nests:['guardian'],skills:['guardian-poison']}).nests, []);
  assert.equal(immediateMechanicText(0), '');
  for (const nest of ['黑龙经典', '梦魇海龙', '']) assert.equal(voiceRule(34023, nest, s), undefined);
  assert.equal(voiceRule(34023, '守卫者巢穴', {...s, skills: []}), undefined);
  assert.equal(voiceRule(34023, '守卫者巢穴', {...s, nests: []}), undefined);
  assert.equal(voiceRule(34023, '守卫者巢穴', {...s, enabled: false}), undefined);
  const clock = new SeaReminderClock();
  clock.started(34023, 0);
  clock.ended(34023, 1000);
  assert.deepEqual(clock.due(60000), []);
});
test('apocalypse reminders anchor to release, not release end', () => {
  const settings = normalizeVoiceSettings({enabled:true});
  for (const [skill, delay, text, upcoming, name] of [
    [50411, 75000, '注意天空', '六芒星已好', '天启 · 六芒星'],
    [50460, 85000, '注意连续追踪', '连续追踪已好', '天启 · 连续追踪'],
    [50408, 95000, '注意旋转激光顺时针', '旋转激光已好', '天启 · 旋转激光'],
    [50407, 75000, '注意旋转', '旋转已好', '天启 · 旋转'],
  ] as const) {
    assert.equal(voiceRule(skill, '梦魇海龙', settings)?.name, name);
    assert.equal(voiceRule(skill, '黑龙经典', settings), undefined);
    assert.equal(seaMechanicText(skill), text);
    const clock = new SeaReminderClock();
    clock.ended(skill, 10000);
    assert.deepEqual(clock.due(10000 + delay), []);
    clock.started(skill, 10000);
    clock.ended(skill, 30000);
    assert.deepEqual(clock.due(10000 + delay - 1), []);
    assert.deepEqual(clock.due(10000 + delay), [upcoming]);
    assert.deepEqual(clock.due(10000 + delay + 1), []);
    clock.started(skill, 200000);
    clock.started(skill, 210000);
    assert.deepEqual(clock.due(200000 + delay), []);
    assert.deepEqual(clock.due(210000 + delay), [upcoming]);
  }
});
import { voiceSpeechText } from '../src/utils/voiceAnnouncements.ts';

test('slam pronunciation is corrected only for speech, including combined reminders', () => {
  assert.equal(voiceSpeechText('注意砸地'), '注意砸帝');
  assert.equal(voiceSpeechText('砸地已好，旋风已好'), '砸帝已好，旋风已好');
  assert.equal(seaMechanicText(50311), '注意砸地');
  assert.equal(seaMechanicText(50311, true), '砸地已好');
  assert.equal(voiceSpeechText('跳'), '跳');
});
import { normalizeVoiceSettings, voiceRule, seaBreathText, seaMechanicText, SeaReminderClock, monsterAppearanceText } from '../src/utils/voiceAnnouncements.ts';

test('minotaur requires creation evidence, exact template and selected nightmare nest', () => {
  const settings = normalizeVoiceSettings({ enabled: true });
  assert.equal(monsterAppearanceText('monster_spawn', 4237036, '梦魇海龙', settings), '速击杀牛头怪');
  assert.equal(monsterAppearanceText(undefined, 4237036, '梦魇海龙', settings), '');
  assert.equal(monsterAppearanceText('monster_spawn', 4237023, '梦魇海龙', settings), '');
  assert.equal(monsterAppearanceText('monster_spawn', 4237036, '黑龙经典', settings), '');
  settings.enabled = false;
  assert.equal(monsterAppearanceText('monster_spawn', 4237036, '梦魇海龙', settings), '');
});
test('flight slam says jump without arming a cooldown reminder', () => {
  const settings = normalizeVoiceSettings({enabled:true});
  assert.equal(voiceRule(325423, '梦魇海龙', settings)?.name, '狮蝎 · 飞天砸');
  assert.equal(voiceRule(325423, '黑龙硬核', settings), undefined);
  assert.equal(seaMechanicText(325423), '跳');
  assert.equal(seaMechanicText(325423, true), '');
  const clock = new SeaReminderClock();
  clock.ended(325423, 10000);
  assert.deepEqual(clock.due(10000), []);
  assert.deepEqual(clock.due(999999), []);
});
test('manticore per-skill reminders use release and exact requested text', () => {
  const settings = normalizeVoiceSettings({enabled:true});
  const clock = new SeaReminderClock();
  const cases = [
    [50318, '注意飞天', '马上起飞', 85000, '狮蝎 · 飞天'],
    [50317, '注意旋风', '旋风已好', 55000, '狮蝎 · 旋风'],
    [50311, '注意砸地', '砸地已好', 55000, '狮蝎 · 砸地'],
    [50313, '注意黑洞', '黑洞已好', 55000, '狮蝎 · 黑洞'],
  ] as const;
  for (const [skill, text, upcoming, delay, name] of cases) {
    assert.equal(seaMechanicText(skill), text);
    assert.equal(voiceRule(skill, '梦魇海龙', settings)?.name, name);
    assert.equal(voiceRule(skill, '海龙经典', settings), undefined);
    clock.started(skill);
    assert.deepEqual(clock.due(999999), []);
    clock.started(skill, 10000);
    assert.deepEqual(clock.due(10000 + delay - 1), []);
    assert.deepEqual(clock.due(10000 + delay), [upcoming]);
    assert.deepEqual(clock.due(10000 + delay + 1), []);
    clock.ended(skill, 10000);
    clock.started(skill);
    assert.deepEqual(clock.due(10000 + delay), []);
  }
});
test('sea release reminders: breath fifty seconds, ice eye fifty-five', () => {
  const clock = new SeaReminderClock();
  clock.started(325450);
  assert.deepEqual(clock.due(99999), []);
  clock.started(325450, 10000);
  clock.started(325447, 10000);
  assert.deepEqual(clock.due(59999), []);
  assert.deepEqual(clock.due(60000), ['即将龙息']);
  assert.deepEqual(clock.due(61000), []);
  assert.deepEqual(clock.due(64999), []);
  assert.deepEqual(clock.due(65000), ['即将冰眼']);
  clock.ended(325450, 10000); clock.started(325450);
  assert.deepEqual(clock.due(60000), []);
  clock.started(325447, 10000); clock.clear();
  assert.deepEqual(clock.due(60000), []);
  clock.ended(325447, 10000);
  assert.deepEqual(clock.due(70000), []);
  assert.equal(seaMechanicText(325450), '注意龙息');
  assert.equal(seaMechanicText(325447), '注意冰眼');
  assert.equal(seaMechanicText(770009), '');
});

test('death cancels ALL timers of that actor, not other bosses, and cannot rearm', () => {
  const clock = new SeaReminderClock();
  for (const skill of [50411,50460,50408,50407]) clock.started(skill, 0, 'apocalypse');
  clock.started(50311, 0, 'manticore');
  clock.health(4235447, 0, 1000, 'apocalypse');
  clock.started(50407, 2000, 'apocalypse');
  assert.deepEqual(clock.due(55000), ['砸地已好']);
  for (const t of [75000,77000,85000,95000]) assert.deepEqual(clock.due(t), []);
  clock.started(50318, 100000, 'manticore');
  clock.health(4235448, 0, 110000, 'manticore');
  assert.deepEqual(clock.due(185000), []);
  clock.started(325447, 200000, 'dragon');
  clock.started(325450, 200000, 'dragon');
  clock.health(4237034, 0, 210000, 'dragon');
  assert.deepEqual(clock.due(250000), []);
  assert.deepEqual(clock.due(255000), []);
});
test('nightmare sea phase-specific safe sides; other nests excluded', () => {
  const s = normalizeVoiceSettings({enabled:true});
  assert.equal(seaBreathText(325430, 4237003), '注意拉冰，三连啄后前往右侧安全区');
  assert.equal(seaBreathText(325431, 4237003), '注意拉冰，三连啄后前往左侧安全区');
  for (const template of [4237011, 4237023, 4237034, 4237035]) {
    assert.equal(seaBreathText(325430, template), '注意拉冰，前往龙右侧安全区');
    assert.equal(seaBreathText(325431, template), '注意拉冰，前往龙左侧安全区');
  }
  assert.equal(seaBreathText(325430), '');
  assert.equal(seaBreathText(325431, 999), '');
  assert.equal(seaBreathText(0), '');
  assert.equal(voiceRule(325430, '梦魇·海龙萨芬特拉', s)?.key, 'sea-breath');
  assert.equal(voiceRule(325431, '海龙经典', s), undefined);
  assert.equal(voiceRule(325430, '黑龙硬核', s), undefined);
});
test('safe defaults and bounded volume', () => {
  assert.equal(normalizeVoiceSettings().enabled, false);
  assert.equal(normalizeVoiceSettings({volume: 4}).volume, 1);
  assert.deepEqual(normalizeVoiceSettings({skills:['bad'],nests:['bad']}).skills, []);
});
test('classic and hardcore are isolated; unknown nests never speak', () => {
  const s = normalizeVoiceSettings({enabled:true});
  assert.equal(voiceRule(34849, '黑龙经典', s)?.key, 'classic-claw');
  assert.equal(voiceRule(735017, '黑龙硬核', s)?.key, 'hard-claw');
  assert.equal(voiceRule(34849, '黑龙硬核', s), undefined);
  assert.equal(voiceRule(34849, '', s), undefined);
  assert.equal(voiceRule(34849, '黑龙经典', {...s,enabled:false}), undefined);
});
test('multi-selection filters nests and skills independently', () => {
  const s = normalizeVoiceSettings({enabled:true,nests:['classic'],skills:['classic-claw']});
  assert.ok(voiceRule(34849, '黑龙经典', s));
  assert.equal(voiceRule(34855, '黑龙经典', s), undefined);
  assert.equal(voiceRule(735017, '黑龙硬核', s), undefined);
});
