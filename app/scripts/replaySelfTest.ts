/**
 * 用抓包回放给「语音播报 + 悬浮窗倒计时」做一次全量自检，产出报告数据。
 *
 *   node --experimental-strip-types scripts/replaySelfTest.ts [<out.json>]
 *
 * 走的是主程序同一份决策核（`src/utils/voiceBatchEngine.ts`）；音频那层用
 * `announcementParts()` + manifest + wav 头判断「这句到底能不能念出来、念多久」，
 * 和播放器的取词、拼接逻辑完全一致。
 */

import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { NEST_COOLDOWN_RULES, countdownNest } from "../src/utils/nestSkillCooldowns.ts";
import { QWEN_VOICES, announcementParts } from "../src/utils/qwenVoicePlayer.ts";
import { SKILL_COUNTDOWN_OPTIONS, visibleSkillCountdowns } from "../src/utils/skillCountdowns.ts";
import { SEA_MECHANICS, VOICE_RULES } from "../src/utils/voiceAnnouncements.ts";
import { loadFixture, replayFixture, type ReplayFixture } from "../tests/helpers/captureReplayHarness.ts";

const FRONTEND = join(import.meta.dirname, "..");
const FIXTURE_DIR = join(FRONTEND, "..", "tests", "fixtures", "capture_replay");
const VOICE_ROOT = join(FRONTEND, "public", "voices", "qwen3");

const manifest = JSON.parse(readFileSync(join(VOICE_ROOT, "manifest.json"), "utf8")) as {
  voices: string[];
  phrases: Record<string, string>;
};

/** 读 wav 头拿时长；播放器解码出来的就是这个长度。 */
function wavDurationMs(path: string): number {
  const buffer = readFileSync(path);
  if (buffer.toString("ascii", 0, 4) !== "RIFF") return 0;
  let offset = 12;
  let byteRate = 0;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString("ascii", offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    if (id === "fmt ") byteRate = buffer.readUInt32LE(offset + 8 + 8);
    if (id === "data") return byteRate ? Math.round((size / byteRate) * 1000) : 0;
    offset += 8 + size + (size % 2);
  }
  return 0;
}

type AudioCheck = {
  ok: boolean;
  parts: string[];
  durationMs: number;
  missingVoices: string[];
  error?: string;
};

const audioCache = new Map<string, AudioCheck>();

function checkAudio(text: string): AudioCheck {
  const cached = audioCache.get(text);
  if (cached) return cached;
  let parts: string[] = [];
  try {
    parts = announcementParts(text, manifest.phrases);
  } catch (error) {
    const result: AudioCheck = {
      ok: false,
      parts: [],
      durationMs: 0,
      missingVoices: [],
      error: error instanceof Error ? error.message : String(error),
    };
    audioCache.set(text, result);
    return result;
  }
  const missingVoices: string[] = [];
  let durationMs = 0;
  for (const voice of QWEN_VOICES) {
    let voiceMs = 0;
    for (const part of parts) {
      const hash = manifest.phrases[part];
      const file = hash ? join(VOICE_ROOT, voice.key, `${hash}.wav`) : "";
      if (!file || !existsSync(file) || statSync(file).size < 1024) {
        if (!missingVoices.includes(voice.key)) missingVoices.push(voice.key);
        continue;
      }
      voiceMs += wavDurationMs(file);
    }
    durationMs = Math.max(durationMs, voiceMs);
  }
  const result: AudioCheck = { ok: missingVoices.length === 0, parts, durationMs, missingVoices };
  audioCache.set(text, result);
  return result;
}

type OverlayRow = {
  ruleKey: string;
  label: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  /** 起手时的剩余秒数（= 冷却长度）。 */
  totalSeconds: number;
  reachedZero: boolean;
  casts: number;
  /** 收尾那一帧剩几秒（0 = 走到头；>0 = 录制结束/Boss 离场时还挂着）。 */
  lastLeft: number;
};

