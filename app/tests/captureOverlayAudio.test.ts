/**
 * 回放的**下游两层**：音频资产 与 悬浮窗可见性。
 *
 * 抓包回放能证明「该念哪句、倒计时多少秒」，但如果那句没有 wav 资产，
 * 实战里就是干张嘴；如果倒计时的发布断了 3 秒，窗口就整行清空 ——
 * 用户看到的正是「语音只播一半 / 倒计时几秒就消失」。
 *
 * 这两层都是程序自己的逻辑和资产，都能离线判定，不需要再打一场。
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { visibleSkillCountdowns } from "../src/utils/skillCountdowns.ts";
import { loadFixture, replayFixture, type ReplayOutcome } from "./helpers/captureReplayHarness.ts";

const FRONTEND = join(import.meta.dirname, "..");
const FIXTURE_DIR = join(FRONTEND, "..", "tests", "fixtures", "capture_replay");
const VOICE_ROOT = join(FRONTEND, "public", "voices", "qwen3");

const fixtures = readdirSync(FIXTURE_DIR)
  .filter((name) => name.endsWith(".json"))
  .sort();

const outcomes = new Map<string, ReplayOutcome>();
for (const name of fixtures) {
  const fixture = loadFixture(join(FIXTURE_DIR, name));
  outcomes.set(fixture.capture, replayFixture(fixture));
}

const manifest = JSON.parse(readFileSync(join(VOICE_ROOT, "manifest.json"), "utf8")) as {
  voices: string[];
  phrases: Record<string, string>;
};
const voices = Array.isArray(manifest.voices) && manifest.voices.length
  ? manifest.voices
  : readdirSync(VOICE_ROOT, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name);

function spokenTexts(): { capture: string; text: string; t: number }[] {
  const rows: { capture: string; text: string; t: number }[] = [];
  for (const [, outcome] of outcomes) {
    for (const row of outcome.spoken) rows.push({ capture: outcome.capture, text: row.text, t: row.t });
  }
  return rows;
}

test("每个音色目录都在，并且 manifest 与目录一致", () => {
  assert.ok(voices.length >= 4, `音色只有 ${voices.length} 个`);
  for (const voice of voices) {
    assert.ok(existsSync(join(VOICE_ROOT, voice)), `缺少音色目录 ${voice}`);
  }
});

test("回放里出现的每一句播报都有 wav 资产", () => {
  const missing: string[] = [];
  for (const { capture, text } of spokenTexts()) {
    const hash = manifest.phrases[text];
    if (!hash) {
      missing.push(`${capture}: 「${text}」不在 manifest`);
      continue;
    }
    for (const voice of voices) {
      if (!existsSync(join(VOICE_ROOT, voice, `${hash}.wav`))) {
        missing.push(`${capture}: 「${text}」缺 ${voice} 的 ${hash}.wav`);
      }
    }
  }
  assert.deepEqual([...new Set(missing)], [], "这些播报念出来会是哑的");
});

test("职业点名是按职业动态拼的，所有职业都得有资产", () => {
  const jobs = new Set<string>();
  for (const { text } of spokenTexts()) {
    const match = /^注意(.+)点名$/.exec(text);
    if (match) jobs.add(match[1]);
  }
  assert.ok(jobs.size > 0, "这批抓包里没出现职业点名，覆盖不到这条链路");
  const missing = [...jobs].filter((job) => !manifest.phrases[`注意${job}点名`]);
  assert.deepEqual(missing, [], `这些职业点名没有资产：${missing.join(",")}`);
});

test("发布不断时，悬浮窗倒计时全程可见（不会自己消失）", () => {
  // visibleSkillCountdowns 是悬浮窗真正用来决定「这一帧显示什么」的函数：
  // 快照超过 3 秒没更新就整表清空。只要发布按 150ms 走，行就该一直在。
  for (const [, outcome] of outcomes) {
    for (const frame of outcome.countdowns) {
      if (!frame.rows.length) continue;
      const snapshot = {
        publishedAt: frame.t,
        rows: frame.rows.map((row) => ({
          key: `${row.ruleKey}:test`,
          ruleKey: row.ruleKey,
          label: row.ruleKey,
          remainingMs: row.remainingMs,
          durationMs: row.durationMs,
          ready: row.ready,
        })),
      };
      const visible = visibleSkillCountdowns(snapshot, frame.t + 150, []);
      assert.equal(
        visible.length,
        frame.rows.length,
        `${outcome.capture} 在 t=${frame.t} 的 ${frame.rows.length} 行里只显示了 ${visible.length} 行`,
      );
    }
  }
});

test("发布断掉超过 3 秒，悬浮窗整表清空 —— 这就是「几秒就消失」的机制", () => {
  const outcome = outcomes.get("capture_20261025_134133.dpscap");
  assert.ok(outcome, "缺少小追那场 fixture");
  const frame = outcome.countdowns.find((item) =>
    item.rows.some((row) => row.ruleKey === "classic-chase"),
  );
  assert.ok(frame, "小追倒计时没出现");
  const snapshot = {
    publishedAt: frame.t,
    rows: frame.rows.map((row) => ({
      key: `${row.ruleKey}:test`,
      ruleKey: row.ruleKey,
      label: row.ruleKey,
      remainingMs: row.remainingMs,
      durationMs: row.durationMs,
      ready: row.ready,
    })),
  };
  assert.ok(
    visibleSkillCountdowns(snapshot, frame.t + 2900, []).length > 0,
    "3 秒内必须还看得见",
  );
  assert.equal(
    visibleSkillCountdowns(snapshot, frame.t + 3100, []).length,
    0,
    "超过 3 秒没发布就必须清空；这条是设计行为，不是 bug",
  );
});

test("Boss 离场会立刻清掉它的倒计时行", () => {
  const outcome = outcomes.get("capture_20261025_134133.dpscap");
  assert.ok(outcome);
  const frame = outcome.countdowns.find((item) =>
    item.rows.some((row) => row.ruleKey === "classic-chase"),
  );
  assert.ok(frame);
  const source = "0x00000009@503681";
  const snapshot = {
    publishedAt: frame.t,
    rows: frame.rows.map((row) => ({
      key: `${row.ruleKey}:test`,
      source,
      ruleKey: row.ruleKey,
      label: row.ruleKey,
      remainingMs: row.remainingMs,
      durationMs: row.durationMs,
      ready: row.ready,
    })),
  };
  assert.equal(
    visibleSkillCountdowns(snapshot, frame.t + 150, [source]).length,
    0,
    "endedBossIds 命中就该整行消失",
  );
});
