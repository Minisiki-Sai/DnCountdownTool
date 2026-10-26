/**
 * 播报白名单（focus）：只播「本机正在打的 Boss」+「副本声明的场景 Boss」。
 *
 * 后端只提供事实：`current` = 本机角色此刻主要在打的那只 Boss 模板
 * （15 秒内伤害占比最高者），`scene` = 副本里选不中、但一直在放技能的 Boss
 * （梦魇海龙 P2 的空中海龙），`others` = 登记 Boss 里其余全部模板。
 * 要不要过滤由两个开关各自决定（语音一个、悬浮窗倒计时一个），所以这里只做
 * 纯函数，不读任何设置。
 *
 * 用白名单而不是「另一侧黑名单」：Boss 死亡但服务器没补血量归零包时，它只是
 * 「不在 focus 里」，不需要先知道它死了。
 */
import { countdownNest } from './nestSkillCooldowns.ts';

export interface SplitGroupHint {
  current: number;
  others: number[];
  /** 白名单本体：本机主目标 + 场景 Boss。旧版本后端没有这个字段。 */
  focus?: number[];
  /** 场景 Boss（不可选中、持续放技能）。 */
  scene?: number[];
}

/** 后端字段是弱类型，识别不出来就返回 null —— 调用方据此「不过滤」。 */
export function readSplitGroupHint(raw: unknown): SplitGroupHint | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as {
    current?: unknown;
    others?: unknown;
    focus?: unknown;
    scene?: unknown;
  };
  const current = Number(value.current);
  if (!Number.isFinite(current) || current <= 0) return null;
  const numbers = (input: unknown): number[] => Array.isArray(input)
    ? input.map(row => Number(row)).filter(row => Number.isFinite(row) && row > 0)
    : [];
  const others = numbers(value.others);
  const focus = numbers(value.focus);
  const scene = numbers(value.scene);
  if (!others.length) return null;
  return {
    current,
    others,
    focus: focus.length ? focus : [current],
    scene,
  };
}

/**
 * 只保留白名单内的事件：剔除「不在本机 focus 里」的 Boss 技能。
 * `hint` 为空（副本未识别 / 选不出本机主目标）时原样返回。
 *
 * **状态类行永远保留**：血量、百分比阈值、怪物出场都是事实，不是「技能播报」。
 * 把它们一起剔掉会让另一侧 Boss 的血量/小怪存活状态凭空消失，进而影响按
 * 百分比或召唤物触发的机制倒计时（回放实测：沙龙硬核 t=99s / t=493s 各有一条
 * 怪物出场行被误吞）。
 */
const STATE_KINDS = new Set(['boss_hp_state', 'boss_hp_threshold', 'monster_spawn']);

export function filterCurrentGroupRows<T extends { templateId?: number; kind?: string }>(
  rows: readonly T[],
  hint: SplitGroupHint | null,
): T[] {
  if (!hint) return [...rows];
  const others = new Set(hint.others);
  return rows.filter(row =>
    STATE_KINDS.has(String(row.kind || '')) || !others.has(Number(row.templateId) || 0));
}

/** focus 集合（模板号）。场景 Boss 也在这里，用于「没掉出名单就不清」的判断。 */
export function focusTemplates(hint: SplitGroupHint | null): number[] {
  if (!hint) return [];
  if (hint.focus && hint.focus.length) return hint.focus;
  return [hint.current];
}

/**
 * 上一次 focus 里有、这一次没有的模板 —— 这些要清掉倒计时和待播。
 *
 * 换边（天启 → 狮蝎）、Boss 死亡、终局切换到海龙，都表现为「这只不在名单里」。
 * 任一时刻 hint 为空（不过滤）时返回空数组：宁可多留一帧，也不要在识别空档
 * 里把还在打的 Boss 清掉。
 */
export function leavingFocus(
  previous: readonly number[],
  current: readonly number[],
): number[] {
  if (!previous.length || !current.length) return [];
  const next = new Set(current);
  return previous.filter(template => !next.has(template));
}

/**
 * 「换副本了吗」的判据：只看副本类别，不看显示名。
 *
 * 后端同一个副本有两套名字在交替（地图精确识别给「黑龙巢穴（三）」，
 * 攻击目标反推给「黑龙经典」），于是 props.nest 每秒跳一次。如果直接拿
 * 这个字符串当重置依据，每跳一次就把倒计时和待播语音全清掉 —— 这正是
 * 实战里「倒计时闪一下就没、语音不播」的成因（回放的 nest 是常量，所以不复现）。
 *
 * 规则：映射到类别就用类别；识别空档（刚进本 / 换关瞬间）保持上一次，
 * 不要把「暂时认不出」当成「换副本」。
 */
export function nestScopeKey(nest: string, previous: string): string {
  return countdownNest(String(nest || "").trim()) || previous;
}