type MechanismRow = {
  ruleKey: string;
  name: string;
  kind: "voice+overlay" | "voice" | "overlay";
  voiceCount: number;
  voiceTexts: { text: string; count: number; firstAt: number; durationMs: number }[];
  overlay?: OverlayRow;
};

type CaptureReport = {
  capture: string;
  nestKey: string;
  nestName: string;
  countdownNest: string;
  durationMs: number;
  frames: number;
  evidenceRows: number;
  drops: Record<string, number>;
  spokenCount: number;
  audioOk: number;
  audioFailed: string[];
  spoken: { t: number; text: string; durationMs: number; parts: number }[];
  overlayRows: OverlayRow[];
  overlayStable: boolean;
  whitelistTotal: number;
  whitelistShown: number;
  mechanisms: MechanismRow[];
  gaps: { ruleKey: string; label: string; reason: string; pending: boolean }[];
  staleDropped: number;
  notes: string[];
};

function rowsForRule(fixture: ReplayFixture, ruleKey: string) {
  const nestRule = NEST_COOLDOWN_RULES.find((item) => item.key === ruleKey);
  const option = SKILL_COUNTDOWN_OPTIONS.find((item) => item.key === ruleKey);
  const ids = nestRule?.ids ?? option?.ids ?? [];
  return fixture.rows.filter((row) => {
    if (!ids.includes(Number(row.skill))) return false;
    if (!nestRule) return true;
    return nestRule.templates.includes(Number(row.templateId ?? 0));
  });
}

