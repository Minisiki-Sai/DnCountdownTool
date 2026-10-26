import test from 'node:test';
import assert from 'node:assert/strict';
import {SandMechanics,SAND_RULES} from '../src/utils/sandMechanics.ts';
import {normalizeVoiceSettings,voiceModes} from '../src/utils/voiceAnnouncements.ts';
import {voiceGroups} from '../src/utils/voicePreviews.ts';
const s=normalizeVoiceSettings({enabled:true}),nest='沙龙硬核';
const event=(t:number,id:number,ms=0,extra={})=>({templateId:t,source:`a@${t}`,skill:id,timestamp:ms,...extra});
test('all direct alerts are exact and entity scoped',()=>{
  const cases:[number,number,string][]=[
    [4150064,729101,'风壁，前往中央'],[4150064,729102,'风壁，前往中央'],[4150064,729105,'注意踢飞'],
    [4150059,730021,'石头人，注意风暴'],[4150057,729085,'蝎子，注意旋转'],[4150058,730022,'狮子，注意三连冲'],
  ];
  for(const t of [4150050,4160001]){
    for(const [id,text] of [[729058,'注意吐息'],[729098,'注意直线'],[729100,'注意大追'],[729099,'注意砸地'],[730025,'注意闪光转身']] as const)cases.push([t,id,text]);
    for(const id of [730001,730002,730003,730004])cases.push([t,id,'注意砸地沙虫']);
  }
  cases.push([4160001,730009,'注意砸地，前往9点'],[4160001,730010,'注意砸地，前往3点']);
  for(const [t,id,text] of cases){
    assert.deepEqual(new SandMechanics().observe(event(t,id),nest,s),[text]);
    assert.deepEqual(new SandMechanics().observe(event(999,id),nest,s),[]);
  }
  for(let i=0;i<4;i++)assert.deepEqual(new SandMechanics().observe(event(4160001,729063+i),nest,s),[SAND_RULES.at(-1)!.texts[i]]);
});
test('first phase only: any basic cast schedules output after10s, duplicate does not shift',()=>{
  for(const id of [729058,729098,729100,729099]){
    const c=new SandMechanics();c.observe(event(4150050,id),nest,s);
    assert.deepEqual(c.observe(event(4150050,id,1000),nest,s),[]);
    assert.deepEqual(c.due(9999,s),[]);
    assert.deepEqual(c.due(10000,s),['35秒输出时间，注意其他机制']);
    assert.deepEqual(c.due(10001,s),[]);
    const p2=new SandMechanics();p2.observe(event(4160001,id),nest,s);
    assert.deepEqual(p2.due(10000,s),[]);
  }
});
test('all timer delays and aliases use one source-scoped shared timer per mechanic',()=>{
  const cases:[number,number,number,string][]=[
    [4150059,730020,25,'石头人，三连砸已好'],[4150057,729085,25,'蝎子，旋转已好'],[4150058,730022,25,'狮子，三连冲已好'],
    ...[4150050,4160001].flatMap(t=>[
      ...[730001,730002,730003,730004].map(id=>[t,id,145,'沙虫已好'] as [number,number,number,string]),
      [t,730025,85,'闪光已好'] as [number,number,number,string],
    ]),
    ...[730009,730010].map(id=>[4160001,id,145,'叹息之墙已好，前往点位'] as [number,number,number,string]),
    ...[729063,729064,729065,729066].map(id=>[4160001,id,175,'龙息已好'] as [number,number,number,string]),
  ];
  for(const [t,id,seconds,text] of cases){
    const c=new SandMechanics();c.observe(event(t,id),nest,s);
    assert.ok(!c.due(seconds*1000-1,s).includes(text));
    assert.ok(c.due(seconds*1000,s).includes(text),String(id));
    assert.deepEqual(c.due(seconds*1000+1,s),[]);
  }
  const c=new SandMechanics();c.observe(event(4150050,730001),nest,s);c.observe(event(4150050,730004,20000),nest,s);
  assert.deepEqual(c.due(145000,s),[]);assert.deepEqual(c.due(165000,s),['沙虫已好']);
});
test('bloodlines, actual worm creation, stage entry, late oldphase, death and seed',()=>{
  for(const [t,h,text] of [[4150059,97,'石头人，三连砸已好'],[4150057,92,'蝎子，旋转已好'],[4150058,97,'狮子，三连冲已好'],[4150050,86,'沙虫已好'],[4160001,26,'龙息已好']] as const)
    assert.deepEqual(new SandMechanics().observe(event(t,0,0,{kind:'boss_hp_threshold',threshold:h,hpPercent:h}),nest,s),[text]);
  const c=new SandMechanics();
  assert.deepEqual(c.observe(event(4150065,0,0,{kind:'monster_spawn'}),nest,s),['注意沙虫']);
  assert.deepEqual(c.observe(event(4150051,0,0,{kind:'monster_spawn'}),nest,s),[]);
  c.observe(event(4150050,729058),nest,s);
  c.observe(event(4160001,0,1000,{kind:'monster_spawn'}),nest,s);
  assert.deepEqual(c.due(10000,s),[]);
  assert.deepEqual(c.observe(event(4150050,729058,20000),nest,s),[]);
  assert.deepEqual(c.due(56000,s),['叹息之墙已好，前往点位']);
  c.observe(event(4160001,730025,60000),nest,s);
  c.observe(event(4160001,0,61000,{kind:'boss_hp_state',hpPercent:0}),nest,s);
  assert.deepEqual(c.due(145000,s),[]);
  c.clear();c.observe(event(4160001,0,0,{kind:'monster_spawn'}),nest,s,true);
  assert.deepEqual(c.due(55000,s),[]);
});
test('nest, preference, status/end and stale guards; selectable previews and modes',()=>{
  const c=new SandMechanics();
  assert.deepEqual(c.observe(event(4160001,730025,0,{recipients:[{id:'p'}]}),nest,s),[]);
  assert.deepEqual(c.observe(event(4160001,730025,0,{confirmedEndTimestamp:0}),nest,s),[]);
  assert.deepEqual(c.due(55000,s),[]);
  assert.deepEqual(c.observe(event(4150064,729105),'沙龙经典',s),[]);
  assert.deepEqual(c.observe(event(4150064,729105),nest,{...s,nests:[]}),[]);
  c.clear();c.observe(event(4150050,729058),nest,s);assert.deepEqual(c.due(16000,s),[]);
  assert.equal(voiceGroups('sand-hard').flatMap(g=>g.rows).length,SAND_RULES.length);
  for(const r of SAND_RULES)assert.deepEqual(voiceModes(r),{release:r.release,warning:r.warning});
});

