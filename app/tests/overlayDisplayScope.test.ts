/**
 * 两层开关的边界：
 *   首页总开关（buffs / boss_actions）   → 整类悬浮窗显不显示，语音不受影响
 *   设置里的分项（Buff / Debuff / 技能）  → 窗口内部显示哪一部分
 * 以及设置页「实时预览」必须跟着分项开关走。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DEFAULT_BUFF_OVERLAY_SETTINGS,
  OVERLAY_WINDOW_LABELS,
  enabledOverlayWindowLabels,
} from "../src/utils/buffOverlaySettings.ts";
import {
  CAPTURE_TOGGLES_STORAGE_KEY,
  captureToggleAllows,
  readCaptureToggles,
  writeCaptureToggles,
} from "../src/utils/captureToggles.ts";
import {
  combatOverlaySnapshotNeeded,
  overlayViewConfiguredForLiveUpdates,
} from "../src/utils/overlayRuntimePolicy.ts";
import { selectStatusPreviewRows } from "../src/utils/overlayStatusPreview.ts";

const BOTH = [OVERLAY_WINDOW_LABELS.status, OVERLAY_WINDOW_LABELS.skill];

test("总开关默认都在开启状态（缓存缺失时宁可显示）", () => {
  assert.deepEqual(enabledOverlayWindowLabels(DEFAULT_BUFF_OVERLAY_SETTINGS, {}), BOTH);
  assert.equal(captureToggleAllows("buffs", {}), true);
  assert.equal(captureToggleAllows("boss_actions", {}), true);
});

test("关掉「增益/减益倒计时」只隐藏状态窗，技能窗保留", () => {
  assert.deepEqual(
    enabledOverlayWindowLabels(DEFAULT_BUFF_OVERLAY_SETTINGS, { buffs: false }),
    [OVERLAY_WINDOW_LABELS.skill],
  );
});

test("关掉「Boss 技能/动作倒计时」只隐藏技能窗", () => {
  assert.deepEqual(
    enabledOverlayWindowLabels(DEFAULT_BUFF_OVERLAY_SETTINGS, { boss_actions: false }),
    [OVERLAY_WINDOW_LABELS.status],
  );
});

test("两个总开关都关掉时不显示任何悬浮窗", () => {
  assert.deepEqual(
    enabledOverlayWindowLabels(DEFAULT_BUFF_OVERLAY_SETTINGS, { buffs: false, boss_actions: false }),
    [],
  );
  assert.equal(combatOverlaySnapshotNeeded(true, DEFAULT_BUFF_OVERLAY_SETTINGS, {
    buffs: false, boss_actions: false,
  }), false);
});

test("总开关不能替用户打开已经在设置里关掉的分项", () => {
  const noDebuff = { ...DEFAULT_BUFF_OVERLAY_SETTINGS, showBuffOverlay: false, showDebuffOverlay: false };
  assert.deepEqual(enabledOverlayWindowLabels(noDebuff, { buffs: true }), [OVERLAY_WINDOW_LABELS.skill]);
  assert.equal(
    overlayViewConfiguredForLiveUpdates("status", noDebuff),
    false,
    "两个分项都关时状态窗本来就不该配置成实时更新",
  );
});

test("只关 Debuff 时状态窗仍在（关的是分项不是整窗）", () => {
  const noDebuff = { ...DEFAULT_BUFF_OVERLAY_SETTINGS, showDebuffOverlay: false };
  assert.deepEqual(enabledOverlayWindowLabels(noDebuff, { buffs: true }), BOTH);
  assert.equal(overlayViewConfiguredForLiveUpdates("status", noDebuff), true);
});

test("实时预览跟随 Buff / Debuff 分项开关", () => {
  const rows = [
    { key: "buff:勇气", kind: "buff" as const },
    { key: "buff:精准", kind: "buff" as const },
    { key: "buff:致命", kind: "buff" as const },
    { key: "buff:祝福", kind: "buff" as const },
    { key: "debuff:damage", kind: "debuff" as const },
    { key: "debuff:resistance", kind: "debuff" as const },
    { key: "debuff:defense", kind: "debuff" as const },
  ];

  assert.deepEqual(
    selectStatusPreviewRows(rows, true, true).map(row => row.key),
    ["buff:勇气", "buff:精准", "buff:致命", "debuff:damage", "debuff:resistance"],
  );
  assert.deepEqual(
    selectStatusPreviewRows(rows, true, false).map(row => row.key),
    ["buff:勇气", "buff:精准", "buff:致命"],
    "关掉 Debuff 后预览里不能残留减益行",
  );
  assert.deepEqual(
    selectStatusPreviewRows(rows, false, true).map(row => row.key),
    ["debuff:damage", "debuff:resistance"],
  );
  assert.deepEqual(selectStatusPreviewRows(rows, false, false), []);
});

test("预览取样上限可调，不会把行数写死", () => {
  const rows = [
    { key: "buff:a", kind: "buff" as const },
    { key: "buff:b", kind: "buff" as const },
    { key: "buff:c", kind: "buff" as const },
  ];
  assert.deepEqual(selectStatusPreviewRows(rows, true, false, { buff: 1 }).map(row => row.key), ["buff:a"]);
  assert.deepEqual(selectStatusPreviewRows(rows, true, false, { buff: 0 }), []);
});

test("缓存读写只保留布尔值，坏数据回退为空状态", () => {
  const store = new Map<string, string>();
  const globalRecord = globalThis as unknown as { localStorage?: Storage };
  globalRecord.localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as unknown as Storage;

  try {
    writeCaptureToggles({ buffs: false, boss_actions: true });
    assert.deepEqual(readCaptureToggles(), { buffs: false, boss_actions: true });
    assert.equal(captureToggleAllows("buffs"), false);
    assert.equal(captureToggleAllows("boss_actions"), true);
    // 不传 toggles 时默认读缓存：窗口显隐/发布器这些调用点靠它。
    assert.deepEqual(enabledOverlayWindowLabels(DEFAULT_BUFF_OVERLAY_SETTINGS), [OVERLAY_WINDOW_LABELS.skill]);

    store.set(CAPTURE_TOGGLES_STORAGE_KEY, "not-json");
    assert.deepEqual(readCaptureToggles(), {});
    store.set(CAPTURE_TOGGLES_STORAGE_KEY, JSON.stringify({ buffs: "false", boss_actions: false }));
    assert.deepEqual(readCaptureToggles(), { boss_actions: false });
  } finally {
    delete globalRecord.localStorage;
  }
});

test("主窗口切换总开关后立即重算窗口显隐", () => {
  const source = readFileSync(
    new URL("../src/composables/liveDps/useLiveCapture.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /writeCaptureToggles\(captureToggles\.value\)/, "读到开关后要更新缓存");
  assert.match(
    source,
    /async function setCaptureToggle[\s\S]{0,900}?writeCaptureToggles\(captureToggles\.value\);[\s\S]{0,400}?await applyOverlayVisibility\(true\)/,
    "切换后必须立刻重新应用可见性，否则要等下一次轮询窗口才隐藏",
  );
  assert.match(
    source,
    /if \(visible && !labels\.length\)[\s\S]{0,300}?visible: false/,
    "没有任何窗口要显示时要显式隐藏残留窗口",
  );
});

test("关掉 Boss 技能总开关后，本地倒计时也停止计算（不只是隐藏窗口）", () => {
  const source = readFileSync(
    new URL("../src/components/VoiceAnnouncementSettings.vue", import.meta.url),
    "utf8",
  );
  assert.match(
    source,
    /const skillScope = captureToggleAllows\('boss_actions'\);[\s\S]{0,220}?const visualActive = skillScope && visual\.showSkillCountdownOverlay;/,
    "总开关必须参与 visualActive 的判断",
  );
  assert.match(
    source,
    /if \(!visualActive\) \{ visualClock\.clear\(\); publishCountdowns\(\[\]\); \}/,
    "关闭后要清空本地倒计时并推一次空行",
  );
  // 喂给 visualClock 的地方现在都在共用引擎里，同样不许绕过 visualActive。
  const engineSource = readFileSync(
    new URL("../src/utils/voiceBatchEngine.ts", import.meta.url),
    "utf8",
  );
  const observes = engineSource.match(/visualClock\.observe\(/g) || [];
  assert.equal(observes.length, 2, "引擎里应该只有首包 seed 和常规批两处喂入");
  assert.match(engineSource, /if \(visualActive\) \{\s*this\.visualClock\.observe\(/);
  assert.equal(
    (engineSource.match(/if \(visual\.showSkillCountdownOverlay\) this\.visualClock\.observe/g) || [])
      .length,
    0,
    "不能再有绕过总开关的 observe",
  );
  assert.equal(
    (source.match(/visualClock\.observe\(/g) || []).length,
    0,
    "页面不该自己喂时钟了，应交给引擎",
  );
  // 定时发布也要跟着关。
  assert.match(source, /captureToggleAllows\('boss_actions'\) && visual\.showSkillCountdownOverlay/);
});

test("打开悬浮窗设置时不能被目录接口卡住（先开弹窗，再异步拉目录）", () => {
  const source = readFileSync(
    new URL("../src/composables/liveDps/useLiveCapture.ts", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("async function openOverlaySettings");
  assert.ok(start > 0, "找不到 openOverlaySettings");
  const block = source.slice(start, start + 900);
  const openAt = block.indexOf("overlaySettingsOpen.value = true;");
  const awaitAt = block.indexOf("await withTimeout(");
  assert.ok(openAt > 0, "函数里要把弹窗打开");
  assert.ok(awaitAt > 0, "目录请求要带超时");
  assert.ok(openAt < awaitAt, "必须先打开弹窗，再等目录接口");
  assert.match(source, /const OVERLAY_CATALOG_TIMEOUT_MS = \d[\d_]*;/);
});

test("设置页预览用同一个取样函数，而不是固定 3+2", () => {
  const source = readFileSync(
    new URL("../src/components/BuffOverlaySettingsDialog.vue", import.meta.url),
    "utf8",
  );
  assert.match(source, /selectStatusPreviewRows\(\s*orderedStatusRows\.value,\s*draft\.showBuffOverlay,\s*draft\.showDebuffOverlay,?\s*\)/);
});
