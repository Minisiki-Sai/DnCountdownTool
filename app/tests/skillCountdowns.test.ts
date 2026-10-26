import test from 'node:test';
import assert from 'node:assert/strict';
import { SeaReminderClock } from '../src/utils/voiceAnnouncements.ts';
import { ShadowExplosionClock } from '../src/utils/shadowExplosion.ts';
import { visibleSkillCountdowns } from '../src/utils/skillCountdowns.ts';
import { SkillCountdownTracker, SKILL_COUNTDOWN_OPTIONS } from '../src/utils/skillCountdowns.ts';
import { normalizeBuffOverlaySettings, enabledOverlayWindowLabels } from '../src/utils/buffOverlaySettings.ts';
import { overlayViewConfiguredForLiveUpdates, combatOverlaySnapshotNeeded } from '../src/utils/overlayRuntimePolicy.ts';

test('confirmed-ended entity suppresses pending and ready timers, not the other Boss', () => {
  const rows = ['dead','alive'].flatMap(source => [false,true].map(ready => ({
    key:source+ready,source,label:source,remainingMs:ready?0:30000,durationMs:30000,ready,
  })));
  const snapshot={rows,publishedAt:1000};
  assert.deepEqual(visibleSkillCountdowns(snapshot,1100,['dead']).map(r=>r.source),['alive','alive']);
  assert.equal(visibleSkillCountdowns(snapshot,1100).length,4);
});

test('encounter completion clears timers without requiring fake zero HP', () => {
  const tracker=new SkillCountdownTracker();
  tracker.observe([{source:'scorpion',skill:729085,templateId:4150057,timestamp:0}], '沙龙硬核');
  assert.equal(tracker.countdowns(31000,['sand-scorpion']).length,1);
  tracker.observe([{source:'scorpion',skill:0,templateId:4150057,timestamp:32000,kind:'boss_entity_end'},
    {source:'scorpion',skill:729085,templateId:4150057,timestamp:33000}], '沙龙硬核');
  assert.deepEqual(tracker.countdowns(34000,['sand-scorpion']),[]);
});

test('visual tracker works without any voice settings and filters independently', () => {
  const tracker = new SkillCountdownTracker();
  const rows = [{ source:'dragon', skill:325447, timestamp:1000 },
    { source:'apocalypse', skill:50411, timestamp:1000 }];
  tracker.observe(rows, '梦魇海龙');
  assert.deepEqual(tracker.countdowns(2000, ['sea-ice-eye']).map(r => r.ruleKey), ['sea-ice-eye']);
  assert.deepEqual(tracker.countdowns(2000, []), []);
  assert.deepEqual(tracker.countdowns(2000, ['sea-apocalypse-star']).map(r => r.ruleKey), ['sea-apocalypse-star']);
  tracker.observe(rows, '黑龙经典');
  assert.deepEqual(tracker.countdowns(2000, ['sea-ice-eye']), []);
});

test('ready skills persist, recast returns to countdown, death is actor scoped', () => {
  const tracker = new SkillCountdownTracker();
  const selected = ['sea-ice-eye','sea-apocalypse-star'];
  tracker.observe([{source:'dragon',skill:325447,timestamp:0,templateId:4237034},
    {source:'apocalypse',skill:50411,timestamp:0,templateId:4235447}], '梦魇海龙');
  assert.equal(tracker.countdowns(60000,selected).find(r => r.ruleKey === 'sea-ice-eye')?.ready,true);
  assert.equal(tracker.countdowns(900000,selected).filter(r => r.ready).length,2);
  tracker.observe([{source:'dragon',skill:325447,timestamp:900000,templateId:4237034}], '梦魇海龙');
  const renewed = tracker.countdowns(901000,selected).find(r => r.ruleKey === 'sea-ice-eye')!;
  assert.equal(renewed.ready,false); assert.equal(renewed.remainingMs,59000);
  tracker.observe([{source:'apocalypse',skill:0,timestamp:901000,templateId:4235447,kind:'boss_hp_state',hpPercent:0}], '梦魇海龙');
  assert.deepEqual(tracker.countdowns(902000,selected).map(r => r.ruleKey),['sea-ice-eye']);
  assert.deepEqual(tracker.countdowns(902000,[]),[]);
  tracker.clear(); assert.deepEqual(tracker.countdowns(902000,selected),[]);
});

