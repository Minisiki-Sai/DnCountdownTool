/**
 * 空中的海龙（梦魇海龙）：打地狱犬时仍要播报并倒计时 冰眼 / 龙息。
 *
 * 这一场海龙本体在天上飞，**玩家打不到它**，所以「当前攻击目标」「Boss: 这一行」
 * 指的都是地面的地狱犬卡卡里（4237012）。这条链路因此只认技能号：
 * 语音 `voiceRule` / `SEA_MECHANICS`、悬浮窗 `SkillCountdownTracker` 都不看
 * 技能来源是不是当前 Boss。这个文件把该约定固定下来，防止以后有人加
 * 「只播当前 Boss 的技能」这类过滤把冰眼/龙息整段干掉。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizeVoiceSettings, voiceRule, seaMechanicText, SeaReminderClock,
} from '../src/utils/voiceAnnouncements.ts';
import { SkillCountdownTracker } from '../src/utils/skillCountdowns.ts';

const NEST = '梦魇海龙';
const DRAGON = '0x0000002A@4237023'; // 空中的海龙
const CERBERUS = '0x00000036@4237012'; // 地面正在打的地狱犬
const settings = normalizeVoiceSettings({ enabled: true });

test('冰眼与龙息按技能号匹配，与当前 Boss 无关', () => {
  assert.equal(voiceRule(325447, NEST, settings)?.key, 'sea-ice-eye');
  assert.equal(voiceRule(325450, NEST, settings)?.key, 'sea-dragon-breath');
  assert.equal(seaMechanicText(325447), '注意冰眼');
  assert.equal(seaMechanicText(325450, true), '即将龙息');
  // 别家副本的号不会串成海龙机制（黑龙冲锋、沙龙踢飞）。
  assert.equal(voiceRule(34715, NEST, settings), undefined);
  assert.equal(voiceRule(729105, NEST, settings), undefined);
});

test('语音提醒：地狱犬掉血/死亡都不影响海龙的开招计时', () => {
  const clock = new SeaReminderClock('warning');
  clock.health(4237012, 80, 1_000, CERBERUS);
  clock.started(325447, 2_000, DRAGON, 4237023);
  clock.started(325450, 2_000, DRAGON, 4237023);
  assert.deepEqual(clock.due(51_999), []);
  assert.deepEqual(clock.due(52_000), ['即将龙息']);
  assert.deepEqual(clock.due(57_000), ['即将冰眼']);
  // 地面 Boss 死亡：只清理它自己的计时器。
  clock.health(4237012, 0, 60_000, CERBERUS);
  clock.started(325447, 61_000, DRAGON, 4237023);
  assert.deepEqual(clock.due(116_000), ['即将冰眼']);
  // 死亡事件没带 actor（旧格式）也不能误清海龙的表。
  clock.health(4237012, 0, 120_000, '');
  clock.started(325447, 121_000, DRAGON, 4237023);
  assert.equal(clock.due(176_000).length, 1);
});

test('悬浮窗倒计时：打地狱犬期间海龙的两条计时器照常显示', () => {
  const tracker = new SkillCountdownTracker();
  const selected = ['sea-ice-eye', 'sea-dragon-breath'];
  tracker.observe([
    { source: CERBERUS, skill: 0, kind: 'boss_hp_state', hpPercent: 80, timestamp: 1_000, templateId: 4237012 },
    { source: DRAGON, skill: 325447, kind: 'protocol_action', timestamp: 1_000, templateId: 4237023 },
    { source: DRAGON, skill: 325450, kind: 'protocol_action', timestamp: 1_000, templateId: 4237023 },
  ], NEST);
  const rows = tracker.countdowns(2_000, selected);
  assert.deepEqual(rows.map(row => row.ruleKey).sort(), ['sea-dragon-breath', 'sea-ice-eye']);
  assert.deepEqual(rows.map(row => Math.round(row.remainingMs / 1000)).sort(), [59, 59]);

  // 地狱犬被打死不应带走海龙的计时器。
  tracker.observe([
    { source: CERBERUS, skill: 0, kind: 'boss_hp_state', hpPercent: 0, timestamp: 3_000, templateId: 4237012 },
  ], NEST);
  assert.deepEqual(
    tracker.countdowns(4_000, selected).map(row => row.ruleKey).sort(),
    ['sea-dragon-breath', 'sea-ice-eye'],
  );
});
