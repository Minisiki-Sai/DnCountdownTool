import test from 'node:test';
import assert from 'node:assert/strict';
import { filterCurrentGroupRows, focusTemplates, leavingFocus, nestScopeKey, readSplitGroupHint } from '../src/utils/splitBossScope.ts';
import { DEFAULT_BUFF_OVERLAY_SETTINGS, normalizeBuffOverlaySettings } from '../src/utils/buffOverlaySettings.ts';
import { normalizeVoiceSettings } from '../src/utils/voiceAnnouncements.ts';

const ZHUWU = 4150058;
const KAJIFU = 4150059;
const DRAGON = 4150050;
const AIR_SEA = 4237034;
const DOG = 4237023;
const APOCALYPSE = 4237011;

const events = [
  { sequence: 1, templateId: KAJIFU, skill: 730021 },
  { sequence: 2, templateId: ZHUWU, skill: 730022 },
  { sequence: 3, templateId: DRAGON, skill: 730025 },
  { sequence: 4, templateId: 0, skill: 0 },
];

test('分组提示：只接受合法的 current + 非空 others', () => {
  assert.deepEqual(readSplitGroupHint({ current: ZHUWU, others: [KAJIFU] }), {
    current: ZHUWU,
    others: [KAJIFU],
    focus: [ZHUWU],
    scene: [],
  });
  assert.equal(readSplitGroupHint(null), null);
  assert.equal(readSplitGroupHint(undefined), null);
  assert.equal(readSplitGroupHint({}), null);
  assert.equal(readSplitGroupHint({ current: 0, others: [KAJIFU] }), null);
  assert.equal(readSplitGroupHint({ current: ZHUWU, others: [] }), null);
  // 脏数据不能变成「过滤掉一切」。
  assert.equal(readSplitGroupHint({ current: ZHUWU, others: ['x', -1, 0] }), null);
  assert.deepEqual(readSplitGroupHint({ current: '4150058', others: ['4150059', 'bad'] }), {
    current: ZHUWU,
    others: [KAJIFU],
    focus: [ZHUWU],
    scene: [],
  });
});

test('场景 Boss 常驻 focus，不会进 others', () => {
  const hint = readSplitGroupHint({
    current: DOG,
    others: [APOCALYPSE, 4235447, 4235448, 4237003, 4237011, 4237035],
    focus: [DOG, AIR_SEA],
    scene: [AIR_SEA],
  });
  assert.deepEqual(hint?.scene, [AIR_SEA]);
  assert.ok(focusTemplates(hint).includes(AIR_SEA));
  assert.ok(!(hint?.others ?? []).includes(AIR_SEA));
});

test('没有 focus 字段的老后端：按 current 单只处理', () => {
  const hint = readSplitGroupHint({ current: ZHUWU, others: [KAJIFU] });
  assert.deepEqual(focusTemplates(hint), [ZHUWU]);
  assert.deepEqual(focusTemplates(null), []);
});

test('血量状态行永远不被模板过滤吞掉', () => {
  const rows = [
    { sequence: 1, templateId: KAJIFU, skill: 730021 },
    { sequence: 2, templateId: KAJIFU, skill: 0, kind: 'boss_hp_state' },
    { sequence: 3, templateId: ZHUWU, skill: 0, kind: 'boss_hp_state' },
  ];
  const kept = filterCurrentGroupRows(rows, { current: ZHUWU, others: [KAJIFU] });
  assert.deepEqual(kept.map(row => row.sequence), [2, 3]);
});

test('换边：掉出 focus 的 Boss 要被清掉，场景 Boss 不被清', () => {
  // 天启 → 狮蝎：天启掉出名单。
  assert.deepEqual(leavingFocus([APOCALYPSE], [DOG]), [APOCALYPSE]);
  // 场景 Boss 一直在名单里，不能被当成「离开」。
  assert.deepEqual(leavingFocus([DOG, AIR_SEA], [DOG, AIR_SEA]), []);
  // 终局：从「地狱犬 + 空中海龙」切到「终局海龙 + 空中海龙」。
  assert.deepEqual(leavingFocus([DOG, AIR_SEA], [4237035, AIR_SEA]), [DOG]);
});

test('hint 为空（识别空档）不清任何东西', () => {
  assert.deepEqual(leavingFocus([APOCALYPSE, AIR_SEA], []), []);
  assert.deepEqual(leavingFocus([], [DOG]), []);
});

test('applyBossLifecycle 用集合差集而不是「上一次是哪一侧」', async () => {
  const { readFileSync } = await import('node:fs');
  // 这段逻辑搬进了共用引擎（页面和抓包回放同一份代码），断言跟着搬。
  const source = readFileSync(new URL('../src/utils/voiceBatchEngine.ts', import.meta.url), 'utf8');
  assert.match(source, /const leaving\s*=\s*leavingFocus\(this\.lastFocusTemplates, focus\)/);
  assert.match(source, /this\.visualClock\.forgetTemplates\(leaving\)/);
  assert.doesNotMatch(source, /lastSideTemplate/);
});

