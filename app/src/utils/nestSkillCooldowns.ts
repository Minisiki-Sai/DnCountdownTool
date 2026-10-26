import { isClassic } from './classicMechanics.ts';

// Client AI cooldowns (seconds), not the earlier voice-warning offsets.
// Sources: Reborn Resource18 blackdragonnest_gate*; Resource04 blackdragon_hc_pather;
// Resource20 HC apostles; Resource17 desertdragon_*_hc. Group rows indicate
// the shared group lock, not a promise that an individual variant can cast.
type Rule = { key:string; nest:string; templates:number[]; ids:number[]; seconds:number | ((template:number)=>number); shared?:boolean };
const classicDragon=[503681,503682,503696], hardDragon=[4181001,4181009], sandDragon=[4150050,4160001];
export const NEST_COOLDOWN_RULES: Rule[] = [
  {key:'classic-ogre-charge',nest:'classic',templates:[503611],ids:[34715],seconds:80},
  {key:'classic-ogre-slam',nest:'classic',templates:[503611],ids:[34714],seconds:50},
  {key:'classic-stomp',nest:'classic',templates:[503621],ids:[34734],seconds:34},
  {key:'classic-meteor',nest:'classic',templates:[503621],ids:[34737],seconds:40},
  {key:'classic-whirlwind',nest:'classic',templates:[503652],ids:[34771],seconds:63},
  {key:'classic-bomb',nest:'classic',templates:[503661],ids:[34798],seconds:55},
  {key:'classic-earthquake',nest:'classic',templates:[503661],ids:[34801],seconds:40},
  {key:'classic-tracking',nest:'classic',templates:classicDragon,ids:[34850],seconds:t=>t===503696?96:150},
  {key:'classic-flight',nest:'classic',templates:classicDragon,ids:[34852,34853,34854,34856,34857,34858,34859,34879,34880],seconds:45,shared:true},
  {key:'classic-gravity',nest:'classic',templates:classicDragon,ids:[34860,34864],seconds:55},
  {key:'classic-claw',nest:'classic',templates:classicDragon,ids:[34849],seconds:62},
  {key:'classic-chase',nest:'classic',templates:classicDragon,ids:[34848],seconds:t=>t===503681?30:t===503682?47:60},
  {key:'hard-roar',nest:'hard',templates:[4181005,4181006,4181007,4181008],ids:[733027],seconds:30},
  {key:'hard-slam',nest:'hard',templates:[4181005,4181006,4181007,4181008],ids:[733036],seconds:30},
  {key:'hard-tracking',nest:'hard',templates:hardDragon,ids:[734006,735006],seconds:t=>t===4181009?70:120},
  {key:'hard-tail',nest:'hard',templates:hardDragon,ids:[734009,735009],seconds:t=>t===4181009?70:60},
  {key:'hard-claw',nest:'hard',templates:[4181009],ids:[734017],seconds:120},
  {key:'hard-gravity',nest:'hard',templates:[4181009],ids:[735010,735011],seconds:120},
  {key:'hard-flight-ready',nest:'hard',templates:[4181009],ids:[734008,735008],seconds:120},
  {key:'sand-kick',nest:'sand-hard',templates:[4150064],ids:[729105],seconds:30},
  {key:'sand-golem-storm',nest:'sand-hard',templates:[4150059],ids:[730021],seconds:30},
  {key:'sand-golem-slam',nest:'sand-hard',templates:[4150059],ids:[730020],seconds:30},
  {key:'sand-scorpion',nest:'sand-hard',templates:[4150057],ids:[729085],seconds:30},
  {key:'sand-lion',nest:'sand-hard',templates:[4150058],ids:[730022],seconds:30},
  {key:'sand-basic',nest:'sand-hard',templates:sandDragon,ids:[729058,729098,729100,729099],seconds:45,shared:true},
  {key:'sand-trap',nest:'sand-hard',templates:sandDragon,ids:[730001,730002,730003,730004],seconds:150,shared:true},
  {key:'sand-flash',nest:'sand-hard',templates:sandDragon,ids:[730025],seconds:90},
  {key:'sand-wall',nest:'sand-hard',templates:[4160001],ids:[730009,730010],seconds:150,shared:true},
  {key:'sand-round-breath',nest:'sand-hard',templates:[4160001],ids:[729063,729064,729065,729066],seconds:180,shared:true},
];
export function countdownNest(nest:string) {
  if(nest.includes('梦魇')&&nest.includes('海龙'))return 'sea-nightmare';
  if(isClassic(nest))return 'classic';
  if(nest.includes('黑龙')&&nest.includes('硬核'))return 'hard';
  if(/沙(?:漠)?龙/.test(nest)&&nest.includes('硬核'))return 'sand-hard';
  return '';
}
export type NestCountdownEvent={source:string;skill:number;timestamp:number;templateId?:number;kind?:string;hpPercent?:number;confirmedEndTimestamp?:number;recipients?:unknown[]};
export class NestSkillCooldownClock {
  private timers=new Map<string,{source:string;ruleKey:string;start:number;deadline:number;durationMs:number;shared:boolean}>();
  private dead=new Set<string>();
  private retired=new Set<string>();
  private recent=new Map<string,number>();
  private hp=new Map<string,number>();
  private dragonSource='';
  clear(){this.timers.clear();this.dead.clear();this.retired.clear();this.recent.clear();this.hp.clear();this.dragonSource='';}
  private cancel(src:string){for(const [key,row] of this.timers)if(row.source===src)this.timers.delete(key);}
  /** 换边后丢掉刚离开那一侧 Boss 的倒计时（不标记已死：切回来仍会重新计时）。 */
  forgetTemplates(templates:readonly number[]){
    const set=new Set(templates.map(Number).filter(v=>v>0));
    if(!set.size)return;
    for(const [key,row] of [...this.timers]){
      const rule=NEST_COOLDOWN_RULES.find(r=>r.key===row.ruleKey);
      if(rule&&rule.templates.some(t=>set.has(Number(t))))this.timers.delete(key);
    }
  }
  private extend(src:string,key:string,deadline:number){const timer=this.timers.get(`${src}:${key}`);if(timer){timer.deadline=Math.max(timer.deadline,deadline);timer.durationMs=timer.deadline-timer.start;}}
  observe(rows:NestCountdownEvent[],nest:string){
    for(const e of rows)if(e.hpPercent===0||e.kind==='boss_entity_end'){this.dead.add(e.source);this.cancel(e.source);}
    for(const e of rows){
      const t=e.templateId||0,src=e.source;
      if(this.dead.has(src)||this.retired.has(src))continue;
      const action=e.skill>0&&!['boss_hp_state','boss_hp_threshold','monster_spawn'].includes(e.kind||'')&&e.confirmedEndTimestamp===undefined&&!e.recipients?.length;
      if(!action&&!['boss_hp_state','boss_hp_threshold','monster_spawn'].includes(e.kind||''))continue;
      const dragons=nest==='classic'?classicDragon:nest==='hard'?hardDragon:sandDragon;
      if(dragons.includes(t)&&this.dragonSource!==src){
        if(this.dragonSource){this.cancel(this.dragonSource);this.retired.add(this.dragonSource);}
        this.dragonSource=src;
      }
      if(e.hpPercent!==undefined){this.hp.set(src,e.hpPercent);if(nest==='classic'&&e.hpPercent<=25)this.timers.delete(`${src}:classic-tracking`);}
      if(!action)continue;
      const eventKey=`${src}:${e.skill}`,last=this.recent.get(eventKey);
      if(last!==undefined&&e.timestamp-last<10000)continue;
      this.recent.set(eventKey,e.timestamp);
      if(this.recent.size>512)for(const [key,stamp] of this.recent)if(e.timestamp-stamp>60000)this.recent.delete(key);
      if(nest==='classic'&&classicDragon.includes(t)&&e.skill===34876){this.cancel(src);continue;}
      for(const rule of NEST_COOLDOWN_RULES){
        if(rule.nest!==nest||!rule.templates.includes(t)||!rule.ids.includes(e.skill))continue;
        if(rule.key==='classic-tracking'&&(this.hp.get(src)??100)<=25)continue;
        const durationMs=(typeof rule.seconds==='number'?rule.seconds:rule.seconds(t))*1000;
        this.timers.set(`${src}:${rule.key}`,{source:src,ruleKey:rule.key,start:e.timestamp,deadline:e.timestamp+durationMs,durationMs,shared:!!rule.shared});
      }
      // Shared locks can delay an already-running self cooldown, in either direction.
      if(nest==='hard'&&t===4181009&&[734006,735006,734009,735009].includes(e.skill)){
        for(const key of ['hard-tracking','hard-tail'])this.extend(src,key,e.timestamp+20000);
      }
      if(nest==='classic'&&t===503652){
        const wait=[34780,34781].includes(e.skill)?15000:e.skill===34773?10000:0;
        if(wait)this.extend(src,'classic-whirlwind',e.timestamp+wait);
      }
      if(nest==='classic'&&t===503621&&e.skill===34739)this.extend(src,'classic-stomp',e.timestamp+16000);
      if(nest==='sand-hard'&&t===4150057&&e.skill===729083)this.extend(src,'sand-scorpion',e.timestamp+15000);
    }
  }
  /** 只读诊断：挂着哪些倒计时、以及 cancel/dead/retired 的计数。
      三者配合能把「被清空」的四种成因分开：副本名变了、总开关断了、
      被判死亡（dead 增）、换边剔除（retired 增）。 */
  debugState(){return {keys:[...this.timers.keys()], dead:this.dead.size, retired:this.retired.size, recent:this.recent.size};}
  countdowns(now:number){return [...this.timers].map(([key,row])=>({key,source:row.source,ruleKey:row.ruleKey,remainingMs:Math.max(0,row.deadline-now),durationMs:row.durationMs,ready:now>=row.deadline,shared:row.shared}));}
}
