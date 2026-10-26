/**
 * 语音播报 / 悬浮窗倒计时的**决策核**。
 *
 * 这里原来长在 `VoiceAnnouncementSettings.vue` 的 poll() 里。搬出来只有一个
 * 目的：让「实战」和「抓包回放」跑**同一份代码**。
 *
 * 之前回放测试是照着页面复刻了一遍门控，页面一改就悄悄分叉 —— 那时候
 * 「回放跑通」只能证明「和昨天一样」。抽成共用模块之后，两边不可能不同。
 *
 * 页面负责的是 I/O：拉接口、出声、推悬浮窗、写观测日志。
 * 本模块负责的是判断：哪些行该看、被哪个门挡下、该念什么、倒计时怎么走。
 */

import { ClassicMechanics } from "./classicMechanics.ts";
import { HardMechanics } from "./hardMechanics.ts";
import { SandMechanics } from "./sandMechanics.ts";
import { ShadowExplosionClock } from "./shadowExplosion.ts";
import { SkillCountdownTracker, type SkillCountdown } from "./skillCountdowns.ts";
import {
  SeaReminderClock,
  hpAnnouncement,
  immediateMechanicText,
  monsterAppearanceText,
  normalizeVoiceSettings,
  seaBreathText,
  seaMechanicKey,
  seaMechanicText,
  voiceRule,
} from "./voiceAnnouncements.ts";
import {
  filterCurrentGroupRows,
  focusTemplates,
  leavingFocus,
  readSplitGroupHint,
  type SplitGroupHint,
} from "./splitBossScope.ts";

export type VoiceSettings = ReturnType<typeof normalizeVoiceSettings>;

export type VoiceEventRow = {
  sequence: number;
  observedAt: number;
  kind?: string;
  skill: number;
  source: string;
  timestamp: number;
  templateId?: number;
  hpPercent?: number;
  threshold?: number;
  confirmedEndTimestamp?: number;
  recipients?: { id?: string; job?: string }[];
};

export type VoiceBatchMeta = {
  state?: string;
  clockRunning?: boolean;
  elapsedSeconds?: number;
  sessionId?: string;
  retiredBossIds?: unknown;
  focusGroup?: unknown;
  splitGroup?: unknown;
  voiceEvents?: VoiceEventRow[];
};

export type VoiceVisualSettings = {
  showSkillCountdownOverlay: boolean;
  selectedSkillCountdowns: string[];
  splitBossOnlyCurrent: boolean;
};

export type VoiceBatchOptions = {
  nest: string;
  settings: VoiceSettings;
  visual: VoiceVisualSettings;
  /** 首页「Boss 技能 / 动作倒计时」总开关。 */
  skillScope: boolean;
  /** 本批到达的墙钟毫秒，对应页面里的 `Date.now()`。 */
  wallClockMs: number;
  /** 音频环境是否可用（WebView 里恒 true；测试里可关掉只看规则）。 */
  supported?: boolean;
  /** 打开后额外记录每句话的出处，给回放测试做「无凭空播报」断言。 */
  trace?: boolean;
};

export type VoiceBatchResult = {
  /** `state`/`clockRunning` 异常，调用方应停止并清空。 */
  reset: boolean;
  /** 语音和悬浮窗都关着，本批不处理。 */
  skipped: boolean;
  /** 倒计时面板被整体清空（开关关了）。 */
  countdownsCleared: boolean;
  /** 首个会话包：只立状态，不补播历史。 */
  bootstrap: boolean;
  /** 本批立刻要念的（顺序即播放顺序）。 */
  immediate: string[];
  /** 与 `immediate` 一一对应的出处；只有 `trace` 打开时才有值。 */
  immediateVia: string[];
  /** 时钟到期/结算出的文案，页面在 immediate 之后一次性念。 */
  deferred: string[];
  staleDropped: number;
  classicTexts: string[];
  hardTexts: string[];
  sandTexts: string[];
  hint: SplitGroupHint | null;
  sequence: number;
  session: string;
  /** 白名单过滤后的行数，只给观测日志用。 */
  voiceRowCount: number;
  visualRowCount: number;
}

const EMPTY_RESULT: Omit<VoiceBatchResult, "reset" | "skipped" | "countdownsCleared" | "bootstrap"> = {
  immediate: [],
  immediateVia: [],
  deferred: [],
  staleDropped: 0,
  classicTexts: [],
  hardTexts: [],
  sandTexts: [],
  hint: null,
  sequence: 0,
  session: "",
  voiceRowCount: 0,
  visualRowCount: 0,
};

