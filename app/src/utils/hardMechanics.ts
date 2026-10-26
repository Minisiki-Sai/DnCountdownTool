import type { ClassicEvent } from './classicMechanics.ts';

export const HARD_RULES = [
  {key:'hard-roar',nest:'hard',name:'使徒 · 吼叫',ids:[733027],release:true,warning:true,texts:['注意吼叫','吼叫已好']},
  {key:'hard-slam',nest:'hard',name:'使徒 · 砸地',ids:[733036],release:true,warning:true,texts:['注意砸地','砸地已好']},
  {key:'hard-split',nest:'hard',name:'佩达一阶段 · 分边',ids:[734019,734020],release:true,warning:false,texts:['准备分边']},
  {key:'hard-breath',nest:'hard',name:'佩达 · 龙息',ids:[733038,733039],release:true,warning:false,texts:['准备龙息']},
  {key:'hard-tracking',nest:'hard',name:'佩达 · 追踪',ids:[734006,735006,734009,735009],release:true,warning:true,texts:['注意追踪','追踪已好','20秒内无追踪，可输出']},
  {key:'hard-tail',nest:'hard',name:'佩达 · 甩尾',ids:[734009,735009],release:true,warning:true,texts:['注意甩尾','甩尾已好']},
  {key:'hard-claw',nest:'hard',name:'佩达 · 点名',ids:[734017,735017],release:true,warning:true,texts:['点名已好']},
  {key:'hard-gravity',nest:'hard',name:'佩达二阶段 · 上升重力',ids:[735010,735011],release:true,warning:true,texts:['注意上升重力','上升重力已好']},
  {key:'hard-flight-ready',nest:'hard',name:'佩达二阶段 · 飞天追踪',ids:[734008,735008],release:false,warning:true,texts:['飞天追踪已好']},
  {key:'hard-combinations',nest:'hard',name:'武器区域 · 组合机制',ids:[732001,732002,732003,732004,732005],release:true,warning:false,texts:['小心火环、中心旋风、撞球、点名','小心火环、闪光、撞球、点名','小心火环、冰墙','小心追踪火、中心旋风、撞球、点名','小心追踪火、闪光、撞球、点名']},
];
type Settings={enabled:boolean;nests:string[];skills:string[];releaseSkills:string[];warningSkills:string[]};
type Notice={key:string;text:string;mode:'release'|'warning'};
const dragons=[4181001,4181009];
const apostles=[4181005,4181006,4181007,4181008];

