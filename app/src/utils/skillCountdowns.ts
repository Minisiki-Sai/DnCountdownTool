import { VOICE_RULES, SEA_MECHANICS, SeaReminderClock, seaMechanicKey } from './voiceAnnouncements.ts';
import { voiceGroups } from './voicePreviews.ts';
import { NEST_COOLDOWN_RULES, NestSkillCooldownClock, countdownNest } from './nestSkillCooldowns.ts';

// Shadow explosion is not a cooldown: its exact start-to-damage duration is
// unverified. Keep its 15s voice warning, but do not display it as a ready time.
export const SKILL_COUNTDOWN_OPTIONS = VOICE_RULES.filter(rule =>
  rule.ids.some(id => SEA_MECHANICS[id]?.cooldownMs !== undefined)||NEST_COOLDOWN_RULES.some(row=>row.key===rule.key));
export const SKILL_COUNTDOWN_NESTS = [
  {key:'sea-nightmare',name:'梦魇海龙'}, {key:'classic',name:'黑龙经典／黑龙123'},
  {key:'hard',name:'黑龙硬核'}, {key:'sand-hard',name:'沙龙硬核'},
].map(nest=>({...nest,rows:SKILL_COUNTDOWN_OPTIONS.filter(r=>r.nest===nest.key),
  groups:voiceGroups(nest.key).map(group=>({...group,
    untimed:group.rows.filter(r=>!SKILL_COUNTDOWN_OPTIONS.some(option=>option.key===r.key)),
    rows:group.rows.filter(r=>SKILL_COUNTDOWN_OPTIONS.some(option=>option.key===r.key)),
  }))}));
export const SKILL_COUNTDOWN_GROUPS = SKILL_COUNTDOWN_NESTS.flatMap(n=>n.groups);
export type SkillCountdown = { key: string; source?: string; ruleKey?: string; label: string; remainingMs: number; durationMs: number; ready?: boolean };
export type SkillCountdownSnapshot = { rows: SkillCountdown[]; publishedAt: number };

type CountdownEvent = { source: string; skill: number; timestamp: number; kind?: string;
  templateId?: number; hpPercent?: number; confirmedEndTimestamp?: number; recipients?:unknown[] };
/** Visual timers deliberately do not read voice-enabled or voice-selection preferences. */
export class SkillCountdownTracker {
  private reminders = new SeaReminderClock('cooldown');
  private recent = new Map<string, number>();
  private other = new NestSkillCooldownClock();
  private nest = '';
  clear() { this.reminders.clear(); this.recent.clear(); this.other.clear(); this.nest=''; }
  /** Boss 离场：丢掉它的倒计时并标记已死，避免「已好」一直挂着。 */
  forgetSources(sources: readonly string[], markDead = true) {
    this.reminders.forgetSources(sources, markDead);
  }
  /** 换边：丢掉刚离开那一侧 Boss 的倒计时。 */
  forgetTemplates(templates: readonly number[]) {
    this.reminders.forgetTemplates(templates);
    this.other.forgetTemplates(templates);
  }
  observe(rows: CountdownEvent[], nest: string) {
    const key=countdownNest(nest);
    if(key!==this.nest){this.clear();this.nest=key;}
    if(!key)return;
    if(key!=='sea-nightmare'){this.other.observe(rows,key);return;}
    // Death wins over any earlier start in the same incoming batch.
    for (const row of rows) if ((row.hpPercent === 0 && row.kind === 'boss_hp_state') || row.kind === 'boss_entity_end') {
      this.reminders.health(row.templateId || 0, 0, row.timestamp, row.source);
    }
    for (const row of rows) {
      if (row.kind === 'boss_hp_state' || row.kind === 'boss_hp_threshold') {
        if (row.hpPercent !== undefined) this.reminders.health(row.templateId || 0, row.hpPercent, row.timestamp, row.source);
      } else if (SEA_MECHANICS[row.skill]?.cooldownMs !== undefined && row.confirmedEndTimestamp === undefined && !row.recipients?.length) {
        const key = `${row.source}:${seaMechanicKey(row.skill)}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < 10000) continue;
        this.recent.set(key, row.timestamp);
        this.reminders.started(row.skill, row.timestamp, row.source, row.templateId);
      }
    }
  }
  /** 只读诊断：认出的是哪个副本、挂着哪些计时器。 */
  debug() { const inner = this.other.debugState(); return { nest: this.nest, timers: inner.keys, dead: inner.dead, retired: inner.retired, recent: inner.recent }; }
  countdowns(nowMs: number, selected: readonly string[]) {
    // Retain ready entries until the next cast, death, or session reset.
    const other=this.other.countdowns(nowMs).map(row=>({...row,
      label:(SKILL_COUNTDOWN_OPTIONS.find(r=>r.key===row.ruleKey)?.name||row.ruleKey)+(row.shared?' · 组冷却':'')}));
    return [...this.reminders.countdowns(nowMs),...other]
      .filter(row => selected.includes(row.ruleKey));
  }
}

// Keep these dimensions shared with the rendered panel; native window sizing
// must include this panel even when Buff/Debuff have no rows at all.
// `identityRows` 计入窗口顶部的「副本 / Boss」两行身份信息。
export function skillCountdownGeometry(count: number, fontSize: number, identityRows = 0) {
  const rowHeight = Math.ceil(fontSize * 1.55) + 14;
  const headerHeight = Math.ceil(fontSize * 1.55);
  const identityHeight = identityRows > 0
    ? identityRows * (Math.ceil(fontSize * 1.45) + 2) + 6
    : 0;
  return { rowHeight, headerHeight, identityHeight,
    height: 16 + identityHeight + (count > 0 ? headerHeight + 6 + count * (rowHeight + 4) + 4 : 0) };
}

// Never leave a frozen timer visible when the main window/backend stops updating.
export function visibleSkillCountdowns(snapshot: SkillCountdownSnapshot, now: number, endedBossIds: readonly string[] = []) {
  const age = Math.max(0, now - snapshot.publishedAt);
  if (age > 3000) return [];
  return snapshot.rows.filter(row => !endedBossIds.includes(row.source || '') && Number.isFinite(row.remainingMs) && (row.ready !== undefined || row.remainingMs > age))
    .map(row => ({ ...row, ready: row.ready === true || (row.ready === false && row.remainingMs <= age),
      remaining: Math.max(0, (row.remainingMs - age) / 1000),
      progress: Math.max(0, Math.min(100, (row.remainingMs - age) / Math.max(1, row.durationMs) * 100)) }))
    .sort((a, b) => a.remaining - b.remaining);
}
