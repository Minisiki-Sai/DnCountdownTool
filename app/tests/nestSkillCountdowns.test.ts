import test from 'node:test';
import assert from 'node:assert/strict';
import { SkillCountdownTracker, SKILL_COUNTDOWN_NESTS, SKILL_COUNTDOWN_OPTIONS } from '../src/utils/skillCountdowns.ts';
import { normalizeBuffOverlaySettings } from '../src/utils/buffOverlaySettings.ts';
const selected=SKILL_COUNTDOWN_OPTIONS.map(r=>r.key);
const e=(t:number,id:number,ms=0,extra={})=>({source:`boss@${t}`,templateId:t,skill:id,timestamp:ms,...extra});
test('four nests expose independently selectable timed skills and identify untimed speech',()=>{
  assert.deepEqual(SKILL_COUNTDOWN_NESTS.map(n=>n.key),['sea-nightmare','classic','hard','sand-hard']);
  for(const nest of SKILL_COUNTDOWN_NESTS){
    assert.ok(nest.rows.length>0);
    assert.deepEqual(nest.groups.flatMap(g=>g.rows).map(r=>r.key).sort(),nest.rows.map(r=>r.key).sort());
  }
  assert.ok(!selected.includes('sand-wind'));
  assert.ok(!selected.includes('classic-takeoff'));
  assert.ok(!selected.includes('sea-shadow-explosion'));
  const saved=normalizeBuffOverlaySettings({selectedSkillCountdowns:['hard-claw','sand-wall','sea-ice-eye']});
  assert.deepEqual(normalizeBuffOverlaySettings(JSON.parse(JSON.stringify(saved))).selectedSkillCountdowns,saved.selectedSkillCountdowns);
  assert.deepEqual(normalizeBuffOverlaySettings({selectedSkillCountdowns:[]}).selectedSkillCountdowns,[]);
});
test('visual cooldowns use full AI duration, independent of voice advance',()=>{
  for(const [nest,t,id,key,seconds] of [
    ['黑龙经典',503611,34715,'classic-ogre-charge',80],
    ['黑龙经典',503621,34734,'classic-stomp',34],
    ['黑龙经典',503652,34771,'classic-whirlwind',63],
    ['黑龙硬核',4181001,734009,'hard-tail',60],
    ['黑龙硬核',4181009,734009,'hard-tail',70],
    ['黑龙硬核',4181009,734017,'hard-claw',120],
    ['黑龙硬核',4181009,735010,'hard-gravity',120],
    ['黑龙硬核',4181009,735011,'hard-gravity',120],
    ['沙龙硬核',4150059,730020,'sand-golem-slam',30],
    ['沙龙硬核',4150058,730022,'sand-lion',30],
    ['沙龙硬核',4160001,730009,'sand-wall',150],
    ['沙龙硬核',4160001,729063,'sand-round-breath',180],
  ] as const){
    const c=new SkillCountdownTracker();c.observe([e(t,id)],nest);
    assert.equal(c.countdowns(0,[key])[0]?.remainingMs,seconds*1000);
    assert.equal(c.countdowns(seconds*1000,[key])[0]?.ready,true);
    c.observe([e(t,id,seconds*1000+1000)],nest);
    assert.equal(c.countdowns(seconds*1000+1000,[key])[0]?.ready,false);
  }
});
test('shared locks delay eligible existing clocks, independent sand groups do not reset each other',()=>{
  const c=new SkillCountdownTracker();
  c.observe([e(4181009,734006,0),e(4181009,734009,60000)],'黑龙硬核');
  assert.equal(c.countdowns(70000,['hard-tracking'])[0].remainingMs,10000);
  assert.equal(c.countdowns(70000,['hard-tail'])[0].remainingMs,60000);
  c.clear();c.observe([e(4150057,729085),e(4150057,729083,24000)],'沙龙硬核');
  assert.equal(c.countdowns(30000,['sand-scorpion'])[0].remainingMs,9000);
  c.clear();c.observe([e(4160001,730001),e(4160001,730009,20000)],'沙龙硬核');
  assert.equal(c.countdowns(150000,['sand-trap'])[0].ready,true);
  assert.equal(c.countdowns(150000,['sand-wall'])[0].remainingMs,20000);
});
test('death, phase switch, other nests, status recipients and ended skills cannot leave false timers',()=>{
  const c=new SkillCountdownTracker();
  c.observe([e(4181009,734017,0,{recipients:[{}]}),e(4181009,735010,0,{confirmedEndTimestamp:0})],'黑龙硬核');
  assert.deepEqual(c.countdowns(0,selected),[]);
  c.observe([e(4181009,734017),e(4181009,0,1000,{kind:'boss_hp_state',hpPercent:0})],'黑龙硬核');
  assert.deepEqual(c.countdowns(1000,selected),[]);
  c.clear();c.observe([e(4150050,730001)],'沙龙硬核');
  c.observe([e(4160001,0,1000,{kind:'monster_spawn'})],'沙龙硬核');
  c.observe([e(4150050,730001,20000)],'沙龙硬核');
  assert.deepEqual(c.countdowns(20000,selected),[]);
  c.observe([e(4160001,730009,21000)],'沙龙硬核');
  c.observe([],'黑龙经典');assert.deepEqual(c.countdowns(22000,selected),[]);
  c.observe([e(503696,34850)],'黑龙经典');
  c.observe([e(503696,0,1000,{kind:'boss_hp_state',hpPercent:25})],'黑龙经典');
  assert.deepEqual(c.countdowns(1000,['classic-tracking']),[]);
});
