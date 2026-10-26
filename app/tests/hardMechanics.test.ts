import test from 'node:test';
import assert from 'node:assert/strict';
import {HardMechanics,HARD_RULES} from '../src/utils/hardMechanics.ts';
import {normalizeVoiceSettings,voiceRule} from '../src/utils/voiceAnnouncements.ts';
const s=normalizeVoiceSettings({enabled:true});
const nest='黑龙硬核';
const event=(t:number,id:number,ms=0,extra={})=>({templateId:t,source:`a@${t}`,skill:id,timestamp:ms,...extra});
test('latest immediate wording and all five combinations',()=>{
  const cases:[number,number,string[]][]=[
    [4181005,733027,['注意吼叫']],[4181008,733036,['注意砸地']],
    [4181001,734019,['准备分边']],[4181001,734020,['准备分边']],
    [4181001,733038,['准备龙息']],[4181001,733039,['准备龙息']],
    [4181001,734006,['注意追踪']],[4181009,735006,['注意追踪']],
    [4181001,734009,['注意甩尾']],[4181009,735009,['注意甩尾','20秒内无追踪，可输出']],
    [4181009,734017,[]],[4181009,735010,['注意上升重力']],[4181009,735011,['注意上升重力']],
  ];
  for(const [t,id,text] of cases)assert.deepEqual(new HardMechanics().observe(event(t,id),nest,s),text);
  const texts=HARD_RULES.find(r=>r.key==='hard-combinations')!.texts;
  for(let i=0;i<5;i++)assert.deepEqual(new HardMechanics().observe(event(4180001,732001+i),nest,s),[texts[i]]);
});
test('release-anchored delays, aliases, one-shot and dedup',()=>{
  const cases:[number,number,number,string][]=[
    [4181005,733027,25,'吼叫已好'],[4181008,733036,25,'砸地已好'],
    ...[734006,735006].flatMap(id=>[[4181001,id,115,'追踪已好'],[4181009,id,65,'追踪已好']] as [number,number,number,string][]),
    ...[734009,735009].flatMap(id=>[[4181001,id,55,'甩尾已好'],[4181009,id,65,'甩尾已好']] as [number,number,number,string][]),
    ...[735010,735011].map(id=>[4181009,id,115,'上升重力已好'] as [number,number,number,string]),
    [4181009,734017,115,'点名已好'],
    ...[734008,735008].map(id=>[4181009,id,115,'飞天追踪已好'] as [number,number,number,string]),
  ];
  for(const [t,id,delay,text] of cases){
    const c=new HardMechanics();c.observe(event(t,id,1000),nest,s);
    assert.deepEqual(c.observe(event(t,id,1500),nest,s),[]);
    assert.ok(!c.due(delay*1000+999,s).includes(text));
    assert.ok(c.due(delay*1000+1000,s).includes(text),String(id));
    assert.deepEqual(c.due(delay*1000+2000,s),[]);
  }
});
test('phase entry starts three clocks once, retires phase1 and rejects late packets',()=>{
  const c=new HardMechanics();c.observe(event(4181001,734006),nest,s);
  c.observe(event(4181009,0,10000,{kind:'monster_spawn'}),nest,s);
  c.observe(event(4181009,0,11000,{kind:'monster_spawn'}),nest,s);
  assert.deepEqual(c.due(65000,s),['点名已好']);
  assert.deepEqual(c.due(75000,s),['上升重力已好']);
  assert.deepEqual(c.observe(event(4181001,734006,80000),nest,s),[]);
  assert.deepEqual(c.due(115000,s),[]);
  assert.deepEqual(c.due(125000,s),['飞天追踪已好']);
});
test('81% only phase1; no replay on seed; clear, death, nest and preference guards',()=>{
  const c=new HardMechanics();
  assert.deepEqual(c.observe(event(4181001,0,0,{kind:'boss_hp_threshold',threshold:81,hpPercent:81}),nest,s),['追踪已好']);
  assert.deepEqual(c.observe(event(4181009,0,1000,{kind:'boss_hp_threshold',threshold:81,hpPercent:81}),nest,s),[]);
  c.clear();c.observe(event(4181009,0,0,{kind:'monster_spawn'}),nest,s,true);
  assert.deepEqual(c.due(55000,s),[]);
  c.observe(event(4181009,735006,60000),nest,s);
  c.observe(event(4181009,0,61000,{kind:'boss_hp_state',hpPercent:0}),nest,s);
  assert.deepEqual(c.due(125000,s),[]);
  assert.deepEqual(new HardMechanics().observe(event(4181005,733027),'黑龙经典',s),[]);
  assert.deepEqual(new HardMechanics().observe(event(4181005,733027),nest,{...s,skills:[]}),[]);
  assert.deepEqual(new HardMechanics().observe(event(4181005,733027),nest,{...s,releaseSkills:[]}),[]);
});
test('status recipients never arm gravity or phase-entry timers; legacy claw remains selectable',()=>{
  const c=new HardMechanics();
  assert.deepEqual(c.observe(event(4181009,734017,0,{recipients:[{id:'p'}]}),nest,s),[]);
  assert.deepEqual(c.due(55000,s),[]);
  assert.equal(voiceRule(734017,nest,s)?.key,'hard-claw');
});

test('phase2 mark and gravity IDs refresh only their own clocks and settings',()=>{
  const c=new HardMechanics();
  c.observe(event(4181009,0,0,{kind:'monster_spawn'}),nest,s);
  assert.deepEqual(c.due(55000,s),['点名已好']);
  assert.deepEqual(c.due(65000,s),['上升重力已好']);
  c.due(115000,s);
  assert.deepEqual(c.observe(event(4181009,734017,120000),nest,s),[]);
  assert.deepEqual(c.observe(event(4181009,735010,130000),nest,s),['注意上升重力']);
  assert.deepEqual(c.observe(event(4181009,735011,150000),nest,s),['注意上升重力']);
  assert.deepEqual(c.due(235000,s),['点名已好']);
  assert.deepEqual(c.due(245000,s),[]);
  assert.deepEqual(c.due(265000,s),['上升重力已好']);
  for(const id of [735010,735011]){
    assert.equal(voiceRule(id,nest,s),undefined,'gravity must not route to a marked-player job alert');
    assert.deepEqual(new HardMechanics().observe(event(4181009,id),nest,{...s,releaseSkills:['hard-claw']}),[]);
  }
  assert.deepEqual(HARD_RULES.find(r=>r.key==='hard-gravity')!.ids,[735010,735011]);
});

test('phase2 tail does not warn at55s; phase1 still does',()=>{
  for(const id of [734009,735009]){
    const c=new HardMechanics();
    // Seed only establishes the phase; isolate the subsequent cast timer.
    c.observe(event(4181009,0,0,{kind:'monster_spawn'}),nest,s,true);
    c.observe(event(4181009,id,1000),nest,s);
    assert.deepEqual(c.due(56000,s),[]);
    assert.deepEqual(c.due(65999,s),[]);
    assert.deepEqual(c.due(66000,s),['甩尾已好']);
    const p1=new HardMechanics();p1.observe(event(4181001,id,1000),nest,s);
    assert.deepEqual(p1.due(56000,s),['甩尾已好']);
  }
});