test('ready display crosses zero locally and still expires with stale heartbeat', () => {
  const snapshot = {publishedAt:1000,rows:[{key:'ice',label:'冰眼',remainingMs:500,durationMs:60000,ready:false}]};
  assert.equal(visibleSkillCountdowns(snapshot,1400)[0].ready,false);
  assert.equal(visibleSkillCountdowns(snapshot,1500)[0].ready,true);
  assert.equal(visibleSkillCountdowns(snapshot,3000)[0].remaining,0);
  assert.deepEqual(visibleSkillCountdowns(snapshot,4001),[]);
});
test('unverified shadow explosion time is not presented as a cooldown', () => {
  const tracker = new SkillCountdownTracker();
  const selected = ['sea-shadow-explosion'];
  tracker.observe([{ source:'shadow1', kind:'shadow_start', templateId:4237025, skill:325478, timestamp:1000 }], '梦魇海龙');
  assert.deepEqual(tracker.countdowns(2000, selected), []);
  assert.equal(SKILL_COUNTDOWN_OPTIONS.some(r => r.key === 'sea-shadow-explosion'), false);
  tracker.observe(['r1','r2'].map(source => ({ source, kind:'monster_spawn', templateId:4237024, skill:0, timestamp:3000 })), '梦魇海龙');
  assert.deepEqual(tracker.countdowns(3000, selected), []);
  tracker.observe([{ source:'shadow3', kind:'shadow_start', templateId:4237025, skill:325478, timestamp:40000 },
    { source:'dragon', kind:'boss_hp_state', templateId:4237023, hpPercent:0, skill:0, timestamp:40001 }], '梦魇海龙');
  assert.deepEqual(tracker.countdowns(41000, selected), []);
});

test('visual timer reaches client cooldown; warning clock remains earlier', () => {
  for (const [skill, cooldown, warning] of [[50318,90000,85000],[50317,60000,55000],
    [50311,60000,55000],[50313,60000,55000],[50456,80000,75000],
    [50457,40000,35000],[50411,80000,75000],[50460,90000,85000],
    [50408,100000,95000],[50407,80000,75000],[325450,60000,50000],
    [325447,60000,55000],[325456,120000,115000]]) {
    const cd = new SeaReminderClock('cooldown'), voice = new SeaReminderClock();
    cd.started(skill, 1000, 'boss'); voice.started(skill, 1000, 'boss');
    assert.equal(cd.countdowns(1000)[0].remainingMs, cooldown);
    assert.equal(voice.countdowns(1000)[0].remainingMs, warning);
    cd.due(1000 + warning);
    assert.equal(cd.countdowns(1000 + warning)[0].remainingMs, cooldown-warning);
    cd.due(1000+cooldown); assert.deepEqual(cd.countdowns(1000+cooldown), []);
  }
});