/** 页面里那三个时间门，导出给回放测试做防漂移断言。 */
export const STALE_WINDOW_MS = 6000;
export const FRESH_WINDOW_S = 5;
export const REPEAT_WINDOW_MS = 10000;

export class VoiceBatchEngine {
  readonly reminders = new SeaReminderClock();
  readonly shadowClock = new ShadowExplosionClock();
  readonly classicClock = new ClassicMechanics();
  readonly hardClock = new HardMechanics();
  readonly sandClock = new SandMechanics();
  readonly visualClock = new SkillCountdownTracker();

  private recent = new Map<string, number>();
  private lastFocusTemplates: number[] = [];
  private session = "";
  private sequence = 0;

  /** 完整清空：换图、停止统计、切记录。 */
  reset(): void {
    this.session = "";
    this.sequence = 0;
    this.lastFocusTemplates = [];
    this.recent.clear();
    this.clearAllClocks();
  }

  /** 只停语音、保留可视倒计时：改音量/静音时用。 */
  clearForAudioChange(): void {
    this.recent.clear();
    this.reminders.clear();
    this.shadowClock.clear();
    this.classicClock.clear();
    this.hardClock.clear();
    this.sandClock.clear();
  }

  private clearAllClocks(): void {
    this.reminders.clear();
    this.shadowClock.clear();
    this.classicClock.clear();
    this.hardClock.clear();
    this.sandClock.clear();
    this.visualClock.clear();
  }

  get currentSession(): string {
    return this.session;
  }

  get lastSequence(): number {
    return this.sequence;
  }

  /** Boss 离场：丢掉它的倒计时并标记已死，避免「已好」一直挂着。 */
  private applyBossLifecycle(
    retired: unknown,
    hint: SplitGroupHint | null,
    options: VoiceBatchOptions,
  ): void {
    const gone = Array.isArray(retired)
      ? retired.map((value) => String(value || "")).filter(Boolean)
      : [];
    if (gone.length) {
      this.visualClock.forgetSources(gone, true);
      this.reminders.forgetSources(gone, true);
    }
    const focus = focusTemplates(hint);
    const leaving = leavingFocus(this.lastFocusTemplates, focus);
    if (leaving.length) {
      // 只在对应的「只播当前分组」开关打开时才丢：关掉过滤的人本来就要都看。
      if (options.settings.splitBossOnly) {
        this.reminders.forgetTemplates(leaving);
        this.classicClock.forgetTemplates(leaving);
        this.hardClock.forgetTemplates(leaving);
        this.sandClock.forgetTemplates(leaving);
      }
      if (options.visual.splitBossOnlyCurrent) {
        this.visualClock.forgetTemplates(leaving);
      }
    }
    if (focus.length) this.lastFocusTemplates = focus;
  }

