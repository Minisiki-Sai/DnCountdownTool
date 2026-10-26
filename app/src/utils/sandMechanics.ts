import type {ClassicEvent} from './classicMechanics.ts';
export const SAND_RULES = [
  {key:'sand-wind',nest:'sand-hard',name:'少女 · 风壁',ids:[729101,729102],release:true,warning:false,texts:['风壁，前往中央']},
  {key:'sand-kick',nest:'sand-hard',name:'少女 · 踢飞',ids:[729105],release:true,warning:false,texts:['注意踢飞']},
  {key:'sand-worm-spawn',nest:'sand-hard',name:'少女 · 沙虫出现',ids:[],release:true,warning:false,texts:['注意沙虫']},
  {key:'sand-golem-storm',nest:'sand-hard',name:'石头人 · 风暴',ids:[730021],release:true,warning:false,texts:['石头人，注意风暴']},
  {key:'sand-golem-slam',nest:'sand-hard',name:'石头人 · 三连砸',ids:[730020],release:false,warning:true,texts:['石头人，三连砸已好']},
  {key:'sand-scorpion',nest:'sand-hard',name:'蝎子 · 旋转',ids:[729085],release:true,warning:true,texts:['蝎子，注意旋转','蝎子，旋转已好']},
  {key:'sand-lion',nest:'sand-hard',name:'狮子 · 三连冲',ids:[730022],release:true,warning:true,texts:['狮子，注意三连冲','狮子，三连冲已好']},
  {key:'sand-basic',nest:'sand-hard',name:'沙漠龙 · 吐息／直线／大追／砸地',ids:[729058,729098,729100,729099],release:true,warning:false,texts:['注意吐息','注意直线','注意大追','注意砸地']},
  {key:'sand-output',nest:'sand-hard',name:'沙漠龙一阶段 · 输出窗口',ids:[],release:false,warning:true,texts:['35秒输出时间，注意其他机制']},
  {key:'sand-trap',nest:'sand-hard',name:'沙漠龙 · 砸地沙虫',ids:[730001,730002,730003,730004],release:true,warning:true,texts:['注意砸地沙虫','沙虫已好']},
  {key:'sand-flash',nest:'sand-hard',name:'沙漠龙 · 闪光',ids:[730025],release:true,warning:true,texts:['注意闪光转身','闪光已好']},
  {key:'sand-wall',nest:'sand-hard',name:'沙漠龙二阶段 · 叹息之墙',ids:[730009,730010],release:true,warning:true,texts:['注意砸地，前往9点','注意砸地，前往3点','叹息之墙已好，前往点位']},
  {key:'sand-round-breath',nest:'sand-hard',name:'沙漠龙二阶段 · 旋转龙息',ids:[729063,729064,729065,729066],release:true,warning:true,texts:['顺时针，注意躲避','逆时针，注意躲避','先顺再逆，注意躲避','先逆再顺，注意躲避','龙息已好']},
];
type Settings={enabled:boolean;nests:string[];skills:string[];releaseSkills:string[];warningSkills:string[]};
type Notice={key:string;text:string;mode:'release'|'warning'};
const dragons=[4150050,4160001];
export class SandMechanics {
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
  private select(rows:Notice[],s:Settings){return rows.filter(n=>s.enabled&&s.nests.includes('sand-hard')&&s.skills.includes(n.key)&&(n.mode==='release'?s.releaseSkills:s.warningSkills).includes(n.key)).map(n=>n.text);}
  observe(e:ClassicEvent,nest:string,s:Settings,seed=false):string[]{
    if(!/沙(?:漠)?龙/.test(nest)||!nest.includes('硬核')){this.clear();return [];}
    const t=e.templateId||0,src=e.source,now=e.timestamp,rows:Notice[]=[];
    if(t)this.templates.set(src,t);
    if(e.hpPercent===0){this.dead.add(src);this.cancel(src);return [];}
    if(this.dead.has(src)||this.retired.has(src))return [];
    const action=e.skill>0&&e.kind!=='boss_hp_threshold'&&e.confirmedEndTimestamp===undefined&&!e.recipients?.length;
    if(!action&&!['monster_spawn','boss_hp_state','boss_hp_threshold'].includes(e.kind||''))return [];
    if(dragons.includes(t)){
      if(this.dragonSource!==src){if(this.dragonSource){this.cancel(this.dragonSource);this.retired.add(this.dragonSource);}this.dragonSource=src;}
      if(!this.entered.has(src)){
        this.entered.add(src);
        if(t===4160001&&!seed)this.arm(src,'sand-wall','叹息之墙已好，前往点位',now,55);
      }
    }
    if(seed)return [];
    const say=(key:string,text:string,mode:Notice['mode']='release')=>rows.push({key,text,mode});
    if(e.kind==='boss_hp_state')return [];
    const key=`${src}:${e.kind}:${e.skill}:${e.threshold??''}`,last=this.seen.get(key);
    if(last!==undefined&&now-last<10000)return [];
    this.seen.set(key,now);
    if(this.seen.size>512)for(const [k,v] of this.seen)if(now-v>60000)this.seen.delete(k);
    if(e.kind==='monster_spawn'){
      if(t===4150065)say('sand-worm-spawn','注意沙虫');
      return this.select(rows,s);
    }
    if(e.kind==='boss_hp_threshold'){
      if(e.threshold===97){
        if(t===4150059)say('sand-golem-slam','石头人，三连砸已好','warning');
        if(t===4150058)say('sand-lion','狮子，三连冲已好','warning');
      }
      if(t===4150057&&e.threshold===92)say('sand-scorpion','蝎子，旋转已好','warning');
      if(t===4150050&&e.threshold===86)say('sand-trap','沙虫已好','warning');
      if(t===4160001&&e.threshold===26)say('sand-round-breath','龙息已好','warning');
      return this.select(rows,s);
    }
    if(!action)return [];
    if(t===4150064){
      if([729101,729102].includes(e.skill))say('sand-wind','风壁，前往中央');
      if(e.skill===729105)say('sand-kick','注意踢飞');
    }
    if(t===4150059){
      if(e.skill===730021)say('sand-golem-storm','石头人，注意风暴');
      if(e.skill===730020)this.arm(src,'sand-golem-slam','石头人，三连砸已好',now,25);
    }
    if(t===4150057&&e.skill===729085){say('sand-scorpion','蝎子，注意旋转');this.arm(src,'sand-scorpion','蝎子，旋转已好',now,25);}
    if(t===4150058&&e.skill===730022){say('sand-lion','狮子，注意三连冲');this.arm(src,'sand-lion','狮子，三连冲已好',now,25);}
    if(dragons.includes(t)){
      const basic=SAND_RULES.find(r=>r.key==='sand-basic')!;
      const index=basic.ids.indexOf(e.skill);
      if(index>=0){say('sand-basic',basic.texts[index]);if(t===4150050)this.arm(src,'sand-output','35秒输出时间，注意其他机制',now,10);}
      if(e.skill>=730001&&e.skill<=730004){say('sand-trap','注意砸地沙虫');this.arm(src,'sand-trap','沙虫已好',now,145);}
      if(e.skill===730025){say('sand-flash','注意闪光转身');this.arm(src,'sand-flash','闪光已好',now,85);}
      if(t===4160001){
        if([730009,730010].includes(e.skill)){say('sand-wall',e.skill===730009?'注意砸地，前往9点':'注意砸地，前往3点');this.arm(src,'sand-wall','叹息之墙已好，前往点位',now,145);}
        if(e.skill>=729063&&e.skill<=729066){
          // Direction order follows the user's explicit ID/text mapping.
          say('sand-round-breath',SAND_RULES.find(r=>r.key==='sand-round-breath')!.texts[e.skill-729063]);
          this.arm(src,'sand-round-breath','龙息已好',now,175);
        }
      }
    }
    return this.select(rows,s);
  }
  due(now:number,s:Settings){const rows:Notice[]=[];for(const [k,p] of this.pending)if(now>=p.deadline){this.pending.delete(k);if(now-p.deadline<=5000&&!this.dead.has(p.source))rows.push(p.notice);}return this.select(rows,s);}
}
