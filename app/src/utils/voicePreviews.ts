import { seaBreathText, seaMechanicText, VOICE_RULES, CLASSIC_ADD_TEXTS, voiceModes } from './voiceAnnouncements.ts';
import { CLASSIC_RULES } from './classicMechanics.ts';
import { HARD_RULES } from './hardMechanics.ts';
import { SAND_RULES } from './sandMechanics.ts';

export function voiceGroups(nest: string) {
  const rows = VOICE_RULES.filter(rule => rule.nest === nest);
  const orders: Record<string,string[]> = {
    'sea-nightmare':['狮蝎','天启','海龙'],
    classic:['食人魔','牛头','惩罚者','千夫长','黑龙'],
    hard:['萝丝琳德','炎之使徒','雷之使徒','佩达'],
    'sand-hard':['陌生的少女','忠实的崇拜者卡基夫','峡谷的监视者阿卜杜拉','暴风支配者朱武','沙漠龙'],
  };
  const boss = (rule: typeof VOICE_RULES[number]) => {
    if(rule.key==='classic-claw')return '黑龙';
    if(rule.key==='hard-claw')return '佩达';
    if(rule.key==='hard-roar')return '炎之使徒';
    if(rule.key==='hard-slam')return '雷之使徒';
    if(rule.key==='hard-combinations')return '萝丝琳德';
    const prefix=rule.name.split(' · ')[0].replace(/一阶段$|二阶段$/,'');
    const sandNames: Record<string,string> = {少女:'陌生的少女',石头人:'忠实的崇拜者卡基夫',蝎子:'峡谷的监视者阿卜杜拉',狮子:'暴风支配者朱武'};
    return nest==='sand-hard' ? sandNames[prefix]||prefix : prefix;
  };
  const label = (rule: typeof VOICE_RULES[number]) => {
    const [prefix,...parts]=rule.name.split(' · ');
    if(!parts.length)return rule.name;
    const phase=prefix.match(/(一阶段|二阶段)$/)?.[1];
    return (phase ? phase+' · ' : '')+parts.join(' · ');
  };
  // Keep unexpected future Boss names visible instead of silently dropping rules.
  const names = [...new Set([...(orders[nest]||[]),...rows.map(boss)])];
  return names.map(name => ({
    key: `${nest}:${name}`, name,
    rows: rows.filter(rule => boss(rule)===name)
      .map(rule => ({ ...rule, label:label(rule), previews: voicePreviews(rule) })),
  })).filter(group=>group.rows.length>0);
}

export function toggleVoiceGroup(selected: string[], keys: string[], enabled: boolean) {
  return enabled ? [...new Set([...selected, ...keys])] : selected.filter(key => !keys.includes(key));
}

type VoiceSelection = {skills:string[];releaseSkills:string[];warningSkills:string[]};
export function selectVoiceGroup(settings:VoiceSelection,keys:string[],enabled:boolean):VoiceSelection {
  const rules=VOICE_RULES.filter(r=>keys.includes(r.key));
  return {
    skills:toggleVoiceGroup(settings.skills,rules.map(r=>r.key),enabled),
    // Disabling the group preserves mode preferences; disabled rows cannot speak.
    // Enabling ALL explicitly enables every supported mode, including hidden single modes.
    releaseSkills:enabled?toggleVoiceGroup(settings.releaseSkills,rules.filter(r=>voiceModes(r).release).map(r=>r.key),true):settings.releaseSkills,
    warningSkills:enabled?toggleVoiceGroup(settings.warningSkills,rules.filter(r=>voiceModes(r).warning).map(r=>r.key),true):settings.warningSkills,
  };
}
export function voiceGroupState(settings:VoiceSelection,keys:string[]) {
  const rules=VOICE_RULES.filter(r=>keys.includes(r.key));
  const checked=rules.length>0&&rules.every(r=>{
    const modes=voiceModes(r);
    return settings.skills.includes(r.key)&&(!modes.release||settings.releaseSkills.includes(r.key))
      &&(!modes.warning||settings.warningSkills.includes(r.key));
  });
  return {checked,indeterminate:!checked&&rules.some(r=>settings.skills.includes(r.key))};
}

export function voicePreviews(rule: typeof VOICE_RULES[number]) {
  const option = (label: string, text: string) => ({ label: `${label}：${text}`, text });
  if (rule.key === 'sand-wall') return [
    option('730009 · 9点安全', '注意砸地，前往9点'),
    option('730010 · 3点安全', '注意砸地，前往3点'),
    option('冷却预警 · 方向未定', '叹息之墙已好，前往点位'),
  ];
  if (['classic-claw','hard-claw'].includes(rule.key)) return [
    option('状态提示','注意影舞者点名'),
    option(rule.key==='hard-claw'?'二阶段冷却预警':'冷却预警','点名已好'),
  ];
  const classic = [...CLASSIC_RULES,...HARD_RULES,...SAND_RULES].find(r=>r.key===rule.key);
  if (classic) return classic.texts.map(text=>option('提示',text));
  if (rule.key === 'classic-paladin-spawn') return [option('生成', CLASSIC_ADD_TEXTS.paladin)];
  if (rule.key === 'classic-bomb-add') return [option('出现', '爆炸兵出现'),option('本轮全部34928', '所有爆炸兵消失')];
  if (rule.key === 'sea-shadow-explosion') return [{ label: '黑影即将爆炸', text: '黑影即将爆炸' }];
  if (rule.key === 'sea-breath') return [
    option('三阶段血量预警', '马上拉冰'),
    option('一阶段·右侧', seaBreathText(325430,4237003)),
    option('一阶段·左侧', seaBreathText(325431,4237003)),
    option('三阶段·左侧', seaBreathText(325431,4237023)),
    option('三阶段·右侧', seaBreathText(325430,4237023)),
  ];
  if (rule.key === 'sea-hp-flight') return [option('66%／56%', '马上飞天、注意躲避龙息消灭暗影'), option('46%／36%／26%／16%／6%', '马上飞天、注意上炮')];
  if (rule.key === 'sea-minotaur') return [option('出现', '速击杀牛头怪'), option('即将出现（地狱犬85/65/45/25%）', '牛头怪即将出现')];
  if (rule.key === 'sea-apocalypse-rotating-laser') return [option('顺时针',seaMechanicText(50408)),option('逆时针',seaMechanicText(50409)),option('冷却预警',seaMechanicText(50408,true))];
  const text = seaMechanicText(rule.ids[0]);
  if (!text) return [option('点名示例', '注意影舞者点名')];
  const result = [option('技能释放',text)];
  const upcoming = seaMechanicText(rule.ids[0],true);
  if (upcoming) result.push(option('冷却预警',upcoming));
  if (rule.key === 'sea-apocalypse-star') result.push(option('42%血量',upcoming));
  if (rule.key === 'sea-apocalypse-tentacles') result.push(option('60%血量',upcoming));
  if (rule.key === 'sea-ice-bind') result.push(option('61%血量',upcoming));
  return result;
}
