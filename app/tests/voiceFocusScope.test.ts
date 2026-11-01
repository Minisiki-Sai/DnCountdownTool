/**
 * 分边副本（梦魇海龙第一关：天启 / 狮蝎）的跨组串台回归。
 *
 * 三个已复现的实战缺陷：
 *  1. 血量阈值是**全团事件**，队友把对侧 Boss 打到 60%/42%，你这边照样收到，
 *     而那两句念的又恰好是对侧的技能名（「召唤触手已好」「六芒星已好」）。
 *  2. 后端在「本机 15 秒没打到登记 Boss」时把 hint 置空（不过滤），空档里放行
 *     的对侧技能行会建出对方那只的倒计时。
 *  3. `leavingFocus` 在 focus 为空时按设计返回空数组，于是上面那些错行既不清、
 *     也不再被 observe 拦下，一路挂到冷却结束再念一句。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { VoiceBatchEngine, type VoiceBatchMeta, type VoiceBatchOptions } from '../src/utils/voiceBatchEngine.ts';
import { normalizeVoiceSettings } from '../src/utils/voiceAnnouncements.ts';

const APOCALYPSE = 4235447; // 天启
const MANTICORE = 4235448; // 狮蝎
const TENTACLES = 50456; // 天启·召唤触手
const STAR = 50411; // 天启·六芒星

/** 狮蝎那一组：focus 只有狮蝎，天启在 others 里。 */
const MANTICORE_SIDE = { current: MANTICORE, focus: [MANTICORE], others: [APOCALYPSE, 4237003], scene: [] };
/** 天启那一组：反过来。 */
const APOCALYPSE_SIDE = { current: APOCALYPSE, focus: [APOCALYPSE], others: [MANTICORE, 4237003], scene: [] };

const NEST = '梦魇海龙';

function options(overrides: Partial<VoiceBatchOptions> = {}): VoiceBatchOptions {
  return {
    nest: NEST,
    settings: normalizeVoiceSettings({ enabled: true, nests: ['sea-nightmare'], splitBossOnly: true }) as never,
    visual: {
      showSkillCountdownOverlay: true,
      selectedSkillCountdowns: [],
      splitBossOnlyCurrent: true,
    },
    skillScope: true,
    wallClockMs: 0,
    supported: true,
    trace: true,
    ...overrides,
  };
}

function meta(
  elapsedSeconds: number,
  rows: Record<string, unknown>[],
  focusGroup: unknown,
  overrides: Partial<VoiceBatchMeta> = {},
): VoiceBatchMeta {
  return {
    state: 'running',
    clockRunning: true,
    elapsedSeconds,
    sessionId: 'session-a',
    retiredBossIds: [],
    focusGroup,
    voiceEvents: rows as never,
    ...overrides,
  };
}

function thresholdRow(sequence: number, templateId: number, threshold: number, at: number) {
  return {
    sequence,
    observedAt: at,
    kind: 'boss_hp_threshold',
    skill: 0,
    source: `0x0000009D@${templateId}`,
    timestamp: at * 1000,
    templateId,
    hpPercent: threshold - 0.01,
    threshold,
  };
}

function skillRow(sequence: number, templateId: number, skill: number, at: number) {
  return {
    sequence,
    observedAt: at,
    kind: 'structured_action',
    skill,
    source: `0x0000009D@${templateId}`,
    timestamp: at * 1000,
    templateId,
  };
}

/** 先跑一批空的，把 session 立起来（首包只立状态、不补播历史）。 */
function bootstrap(engine: VoiceBatchEngine, focusGroup: unknown, elapsedSeconds = 1) {
  return engine.apply(meta(elapsedSeconds, [], focusGroup), options({ wallClockMs: elapsedSeconds * 1000 }));
}

