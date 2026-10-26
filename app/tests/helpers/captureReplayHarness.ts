/**
 * 用真抓包回放「语音播报 + 悬浮窗倒计时」。
 *
 * 这里**不含任何规则**：决策全部交给 `src/utils/voiceBatchEngine.ts`，
 * 而那正是主窗口 poll() 用的同一个类。所以回放和实战不是「看起来一样」，
 * 而是同一份代码 —— 页面改了回放会跟着改，不会悄悄分叉。
 *
 * 与实机唯一的差异是投递时刻：实机按 150ms 轮询取批，批到达比事件时间晚
 * 0~150ms（主窗口被节流时更久）；这里可以用 `deliveryDelayMs` 精确指定，
 * 默认 0。延迟矩阵测试就是靠它来覆盖「实机抖一下会怎样」。
 */

import { readFileSync } from "node:fs";
import { VoiceBatchEngine, type VoiceEventRow } from "../../src/utils/voiceBatchEngine.ts";
import { VOICE_RULES, normalizeVoiceSettings } from "../../src/utils/voiceAnnouncements.ts";

export const TICK_MS = 150;
export const STALE_WINDOW_MS = 6000;
export const FRESH_WINDOW_S = 5;
export const REPEAT_WINDOW_MS = 10000;

export type ReplayRow = {
  t: number;
  timestamp: number;
  /** 正式馈线入库时给的序号（回放可能跳号：被 10 秒去重的行不会入库）。 */
  sequence?: number;
  /** 回放里 = 事件时间（零延迟投递）。 */
  observedAt?: number;
  kind?: string;
  evidenceKind?: string;
  skill: number;
  templateId?: number;
  source: string;
  hpPercent?: number;
  threshold?: number;
  confirmedEndTimestamp?: number;
  recipients?: { id?: string; job?: string }[];
};

export type ReplayFixture = {
  capture: string;
  nestKey: string;
  nestName: string;
  frames: number;
  durationMs: number;
  bossActorCount: number;
  drops: Record<string, number>;
  kindCounts: Record<string, number>;
  rows: ReplayRow[];
};

export type ReplayOutcome = {
  capture: string;
  nest: string;
  /** `via`: `row:<ts>:<skill>:<why>` / `clock:<ts>` / `reminder:due`。 */
  spoken: { t: number; text: string; via: string }[];
  countdowns: {
    t: number;
    rows: { ruleKey: string; left: number; remainingMs: number; durationMs: number; ready: boolean }[];
  }[];
  /** 因超过 6 秒时效门被丢弃的行数：验证「丢弃是被计数的，不是静默的」。 */
  staleDropped: number;
  batches: number;
  firstRowAt: number;
  lastRowAt: number;
};

export function loadFixture(path: string): ReplayFixture {
  return JSON.parse(readFileSync(path, "utf8")) as ReplayFixture;
}

/** 全开设置：对应「玩家把该副本该机制都打开了」。 */
export function allRulesOn() {
  const keys = VOICE_RULES.map((rule) => rule.key);
  return normalizeVoiceSettings({
    enabled: true,
    nests: ["classic", "hard", "sand-hard", "sea-nightmare"],
    skills: keys,
    releaseSkills: keys,
    warningSkills: keys,
  });
}

export function allCountdownsOn(): string[] {
  return VOICE_RULES.map((rule) => rule.key);
}

export function replayFixture(
  fixture: ReplayFixture,
  options: { deliveryDelayMs?: number; settings?: ReturnType<typeof allRulesOn> } = {},
): ReplayOutcome {
  const delay = Math.max(0, options.deliveryDelayMs ?? 0);
  const settings = options.settings ?? allRulesOn();
  const nest = fixture.nestName;
  const sorted = [...fixture.rows].sort((a, b) => a.timestamp - b.timestamp);

  // 序号与 observedAt 现在由正式回放链路给（feed 入库值），这里只在老 fixture
  // 缺字段时兜底。回放的 observedAt 等于事件时间，也就是「零延迟投递」；
  // 实战的投递抖动用 deliveryDelayMs 模拟，不靠伪造墙钟。
  const rows: VoiceEventRow[] = sorted.map((row, index) => ({
    ...(row as unknown as VoiceEventRow),
    sequence: Number(row.sequence ?? index + 1),
    observedAt: Number(row.observedAt ?? row.timestamp / 1000),
  }));

  const engine = new VoiceBatchEngine();
  const visual = {
    showSkillCountdownOverlay: true,
    selectedSkillCountdowns: allCountdownsOn(),
    splitBossOnlyCurrent: true,
  };
  const baseOptions = { nest, settings, visual, skillScope: true, trace: true };

  const spoken: ReplayOutcome["spoken"] = [];
  const countdowns: ReplayOutcome["countdowns"] = [];
  let staleDropped = 0;
  let batches = 0;
  let cursor = 0;
  let lastShape = "";
  const lastAt = rows.length ? rows[rows.length - 1].timestamp : 0;
  const limit = lastAt + 90_000;
  const sessionId = fixture.capture;

  for (let now = 0; now <= limit; now += TICK_MS) {
    const batch: VoiceEventRow[] = [];
    while (cursor < rows.length && rows[cursor].timestamp + delay <= now) {
      batch.push(rows[cursor]);
      cursor += 1;
    }
    if (batch.length) {
      batches += 1;
      const result = engine.apply(
        {
          state: "running",
          clockRunning: true,
          elapsedSeconds: now / 1000,
          sessionId,
          voiceEvents: batch,
        },
        { ...baseOptions, wallClockMs: now },
      );
      staleDropped += result.staleDropped;
      result.immediate.forEach((text, index) => {
        spoken.push({ t: now, text, via: result.immediateVia[index] || "batch" });
      });
      // 时钟结算出来的那批，页面里也是这一拍一次性念。
      const anchor = batch[batch.length - 1].timestamp;
      for (const text of result.deferred) spoken.push({ t: now, text, via: `clock:${anchor}` });
    }
    for (const text of engine.due(now, settings)) {
      spoken.push({ t: now, text, via: "reminder:due" });
    }
    const live = engine.countdowns(now, visual.selectedSkillCountdowns, true);
    const shape = live
      .map((row) => `${row.ruleKey}:${Math.ceil(row.remainingMs / 1000)}`)
      .sort()
      .join("|");
    if (shape !== lastShape) {
      lastShape = shape;
      countdowns.push({
        t: now,
        rows: live.map((row) => ({
          ruleKey: row.ruleKey,
          left: Math.ceil(row.remainingMs / 1000),
          remainingMs: row.remainingMs,
          durationMs: row.durationMs,
          ready: row.ready === true,
        })),
      });
    }
  }

  return {
    capture: fixture.capture,
    nest,
    spoken,
    countdowns,
    staleDropped,
    batches,
    firstRowAt: rows.length ? rows[0].timestamp : 0,
    lastRowAt: lastAt,
  };
}

export function describeOutcome(outcome: ReplayOutcome): string {
  const lines = [`${outcome.capture}  nest=${outcome.nest}`];
  lines.push(`  语音 ${outcome.spoken.length} 条  staleDropped ${outcome.staleDropped}`);
  for (const row of outcome.spoken.slice(0, 40))
    lines.push(`    t=${(row.t / 1000).toFixed(1)}s  ${row.text}`);
  const ruleKeys = new Set<string>();
  for (const frame of outcome.countdowns) for (const row of frame.rows) ruleKeys.add(row.ruleKey);
  lines.push(`  悬浮窗出现过的行 ${[...ruleKeys].join(", ") || "(无)"}`);
  return lines.join("\n");
}