function buildCaptureReport(fixture: ReplayFixture): CaptureReport {
  const outcome = replayFixture(fixture);
  const nest = countdownNest(fixture.nestName);
  const whitelist = SKILL_COUNTDOWN_OPTIONS.filter((rule) => rule.nest === nest).map((rule) => ({
    key: rule.key,
    name: rule.name,
  }));

  // 悬浮窗：按帧还原每一行的生命周期。帧只在内容变化时记录，所以
  // 起止时刻就是这一行真正出现/消失的时刻。
  const overlay = new Map<string, OverlayRow>();
  let overlayStable = true;
  for (const frame of outcome.countdowns) {
    const snapshot = {
      publishedAt: frame.t,
      rows: frame.rows.map((row) => ({
        key: `${row.ruleKey}:${frame.t}`,
        ruleKey: row.ruleKey,
        label: SKILL_COUNTDOWN_OPTIONS.find((item) => item.key === row.ruleKey)?.name ?? row.ruleKey,
        remainingMs: row.remainingMs,
        durationMs: row.durationMs,
        ready: row.ready,
      })),
    };
    // 真实可见性判定：和悬浮窗用的是同一个函数。
    if (visibleSkillCountdowns(snapshot, frame.t + 150, []).length !== frame.rows.length)
      overlayStable = false;
    for (const row of frame.rows) {
      const current = overlay.get(row.ruleKey);
      if (!current) {
        overlay.set(row.ruleKey, {
          ruleKey: row.ruleKey,
          label: snapshot.rows.find((item) => item.ruleKey === row.ruleKey)?.label ?? row.ruleKey,
          startMs: frame.t,
          endMs: frame.t,
          durationMs: row.durationMs,
          totalSeconds: Math.round(row.durationMs / 1000),
          reachedZero: row.ready === true,
          casts: 1,
          lastLeft: row.left,
        });
        continue;
      }
      // 剩余秒数往回跳 = 重新施放（计时重置）。
      if (row.left > current.lastLeft + 1) current.casts += 1;
      current.endMs = frame.t;
      current.reachedZero = current.reachedZero || row.ready === true;
      current.lastLeft = row.left;
    }
  }

  const spoken = outcome.spoken.map((row) => {
    const audio = checkAudio(row.text);
    return { t: row.t, text: row.text, durationMs: audio.durationMs, parts: audio.parts.length };
  });
  const failed = [
    ...new Set(outcome.spoken.filter((row) => !checkAudio(row.text).ok).map((row) => row.text)),
  ];

  // 每条语音文案归到规则上：报告要按机制列，才好一条条听。
  // 同一个文案可能属于不同副本（「注意砸地」黑龙/沙龙都有），所以先按本场副本筛，
  // 再优先选「本场也有倒计时行」的那条，避免把沙漠龙的砸地算到食人魔头上。
  // 文案 → 候选规则。副本级规则看 texts，海龙那套文案挂在 SEA_MECHANICS 上。
  const textToRules = new Map<string, string[]>();
  const push = (text: string, key: string) => {
    const list = textToRules.get(text) ?? [];
    if (!list.includes(key)) list.push(key);
    textToRules.set(text, list);
  };
  for (const rule of VOICE_RULES) {
    if (Array.isArray(rule.texts)) for (const text of rule.texts) push(text, rule.key);
    for (const id of rule.ids) {
      const mech = (SEA_MECHANICS as Record<number, { text?: string; upcoming?: string }>)[id];
      if (mech?.text) push(mech.text, rule.key);
      if (mech?.upcoming) push(mech.upcoming, rule.key);
    }
  }
  // 按血量/模板号拼出来的文案，规则表里没有 texts，单独登记。
  for (const text of ["黑影即将爆炸"]) push(text, "sea-shadow-explosion");
  for (const text of ["速击杀牛头怪", "牛头怪即将出现"]) push(text, "sea-minotaur");
  for (const text of ["马上拉冰"]) push(text, "sea-breath");
  for (const text of ["马上飞天、注意上炮", "马上飞天、注意躲避龙息消灭暗影"]) push(text, "sea-hp-flight");
  const byKey = new Map(VOICE_RULES.map((rule) => [rule.key, rule]));
  const voiceByRule = new Map<
    string,
    { text: string; count: number; firstAt: number; durationMs: number }[]
  >();
  for (const row of spoken) {
    let candidates = (textToRules.get(row.text) ?? [])
      .map((key) => byKey.get(key))
      .filter((rule): rule is NonNullable<typeof rule> => Boolean(rule));
    if (!candidates.length && /^注意.+点名$/.test(row.text))
      candidates = VOICE_RULES.filter((rule) => rule.name.includes("点名"));
    if (!candidates.length && row.text.startsWith("注意拉冰"))
      candidates = VOICE_RULES.filter((rule) => rule.key === "sea-breath");
    const scoped = candidates.filter((rule) => rule.nest === nest);
    const pool = scoped.length ? scoped : candidates;
    const ruleKey = (pool.find((rule) => overlay.has(rule.key)) ?? pool[0])?.key ?? "unknown";
    if (ruleKey === "unknown") continue;
    const list = voiceByRule.get(ruleKey) ?? [];
    const hit = list.find((item) => item.text === row.text);
    if (hit) hit.count += 1;
    else list.push({ text: row.text, count: 1, firstAt: row.t, durationMs: row.durationMs });
    voiceByRule.set(ruleKey, list);
  }

  const nameOf = (key: string) =>
    VOICE_RULES.find((rule) => rule.key === key)?.name ??
    SKILL_COUNTDOWN_OPTIONS.find((option) => option.key === key)?.name ??
    key;

  const mechanisms: MechanismRow[] = [];
  const keys = new Set([...voiceByRule.keys(), ...overlay.keys()]);
  for (const ruleKey of keys) {
    const voice = voiceByRule.get(ruleKey) ?? [];
    const row = overlay.get(ruleKey);
    mechanisms.push({
      ruleKey,
      name: nameOf(ruleKey),
      kind: voice.length && row ? "voice+overlay" : voice.length ? "voice" : "overlay",
      voiceCount: voice.reduce((sum, item) => sum + item.count, 0),
      voiceTexts: [...voice].sort((a, b) => a.firstAt - b.firstAt),
      overlay: row,
    });
  }
  mechanisms.sort(
    (a, b) =>
      (a.overlay?.startMs ?? a.voiceTexts[0]?.firstAt ?? 0) -
      (b.overlay?.startMs ?? b.voiceTexts[0]?.firstAt ?? 0),
  );

  const gaps: CaptureReport["gaps"] = [];
  for (const rule of whitelist) {
    if (overlay.has(rule.key) || voiceByRule.has(rule.key)) continue;
    const evidence = rowsForRule(fixture, rule.key);
    gaps.push({
      ruleKey: rule.key,
      label: rule.name,
      reason: evidence.length
        ? `有 ${evidence.length} 条证据行，但语音和倒计时都没出现`
        : "本场没有该技能的抓包记录",
      pending: evidence.length > 0,
    });
  }

  const notes: string[] = [];
  if (!fixture.rows.length) notes.push("空包（录制误触，没有事件）");
  if (fixture.drops && Object.keys(fixture.drops).length)
    notes.push(`解码丢弃：${JSON.stringify(fixture.drops)}`);
  const overlayOnly = mechanisms.filter((item) => item.kind === "overlay");
  if (overlayOnly.length)
    notes.push(`只有倒计时、没有语音：${overlayOnly.map((item) => item.name).join("、")}`);

  return {
    capture: fixture.capture,
    nestKey: fixture.nestKey || "",
    nestName: fixture.nestName || "(未知)",
    countdownNest: nest,
    durationMs: fixture.durationMs,
    frames: fixture.frames,
    evidenceRows: fixture.rows.length,
    drops: fixture.drops || {},
    spokenCount: spoken.length,
    audioOk: spoken.length - outcome.spoken.filter((row) => !checkAudio(row.text).ok).length,
    audioFailed: failed,
    spoken,
    overlayRows: [...overlay.values()].sort((a, b) => a.startMs - b.startMs),
    overlayStable,
    whitelistTotal: whitelist.length,
    whitelistShown: whitelist.filter((rule) => overlay.has(rule.key) || voiceByRule.has(rule.key)).length,
    mechanisms,
    gaps,
    staleDropped: outcome.staleDropped,
    notes,
  };
}