  /**
   * 处理一批事件。返回「该念什么」，不出声、不发网络、不碰界面。
   */
  apply(meta: VoiceBatchMeta, options: VoiceBatchOptions): VoiceBatchResult {
    if (meta.state !== "running" || meta.clockRunning === false) {
      this.reset();
      return { ...EMPTY_RESULT, reset: true, skipped: true, countdownsCleared: true, bootstrap: false };
    }
    const supported = options.supported !== false;
    const visualActive = options.skillScope && options.visual.showSkillCountdownOverlay;
    const countdownsCleared = !visualActive;
    if (countdownsCleared) this.visualClock.clear();
    if ((!options.settings.enabled || !supported) && !visualActive) {
      return { ...EMPTY_RESULT, reset: false, skipped: true, countdownsCleared, bootstrap: false };
    }

    const rows = (meta.voiceEvents || []) as VoiceEventRow[];
    const latest = rows.length ? Math.max(...rows.map((row) => row.sequence)) : 0;
    // 白名单（focus）：语音一个开关、悬浮窗一个开关各管各的。关掉任一个就拿到
    // 完整事件流；识别不出主目标时 hint 为 null，等于不过滤。
    const hint = readSplitGroupHint(meta.focusGroup ?? meta.splitGroup);
    this.applyBossLifecycle(meta.retiredBossIds, hint, options);
    const voiceRows = options.settings.splitBossOnly ? filterCurrentGroupRows(rows, hint) : rows;
    const visualRows = options.visual.splitBossOnlyCurrent ? filterCurrentGroupRows(rows, hint) : rows;

    const sessionId = String(meta.sessionId || "");
    if (this.session !== sessionId) {
      // 首包只立状态，绝不补播历史（换记录/刚打开页面时尤其明显）。
      this.reminders.clear();
      this.shadowClock.clear();
      this.classicClock.clear();
      this.hardClock.clear();
      this.sandClock.clear();
      this.visualClock.clear();
      this.session = sessionId;
      this.sequence = latest;
      if (visualActive) {
        this.visualClock.observe(
          visualRows.filter((row) => row.kind === "boss_hp_state" && row.hpPercent !== undefined) as never,
          options.nest,
        );
      }
      for (const row of voiceRows) {
        if (row.kind === "boss_hp_state" && row.hpPercent !== undefined)
          this.reminders.health(row.templateId || 0, row.hpPercent, row.timestamp, row.source);
      }
      for (const row of voiceRows)
        this.classicClock.observe(row as never, options.nest, options.settings as never, true);
      for (const row of voiceRows)
        this.hardClock.observe(row as never, options.nest, options.settings as never, true);
      for (const row of voiceRows)
        this.sandClock.observe(row as never, options.nest, options.settings as never, true);
      return {
        ...EMPTY_RESULT,
        reset: false,
        skipped: false,
        countdownsCleared,
        bootstrap: true,
        hint,
        sequence: this.sequence,
        session: this.session,
        voiceRowCount: voiceRows.length,
        visualRowCount: visualRows.length,
      };
    }

    const elapsedMs = (meta.elapsedSeconds || 0) * 1000;
    const isFresh = (row: VoiceEventRow) =>
      options.wallClockMs / 1000 - row.observedAt <= FRESH_WINDOW_S &&
      elapsedMs - row.timestamp <= STALE_WINDOW_MS;

    const classicTexts: string[] = [];
    const hardTexts: string[] = [];
    const sandTexts: string[] = [];
    const hpTexts: string[] = [];
    const immediate: string[] = [];
    const immediateVia: string[] = [];
    const say = (text: string, via: string) => {
      immediate.push(text);
      if (options.trace) immediateVia.push(via);
    };
    let staleDropped = 0;

    // 先清死者，即使同一批里还夹着它死亡后的施法。
    for (const row of voiceRows)
      if (row.sequence > this.sequence && row.hpPercent === 0)
        this.sandClock.observe(row as never, options.nest, options.settings as never);
    for (const row of voiceRows)
      if (row.sequence > this.sequence && isFresh(row))
        sandTexts.push(...this.sandClock.observe(row as never, options.nest, options.settings as never));
    for (const row of voiceRows)
      if (row.sequence > this.sequence && row.hpPercent === 0)
        this.hardClock.observe(row as never, options.nest, options.settings as never);
    for (const row of voiceRows)
      if (row.sequence > this.sequence && isFresh(row))
        hardTexts.push(...this.hardClock.observe(row as never, options.nest, options.settings as never));
    for (const row of voiceRows)
      if (row.sequence > this.sequence && row.hpPercent === 0)
        this.classicClock.observe(row as never, options.nest, options.settings as never);
    for (const row of voiceRows)
      if (row.sequence > this.sequence && isFresh(row))
        classicTexts.push(...this.classicClock.observe(row as never, options.nest, options.settings as never));
    if (visualActive) {
      this.visualClock.observe(
        visualRows.filter(
          (row) =>
            row.sequence > this.sequence &&
            ((row.kind === "boss_hp_state" && row.hpPercent === 0) ||
              row.kind === "boss_entity_end" ||
              isFresh(row)),
        ) as never,
        options.nest,
      );
    }
    // 死亡要先落地：同一批里靠前的施法不能越过已经确认死亡的来源。
    for (const row of voiceRows)
      if (
        row.sequence > this.sequence &&
        ((row.kind === "boss_hp_state" && row.hpPercent === 0) || row.kind === "boss_entity_end")
      )
        this.reminders.health(row.templateId || 0, 0, row.timestamp, row.source);

    for (const row of voiceRows) {
      if (row.sequence <= this.sequence) continue;
      this.sequence = row.sequence;
      if (!options.settings.enabled || !supported) continue;
      // 清理是权威的：即使界面被暂停、这条事件已经旧了，也要处理。
      if (
        (row.kind === "boss_hp_state" && row.hpPercent === 0) ||
        row.kind === "boss_entity_end"
      ) {
        if (row.templateId === 4237023) this.shadowClock.clear();
        this.reminders.health(row.templateId || 0, 0, row.timestamp, row.source);
        continue;
      }
      if (!isFresh(row)) {
        staleDropped += 1;
        continue;
      }
      if (row.kind === "monster_spawn" && row.templateId === 4237024) {
        this.shadowClock.rescued(row.source, row.timestamp);
        continue;
      }
      if (row.kind === "shadow_start" && row.skill === 325478 && row.templateId === 4237025) {
        const shadowRule = voiceRule(row.skill, options.nest, options.settings as never);
        if (shadowRule && options.settings.warningSkills.includes(shadowRule.key))
          this.shadowClock.started(row.source, row.timestamp);
        continue;
      }
      if (this.reminders.isDead(row.source)) continue;
      if (row.kind === "boss_hp_state" || row.kind === "boss_hp_threshold") {
        if (row.hpPercent !== undefined)
          this.reminders.health(row.templateId || 0, row.hpPercent, row.timestamp, row.source);
        if (row.kind === "boss_hp_threshold") {
          const text = hpAnnouncement(
            row.templateId || 0,
            row.threshold || 0,
            options.nest,
            options.settings as never,
          );
          if (text) hpTexts.push(text);
        }
        continue;
      }
      const appearanceText = monsterAppearanceText(
        row.kind,
        row.templateId,
        options.nest,
        options.settings as never,
      );
      if (appearanceText) {
        const key = `spawn:${row.templateId}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < REPEAT_WINDOW_MS) continue;
        this.recent.set(key, row.timestamp);
        say(appearanceText, `row:${row.timestamp}:${row.skill}:appearance`);
        continue;
      }
      const rule = voiceRule(row.skill, options.nest, options.settings as never);
      if (!rule) continue;
      const immediateText = immediateMechanicText(row.skill);
      if (immediateText) {
        const key = `${row.source}:${row.skill}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < REPEAT_WINDOW_MS) continue;
        this.recent.set(key, row.timestamp);
        if (options.settings.releaseSkills.includes(rule.key))
          say(immediateText, `row:${row.timestamp}:${row.skill}:immediate`);
        continue;
      }
      if (seaMechanicText(row.skill)) {
        if (row.confirmedEndTimestamp !== undefined) {
          this.reminders.ended(row.skill, row.confirmedEndTimestamp);
          continue;
        }
        const key = `${row.source}:${seaMechanicKey(row.skill)}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < REPEAT_WINDOW_MS) continue;
        this.recent.set(key, row.timestamp);
        if (options.settings.warningSkills.includes(rule.key))
          this.reminders.started(row.skill, row.timestamp, row.source);
        if (options.settings.releaseSkills.includes(rule.key))
          say(seaMechanicText(row.skill), `row:${row.timestamp}:${row.skill}:sea`);
        continue;
      }
      if (rule.key === "sea-breath") {
        const text = seaBreathText(row.skill, row.templateId);
        if (!text) continue;
        const key = `${row.source}:${row.templateId}:${row.skill}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < REPEAT_WINDOW_MS) continue;
        this.recent.set(key, row.timestamp);
        if (options.settings.releaseSkills.includes(rule.key))
          say(text, `row:${row.timestamp}:${row.skill}:breath`);
        continue;
      }
      const recipients = (row.recipients || []).filter((target) => {
        const key = `${row.source}:${rule.key}:${target.id}`;
        if (row.timestamp - (this.recent.get(key) ?? -Infinity) < REPEAT_WINDOW_MS) return false;
        this.recent.set(key, row.timestamp);
        return true;
      });
      if (recipients.length && options.settings.releaseSkills.includes(rule.key)) {
        for (const job of new Set(recipients.map((target) => target.job)))
          say(`注意${job}点名`, `row:${row.timestamp}:${row.skill}:recipients`);
      }
    }

    const deferred = [...new Set([...classicTexts, ...hardTexts, ...sandTexts, ...hpTexts])];
    return {
      reset: false,
      skipped: false,
      countdownsCleared,
      bootstrap: false,
      immediate,
      immediateVia,
      deferred,
      staleDropped,
      classicTexts,
      hardTexts,
      sandTexts,
      hint,
      sequence: this.sequence,
      session: this.session,
      voiceRowCount: voiceRows.length,
      visualRowCount: visualRows.length,
    };
  }

  /** 到期提醒。页面每 150ms 调一次。 */
  due(nowMs: number, settings: VoiceSettings): string[] {
    return [
      ...new Set([
        ...this.reminders.due(nowMs),
        ...this.classicClock.due(nowMs, settings as never),
        ...this.hardClock.due(nowMs, settings as never),
        ...this.sandClock.due(nowMs, settings as never),
        ...this.shadowClock.due(nowMs),
      ]),
    ];
  }

  /** 悬浮窗当前的倒计时行。 */
  /** 只读诊断：倒计时这一路的内部状态，用来定位「行为什么被清空」。 */
  debugCountdowns() {
    return this.visualClock.debug();
  }

  countdowns(nowMs: number, selected: readonly string[], visualActive: boolean): SkillCountdown[] {
    if (!visualActive) return [];
    return this.visualClock.countdowns(nowMs, selected);
  }
}
