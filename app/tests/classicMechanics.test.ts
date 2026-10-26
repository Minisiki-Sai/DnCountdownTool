import test from 'node:test';
import assert from 'node:assert/strict';
import {ClassicMechanics,CLASSIC_RULES} from '../src/utils/classicMechanics.ts';
import {normalizeVoiceSettings} from '../src/utils/voiceAnnouncements.ts';
const settings=normalizeVoiceSettings({enabled:true});
const nest='黑龙经典';
function setup(t:number) {const c=new ClassicMechanics();const send=(skill:number,time=0,extra:object={})=>c.observe({skill,timestamp:time,source:`actor@${t}`,templateId:t,...extra},nest,settings);return {c,send};}
test('all user delays are release anchored and one-shot',()=>{
  const cases:[number,number,number,string][]=[[503611,34715,75,'准备冲撞机制'],[503611,34714,50,'砸地已好'],[503621,34734,29,'践踏已好'],[503621,34737,35,'火陨石已好'],[503652,34771,58,'旋风已好'],[503661,34798,50,'炸弹机制已好'],[503661,34801,35,'三连砸已好'],[503682,34850,145,'飞天追踪已好'],[503696,34860,50,'上升重力已好'],[503696,34864,50,'上升重力已好'],[503696,34849,57,'点名已好']];
  for(const [t,skill,delay,text] of cases){const {c,send}=setup(t);send(skill,100000);assert.ok(!c.due(100000+delay*1000-1,settings).includes(text));assert.ok(c.due(100000+delay*1000,settings).includes(text),`${skill}`);assert.deepEqual(c.due(100001+delay*1000,settings),[]);}
});
test('HP warnings use exact user thresholds, including 58%, and strict below49',()=>{
  for(const [t,h,text] of [[503611,78,'准备冲撞机制'],[503611,52,'砸地已好'],[503621,58,'准备水球站位'],[503652,49,'旋风已好'],[503661,74,'三连砸已好'],[503681,78,'马上起飞'],[503682,72,'飞天追踪已好'],[503696,32,'上升重力已好'],[503696,42,'点名已好']] as const){const {send}=setup(t);assert.deepEqual(send(0,0,{kind:'boss_hp_threshold',threshold:h,hpPercent:h}),[text]);}
  const {send}=setup(503652);send(0,0,{kind:'boss_hp_state',hpPercent:49});assert.deepEqual(send(34780,1000),[]);send(0,2000,{kind:'boss_hp_state',hpPercent:48.9});assert.deepEqual(send(34781,3000),['15秒内无旋风，可输出']);assert.deepEqual(send(34773,4000),['10秒内无旋风，可输出']);
});
test('flight group refreshes one shared timer, all charge variants have exact wording',()=>{
  const {c,send}=setup(503696);
  for(const [id,text] of [[34856,'注意冲锋1次'],[34857,'注意冲锋2次'],[34858,'注意冲锋一次接砸地'],[34859,'注意冲锋两次接砸地']] as const)assert.deepEqual(send(id,id*100),[text]);
  send(34879,4000000);send(34880,4020000);assert.deepEqual(c.due(4040000,settings),[]);assert.deepEqual(c.due(4060000,settings),['飞天技能已好']);
});
test('25% cancels tracking pending and future casts; phase swap clears old timers',()=>{
  const {c,send}=setup(503682);send(34850,0);assert.deepEqual(send(0,100000,{kind:'boss_hp_threshold',threshold:25,hpPercent:25}),['飞天追踪已取消']);send(34850,110000);assert.ok(!c.due(255000,settings).includes('飞天追踪已好'));
  send(34848,300000);c.observe({source:'new@503696',templateId:503696,skill:34848,timestamp:310000},nest,settings);assert.deepEqual(c.due(342000,settings),[]);assert.deepEqual(c.due(365000,settings),['小追已好']);
});
test('initial timers and three phase-specific chase delays',()=>{
  for(const [t,delay,text] of [[503621,20,'火陨石已好'],[503661,20,'炸弹机制已好'],[503681,5,'小追已好']] as const){const {c,send}=setup(t);send(0,0,{kind:'boss_hp_state',hpPercent:100});assert.deepEqual(c.due(20000,settings),[]);send(0,30000,{kind:'boss_hp_state',hpPercent:99});assert.deepEqual(c.due(30000+delay*1000,settings),[text]);}
  for(const [t,delay] of [[503681,25],[503682,42],[503696,55]]){const {c,send}=setup(t);send(34848);assert.deepEqual(c.due(delay*1000,settings),['小追已好']);}
});
test('only all known bomb actors using34928 completes wave, not34926 or duplicate exit',()=>{
  const {c,send}=setup(503682);send(34876);
  const add=(source:string,kind:string,skill:number,time:number)=>c.observe({source,kind,skill,timestamp:time,templateId:503693},nest,settings);
  assert.deepEqual(add('a','monster_spawn',0,1000),['爆炸兵出现']);add('b','monster_spawn',0,1100);
  assert.deepEqual(add('a','classic_add_exit',34928,2000),[]);assert.deepEqual(add('b','classic_add_exit',34926,3000),[]);
  assert.deepEqual(add('b','classic_add_exit',34928,4000),['所有爆炸兵消失']);assert.deepEqual(add('b','classic_add_exit',34928,15000),[]);
  send(34876,20000);add('c','monster_spawn',0,21000);assert.deepEqual(add('a','classic_add_exit',34928,22000),[]);assert.deepEqual(add('c','classic_add_exit',34928,23000),['所有爆炸兵消失']);
});
test('death, stale session seeds, wrong nests, old Punisher and disabled prefs are safe',()=>{
  const {c,send}=setup(503611);send(34714);send(0,1000,{kind:'boss_hp_state',hpPercent:0});assert.deepEqual(c.due(50000,settings),[]);
  const old=setup(503639);assert.deepEqual(old.send(34771),[]);
  const e={skill:34714,source:'a',timestamp:0,templateId:503611};assert.deepEqual(c.observe(e,'黑龙硬核',settings),[]);
  c.observe(e,nest,settings,true);assert.deepEqual(c.due(50000,settings),[]);
  assert.ok(CLASSIC_RULES.every(r=>settings.skills.includes(r.key)));
});