const fixtures = readdirSync(FIXTURE_DIR)
  .filter((name) => name.endsWith(".json"))
  .sort();

const reports = fixtures.map((name) => buildCaptureReport(loadFixture(join(FIXTURE_DIR, name))));

// 全库音频完整性：不止这几场用到的句子。
const libraryMissing: string[] = [];
for (const voice of QWEN_VOICES)
  for (const [text, hash] of Object.entries(manifest.phrases))
    if (!existsSync(join(VOICE_ROOT, voice.key, `${hash}.wav`))) libraryMissing.push(`${voice.key}/${text}`);
const audioStats = {
  phrases: Object.keys(manifest.phrases).length,
  voices: QWEN_VOICES.length,
  libraryMissing: libraryMissing.length,
  splitFailed: [...audioCache.values()].filter((item) => item.error).length,
};

const outPath = process.argv[2] || join(FRONTEND, "..", ".dev", "selftest", "replay-selftest.json");
writeFileSync(
  outPath,
  JSON.stringify({ generatedAt: new Date().toISOString(), audioStats, reports }, null, 2),
  "utf8",
);
console.log(`音频资产：${audioStats.phrases} 句 × ${audioStats.voices} 音色，缺失 ${audioStats.libraryMissing} 个`);

for (const report of reports) {
  console.log(
    `${report.capture}  ${report.nestName}  ${(report.durationMs / 1000).toFixed(0)}s  ` +
      `语音 ${report.spokenCount}（可播 ${report.audioOk}）  悬浮窗 ${report.overlayRows.length} 行  ` +
      `白名单 ${report.whitelistShown}/${report.whitelistTotal}  stale ${report.staleDropped}`,
  );
  for (const gap of report.gaps) console.log(`    ${gap.pending ? "✗" : "·"} ${gap.label}：${gap.reason}`);
  if (!report.overlayStable) console.log("    ✗ 悬浮窗发布断档");
}
console.log(`\n写入 ${outPath}`);
