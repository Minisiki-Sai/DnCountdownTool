/**
 * 回放数据源的语义：分批、游标、暂停/跳转、以及「只换数据源不加分支」这条底线。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import {
  pauseReplay,
  replayActive,
  replayBuffOverlayStatus,
  replayCaptureStatus,
  replayElapsedMs,
  replayFinished,
  replaySession,
  resumeReplay,
  seekReplay,
  startReplay,
  stopReplay,
  takeReplayBatch,
  type ReplayPayload,
} from "../src/services/replaySource.ts";

function payload(rowCount = 5): ReplayPayload {
  return {
    capture: "capture_test.dpscap",
    nestName: "黑龙经典",
    nestKey: "classic",
    durationMs: 1_000,
    rows: Array.from({ length: rowCount }, (_, index) => ({
      timestamp: index * 100,
      t: index,
      kind: "structured_action",
      skill: 34771 + index,
      templateId: 503652,
      source: "0x00000001@503652",
    })),
  };
}

test("按墙钟分批吐行，序号连续", async () => {
  startReplay("job1", payload());
  await new Promise((resolve) => setTimeout(resolve, 260));
  const first = takeReplayBatch();
  assert.ok(first);
  assert.equal(first.state, "running");
  assert.equal(first.clockRunning, true);
  assert.equal(first.sessionId, "replay:job1");
  assert.ok(first.voiceEvents.length >= 2, `第一拍只拿到 ${first.voiceEvents.length} 行`);
  assert.deepEqual(
    first.voiceEvents.map((row) => row.sequence),
    first.voiceEvents.map((_, index) => index + 1),
    "序号必须从 1 开始连续递增",
  );
  // observedAt 用当前墙钟，这样 5 秒新鲜度门和实战一致。
  const nowSeconds = Date.now() / 1000;
  for (const row of first.voiceEvents) {
    assert.ok(Math.abs(nowSeconds - row.observedAt) < 2);
  }
  await new Promise((resolve) => setTimeout(resolve, 900));
  const rest = takeReplayBatch();
  assert.ok(rest);
  assert.equal(first.voiceEvents.length + rest.voiceEvents.length, 5, "五行必须全部吐出");
  stopReplay();
});

test("跳转会把游标重算到目标时刻之前的位置", async () => {
  startReplay("job2", payload(), { offsetMs: 0 });
  seekReplay(200);
  await new Promise((resolve) => setTimeout(resolve, 260));
  const batch = takeReplayBatch();
  assert.ok(batch);
  assert.ok(batch.voiceEvents.length > 0, "跳转后应该继续放后面的事件");
  for (const row of batch.voiceEvents) {
    assert.ok(row.timestamp >= 200, `跳转后不该重放 ${row.timestamp}ms 之前的事件`);
  }
  stopReplay();
});

test("暂停后不再推进游标，继续后接着走", async () => {
  startReplay("job3", payload(), { offsetMs: 0 });
  await new Promise((resolve) => setTimeout(resolve, 260));
  assert.ok((takeReplayBatch()?.voiceEvents.length ?? 0) > 0);
  pauseReplay();
  const frozen = replayElapsedMs();
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(replayElapsedMs(), frozen, "暂停期间游标不能动");
  assert.equal(takeReplayBatch()?.voiceEvents.length, 0);
  resumeReplay();
  assert.ok(replayActive());
  stopReplay();
  assert.equal(replayActive(), false);
  assert.equal(takeReplayBatch(), null);
});

test("回放时装作「正在统计」，但不动原始字段", () => {
  startReplay("job4", payload());
  const base = { state: "idle", status: "待机", npcapInstalled: true, sourceFile: "x" };
  const patched = replayCaptureStatus(base);
  assert.equal(patched.state, "running");
  assert.equal(patched.nestLabel, "黑龙经典");
  assert.equal(patched.nestKey, "classic");
  assert.equal(patched.npcapInstalled, true, "其它字段必须原样保留");
  stopReplay();
  assert.equal(replayCaptureStatus(base).state, "idle", "没有回放时不许改状态");
});

test("悬浮窗快照也必须冒充「正在统计」，否则窗口写「统计已停止」", () => {
  // 悬浮窗窗口不访问后端，只吃主窗口推来的 combat.snapshot（源头就是
  // /api/capture/buff-overlay）。state 不是 running 时它会整块判成已停止，
  // 连倒计时行都不渲染 —— 于是语音在念、窗口却说没在统计。
  startReplay("job6", payload());
  const base = {
    state: "stopped",
    status: "待机",
    elapsedSeconds: 0,
    // 上一场真实抓包留下的残留，回放时一条都不能带进去。
    bossName: "上一场的 Boss",
    bossTargetId: "0x00000009@503652",
    bossHealth: [{ instanceId: "a", targetId: "b", name: "旧血条" }],
    endedBossIds: ["0x00000009@503652"],
    players: [{ source_id: "old", name: "旧玩家", damage: 1 }],
    buffOptions: [{ key: "x" }],
  };
  const patched = replayBuffOverlayStatus(base);
  assert.equal(patched.state, "running");
  assert.equal(patched.nestName, "黑龙经典");
  assert.equal(patched.nestKey, "classic");
  assert.deepEqual(patched.endedBossIds, [], "旧的结束标记会抹掉刚建起来的倒计时");
  assert.deepEqual(patched.bossHealth, []);
  assert.deepEqual(patched.players, []);
  assert.equal(patched.bossName, "");
  assert.deepEqual(patched.buffOptions, [{ key: "x" }], "增益目录还得留着，设置面板要用");
  stopReplay();
  assert.equal(replayBuffOverlayStatus(base).state, "stopped", "没有回放时不许改状态");
});

test("Boss 行按游标取「当时那一只」，不填上一场的残留", () => {
  // 证据行只有模板号，名字在后端给的 bosses 名单里。回放跳到哪，就该显示
  // 那一刻在场的 Boss；把名字清空的旧写法会让悬浮窗一直写「未识别」。
  startReplay(
    "job7",
    {
      ...payload(2),
      bosses: [
        { templateId: 503639, name: "黑色戒指惩罚者梅鲁卡", targetId: "0x31@503639" },
        { templateId: 503652, name: "黑色戒指惩罚者梅鲁卡（二阶段）", targetId: "0x6C@503652" },
      ],
      rows: [
        { timestamp: 0, skill: 0, templateId: 503639, source: "0x31@503639" },
        { timestamp: 500, skill: 34771, templateId: 503652, source: "0x6C@503652" },
      ],
    },
    { offsetMs: 100 },
  );
  const early = replayBuffOverlayStatus({
    bossName: "上一场的 Boss",
    bossTemplateId: 999,
    bossTargetId: "0x99@999",
    endedBossIds: ["0x6C@503652"],
  });
  assert.equal(early.bossName, "黑色戒指惩罚者梅鲁卡");
  assert.equal(early.bossTemplateId, 503639);
  assert.deepEqual(early.endedBossIds, [], "旧的结束标记会把旋风行过滤掉");

  seekReplay(600);
  const late = replayBuffOverlayStatus({ bossName: "上一场的 Boss" });
  assert.equal(late.bossName, "黑色戒指惩罚者梅鲁卡（二阶段）");
  assert.equal(late.bossTemplateId, 503652);
  stopReplay();
});

test("名单里查不到模板号时回落到最后出场那只，不突然变未识别", () => {
  startReplay("job8", {
    ...payload(1),
    bosses: [{ templateId: 503652, name: "黑色戒指惩罚者梅鲁卡", targetId: "0x6C@503652" }],
    rows: [{ timestamp: 0, skill: 0, templateId: 888888, source: "0x01@888888" }],
  });
  const patched = replayBuffOverlayStatus({ bossName: "" });
  assert.equal(patched.bossName, "黑色戒指惩罚者梅鲁卡");
  stopReplay();
});

test("时长用尽会判定结束", async () => {
  startReplay("job5", payload(), { offsetMs: 0 });
  await new Promise((resolve) => setTimeout(resolve, 1_100));
  assert.equal(replayFinished(), true);
  assert.equal(replaySession()?.paused, false);
  stopReplay();
});

test("语音组件里不许出现「回放中」分支", () => {
  // 这条是底线：回放必须靠换数据源实现。一旦有人往播报逻辑里加 if (回放)，
  // 「回放跑通 = 实战对」这个前提立刻失效。
  const page = readFileSync(
    join(import.meta.dirname, "..", "src", "components", "VoiceAnnouncementSettings.vue"),
    "utf8",
  );
  const engine = readFileSync(
    join(import.meta.dirname, "..", "src", "utils", "voiceBatchEngine.ts"),
    "utf8",
  );
  for (const source of [page, engine]) {
    assert.ok(!/replay/i.test(source), "播报链路里出现了 replay 分支");
  }
});

test("开发者模式默认关闭，入口只有开了才出现", () => {
  const settings = readFileSync(
    join(import.meta.dirname, "..", "src", "views", "SettingsView.vue"),
    "utf8",
  );
  assert.match(settings, /localStorage\.getItem\(DEV_MODE_KEY\) === "1"/);
  assert.match(settings, /v-if="developerMode"/);
  assert.match(settings, /emit\('open-replay'\)/);
});
