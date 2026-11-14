/**
 * 首页两张总开关卡的标签兜底。
 *
 * 背景：这两张卡的文字以前只来自 `/api/capture/toggles`，请求失败时 `<section>`
 * 渲染成空的，表现就是「第一次打开客户端，首页两个按钮不见了，重开一次才有」。
 * 壳和引擎并行启动，首次启动 WebView2 冷建配置目录、引擎还没 bind 48710，
 * 这个请求必然抢在后端前面，所以兜底标签是必需品，而 1 秒轮询负责补上权威值。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  CAPTURE_TOGGLE_FALLBACK_LABELS,
  resolveCaptureToggleLabels,
} from "../src/utils/captureToggles.ts";

const BACKEND_SOURCE = new URL("../../engine/live/capture_toggles.py", import.meta.url);

/** 从后端源码里抠出权威文案，确保兜底与它逐字一致（否则应答前后会闪一下）。 */
function backendLabels(): Record<string, string> {
  const source = readFileSync(BACKEND_SOURCE, "utf8");
  const labels: Record<string, string> = {};
  for (const match of source.matchAll(/"(buffs|boss_actions)":\s*"([^"]+)"/g)) {
    labels[match[1]] = match[2];
  }
  return labels;
}

test("兜底文案与后端 CAPTURE_TOGGLE_LABELS 逐字一致", () => {
  assert.deepEqual(CAPTURE_TOGGLE_FALLBACK_LABELS, backendLabels());
});

test("后端应答时用后端标签，并标记为权威", () => {
  const resolved = resolveCaptureToggleLabels({
    buffs: "增益 / 减益倒计时",
    boss_actions: "Boss 技能 / 动作倒计时",
  });
  assert.deepEqual(resolved.labels, {
    buffs: "增益 / 减益倒计时",
    boss_actions: "Boss 技能 / 动作倒计时",
  });
  assert.equal(resolved.authoritative, true);
});

test("后端还没起来时用兜底，两张卡都在且不算权威", () => {
  for (const payload of [undefined, null, {}]) {
    const resolved = resolveCaptureToggleLabels(payload);
    assert.deepEqual(resolved.labels, CAPTURE_TOGGLE_FALLBACK_LABELS);
    assert.equal(Object.keys(resolved.labels).length, 2);
    assert.equal(resolved.authoritative, false);
  }
});

test("空白标签视为没拿到，不能让一张卡消失", () => {
  const resolved = resolveCaptureToggleLabels({ buffs: "  ", boss_actions: "" });
  assert.deepEqual(resolved.labels, CAPTURE_TOGGLE_FALLBACK_LABELS);
  assert.equal(resolved.authoritative, false);
});

test("后端只回了一个 key 时，另一张卡沿用兜底", () => {
  const resolved = resolveCaptureToggleLabels({ buffs: "增益 / 减益倒计时" });
  assert.equal(resolved.labels.buffs, "增益 / 减益倒计时");
  assert.equal(resolved.labels.boss_actions, CAPTURE_TOGGLE_FALLBACK_LABELS.boss_actions);
  assert.equal(resolved.authoritative, true);
});

test("后端新增分类不会被吞掉", () => {
  const resolved = resolveCaptureToggleLabels({
    buffs: "增益 / 减益倒计时",
    boss_actions: "Boss 技能 / 动作倒计时",
    summon: "召唤物倒计时",
  });
  assert.equal(resolved.labels.summon, "召唤物倒计时");
  assert.equal(Object.keys(resolved.labels).length, 3);
});