test('client HP ranges and phase variants use cooldown rather than warning thresholds', () => {
  const c = new SeaReminderClock('cooldown');
  c.started(325456, 0, 'dragon', 4237023);
  c.health(4237023,30,10000,'dragon');
  assert.equal(c.countdowns(10000)[0].remainingMs,110000);
  c.health(4237023,29,10000,'dragon');
  assert.equal(c.countdowns(10000)[0].remainingMs,50000);
  c.clear(); c.started(50411,0,'apocalypse',4235447);
  c.health(4235447,20,10000,'apocalypse');
  assert.equal(c.countdowns(10000)[0].remainingMs,50000);
  c.clear(); c.started(325450,0,'hc',4237011);
  assert.deepEqual(c.countdowns(0),[]);
  c.started(325447,0,'hc',4237011);
  assert.equal(c.countdowns(0)[0].remainingMs,40000);
  c.clear(); c.started(325447,0,'half',4237035);
  assert.deepEqual(c.countdowns(0),[]);
  c.started(325450,0,'half',4237035);
  assert.equal(c.countdowns(0)[0].remainingMs,60000);
});
test('selection round-trips, preserves none, migrates old settings and excludes untimed alerts', () => {
  assert.equal(normalizeBuffOverlaySettings({}).selectedSkillCountdowns.length, SKILL_COUNTDOWN_OPTIONS.length);
  assert.deepEqual(normalizeBuffOverlaySettings({ selectedSkillCountdowns:[] }).selectedSkillCountdowns, []);
  const selected = normalizeBuffOverlaySettings({ selectedSkillCountdowns:['sea-ice-eye','invalid','sea-ice-eye'] });
  assert.deepEqual(selected.selectedSkillCountdowns, ['sea-ice-eye']);
  assert.deepEqual(normalizeBuffOverlaySettings(JSON.parse(JSON.stringify(selected))).selectedSkillCountdowns, ['sea-ice-eye']);
  assert.equal(SKILL_COUNTDOWN_OPTIONS.some(r => r.key === 'sea-minotaur' || r.key === 'sea-hp-flight' || r.key === 'sea-manticore-flight-slam'), false);
});

test('display reads the same clock: HP shortening, death and reset are reflected immediately', () => {
  const c = new SeaReminderClock();
  c.started(325456, 1000, 'dragon');
  assert.equal(c.countdowns(11000)[0].remainingMs, 105000);
  c.health(4237023, 30, 11000, 'dragon');
  assert.equal(c.countdowns(11000)[0].remainingMs, 45000);
  assert.match(c.countdowns(11000)[0].label, /冰捆/);
  c.health(4237023, 0, 12000, 'dragon');
  assert.deepEqual(c.countdowns(12000), []);
  c.started(50411, 13000, 'apocalypse'); c.clear();
  assert.deepEqual(c.countdowns(14000), []);
});
test('shadow display disappears on two rescues and restarts independently for second wave', () => {
  const c = new ShadowExplosionClock();
  c.started('a', 1000); c.started('b', 2000);
  assert.equal(c.countdowns(3000)[0].remainingMs, 13000);
  c.rescued('r1', 4000); c.rescued('r2', 5000);
  assert.deepEqual(c.countdowns(5000), []);
  c.started('c', 40000); c.rescued('r1', 41000); c.rescued('r2', 41000);
  assert.equal(c.countdowns(42000)[0].remainingMs, 13000);
  assert.deepEqual(c.countdowns(55000), []);
});
test('rows tick, sort, expire and disappear after heartbeat stops', () => {
  const snapshot = { publishedAt: 1000, rows: [
    { key: 'long', label: '冰眼', remainingMs: 10000, durationMs: 20000 },
    { key: 'short', label: '黑影爆炸', remainingMs: 2000, durationMs: 15000 },
  ] };
  const rows = visibleSkillCountdowns(snapshot, 2000);
  assert.deepEqual(rows.map(r => r.key), ['short', 'long']);
  assert.equal(rows[0].remaining, 1);
  assert.equal(rows[1].progress, 45);
  assert.deepEqual(visibleSkillCountdowns(snapshot, 3000).map(r => r.key), ['long']);
  assert.deepEqual(visibleSkillCountdowns(snapshot, 4001), []);
});
test('countdowns own an independent window without Buff/Debuff, in either mode', () => {
  for (const overlayPerformanceMode of [true, false]) {
    const settings = normalizeBuffOverlaySettings({ overlayPerformanceMode,
      showBuffOverlay: false, showDebuffOverlay: false, showSkillCountdownOverlay: true,
      showClearedBuffAlert: false, showMissingBuffAlert: false, showLowHealthAlert: false,
    });
    assert.deepEqual(enabledOverlayWindowLabels(settings), ['skill-countdown-overlay']);
    assert.equal(overlayViewConfiguredForLiveUpdates('skill', settings), true);
    assert.equal(overlayViewConfiguredForLiveUpdates('status', settings), false);
    assert.equal(combatOverlaySnapshotNeeded(true, settings), true);
    assert.equal(normalizeBuffOverlaySettings(JSON.parse(JSON.stringify(settings))).showSkillCountdownOverlay, true);
  }
  assert.equal(normalizeBuffOverlaySettings({}).showSkillCountdownOverlay, true);
});