test('只保留当前分组：丢掉另一侧的 Boss，保留本侧与无关模板', () => {
  const kept = filterCurrentGroupRows(events, { current: ZHUWU, others: [KAJIFU] });
  assert.deepEqual(kept.map(row => row.sequence), [2, 3, 4]);
  const other = filterCurrentGroupRows(events, { current: KAJIFU, others: [ZHUWU] });
  assert.deepEqual(other.map(row => row.sequence), [1, 3, 4]);
});

test('识别不出分组时不过滤，且不修改入参', () => {
  const source = [...events];
  assert.equal(filterCurrentGroupRows(source, null).length, 4);
  assert.equal(source.length, 4);
  assert.deepEqual(filterCurrentGroupRows([], { current: ZHUWU, others: [KAJIFU] }), []);
});

test('两个开关默认都选中，显式关掉才保留 false', () => {
  assert.equal(DEFAULT_BUFF_OVERLAY_SETTINGS.splitBossOnlyCurrent, true);
  assert.equal(normalizeBuffOverlaySettings({}).splitBossOnlyCurrent, true);
  assert.equal(normalizeBuffOverlaySettings({ splitBossOnlyCurrent: false }).splitBossOnlyCurrent, false);
  assert.equal(normalizeVoiceSettings({}).splitBossOnly, true);
  assert.equal(normalizeVoiceSettings({ splitBossOnly: false }).splitBossOnly, false);
  // 旧的存档（没有这个字段）要按默认选中处理。
  const legacyOverlay = normalizeBuffOverlaySettings(JSON.parse('{"showSkillCountdownOverlay":true}'));
  assert.equal(legacyOverlay.splitBossOnlyCurrent, true);
  const legacyVoice = normalizeVoiceSettings(JSON.parse('{"enabled":true,"skills":["sand-lion"]}'));
  assert.equal(legacyVoice.splitBossOnly, true);
});

test('两个开关在轮询里各自生效（语音用 splitBossOnly，倒计时用 splitBossOnlyCurrent）', async () => {
  const { readFileSync } = await import('node:fs');
  // 轮询的决策段在共用引擎里（页面只负责 I/O），断言跟着搬。
  const source = readFileSync(new URL('../src/utils/voiceBatchEngine.ts', import.meta.url), 'utf8');
  assert.match(source, /const voiceRows\s*=\s*options\.settings\.splitBossOnly\s*\?/, '语音侧必须用语音开关');
  assert.match(source, /const visualRows\s*=\s*options\.visual\.splitBossOnlyCurrent\s*\?/, '倒计时侧必须用悬浮窗开关');
  assert.match(source, /this\.visualClock\.observe\(\s*visualRows\.filter/, '视觉时钟要喂过滤后的行');
  // 语音的几条时钟必须喂 voiceRows，不能被悬浮窗开关影响。
  for (const clock of ['sandClock', 'hardClock', 'classicClock']) {
    assert.match(source, new RegExp(`for \\(const row of voiceRows\\)[\\s\\S]{0,200}?this\\.${clock}\\.observe`), `${clock} 应使用 voiceRows`);
  }
});

test('同一副本的两套名字来回跳，不能当成换副本', () => {
  // 实战实测：后端地图精确识别给「黑龙巢穴（三）」，攻击目标反推给「黑龙经典」，
  // 每秒交替一次。以前 props.nest 直接进 watch，每跳一次 reset 一次，
  // 倒计时刚建立就被清空、待播语音一起丢。
  const timeline = ['黑龙巢穴（三）', '黑龙经典', '黑龙巢穴（三）', '黑龙经典', '黑龙经典', '黑龙巢穴（三）'];
  let scope = '';
  const changes: string[] = [];
  for (const nest of timeline) {
    const next = nestScopeKey(nest, scope);
    if (next !== scope) changes.push(next);
    scope = next;
  }
  assert.deepEqual(changes, ['classic'], `名字抖动不该产生多余的重置，实际变了 ${changes.length} 次`);
});

test('识别空档保持上一次的副本，不当成换副本', () => {
  // 刚进本 / 换关瞬间会出现空名字，那时不该清掉正在跑的时钟。
  assert.equal(nestScopeKey('', 'classic'), 'classic');
  assert.equal(nestScopeKey('城镇', 'classic'), 'classic');
  assert.equal(nestScopeKey('', ''), '');
});

test('真的换副本仍然要重置', () => {
  assert.equal(nestScopeKey('黑龙巢穴（硬核）', 'classic'), 'hard');
  assert.equal(nestScopeKey('梦魇·海龙萨芬特拉', 'hard'), 'sea-nightmare');
  assert.equal(nestScopeKey('沙漠龙巢穴（硬核模式）', 'classic'), 'sand-hard');
});
