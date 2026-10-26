// User-configured classic Black Dragon warnings. Timers are release anchored,
// not promises that the AI will cast immediately when the warning fires.
export const CLASSIC_RULES = [
  { key:'classic-ogre-charge', nest:'classic', name:'食人魔 · 冲撞机制', ids:[34715], release:false, warning:true, texts:['准备冲撞机制'] },
  { key:'classic-ogre-slam', nest:'classic', name:'食人魔 · 砸地', ids:[34714], release:true, warning:true, texts:['注意砸地','砸地已好'] },
  { key:'classic-water', nest:'classic', name:'牛头 · 水球机制', ids:[34738,34743], release:true, warning:true, texts:['准备水球站位','20秒内无水球机制，可输出'] },
  { key:'classic-stomp', nest:'classic', name:'牛头 · 践踏', ids:[34734], release:true, warning:true, texts:['小心践踏','践踏已好'] },
  { key:'classic-meteor', nest:'classic', name:'牛头 · 火陨石', ids:[34737], release:false, warning:true, texts:['火陨石已好'] },
  { key:'classic-whirlwind', nest:'classic', name:'惩罚者 · 旋风', ids:[34771,34780,34781,34773], release:true, warning:true, texts:['注意旋风','旋风已好','15秒内无旋风，可输出','10秒内无旋风，可输出'] },
  { key:'classic-bomb', nest:'classic', name:'千夫长 · 炸弹机制', ids:[34798], release:false, warning:true, texts:['炸弹机制已好'] },
  { key:'classic-earthquake', nest:'classic', name:'千夫长 · 三连砸', ids:[34801], release:false, warning:true, texts:['三连砸已好'] },
  { key:'classic-takeoff', nest:'classic', name:'黑龙 · 起飞血线', ids:[], release:false, warning:true, texts:['马上起飞'] },
  { key:'classic-tracking', nest:'classic', name:'黑龙 · 飞天追踪', ids:[34850], release:false, warning:true, texts:['飞天追踪已好','飞天追踪已取消'] },
  { key:'classic-flight', nest:'classic', name:'黑龙 · 飞天组', ids:[34852,34853,34854,34856,34857,34858,34859,34879,34880], release:true, warning:true, texts:['飞天技能已好','注意冲锋1次','注意冲锋2次','注意冲锋一次接砸地','注意冲锋两次接砸地'] },
  { key:'classic-gravity', nest:'classic', name:'黑龙 · 上升重力', ids:[34860,34864], release:false, warning:true, texts:['上升重力已好'] },
  { key:'classic-claw', nest:'classic', name:'黑龙 · 点名', ids:[34849], release:true, warning:true, texts:['点名已好'] },
  { key:'classic-chase', nest:'classic', name:'黑龙 · 小追', ids:[34848], release:false, warning:true, texts:['小追已好'] },
];
export type ClassicEvent = {kind?:string; skill:number; source:string; timestamp:number; templateId?:number; hpPercent?:number; threshold?:number; confirmedEndTimestamp?:number; recipients?:unknown[]};
type Settings = {enabled:boolean;nests:string[];skills:string[];releaseSkills:string[];warningSkills:string[]};
type Notice = {key:string;text:string;mode:'release'|'warning'};
const dragon = [503681,503682,503696];
const bosses = [503611,503621,503652,503661,...dragon];
export function isClassic(nest:string) { return nest.includes('黑龙') && !nest.includes('硬核') && (nest.includes('经典') || /^黑龙(?:巢穴)?[（(]?(?:123|[一二三123ⅠⅡⅢ])[）)]?(?:$|[（(])/.test(nest.replace(/\s/g,''))); }
export class ClassicMechanics {
  private pending = new Map<string,{source:string;deadline:number;notice:Notice}>();
  private hp = new Map<string,number>();
  private started = new Set<string>();
  private dead = new Set<string>();
  private retired = new Set<string>();
  private seen = new Map<string,number>();
  private templates=new Map<string,number>();
  private dragonSource = '';
  private trackingCancelled = false;
  private addsActive = false;
  private bombs = new Map<string,boolean>();
  clear() {this.pending.clear();this.hp.clear();this.started.clear();this.dead.clear();this.retired.clear();this.seen.clear();this.dragonSource='';this.trackingCancelled=false;this.addsActive=false;this.bombs.clear();;this.templates.clear();}
  private cancel(source:string) {for(const [k,p] of this.pending) if(p.source===source)this.pending.delete(k);}
  /** 换边后丢掉刚离开那一侧 Boss 的提醒（不标记已死：切回来仍会重新计时）。 */
  forgetTemplates(templates:readonly number[]){
    const set=new Set(templates.map(Number).filter(v=>v>0));
    if(!set.size)return;
    for(const [k,p] of [...this.pending]){
      const t=this.templates.get(p.source);
      if(t!==undefined&&set.has(t))this.pending.delete(k);
    }
  }
  private arm(source:string,key:string,text:string,stamp:number,delay:number) {this.pending.set(`${source}:${key}`,{source,deadline:stamp+delay*1000,notice:{key,text,mode:'warning'}});}
  private select(notices:Notice[],s:Settings) {return notices.filter(n=>s.enabled&&s.nests.includes('classic')&&s.skills.includes(n.key)&&(n.mode==='release'?s.releaseSkills:s.warningSkills).includes(n.key)).map(n=>n.text);}
  observe(e:ClassicEvent,nest:string,s:Settings,seed=false):string[] {
    if(!isClassic(nest)) {this.clear();return [];}
    const t=e.templateId||0, src=e.source, now=e.timestamp, notices:Notice[]=[];
    if(t)this.templates.set(src,t);
    if(this.retired.has(src))return [];
    const say=(key:string,text:string,mode:Notice['mode']='warning')=>notices.push({key,text,mode});
    if(e.hpPercent===0) {this.dead.add(src);this.cancel(src);if(dragon.includes(t)){this.addsActive=false;this.bombs.clear();}return [];}
    if(this.dead.has(src))return [];
    if(dragon.includes(t)&&this.dragonSource!==src) {
      if(this.dragonSource){this.cancel(this.dragonSource);this.retired.add(this.dragonSource);}
      this.dragonSource=src;this.addsActive=false;this.bombs.clear();
      // A new dragon entity is a new phase, not a fresh whole encounter.
      if(t===503681)this.trackingCancelled=false;
    }
    if(e.hpPercent!==undefined) {
      this.hp.set(src,e.hpPercent);
      if(dragon.includes(t)&&e.hpPercent<=25){this.trackingCancelled=true;this.pending.delete(`${src}:classic-tracking`);}
    }
    if(seed){if(bosses.includes(t))this.started.add(src);return [];}
    // Initial delays use first combat evidence, not merely entering the room.
    const combat = (e.hpPercent!==undefined&&e.hpPercent>0&&e.hpPercent<100) || (e.skill>0&&e.kind!=='boss_hp_threshold'&&!e.recipients?.length);
    if(bosses.includes(t)&&combat&&!this.started.has(src)) {
      this.started.add(src);
      if(t===503621)this.arm(src,'classic-meteor','火陨石已好',now,20);
      if(t===503661)this.arm(src,'classic-bomb','炸弹机制已好',now,20);
      if(dragon.includes(t))this.arm(src,'classic-chase','小追已好',now,5);
    }
    const eventKey=`${src}:${e.kind}:${e.skill}:${e.threshold??''}`;
    if(e.kind!=='boss_hp_state') {
      const old=this.seen.get(eventKey);
      if(old!==undefined&&now-old<10000)return [];
      this.seen.set(eventKey,now);
      if(this.seen.size>512)for(const [k,v] of this.seen)if(now-v>60000)this.seen.delete(k);
    }
    if(e.kind==='boss_hp_threshold') {
      const h=e.threshold;
      if(t===503611&&h===78)say('classic-ogre-charge','准备冲撞机制');
      if(t===503611&&h===52)say('classic-ogre-slam','砸地已好');
      if(t===503621&&[78,58,28].includes(h||0))say('classic-water','准备水球站位');
      if(t===503652&&h===49)say('classic-whirlwind','旋风已好');
      if(t===503661&&h===74)say('classic-earthquake','三连砸已好');
      if(dragon.includes(t)) {
        if(h===78)say('classic-takeoff','马上起飞');
        if(h===72&&!this.trackingCancelled)say('classic-tracking','飞天追踪已好');
        if(h===25){this.trackingCancelled=true;this.pending.delete(`${src}:classic-tracking`);say('classic-tracking','飞天追踪已取消');}
        if(h===32)say('classic-gravity','上升重力已好');
        if(h===42)say('classic-claw','点名已好');
      }
      return this.select(notices,s);
    }
    if(e.kind==='monster_spawn'&&this.addsActive) {
      if(t===503691)say('classic-paladin-spawn','圣骑士出现','release');
      if(t===503693){this.bombs.set(src,false);say('classic-bomb-add','爆炸兵出现','release');}
      return this.select(notices,s);
    }
    if(e.kind==='classic_add_exit'&&t===503693&&this.addsActive&&e.skill===34928) {
      if(this.bombs.has(src))this.bombs.set(src,true);
      if(this.bombs.size&&[...this.bombs.values()].every(Boolean)) {
        say('classic-bomb-add','所有爆炸兵消失','warning');this.bombs.clear();
      }
      return this.select(notices,s);
    }
    if(e.kind==='boss_hp_state'||e.confirmedEndTimestamp!==undefined||e.recipients?.length)return [];
    if(t===503611) {
      if(e.skill===34715)this.arm(src,'classic-ogre-charge','准备冲撞机制',now,75);
      if(e.skill===34714){say('classic-ogre-slam','注意砸地','release');this.arm(src,'classic-ogre-slam','砸地已好',now,50);}
    }
    if(t===503621) {
      if(e.skill===34738)say('classic-water','准备水球站位','release');
      if(e.skill===34743)say('classic-water','20秒内无水球机制，可输出','release');
      if(e.skill===34734){say('classic-stomp','小心践踏','release');this.arm(src,'classic-stomp','践踏已好',now,29);}
      if(e.skill===34737)this.arm(src,'classic-meteor','火陨石已好',now,35);
    }
    if(t===503652) {
      if(e.skill===34771){say('classic-whirlwind','注意旋风','release');this.arm(src,'classic-whirlwind','旋风已好',now,58);}
      const wait=[34780,34781].includes(e.skill)?15:e.skill===34773?10:0;
      if(wait&&(this.hp.get(src)??100)<49) {
        say('classic-whirlwind',`${wait}秒内无旋风，可输出`,'release');
        const p=this.pending.get(`${src}:classic-whirlwind`);if(p)p.deadline=Math.max(p.deadline,now+wait*1000);
      }
    }
    if(t===503661) {
      if(e.skill===34798)this.arm(src,'classic-bomb','炸弹机制已好',now,50);
      if(e.skill===34801)this.arm(src,'classic-earthquake','三连砸已好',now,35);
    }
    if(dragon.includes(t)) {
      if(e.skill===34876){this.cancel(src);this.addsActive=true;this.bombs.clear();}
      if(e.skill===34877){this.addsActive=false;this.bombs.clear();}
      if(e.skill===34850&&!this.trackingCancelled)this.arm(src,'classic-tracking','飞天追踪已好',now,145);
      if(CLASSIC_RULES.find(r=>r.key==='classic-flight')!.ids.includes(e.skill)) {
        this.arm(src,'classic-flight','飞天技能已好',now,40);
        const text:Record<number,string>={34856:'注意冲锋1次',34857:'注意冲锋2次',34858:'注意冲锋一次接砸地',34859:'注意冲锋两次接砸地'};
        if(text[e.skill])say('classic-flight',text[e.skill],'release');
      }
      if([34860,34864].includes(e.skill))this.arm(src,'classic-gravity','上升重力已好',now,50);
      if(e.skill===34849)this.arm(src,'classic-claw','点名已好',now,57);
      if(e.skill===34848)this.arm(src,'classic-chase','小追已好',now,t===503681?25:t===503682?42:55);
    }
    return this.select(notices,s);
  }
  due(now:number,s:Settings):string[] {const notices:Notice[]=[];for(const [k,p] of this.pending)if(now>=p.deadline){this.pending.delete(k);if(now-p.deadline<=5000&&!this.dead.has(p.source))notices.push(p.notice);}return this.select(notices,s);}
}