test('对侧 Boss 的血量阈值只更新状态，不念技能名', () => {
  const engine = new VoiceBatchEngine();
  bootstrap(engine, MANTICORE_SIDE);

  // 队友把天启打到 60% —— 你在狮蝎侧，不该听到「召唤触手已好」。
  const crossed = engine.apply(
    meta(20, [thresholdRow(1, APOCALYPSE, 60, 20)], MANTICORE_SIDE),
    options({ wallClockMs: 20000 }),
  );
  assert.deepEqual(crossed.deferred, []);
  assert.deepEqual(crossed.immediate, []);

  // 狮蝎自己的阈值照念（这是本组的事实）。
  const own = engine.apply(
    meta(30, [thresholdRow(2, MANTICORE, 60, 30)], MANTICORE_SIDE),
    options({ wallClockMs: 30000 }),
  );
  // 狮蝎 60% 没有专属文案时为空数组也算通过——重点是不能出现天启的文本。
  assert.ok(!own.deferred.includes('召唤触手已好'));

  // 换回天启侧，同一个阈值就该念了。
  const back = engine.apply(
    meta(40, [thresholdRow(3, APOCALYPSE, 60, 40)], APOCALYPSE_SIDE),
    options({ wallClockMs: 40000 }),
  );
  assert.deepEqual(back.deferred, ['召唤触手已好']);

  // 关掉「只播当前分组」的人本来就该全听。
  const off = new VoiceBatchEngine();
  bootstrap(off, MANTICORE_SIDE);
  const loud = off.apply(
    meta(20, [thresholdRow(1, APOCALYPSE, 42, 20)], MANTICORE_SIDE),
    options({
      wallClockMs: 20000,
      settings: normalizeVoiceSettings({
        enabled: true,
        nests: ['sea-nightmare'],
        splitBossOnly: false,
      }) as never,
    }),
  );
  assert.deepEqual(loud.deferred, ['六芒星已好']);
});

test('hint 空档沿用上一次判定，对侧技能不会混进来', () => {
  const engine = new VoiceBatchEngine();
  bootstrap(engine, MANTICORE_SIDE);

  // 后端这一刻选不出主目标（本机刚停手），给 null。
  const blank = engine.apply(
    meta(20, [skillRow(1, APOCALYPSE, TENTACLES, 20)], null),
    options({ wallClockMs: 20000 }),
  );

  assert.deepEqual(blank.immediate, [], '空档不该把天启的技能念出来');
  assert.ok(blank.hint, '空档要沿用上一次的 focus');
  assert.equal(blank.hint?.current, MANTICORE);

  // 倒计时也不能建：否则它会一直挂在悬浮窗上直到冷却结束。
  const countdowns = engine.countdowns(20000, ['sea-apocalypse-tentacles', 'sea-swap'], true);
  assert.equal(countdowns.length, 0, '空档期间不该建立对侧 Boss 的倒计时');
});

test('空档超过窗口 / 换了副本 / 换了会话，都不再沿用', () => {
  const stale = new VoiceBatchEngine();
  bootstrap(stale, MANTICORE_SIDE, 1);
  // 空档 120 秒（超过 STICKY_HINT_WINDOW_S=60）：视为已脱离战斗，不再沿用。
  const late = stale.apply(
    meta(121, [skillRow(1, APOCALYPSE, TENTACLES, 121)], null),
    options({ wallClockMs: 121000 }),
  );
  assert.equal(late.hint, null);
  assert.deepEqual(late.immediate, ['注意触手'], '放弃沿用后就该放行（宁可多播，不可漏播）');

  const otherNest = new VoiceBatchEngine();
  bootstrap(otherNest, MANTICORE_SIDE, 1);
  const switched = otherNest.apply(
    meta(20, [skillRow(1, APOCALYPSE, TENTACLES, 20)], null),
    options({ wallClockMs: 20000, nest: '黑龙巢穴（经典）' }),
  );
  assert.equal(switched.hint, null, '换副本不能把上一只的白名单套过来');

  const otherSession = new VoiceBatchEngine();
  bootstrap(otherSession, MANTICORE_SIDE, 1);
  const nextRun = otherSession.apply(
    meta(5, [skillRow(1, APOCALYPSE, TENTACLES, 5)], null, { sessionId: 'session-b' }),
    options({ wallClockMs: 5000 }),
  );
  assert.equal(nextRun.hint, null, '换统计会话不能带上一场的 focus');
});

test('空档沿用不会把本侧的行吞掉', () => {
  const engine = new VoiceBatchEngine();
  bootstrap(engine, MANTICORE_SIDE);
  const rows = [
    skillRow(1, APOCALYPSE, STAR, 20),
    skillRow(2, MANTICORE, 50317, 20),
  ];
  const result = engine.apply(meta(20, rows, null), options({ wallClockMs: 20000 }));
  assert.equal(result.voiceRowCount, 1, '只留下本侧那一行');
  assert.deepEqual(result.immediate, ['注意旋风']);
});
