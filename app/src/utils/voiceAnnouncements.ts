import { CLASSIC_RULES } from './classicMechanics.ts';
import { HARD_RULES } from './hardMechanics.ts';
import { SAND_RULES } from './sandMechanics.ts';
export const VOICE_KEY = 'dragon-nest-voice-v1';
// Speech-only homophone avoids engines interpreting 地 as the particle "de".
// Keep display text unchanged. Apply to both live announcements and previews.
export function voiceSpeechText(text: string): string {
  return text.replaceAll('砸地', '砸帝');
}
export const VOICE_RULES = [
  ...CLASSIC_RULES,
  ...HARD_RULES,
  ...SAND_RULES,
  { key: 'classic-paladin-spawn', nest: 'classic', name: '黑龙 · 圣骑士出现', ids: [] },
  { key: 'classic-bomb-add', nest: 'classic', name: '黑龙 · 爆炸兵', ids: [] },
  { key: 'sea-manticore-flight', nest: 'sea-nightmare', name: '狮蝎 · 飞天', ids: [50318] },
  { key: 'sea-manticore-whirlwind', nest: 'sea-nightmare', name: '狮蝎 · 旋风', ids: [50317] },
  { key: 'sea-manticore-slam', nest: 'sea-nightmare', name: '狮蝎 · 砸地', ids: [50311] },
  { key: 'sea-manticore-blackhole', nest: 'sea-nightmare', name: '狮蝎 · 黑洞', ids: [50313] },
  { key: 'sea-manticore-flight-slam', nest: 'sea-nightmare', name: '狮蝎 · 飞天砸', ids: [325423] },
  { key: 'sea-apocalypse-star', nest: 'sea-nightmare', name: '天启 · 六芒星', ids: [50411] },
  { key: 'sea-apocalypse-ground-laser', nest: 'sea-nightmare', name: '天启 · 地面激光', ids: [50457, 50458, 50459] },
  { key: 'sea-apocalypse-tentacles', nest: 'sea-nightmare', name: '天启 · 召唤触手', ids: [50456] },
  { key: 'sea-apocalypse-tracking', nest: 'sea-nightmare', name: '天启 · 连续追踪', ids: [50460] },
  { key: 'sea-apocalypse-rotating-laser', nest: 'sea-nightmare', name: '天启 · 旋转激光', ids: [50408, 50409] },
  { key: 'sea-apocalypse-whirlwind', nest: 'sea-nightmare', name: '天启 · 旋转', ids: [50407] },
  { key: 'sea-breath', nest: 'sea-nightmare', name: '海龙 · 拉冰', ids: [325430, 325431] },
  { key: 'sea-minotaur', nest: 'sea-nightmare', name: '海龙 · 牛头怪', ids: [] },
  { key: 'sea-dragon-breath', nest: 'sea-nightmare', name: '海龙 · 萨芬特拉之威（龙息）', ids: [325450] },
  { key: 'sea-ice-eye', nest: 'sea-nightmare', name: '海龙 · 冰眼', ids: [325447] },
  { key: 'sea-ice-bind', nest: 'sea-nightmare', name: '海龙 · 冰捆', ids: [325456] },
  { key: 'sea-shadow-explosion', nest: 'sea-nightmare', name: '海龙 · 黑影爆炸', ids: [325478] },
  { key: 'sea-hp-flight', nest: 'sea-nightmare', name: '海龙 · 飞天预警', ids: [] },
];
export function immediateMechanicText(skill: number): string {
  return '';
}
export const SEA_MECHANICS: Record<number, { text: string; upcoming: string; cooldownMs?: number; reminderMs?: number; anchor?: 'start' | 'end' }> = {
  50456: { text: '注意触手', upcoming: '召唤触手已好', cooldownMs: 80000, reminderMs: 75000, anchor: 'start' },
  50457: { text: '注意地面激光', upcoming: '地面激光已好', cooldownMs: 40000, reminderMs: 35000, anchor: 'start' },
  50458: { text: '注意地面激光', upcoming: '地面激光已好', cooldownMs: 40000, reminderMs: 35000, anchor: 'start' },
  50459: { text: '注意地面激光', upcoming: '地面激光已好', cooldownMs: 40000, reminderMs: 35000, anchor: 'start' },
  325456: { text: '注意冰捆', upcoming: '冰捆已好', cooldownMs: 120000, reminderMs: 115000, anchor: 'start' },
  50411: { text: '注意天空', upcoming: '六芒星已好', cooldownMs: 80000, reminderMs: 75000, anchor: 'start' },
  50460: { text: '注意连续追踪', upcoming: '连续追踪已好', cooldownMs: 90000, reminderMs: 85000, anchor: 'start' },
  50408: { text: '注意旋转激光顺时针', upcoming: '旋转激光已好', cooldownMs: 100000, reminderMs: 95000, anchor: 'start' },
  50409: { text: '注意旋转激光逆时针', upcoming: '旋转激光已好', cooldownMs: 100000, reminderMs: 95000, anchor: 'start' },
  50407: { text: '注意旋转', upcoming: '旋转已好', cooldownMs: 80000, reminderMs: 75000, anchor: 'start' },
  325423: { text: '跳', upcoming: '' },
  325450: { text: '注意龙息', upcoming: '即将龙息', cooldownMs: 60000, reminderMs: 50000, anchor: 'start' },
  325447: { text: '注意冰眼', upcoming: '即将冰眼', cooldownMs: 60000, reminderMs: 55000, anchor: 'start' },
  50318: { text: '注意飞天', upcoming: '马上起飞', cooldownMs: 90000, reminderMs: 85000, anchor: 'start' },
  50317: { text: '注意旋风', upcoming: '旋风已好', cooldownMs: 60000, reminderMs: 55000, anchor: 'start' },
  50311: { text: '注意砸地', upcoming: '砸地已好', cooldownMs: 60000, reminderMs: 55000, anchor: 'start' },
  50313: { text: '注意黑洞', upcoming: '黑洞已好', cooldownMs: 60000, reminderMs: 55000, anchor: 'start' },
};
export function seaMechanicText(skill: number, upcoming = false): string {
  const rule = SEA_MECHANICS[upcoming ? seaMechanicKey(skill) : skill];
  return rule ? (upcoming ? rule.upcoming : rule.text) : '';
}
// Both rotation directions share a single mechanic, preference and countdown.
export function seaMechanicKey(skill: number): number {
  return skill === 50409 ? 50408 : [50458,50459].includes(skill) ? 50457 : skill;
}
export function monsterAppearanceText(kind: string | undefined, templateId: number | undefined, nest: string, settings: ReturnType<typeof normalizeVoiceSettings>): string {
  return kind === 'monster_spawn' && templateId === 4237036
    && nest.includes('梦魇') && nest.includes('海龙')
    && settings.enabled && settings.nests.includes('sea-nightmare')
    && settings.skills.includes('sea-minotaur') && settings.releaseSkills.includes('sea-minotaur') ? '速击杀牛头怪' : '';
}