test('golem and lion first warning is 97%, not 92%; later casts keep the 25s clock',()=>{
  for(const [t,id,text] of [[4150059,730020,'石头人，三连砸已好'],[4150058,730022,'狮子，三连冲已好']] as const){
    const c=new SandMechanics();
    assert.deepEqual(c.observe(event(t,0,0,{kind:'boss_hp_threshold',threshold:97,hpPercent:97}),nest,s),[text]);
    c.observe(event(t,id,1000),nest,s);
    assert.deepEqual(c.observe(event(t,0,11000,{kind:'boss_hp_threshold',threshold:92,hpPercent:92}),nest,s),[]);
    assert.deepEqual(c.due(25999,s),[]);
    assert.deepEqual(c.due(26000,s),[text]);
  }
  assert.deepEqual(new SandMechanics().observe(event(4150057,0,0,{kind:'boss_hp_threshold',threshold:97,hpPercent:97}),nest,s),[]);
});

test('safe-side speech and previews agree; readiness does not guess the next side',()=>{
  const previews=voiceGroups('sand-hard').flatMap(g=>g.rows).find(r=>r.key==='sand-wall')!.previews;
  assert.deepEqual(previews.map(p=>p.text),['注意砸地，前往9点','注意砸地，前往3点','叹息之墙已好，前往点位']);
  for(const [id,text] of [[730009,'注意砸地，前往9点'],[730010,'注意砸地，前往3点']] as const){
    const c=new SandMechanics();
    assert.deepEqual(c.observe(event(4160001,id),nest,s),[text]);
    assert.deepEqual(c.due(145000,s),['叹息之墙已好，前往点位']);
  }
});
