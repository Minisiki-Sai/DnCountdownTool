import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/**
 * 技能倒计时窗口必须订阅 skill.countdowns。
 *
 * 线上事故：订阅守卫写成了 `props.view === 'status'`，而技能窗的 view 是
 * `'skill'`、增益窗才是 `'status'`。结果技能窗永远收不到倒计时快照
 * （`skillCountdownRows` 恒为空），增益窗订阅了却从不渲染它 —— 表现就是
 * 「语音有声、悬浮窗一直不出倒计时」，四个副本全中。
 */

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "src", "composables", "overlay", "useBuffOverlay.ts"),
  "utf8",
);

test("skill.countdowns 只由技能窗（isSkillView）订阅", () => {
  const index = source.indexOf("subscribeOverlayEvent<SkillCountdownSnapshot>");
  assert.ok(index > 0, "找不到 skill.countdowns 的订阅代码");

  // 订阅语句前面最近的一次守卫才是它的条件。
  const before = source.slice(Math.max(0, index - 600), index);
  assert.match(before, /isSkillView\.value/);
  assert.doesNotMatch(before, /props\.view\s*===\s*["']status["']/);
});

test("倒计时行只在技能窗渲染", () => {
  const index = source.indexOf("const skillCountdownRows");
  assert.ok(index > 0);
  const body = source.slice(index, index + 400);
  assert.match(body, /props\.view !== ["']skill["']/);
});