export const CLASSIC_ADD_TEXTS = {
  paladin: '圣骑士出现', red: '红衣追随者出现', exit: '红衣追随者退场', explosion: '红衣追随者爆炸，注意躲避',
};
export function classicAddAnnouncement(kind: string | undefined, template: number | undefined, skill: number,
  nest: string, settings: ReturnType<typeof normalizeVoiceSettings>) {
  // Exact classic template IDs, plus nest gating; no HC or unrelated model reuse.
  const memoria = /^黑龙(?:巢穴)?[（(]?(?:123|[一二三123ⅠⅡⅢ])[）)]?(?:$|[（(])/.test(nest.replace(/\s/g, ''));
  if (!settings.enabled || !nest.includes('黑龙') || nest.includes('硬核')
    || (!nest.includes('经典') && !memoria) || !settings.nests.includes('classic')) return '';
  let key = '', text = '';
  if (kind === 'monster_spawn' && template === 503691) { key = 'classic-paladin-spawn'; text = CLASSIC_ADD_TEXTS.paladin; }
  if (kind === 'monster_spawn' && template === 503693) { key = 'classic-bomb-add'; text = CLASSIC_ADD_TEXTS.red; }
  if (kind === 'classic_add_exit' && template === 503693 && [34926,34928].includes(skill)) {
    key = 'classic-bomb-add'; text = skill === 34928 ? CLASSIC_ADD_TEXTS.exit : CLASSIC_ADD_TEXTS.explosion;
  }
  const modes = kind === 'classic_add_exit' ? settings.warningSkills : settings.releaseSkills;
  return settings.skills.includes(key) && modes.includes(key) ? text : '';
}

// 空中的海龙是特例：梦魇海龙里它飞在天上，玩家打不到它，所以
// 「当前攻击目标 / 悬浮窗 Boss 行」指向的都是地面的地狱犬卡卡里。
// 冰眼与龙息因此**只能按技能号匹配**（见 SEA_MECHANICS 与 voiceRule），
// 不能加「技能来源必须是当前 Boss / 当前目标」这类过滤，否则整段地狱犬
// 阶段的播报和倒计时会一起消失。回归用例：tests/seaSkyBossAnnounce.test.ts。
// All countdowns start at action-backed releases, never late hits or statuses.
const BOSS_SKILLS: Record<number, number[]> = {
  4235448: [50318,50317,50311,50313,325423],
  4235447: [50411,50460,50408,50407,50456,50457],
  4237003: [325450,325447,325456],
  4237011: [325450,325447,325456],
  4237034: [325450,325447,325456],
  4237035: [325450,325447,325456],
  4237023: [325450,325447,325456],
};
// All HP-dependent reminders use the original cast anchor. Crossing a threshold
// after the shorter delay has elapsed schedules one immediate reminder.
const HP_REMINDER_RULES = [
  { template: 4235447, skills: [50411,50456], threshold: 20, inclusive: false, delayMs: 55000 },
  { template: 4237023, skills: [325456], threshold: 30, inclusive: true, delayMs: 55000 },
];
export class SeaReminderClock {
  private timing: 'warning' | 'cooldown';
  constructor(timing: 'warning' | 'cooldown' = 'warning') { this.timing = timing; }
  private templates = new Map<string, number>();
  private pending = new Map<string, { skill: number; source: string; anchor: number; deadline: number }>();
  private dead = new Set<string>();
  private healthBySource = new Map<string, { template: number; percent: number }>();
  clear() { this.pending.clear(); this.dead.clear(); this.healthBySource.clear(); this.templates.clear(); }
  isDead(source: string) { return this.dead.has(source); }
  /** 忘掉这些来源的全部倒计时（Boss 已离场）。`markDead` 后不再重新计时。 */
  forgetSources(sources: readonly string[], markDead = false) {
    const set = new Set(sources.map(String).filter(Boolean));
    if (!set.size) return;
    for (const [key, row] of [...this.pending]) if (set.has(row.source)) this.pending.delete(key);
    if (markDead) for (const source of set) this.dead.add(source);
  }
  /** 换边后丢掉「刚离开那一侧」的倒计时，否则它会一直倒数/提示到冷却结束。 */
  forgetTemplates(templates: readonly number[]) {
    const set = new Set(templates.map(Number).filter(value => value > 0));
    if (!set.size) return;
    for (const [key, row] of [...this.pending]) {
      const template = this.templates.get(row.source);
      if (template !== undefined && set.has(template)) this.pending.delete(key);
    }
  }
  private delay(skill: number, source: string) {
    const hp = this.healthBySource.get(source);
    if (this.timing === 'cooldown') {
      const template = this.templates.get(source) ?? hp?.template;
      // The same skill IDs occur in different AIs. HC breath is a one-off
      // HP trigger; Half has no 325447 entry. Neither is a repeating 60s CD.
      if (skill === 325450 && template === 4237011) return undefined;
      if (skill === 325447 && template === 4237035) return undefined;
      if (skill === 325447 && template === 4237011) return 40000;
      if (template === 4235447 && hp && hp.percent <= 20 && [50411, 50456].includes(skill)) return 60000;
      // Client AI ranges: 30..59 =>120s; 0..29 =>60s, not <=30.
      if (template === 4237023 && hp && hp.percent <= 29 && skill === 325456) return 60000;
      return SEA_MECHANICS[skill]?.cooldownMs;
    }
    const rule = HP_REMINDER_RULES.find(r => r.skills.includes(skill) && r.template === hp?.template);
    return rule && hp && (rule.inclusive ? hp.percent <= rule.threshold : hp.percent < rule.threshold)
      ? rule.delayMs : SEA_MECHANICS[skill]?.reminderMs;
  }
  health(template: number, percent: number, nowMs: number, source = '') {
    if (!Number.isFinite(percent) || percent < 0 || percent > 100) return;
    if (percent === 0) {
      if (source) this.dead.add(source);
      for (const [key, pending] of this.pending) {
        if (source ? pending.source === source : BOSS_SKILLS[template]?.includes(pending.skill)) this.pending.delete(key);
      }
      return;
    }
    if (this.isDead(source)) return;
    const oldDelays = new Map([...this.pending].map(([key, p]) => [key, this.delay(p.skill, p.source)]));
    this.healthBySource.set(source, { template, percent });
    this.templates.set(source, template);
    for (const [key, pending] of this.pending) {
      const delay = this.delay(pending.skill, pending.source);
      if (pending.source === source && delay !== oldDelays.get(key)) {
        if (delay === undefined) this.pending.delete(key);
        else pending.deadline = this.timing === 'cooldown' ? pending.anchor + delay : Math.max(nowMs, pending.anchor + delay);
      }
    }
  }
  started(skill: number, startMs?: number, source = '', template?: number) {
    if (template) this.templates.set(source, template);
    skill = seaMechanicKey(skill);
    const key = `${source}:${skill}`;
    this.pending.delete(key);
    if (source && this.dead.has(source)) return;
    const rule = SEA_MECHANICS[skill];
    const delay = this.delay(skill, source);
    if (rule?.anchor === 'start' && rule.reminderMs !== undefined
      && delay !== undefined && startMs !== undefined && Number.isFinite(startMs) && startMs >= 0) {
      this.pending.set(key, { skill, source, anchor: startMs, deadline: startMs + delay });
    }
  }
  ended(_skill: number, _endMs: number) { /* Legacy end events never arm or shift a release timer. */ }
  countdowns(nowMs: number) {
    return [...this.pending].filter(([, p]) => this.timing === 'cooldown' || p.deadline > nowMs).map(([key, p]) => ({
      key, source: p.source, ruleKey: VOICE_RULES.find(r => r.ids.includes(p.skill))?.key || '',
      label: VOICE_RULES.find(r => r.ids.includes(p.skill))?.name || seaMechanicText(p.skill, true),
      remainingMs: Math.max(0, p.deadline - nowMs), durationMs: p.deadline - p.anchor,
      ready: this.timing === 'cooldown' && p.deadline <= nowMs,
    }));
  }
  due(nowMs: number): string[] {
    const texts: string[] = [];
    for (const [key, { skill, deadline }] of this.pending) {
      if (nowMs < deadline) continue;
      this.pending.delete(key);
      if (nowMs - deadline <= 5000) texts.push(seaMechanicText(skill, true));
    }
    return texts;
  }
}
export function seaBreathText(skill: number, templateId?: number): string {
  if (templateId === 4237003) {
    return skill === 325430 ? '注意拉冰，三连啄后前往右侧安全区' : skill === 325431 ? '注意拉冰，三连啄后前往左侧安全区' : '';
  }
  if (![4237011, 4237034, 4237035, 4237023].includes(templateId || 0)) return '';
  return skill === 325430 ? '注意拉冰，前往龙右侧安全区' : skill === 325431 ? '注意拉冰，前往龙左侧安全区' : '';
}
export function normalizeVoiceSettings(raw: any = {}) {
  raw = raw && typeof raw === 'object' ? raw : {};
  if (!raw.bombMerged && Array.isArray(raw.skills)) {
    const key='classic-bomb-add', spawn='classic-red-add-spawn', exit='classic-red-add-exit';
    const on=(old:string)=>raw.skills.includes(old)&&(!Array.isArray(raw.releaseSkills)||raw.releaseSkills.includes(old));
    const appear=on(spawn),disappear=on(exit);
    const clean=(list:string[])=>list.filter(k=>![key,spawn,exit].includes(k));
    raw={...raw,
      skills:[...clean(raw.skills),...(appear||disappear?[key]:[])],
      releaseSkills:[...clean(Array.isArray(raw.releaseSkills)?raw.releaseSkills:VOICE_RULES.map(r=>r.key)),...(appear?[key]:[])],
      warningSkills:[...clean(Array.isArray(raw.warningSkills)?raw.warningSkills:[...VOICE_RULES.map(r=>r.key),'classic-claw-ready','hard-mark-ready']),...(disappear?[key]:[])],
    };
  }
  // Old separate choices become independent modes of one row. Do not enable a
  // previously disabled warning merely because the status row was selected.
  if (!raw.clawMerged && Array.isArray(raw.skills)) {
    const skills: string[] = [...raw.skills];
    const release: string[] = Array.isArray(raw.releaseSkills) ? [...raw.releaseSkills] : VOICE_RULES.map(r=>r.key);
    const warning: string[] = Array.isArray(raw.warningSkills) ? [...raw.warningSkills] : VOICE_RULES.map(r=>r.key);
    for (const [key,ready] of [['classic-claw','classic-claw-ready'],['hard-claw','hard-mark-ready']]) {
      const onRelease=raw.skills.includes(key)&&(!Array.isArray(raw.releaseSkills)||raw.releaseSkills.includes(key));
      const onWarning=raw.skills.includes(ready)&&(!Array.isArray(raw.warningSkills)||raw.warningSkills.includes(ready));
      for (const list of [skills,release,warning]) {
        for(let i=list.length-1;i>=0;i--)if(list[i]===key||list[i]===ready)list.splice(i,1);
      }
      if(onRelease||onWarning)skills.push(key);
      if(onRelease)release.push(key);
      if(onWarning)warning.push(key);
    }
    raw={...raw,skills,releaseSkills:release,warningSkills:warning};
  }
  return {
    clawMerged: true,
    bombMerged: true,
    enabled: raw.enabled === true,
    // 分边副本（黑龙硬核／沙龙硬核）默认只播报自己正在打的那只 Boss；
    // 与「悬浮窗倒计时」的同名开关相互独立。
    splitBossOnly: raw.splitBossOnly !== false,
    volume: Number.isFinite(raw.volume) ? Math.max(0, Math.min(1, raw.volume)) : 0.8,
    voice: typeof raw.voice === 'string' ? raw.voice : '',
    nests: Array.isArray(raw.nests) ? raw.nests.filter((x: string) => ['classic', 'hard', 'sand-hard', 'sea-nightmare'].includes(x)) : ['classic', 'hard', 'sand-hard', 'sea-nightmare'],
    skills: Array.isArray(raw.skills) ? [...new Set<string>(raw.skills.map((x: string) => x === 'sea-hp-ice' ? 'sea-breath' : x).filter((x: string) => VOICE_RULES.some(r => r.key === x)))] : VOICE_RULES.map(r => r.key),
    releaseSkills: Array.isArray(raw.releaseSkills) ? VOICE_RULES.filter(r => raw.releaseSkills.includes(r.key)).map(r => r.key) : VOICE_RULES.map(r => r.key),
    warningSkills: Array.isArray(raw.warningSkills) ? VOICE_RULES.filter(r => raw.warningSkills.includes(r.key)).map(r => r.key) : VOICE_RULES.map(r => r.key),
  };
}
export function voiceRule(skill: number, nest: string, settings: ReturnType<typeof normalizeVoiceSettings>) {
  if (!settings.enabled) return undefined;
  // Memoria reuses the classic claw/status skill. Keep the existing preference
  // key so enabled classic announcements also work after entering Black Dragon 3.
  const memoria = /^黑龙(?:巢穴)?[（(]?(?:123|[一二三123ⅠⅡⅢ])[）)]?(?:$|[（(])/.test(nest.replace(/\s/g, ''));
  const kind = nest.includes('梦魇') && nest.includes('海龙') ? 'sea-nightmare'
    : nest.includes('黑龙') ? (nest.includes('硬核') ? 'hard' : nest.includes('经典') || memoria ? 'classic' : '') : '';
  return VOICE_RULES.find(r => (['classic-claw','hard-claw'].includes(r.key) || ![...CLASSIC_RULES,...HARD_RULES].some(c=>c.key===r.key)) && r.nest === kind && settings.nests.includes(kind) && settings.skills.includes(r.key) && r.ids.includes(skill));
}

export function hpAnnouncement(template: number, threshold: number, nest: string, settings: ReturnType<typeof normalizeVoiceSettings>): string {
  if (!settings.enabled || !nest.includes('梦魇') || !nest.includes('海龙') || !settings.nests.includes('sea-nightmare')) return '';
  let key = '', text = '';
  if (template === 4237012 && [85,65,45,25].includes(threshold)) { key = 'sea-minotaur'; text = '牛头怪即将出现'; }
  if (template === 4235447 && threshold === 42) { key = 'sea-apocalypse-star'; text = '六芒星已好'; }
  if (template === 4235447 && threshold === 60) { key = 'sea-apocalypse-tentacles'; text = '召唤触手已好'; }
  if (template === 4237023) {
    if ([62,52,42,32,22].includes(threshold)) { key = 'sea-breath'; text = '马上拉冰'; }
    if (threshold === 61) { key = 'sea-ice-bind'; text = '冰捆已好'; }
    if ([66,56].includes(threshold)) { key = 'sea-hp-flight'; text = '马上飞天、注意躲避龙息消灭暗影'; }
    if ([46,36,26,16,6].includes(threshold)) { key = 'sea-hp-flight'; text = '马上飞天、注意上炮'; }
  }
  return settings.skills.includes(key) && settings.warningSkills.includes(key) ? text : '';
}

export function voiceModes(rule: typeof VOICE_RULES[number]) {
  if (rule.key === 'classic-bomb-add') return {release:true,warning:true};
  const classic = [...CLASSIC_RULES,...HARD_RULES,...SAND_RULES].find(r => r.key === rule.key);
  if (classic) return { release:classic.release, warning:classic.warning };
  if (rule.key === 'sea-shadow-explosion') return { release: false, warning: true };
  return {
    release: rule.key !== 'sea-hp-flight',
    warning: ['sea-breath', 'sea-minotaur', 'sea-hp-flight'].includes(rule.key) || !!seaMechanicText(rule.ids[0], true),
  };
}