export class HardMechanics {
  private pending=new Map<string,{source:string;deadline:number;notice:Notice}>();
  private templates=new Map<string,number>();
  private seen=new Map<string,number>();
  private dead=new Set<string>();
  private retired=new Set<string>();
  private entered=new Set<string>();
  private dragonSource='';
  clear(){this.pending.clear();this.seen.clear();this.dead.clear();this.retired.clear();this.entered.clear();this.dragonSource='';;this.templates.clear();}
  private cancel(src:string){for(const [k,p] of this.pending)if(p.source===src)this.pending.delete(k);}
  /** 换边后丢掉刚离开那一侧 Boss 的提醒（不标记已死：切回来仍会重新计时）。 */
  forgetTemplates(templates:readonly number[]){
    const set=new Set(templates.map(Number).filter(v=>v>0));
    if(!set.size)return;
    for(const [k,p] of [...this.pending]){
      const t=this.templates.get(p.source);
      if(t!==undefined&&set.has(t))this.pending.delete(k);
    }
  }
  private arm(src:string,key:string,text:string,now:number,delay:number){this.pending.set(`${src}:${key}`,{source:src,deadline:now+delay*1000,notice:{key,text,mode:'warning'}});}
  private select(rows:Notice[],s:Settings){return rows.filter(n=>s.enabled&&s.nests.includes('hard')&&s.skills.includes(n.key)&&(n.mode==='release'?s.releaseSkills:s.warningSkills).includes(n.key)).map(n=>n.text);}
  observe(e:ClassicEvent,nest:string,s:Settings,seed=false):string[]{
    if(!nest.includes('黑龙')||!nest.includes('硬核')){this.clear();return [];}
    const t=e.templateId||0,src=e.source,now=e.timestamp,rows:Notice[]=[];
    if(t)this.templates.set(src,t);
    if(e.hpPercent===0){this.dead.add(src);this.cancel(src);return [];}
    if(this.dead.has(src)||this.retired.has(src))return [];
    const action=e.skill>0&&e.kind!=='boss_hp_threshold'&&e.confirmedEndTimestamp===undefined&&!e.recipients?.length;
    if(!action&&e.kind!=='monster_spawn'&&e.kind!=='boss_hp_state'&&e.kind!=='boss_hp_threshold')return [];
    if(dragons.includes(t)){
      if(this.dragonSource!==src){
        if(this.dragonSource){this.cancel(this.dragonSource);this.retired.add(this.dragonSource);}
        this.dragonSource=src;
      }
      if(!this.entered.has(src)){
        this.entered.add(src);
        // Spawn is preferred; first fresh HP/action is fallback if creation was missed.
        if(t===4181009&&!seed){
          this.arm(src,'hard-claw','点名已好',now,55);
          this.arm(src,'hard-gravity','上升重力已好',now,65);
          this.arm(src,'hard-flight-ready','飞天追踪已好',now,115);
        }
      }
    }
    if(seed)return [];
    const say=(key:string,text:string,mode:Notice['mode']='release')=>rows.push({key,text,mode});
    if(e.kind==='boss_hp_state')return [];
    const key=`${src}:${e.kind}:${e.skill}:${e.threshold??''}`,last=this.seen.get(key);
    if(last!==undefined&&now-last<10000)return [];
    this.seen.set(key,now);
    if(this.seen.size>512)for(const [k,v] of this.seen)if(now-v>60000)this.seen.delete(k);
    if(e.kind==='boss_hp_threshold'){
      if(t===4181001&&e.threshold===81)say('hard-tracking','追踪已好','warning');
      return this.select(rows,s);
    }
    if(!action)return [];
    if(apostles.includes(t)){
      if(e.skill===733027){say('hard-roar','注意吼叫');this.arm(src,'hard-roar','吼叫已好',now,25);}
      if(e.skill===733036){say('hard-slam','注意砸地');this.arm(src,'hard-slam','砸地已好',now,25);}
    }
    if(dragons.includes(t)){
      if([733038,733039].includes(e.skill))say('hard-breath','准备龙息');
      if(t===4181001&&[734019,734020].includes(e.skill))say('hard-split','准备分边');
      if([734006,735006].includes(e.skill)){
        say('hard-tracking','注意追踪');this.arm(src,'hard-tracking','追踪已好',now,t===4181009?65:115);
      }
      if([734009,735009].includes(e.skill)){
        say('hard-tail','注意甩尾');
        this.arm(src,'hard-tail','甩尾已好',now,t===4181009?65:55);
        if(t===4181009)say('hard-tracking','20秒内无追踪，可输出');
      }
      if(t===4181009){
        if(e.skill===734017)this.arm(src,'hard-claw','点名已好',now,115);
        if([735010,735011].includes(e.skill)){say('hard-gravity','注意上升重力');this.arm(src,'hard-gravity','上升重力已好',now,115);}
        if([734008,735008].includes(e.skill))this.arm(src,'hard-flight-ready','飞天追踪已好',now,115);
      }
    }
    if(t===4180001&&e.skill>=732001&&e.skill<=732005)say('hard-combinations',HARD_RULES.find(r=>r.key==='hard-combinations')!.texts[e.skill-732001]);
    return this.select(rows,s);
  }
  due(now:number,s:Settings){const rows:Notice[]=[];for(const [k,p] of this.pending)if(now>=p.deadline){this.pending.delete(k);if(now-p.deadline<=5000&&!this.dead.has(p.source))rows.push(p.notice);}return this.select(rows,s);}
}
