/**
 * 用昨天的实战抓包回放「语音播报 + 悬浮窗倒计时」，代替实机验证。
 *
 * 数据来源：`tests/fixtures/capture_replay/*.json`，由
 *   python tools/capture_replay.py <抓包目录> --out tests/fixtures/capture_replay
 * 生成。回放链路是「抓包 → 解码 → 证据行 → 真实前端规则」，不复制规则。
 *
 * 这里断言三类东西：
 *   1. 守恒：解码器不许有「解出来但被丢掉」的 Boss 技能（`drops`）。
 *   2. 语义：每个副本的关键机制必须真的播出来，倒计时行必须来自已知规则。
 *   3. 防漂移：回放约定的门控常量必须和页面源码一致。
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { NEST_COOLDOWN_RULES } from "../src/utils/nestSkillCooldowns.ts";
import { SEA_MECHANICS, VOICE_RULES } from "../src/utils/voiceAnnouncements.ts";
import {
  FRESH_WINDOW_S,
  REPEAT_WINDOW_MS,
  STALE_WINDOW_MS,
  TICK_MS,
  describeOutcome,
  loadFixture,
  replayFixture,
  type ReplayOutcome,
} from "./helpers/captureReplayHarness.ts";

const FIXTURE_DIR = join(import.meta.dirname, "..", "..", "tests", "fixtures", "capture_replay");
const fixtures = readdirSync(FIXTURE_DIR)
  .filter((name) => name.endsWith(".json"))
  .sort();

const outcomes = new Map<string, ReplayOutcome>();
for (const name of fixtures) {
  const fixture = loadFixture(join(FIXTURE_DIR, name));
  outcomes.set(fixture.capture, replayFixture(fixture));
}

function outcomeOf(capture: string): ReplayOutcome {
  const found = outcomes.get(capture);
  assert.ok(found, `fixture 里没有 ${capture}`);
  return found;
}

function spokenTexts(outcome: ReplayOutcome): string[] {
  return outcome.spoken.map((row) => row.text);
}

function countdownRuleKeys(outcome: ReplayOutcome): Set<string> {
  const keys = new Set<string>();
  for (const frame of outcome.countdowns) for (const row of frame.rows) keys.add(row.ruleKey);
  return keys;
}

test("fixture 覆盖到了昨天的四类实战", () => {
  assert.ok(fixtures.length >= 10, `fixture 只有 ${fixtures.length} 个，覆盖不足`);
  const names = fixtures.join("\n");
  for (const keyword of ["梦魇海龙", "黑龙硬核", "黑龙经典", "沙龙硬核"]) {
    assert.ok(names.includes(keyword), `缺少 ${keyword} 的抓包：\n${names}`);
  }
});

test("每个抓包解码都守恒：没有 Boss 技能被静默丢掉", () => {
  for (const name of fixtures) {
    const fixture = loadFixture(join(FIXTURE_DIR, name));
    assert.deepEqual(
      fixture.drops,
      {},
      `${fixture.capture} 有被丢弃的 Boss 技能：${JSON.stringify(fixture.drops)}`,
    );
  }
});

test("每个抓包都能认出副本", () => {
  for (const [, outcome] of outcomes) {
    if (outcome.capture.includes("134741")) continue; // 5 秒空包，本来就认不出
    assert.ok(outcome.nest, `${outcome.capture} 没识别出副本`);
  }
});

test("黑龙经典：砸地/冲撞/旋风/小追/点名 都要播出来", () => {
  const texts = spokenTexts(outcomeOf("capture_20261024_黑龙经典.dpscap"));
  for (const expected of ["准备冲撞机制", "注意砸地", "注意旋风", "小追已好", "马上起飞"]) {
    assert.ok(
      texts.some((text) => text.includes(expected)),
      `黑龙经典缺「${expected}」\n` + texts.slice(0, 40).join(" / "),
    );
  }
});

test("黑龙经典：悬浮窗倒计时覆盖一王到龙的全部机制", () => {
  const keys = countdownRuleKeys(outcomeOf("capture_20261024_黑龙经典.dpscap"));
  for (const expected of [
    "classic-ogre-charge",
    "classic-whirlwind",
    "classic-chase",
    "classic-tracking",
  ]) {
    assert.ok(keys.has(expected), `黑龙经典倒计时缺 ${expected}，实际 ${[...keys].join(",")}`);
  }
});

test("黑龙硬核：职业点名与追踪/甩尾都要播", () => {
  const texts = spokenTexts(outcomeOf("capture_20261024_黑龙硬核.dpscap"));
  assert.ok(
    texts.some((text) => /^注意.+点名$/.test(text)),
    `黑龙硬核没有职业点名播报：${texts.slice(0, 30).join(" / ")}`,
  );
  for (const expected of ["注意追踪", "注意甩尾", "上升重力已好"]) {
    assert.ok(texts.some((text) => text.includes(expected)), `黑龙硬核缺「${expected}」`);
  }
});

test("黑龙硬核：分边冷却行要出现", () => {
  const keys = countdownRuleKeys(outcomeOf("capture_20261024_黑龙硬核.dpscap"));
  for (const expected of ["hard-tracking", "hard-tail", "hard-gravity"]) {
    assert.ok(keys.has(expected), `黑龙硬核倒计时缺 ${expected}，实际 ${[...keys].join(",")}`);
  }
});

test("梦魇海龙：海龙与蝎狮两套机制都要有", () => {
  const outcome = outcomeOf("capture_20261024_梦魇海龙.dpscap");
  const keys = countdownRuleKeys(outcome);
  assert.ok(
    [...keys].some((key) => key.startsWith("sea-apocalypse-")),
    `梦魇海龙没有海龙机制行：${[...keys].join(",")}`,
  );
  assert.ok(
    [...keys].some((key) => key.startsWith("sea-manticore-")),
    `梦魇海龙没有蝎狮机制行：${[...keys].join(",")}`,
  );
  const texts = spokenTexts(outcome);
  for (const expected of ["注意地面激光", "注意旋转", "注意飞天"]) {
    assert.ok(texts.some((text) => text.includes(expected)), `梦魇海龙缺「${expected}」`);
  }
});

test("沙龙硬核：沙虫/蝎子/石头人/狮子/龙 五段机制都要有", () => {
  const outcome = outcomeOf("capture_20261024_沙龙硬核.dpscap");
  const texts = spokenTexts(outcome);
  for (const expected of ["注意沙虫", "注意踢飞", "注意闪光转身", "注意三连冲"]) {
    assert.ok(texts.some((text) => text.includes(expected)), `沙龙硬核缺「${expected}」`);
  }
  const keys = countdownRuleKeys(outcome);
  for (const expected of ["sand-basic", "sand-scorpion", "sand-lion", "sand-golem-storm"]) {
    assert.ok(keys.has(expected), `沙龙硬核倒计时缺 ${expected}`);
  }
});

test("黑龙巢穴（三）：小追必须有播报且有倒计时", () => {
  // 线上事故：红龙佩达 P1/P2 在客户端怪物表里 boss=False，技能证据被整段丢掉，
  // 表现为「血量有、机制一条没有」。这条断言就是那个 bug 的锁。
  for (const capture of ["capture_20261025_134133.dpscap", "capture_20261025_134808.dpscap"]) {
    const outcome = outcomeOf(capture);
    const texts = spokenTexts(outcome);
    assert.ok(
      texts.includes("小追已好"),
      `${capture} 没有小追播报：${texts.join(" / ") || "(空)"}`,
    );
    assert.ok(
      countdownRuleKeys(outcome).has("classic-chase"),
      `${capture} 没有小追倒计时行`,
    );
  }
});

test("小追倒计时必须从满时长一路走到 0，而不是露一下就消失", () => {
  const outcome = outcomeOf("capture_20261025_134133.dpscap");
  const frames = outcome.countdowns.filter((frame) =>
    frame.rows.some((row) => row.ruleKey === "classic-chase"),
  );
  assert.ok(frames.length > 10, `小追倒计时只出现 ${frames.length} 帧，像是闪一下就没了`);
  const first = frames[0].rows.find((row) => row.ruleKey === "classic-chase");
  const last = frames[frames.length - 1].rows.find((row) => row.ruleKey === "classic-chase");
  assert.ok(first && last);
  assert.equal(first.left, 30, `小追起始不该是 ${first?.left}s`);
  assert.equal(last.left, 0);
  assert.equal(last.ready, true);
});

test("所有悬浮窗行都必须来自已知规则表", () => {
  const known = new Set<string>([
    ...VOICE_RULES.map((rule) => rule.key),
    ...NEST_COOLDOWN_RULES.map((rule) => rule.key),
    ...Object.values(SEA_MECHANICS).map(() => ""),
  ]);
  for (const [, outcome] of outcomes) {
    for (const key of countdownRuleKeys(outcome)) {
      assert.ok(known.has(key), `${outcome.capture} 出现了规则表里没有的行 ${key}`);
    }
  }
});

test("每句播报都能追溯到触发它的那行数据，不存在凭空播报", () => {
  // 比「文案白名单」更硬的做法：回放给每句话标 `via`，指向具体的行（或到期提醒）。
  // 只要出现一句无出处的话，说明规则层或回放层有幽灵路径。
  for (const name of fixtures) {
    const fixture = loadFixture(join(FIXTURE_DIR, name));
    const outcome = outcomes.get(fixture.capture)!;
    const rowKeys = new Set(
      fixture.rows.map((row) => `row:${row.timestamp}:${row.skill}`),
    );
    for (const row of outcome.spoken) {
      assert.ok(row.via, `${outcome.capture} 在 t=${(row.t / 1000).toFixed(1)}s 播了「${row.text}」但没有 via`);
      if (row.via === "reminder:due") continue;
      if (row.via.startsWith("clock:")) {
        const stamp = row.via.slice("clock:".length);
        assert.ok(
          fixture.rows.some((item) => String(item.timestamp) === stamp),
          `${outcome.capture} 的「${row.text}」挂在不存在的批 ${row.via}`,
        );
        continue;
      }
      const [timestamp, skill] = row.via.split(":").slice(1, 3);
      assert.ok(
        rowKeys.has(`row:${timestamp}:${skill}`),
        `${outcome.capture} 的「${row.text}」追溯到不存在的行 ${row.via}`,
      );
    }
  }
});

test("门控只有一份实现：页面不许再自己抄一遍", () => {
  // 门控常量与判断逻辑现在只在 voiceBatchEngine 里。页面如果又抄一份，
  // 回放（走引擎）和实战（走页面）就会悄悄分叉 —— 这正是这次重构要消灭的。
  assert.equal(STALE_WINDOW_MS, 6000, "6 秒时效门被改了，回放和实机要一起改");
  assert.equal(FRESH_WINDOW_S, 5, "5 秒新鲜度门被改了");
  assert.equal(REPEAT_WINDOW_MS, 10000, "10 秒去重窗口被改了");
  assert.equal(TICK_MS, 150, "tick 周期被改了");

  const page = readFileSync(
    join(import.meta.dirname, "..", "src", "components", "VoiceAnnouncementSettings.vue"),
    "utf8",
  );
  for (const duplicated of [
    "observedAt <= 5",
    "observedAt<=5",
    "> 6000",
    ">6000",
  ]) {
    assert.ok(
      !page.includes(duplicated),
      `页面里又出现了自己抄的门控「${duplicated}」，应该改用 voiceBatchEngine`,
    );
  }
});

test("回放结果可复现：同一份 fixture 跑两次结果一致", () => {
  const name = "capture_20261025_134133.json";
  const first = replayFixture(loadFixture(join(FIXTURE_DIR, name)));
  const second = replayFixture(loadFixture(join(FIXTURE_DIR, name)));
  assert.deepEqual(describeOutcome(first), describeOutcome(second));
  assert.deepEqual(first.spoken, second.spoken);
});

// ── 投递延迟矩阵 ────────────────────────────────────────────────────────────
// 实机按 150ms 轮询取批，主窗口被节流时批会晚到几秒甚至几十秒。抓包记不到
// 「到底晚了多少」，所以不能靠再打一场来复现 —— 但延迟是参数，可以全测一遍。
test("延迟 ≤3 秒：该播的全播出来，一条都不能少", () => {
  // 实测边界：时效门是 `elapsed - timestamp <= 6000`，再叠加 150ms 心跳粒度，
  // 所以真正安全的投递延迟在 3 秒量级，到 6 秒就已经踩线了。这条锁住安全区。
  const fixture = loadFixture(join(FIXTURE_DIR, "capture_20261024_黑龙经典.json"));
  const baseline = new Set(replayFixture(fixture).spoken.map((row) => row.text));
  for (const delay of [0, 150, 1000, 3000]) {
    const outcome = replayFixture(fixture, { deliveryDelayMs: delay });
    const texts = new Set(outcome.spoken.map((row) => row.text));
    const missing = [...baseline].filter((text) => !texts.has(text));
    assert.deepEqual(missing, [], `延迟 ${delay}ms 时丢了：${missing.join(" / ")}`);
    assert.equal(outcome.staleDropped, 0, `延迟 ${delay}ms 不该触发时效门`);
  }
});

test("延迟 6 秒就已经踩线：时效门按设计开始丢行，并且被计数", () => {
  const fixture = loadFixture(join(FIXTURE_DIR, "capture_20261024_黑龙经典.json"));
  const outcome = replayFixture(fixture, { deliveryDelayMs: 6000 });
  assert.ok(
    outcome.staleDropped > 0,
    "6 秒延迟必须开始丢行；如果一条都没丢，说明失效门被谁放开了",
  );
});

test("延迟 >6 秒：超时行被计数丢弃，不是静默消失", () => {
  const fixture = loadFixture(join(FIXTURE_DIR, "capture_20261024_黑龙经典.json"));
  for (const delay of [7000, 30000]) {
    const outcome = replayFixture(fixture, { deliveryDelayMs: delay });
    assert.ok(
      outcome.staleDropped > 0,
      `延迟 ${delay}ms 必须触发时效门并被计数，否则等于静默丢播报`,
    );
  }
});

test("延迟 30 秒：倒计时不再是「几秒就消失」，而是与丢行数同源", () => {
  // 这条锁的是「主窗口被节流」这类现场：行还在，但已经过期，被时效门挡掉。
  // 表现就是用户看到的「机制出来了，倒计时几秒就没了 / 语音只播一半」。
  const fixture = loadFixture(join(FIXTURE_DIR, "capture_20261024_黑龙经典.json"));
  const healthy = replayFixture(fixture);
  const throttled = replayFixture(fixture, { deliveryDelayMs: 30000 });
  assert.ok(
    throttled.spoken.length < healthy.spoken.length,
    "严重节流下播报必然变少；如果没变少，说明时效门根本没生效",
  );
  assert.ok(throttled.staleDropped > healthy.staleDropped);
});