test('silent-past-zero boss is forgotten so ready rows stop lingering', () => {
  // 梦魇海龙实测：审判者天启最后一条血量停在 0.017%，服务器不再发 0，
  // 于是它的「六芒星/旋转…已好」会一直挂在终局阶段。retiredBossIds 到位后
  // 必须一次性清掉，且不会再被重新计时。
  const tracker = new SkillCountdownTracker();
  tracker.observe([
    { source: 'apocalypse', skill: 50411, templateId: 4235447, timestamp: 0 },
    { source: 'dragon', skill: 325447, templateId: 4237034, timestamp: 0 },
  ], '梦魇海龙');
  assert.equal(tracker.countdowns(100_000, ['sea-apocalypse-star', 'sea-ice-eye']).length, 2);
  tracker.forgetSources(['apocalypse'], true);
  assert.deepEqual(
    tracker.countdowns(100_000, ['sea-apocalypse-star', 'sea-ice-eye']).map(row => row.ruleKey),
    ['sea-ice-eye'],
  );
  // 已离场的来源即使再收到旧事件也不能复活。
  tracker.observe([{ source: 'apocalypse', skill: 50411, templateId: 4235447, timestamp: 101_000 }], '梦魇海龙');
  assert.deepEqual(
    tracker.countdowns(102_000, ['sea-apocalypse-star']),
    [],
  );
});

test('switching sides drops the countdowns of the boss just left', () => {
  // 换边后旧侧已经起好的倒计时必须丢掉，否则「已好」会在到点时继续播报
  // （用户看到的就是「我换到狮蝎了，天启的技能还在提示」）。
  const tracker = new SkillCountdownTracker();
  tracker.observe([{ source: 'apocalypse', skill: 50411, templateId: 4235447, timestamp: 0 }], '梦魇海龙');
  assert.equal(tracker.countdowns(1000, ['sea-apocalypse-star']).length, 1);
  tracker.forgetTemplates([4235447]);
  assert.deepEqual(tracker.countdowns(2000, ['sea-apocalypse-star']), []);
  // 切回来重新放技能可以再次计时（不标记已死）。间隔要超过 10 秒去重窗口。
  tracker.observe([{ source: 'apocalypse', skill: 50411, templateId: 4235447, timestamp: 12_000 }], '梦魇海龙');
  assert.equal(tracker.countdowns(13_000, ['sea-apocalypse-star']).length, 1);
});

test('sea reminder clock forgets sources and templates independently', () => {
  const clock = new SeaReminderClock('cooldown');
  clock.started(325447, 0, 'dragon', 4237034);
  clock.started(50411, 0, 'apocalypse', 4235447);
  assert.equal(clock.countdowns(1000).length, 2);
  clock.forgetTemplates([4235447]);
  assert.deepEqual(clock.countdowns(1000).map(row => row.label), ['海龙 · 冰眼']);
  clock.forgetSources(['dragon'], true);
  assert.deepEqual(clock.countdowns(1000), []);
  clock.started(325447, 2000, 'dragon', 4237034);
  assert.deepEqual(clock.countdowns(3000), []);
});
