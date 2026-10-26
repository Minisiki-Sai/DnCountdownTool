<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import { ShadowExplosionClock } from '../utils/shadowExplosion';
import { ClassicMechanics } from '../utils/classicMechanics';
import { HardMechanics } from '../utils/hardMechanics';
import { SandMechanics } from '../utils/sandMechanics';
import { publishOverlayEvent } from '../utils/overlayEventBus';
import { OVERLAY_EVENT_TOPICS } from '../utils/overlayEventProtocol';
import { SkillCountdownTracker, type SkillCountdown } from '../utils/skillCountdowns';
import { BUFF_OVERLAY_STORAGE_KEY, readBuffOverlaySettings } from '../utils/buffOverlaySettings';
import { voiceGroups, selectVoiceGroup, voiceGroupState } from '../utils/voicePreviews';
import { QWEN_VOICES, qwenVoiceKey, QwenVoicePlayer } from '../utils/qwenVoicePlayer';
import { LiveReminderTime } from '../utils/liveReminderTime';
import { simulatorApi } from '../services/api';
import { immediateMechanicText, hpAnnouncement, seaMechanicKey, voiceModes, classicAddAnnouncement } from '../utils/voiceAnnouncements';
import { VOICE_KEY, VOICE_RULES, normalizeVoiceSettings, voiceRule, seaBreathText, seaMechanicText, SeaReminderClock, monsterAppearanceText } from '../utils/voiceAnnouncements';
import { countdownNest } from '../utils/nestSkillCooldowns';
import { nestScopeKey } from '../utils/splitBossScope';
import { VoiceBatchEngine, type VoiceBatchResult } from '../utils/voiceBatchEngine';
import { filterCurrentGroupRows, focusTemplates, leavingFocus, readSplitGroupHint } from '../utils/splitBossScope';
import { captureToggleAllows } from '../utils/captureToggles';
const props = defineProps<{ running: boolean; nest: string; live: boolean }>();
const open = ref(false);
const message = ref('');
const voiceChoices = QWEN_VOICES;
const previewIndex = ref<Record<string, number>>({});
const expandedGroups = ref<Record<string, boolean>>({
  'sea-nightmare:狮蝎': true, 'classic:食人魔': true,
  'hard:萝丝琳德': true, 'sand-hard:陌生的少女': true,
});
const groupsByNest = Object.fromEntries(['classic','hard','sand-hard','sea-nightmare'].map(key => [key,voiceGroups(key)]));
function enabledCount(keys: string[]) { return keys.filter(key => settings.value.skills.includes(key)).length; }
function selectGroup(keys: string[], event: Event) {
  Object.assign(settings.value, selectVoiceGroup(settings.value, keys, (event.target as HTMLInputElement).checked));
}
let saved = {};
try { saved = JSON.parse(localStorage.getItem(VOICE_KEY) || '{}'); } catch { /* damaged preferences use safe defaults */ }
const settings = ref(normalizeVoiceSettings(saved));
// A single-mode row is controlled solely by its main checkbox. Preserve old
// disabled choices when removing the now-redundant mode checkbox.
for (const rule of VOICE_RULES) {
  const modes = voiceModes(rule);
  if (modes.release === modes.warning) continue;
  const selected = modes.release ? settings.value.releaseSkills : settings.value.warningSkills;
  if (!selected.includes(rule.key)) {
    settings.value.skills = settings.value.skills.filter(key => key !== rule.key);
    selected.push(rule.key);
  }
}
const nestChoices = [
  { key: 'classic', name: '黑龙经典／黑龙123' },
  { key: 'hard', name: '黑龙硬核' },
  { key: 'sand-hard', name: '沙龙硬核' },
  { key: 'sea-nightmare', name: '梦魇海龙' },
];
settings.value.voice = qwenVoiceKey(settings.value.voice);
const supported = typeof window.AudioContext !== 'undefined';
const player = new QwenVoicePlayer(`${import.meta.env.BASE_URL}voices/qwen3/`);
function warmVoice() {
  if (supported) void player.warm(settings.value.voice).catch(e => { message.value = String(e.message || e); });
}
function openSettings() {
  open.value = true;
  if (supported) void player.unlock().catch(() => {});
  warmVoice();
}
function speak(text: string | string[], preview = false) {
  if (!supported) { message.value = '当前运行环境不支持音频播放。'; return; }
  const texts = Array.isArray(text) ? text : [text];
  message.value = texts.join('，');
  void player.play(texts, settings.value.voice, settings.value.volume, preview)
    .catch(e => { message.value = `语音播放失败：${e.message || e}`; });
}
let busy = false, generation = 0;
// 决策逻辑（时钟、去重、门控）都在 VoiceBatchEngine 里，和抓包回放测试共用
// 同一份代码 —— 页面这边只剩 I/O：拉事件、出声、推悬浮窗、写观测。
const engine = new VoiceBatchEngine();
const visualClock = engine.visualClock;
const reminders = engine.reminders;
const reminderTime = new LiveReminderTime();
let clockRunning = false;
let visualSettings = readBuffOverlaySettings(), visualSettingsRaw: string | null = null;
function countdownSettings() {
  const raw = localStorage.getItem(BUFF_OVERLAY_STORAGE_KEY);
  if (raw !== visualSettingsRaw) { visualSettingsRaw = raw; visualSettings = readBuffOverlaySettings(); }
  return visualSettings;
}
let countdownPublishedAt = 0, countdownSignature = '';
function publishCountdowns(rows: SkillCountdown[], force = false) {
  const now = Date.now(), signature = rows.map(row => row.key).join('|');
  if (!force && signature === countdownSignature && (!rows.length || now - countdownPublishedAt < 250)) return;
  const changed = signature !== countdownSignature;
  countdownPublishedAt = now; countdownSignature = signature;
  void publishOverlayEvent(OVERLAY_EVENT_TOPICS.skillCountdowns, { rows, publishedAt: now }, ['skill-countdown-overlay']).catch(() => {});
  // 行集合一变就记一条：悬浮窗「倒计时几秒后消失」只能靠这个还原。
  if (changed) {
    void simulatorApi
      .probeEvent('frontend_countdowns', {
        count: rows.length,
        // 带上满时长：只有 `left` 时，分不清「走完了」和「被清空/重置了」。
        rows: rows.slice(0, 10).map(row => ({
          k: row.ruleKey,
          left: Math.round(row.remainingMs / 1000),
          dur: Math.round(row.durationMs / 1000),
          ready: row.ready === true,
        })),
      })
      .catch(() => undefined);
  }
}
function reset() {
  generation++;
  clockRunning = false;
  reminderTime.clear();
  engine.reset();
  publishCountdowns([], true);
  player.stop();
}
watch(settings, () => {
  try { localStorage.setItem(VOICE_KEY, JSON.stringify(settings.value)); } catch { message.value = '配置保存失败，存储空间不可用。'; }
  reportSettingsSnapshot('change');
  if (countdownSettings().showSkillCountdownOverlay) {
    // 改音量/静音不能把独立运行的悬浮窗倒计时一起抹掉。
    generation++;
    engine.clearForAudioChange();
    player.stop();
  } else reset();
}, { deep: true });
watch(() => settings.value.voice, warmVoice);
/* 换副本才重置，名字变化不算。
   后端同一个副本有两套名字在交替（地图精确识别「黑龙巢穴（三）」/ 攻击目标
   反推「黑龙经典」），props.nest 每秒跳一次。以前直接 watch 这个字符串，
   每跳一次就 reset 一次 —— 倒计时刚建就被清、待播语音一起丢，实战表现就是
   「倒计时闪一下就没、语音不播」。回放的 nest 是常量，所以从不复现。
   现在只看副本类别（classic / hard / ...），同一类别内怎么跳都不重置。 */
let lastNestScope = "";
function syncNestScope(): string {
  lastNestScope = nestScopeKey(String(props.nest || ""), lastNestScope);
  return lastNestScope;
}
watch(() => props.running, reset);
watch(() => props.live, reset);
watch(syncNestScope, reset);
/* 观测：每 ~6 秒把这一批的处理结果写一条到观测流水。
   只读状态、不改任何播报行为。用来判断是「没收到行」还是「收到被哪个门丢了」。 */
let traceTick = 0;
function reportTrace(
  data: { state?: string; clockRunning?: boolean; elapsedSeconds?: number; sessionId?: string; retiredBossIds?: unknown },
  rows: readonly { timestamp: number; skill: number }[],
  result: VoiceBatchResult,
) {
  if (++traceTick % 40 !== 0) return;
  // 悬浮窗开关在另一个窗口的 localStorage 里，改它不会触发上面的 watch；
  // 顺手每 ~6 秒比对一次，保证设置快照跟得上（签名相同就不会写）。
  reportSettingsSnapshot('change');
  const lastTs = rows.length ? rows[rows.length - 1].timestamp : 0;
  const fields = {
    state: data.state,
    clock: data.clockRunning,
    elapsedMs: Math.round((data.elapsedSeconds || 0) * 1000),
    lastTs,
    driftMs: Math.round((data.elapsedSeconds || 0) * 1000) - lastTs,
    rows: rows.length,
    voice: result.voiceRowCount,
    visual: result.visualRowCount,
    staleDropped: result.staleDropped,
    classic: result.classicTexts,
    hard: result.hardTexts,
    hint: result.hint
      ? { current: result.hint.current, focus: result.hint.focus, others: result.hint.others }
      : null,
    // Boss 离场会把倒计时整行抹掉；不记这个，「行突然消失」永远查不到原因。
    retired: Array.isArray(data.retiredBossIds)
      ? (data.retiredBossIds as unknown[]).map(value => String(value)).slice(0, 8)
      : [],
    session: String(data.sessionId || "").slice(0, 8),
  };
  void simulatorApi.probeEvent("frontend_voice_batch", fields).catch(() => undefined);
}

/** 心跳（临时诊断）：即使 state/clock 异常也能留下痕迹，每 ~60 秒一条。 */
let heartbeatTick = 0;
function reportHeartbeat(data: { state?: string; clockRunning?: boolean; elapsedSeconds?: number; voiceEvents?: unknown[] }) {
  if (++heartbeatTick % 400 !== 0) return;
  void simulatorApi
    .probeEvent("frontend_heartbeat", {
      state: data.state,
      clock: data.clockRunning,
      elapsedMs: Math.round((data.elapsedSeconds || 0) * 1000),
      rows: (data.voiceEvents || []).length,
    })
    .catch(() => undefined);
}

async function poll() {
  if (busy || !props.running || !props.live) return;
  const visual = countdownSettings();
  // 首页「Boss 技能 / 动作倒计时」总开关关掉后，连本地倒计时一起停：不只是
  // 不显示窗口，而是真的不再计算（「关闭即停止解析」）。代价是重新打开要等
  // 下一次 Boss 出招——两个抓包实测中位 ~12s、90 分位 ~53~62s、最长 ~167s。
  // 语音播报不吃这条路径（它读同一份 voiceEvents 但只用来出声）。
  const skillScope = captureToggleAllows('boss_actions');
  const visualActive = skillScope && visual.showSkillCountdownOverlay;
  if (!visualActive) { visualClock.clear(); publishCountdowns([]); }
  if ((!settings.value.enabled || !supported) && !visualActive) return;
  busy = true; const token = generation;
  try {
    const data = await simulatorApi.captureVoiceEvents();
    if (token !== generation) return;
    reportHeartbeat(data);
    if (data.state !== 'running' || data.clockRunning === false) { reset(); return; }
    const rows = data.voiceEvents || [];
    // 决策全在引擎里（和抓包回放测试共用同一份代码）；这里只负责出声与推窗。
    const result = engine.apply(data, {
      nest: props.nest,
      settings: settings.value,
      visual,
      skillScope,
      wallClockMs: Date.now(),
      supported,
    });
    if (result.reset) { reset(); return; }
    if (result.skipped) return;
    if (result.bootstrap) {
      // 首包只立状态，绝不补播历史（换记录、刚打开页面时尤其明显）。
      reminderTime.clear();
      reminderTime.observe((data.elapsedSeconds || 0) * 1000);
      clockRunning = true;
      publishCountdowns([], true);
      return;
    }
    reminderTime.observe((data.elapsedSeconds || 0) * 1000);
    clockRunning = true;
    // 顺序与改造前一致：先播循环里逐条触发的（出现/机制/拉冰/点名），
    // 再一次性播时钟结算出来的那批。
    if (result.immediate.length) speak(result.immediate);
    const texts = result.deferred;
    if (settings.value.enabled && supported && texts.length) speak(texts);
    // 有产出就立刻记一条：每 6 秒抽样会把瞬时播报整个漏掉。
    if (texts.length) {
      void simulatorApi
        .probeEvent('frontend_spoken', {
          texts,
          enabled: settings.value.enabled,
          nests: settings.value.nests,
          classicOn: settings.value.skills.includes('classic-whirlwind'),
          classicRelease: settings.value.releaseSkills.includes('classic-whirlwind'),
          nest: props.nest,
        })
        .catch(() => undefined);
    }
    reportTrace(data, rows, result);
    tickReminders();
  } catch { message.value = '暂时无法获取实时事件，将自动重试。'; }
  finally { busy = false; }
}
/* 只读诊断：倒计时行集合一变就记一条上下文。
   实战里「行几秒就没了」有三种成因 —— 副本名算空、总开关瞬断、计时器被清，
   只看行数分不出来，所以把当时的输入一起记下来。不参与任何判断。 */
let countdownTraceState: { count: number; nest: string; active: boolean | null; selected: number } = {
  count: -1, nest: '', active: null, selected: -1,
};
function traceCountdowns(count: number, active: boolean, selected: number) {
  const nest = String(props.nest || '');
  const prev = countdownTraceState;
  if (prev.count === count && prev.nest === nest && prev.active === active && prev.selected === selected) return;
  countdownTraceState = { count, nest, active, selected };
  const debug = engine.debugCountdowns();
  void simulatorApi
    .probeEvent('frontend_countdown_trace', {
      prevCount: prev.count,
      count,
      prevNest: prev.nest,
      nest,
      nestKey: countdownNest(nest),
      prevActive: prev.active,
      active,
      selected,
      trackerNest: debug.nest,
      timers: debug.timers.slice(0, 8),
      dead: debug.dead,
      retired: debug.retired,
      recent: debug.recent,
    })
    .catch(() => undefined);
}

function tickReminders() {
  if (!clockRunning || !props.running || !props.live) return;
  const now = reminderTime.now();
  if (now === undefined) return;
  if (settings.value.enabled && supported) {
    const texts = engine.due(now, settings.value);
    if (texts.length) {
      speak(texts);
      // 提醒类文本（如「旋风已好」）也立刻记一条，采样同样会漏。
      void simulatorApi
        .probeEvent('frontend_spoken', {
          texts,
          source: 'reminder',
          enabled: settings.value.enabled,
          nest: props.nest,
        })
        .catch(() => undefined);
    }
  }
  const visual = countdownSettings();
  const visualActive = captureToggleAllows('boss_actions') && visual.showSkillCountdownOverlay;
  const liveRows = engine.countdowns(now, visual.selectedSkillCountdowns, visualActive);
  // 只读诊断，不改任何判断：行数一变就把当时的输入一起记下来。
  traceCountdowns(liveRows.length, visualActive, visual.selectedSkillCountdowns.length);
  // 总开关关掉时倒计时已经清空过，这里跟着不发布，避免把旧行再推给悬浮窗。
  publishCountdowns(liveRows);
  // 悬浮窗开关是另一个窗口的 localStorage，改动不会触发上面的 watch；
  // 这里每 tick 复算一次（值没变就是一次比较），保证开关一变就换挡。
  syncPollRate();
}
let timer: number | undefined;
let reminderTimer: number | undefined;
let pollMs = 0;
// 拉取 voice-events 的间隔：播报时需要 150ms 才能把播报延迟压到一帧预算内；
// 不播报时降到 30s，避免 150ms 空转。
const FAST_POLL_MS = 150;
const IDLE_POLL_MS = 30_000;
// 本地倒计时 tick 保持高频：它不产生网络请求，但决定悬浮窗秒数是否跟手。
const CLOCK_TICK_MS = 150;
/**
 * 当前场景该用多高的「拉取」频率。
 *
 * - 不在战斗：本来就不会有播报，直接低频。
 * - 技能倒计时悬浮窗打开时：必须高频。倒计时和播报共用一个事件流，慢轮询
 *   批次里的技能事件会被下面 5 秒新鲜度过滤掉，结果就是「有事件但永远不显示」。
 * - 副本没勾选白名单：语音播报会空转，同样是低频。
 * - 定位不到副本（识别失败）：保持高频。宁可多请求几次，也不能把
 *   「识别暂时为空」变成「播报晚 30 秒」。
 */
function desiredPollMs(): number {
  if (!props.running || !props.live) return IDLE_POLL_MS;
  const key = countdownNest(String(props.nest || '').trim());
  if (!key) return FAST_POLL_MS;
  if (countdownSettings().showSkillCountdownOverlay) return FAST_POLL_MS;
  return settings.value.nests.includes(key) ? FAST_POLL_MS : IDLE_POLL_MS;
}
function syncPollRate() {
  const next = desiredPollMs();
  if (next === pollMs) return;
  pollMs = next;
  window.clearInterval(timer);
  timer = window.setInterval(poll, next);
}
/**
 * 记录「这一场到底开了什么」。
 *
 * 抓包只记网络流量，记不到玩家的开关。没有这份快照，回放就不知道当时
 * 该不该播 —— 同一个包在不同配置下结论相反，回放等于白跑。
 * 变了才写，一场下来通常只有一两条。
 */
let settingsSignature = '';
function reportSettingsSnapshot(why: string) {
  const voice = settings.value;
  const visual = countdownSettings();
  const snapshot = {
    why,
    enabled: voice.enabled,
    splitBossOnly: voice.splitBossOnly,
    nests: voice.nests,
    skills: voice.skills,
    releaseSkills: voice.releaseSkills,
    warningSkills: voice.warningSkills,
    showSkillCountdownOverlay: visual.showSkillCountdownOverlay,
    selectedSkillCountdowns: visual.selectedSkillCountdowns,
    splitBossOnlyCurrent: visual.splitBossOnlyCurrent,
    bossActionsOn: captureToggleAllows('boss_actions'),
    buffsOn: captureToggleAllows('buffs'),
    nest: props.nest,
  };
  const signature = JSON.stringify({ ...snapshot, why: undefined, nest: undefined });
  if (signature === settingsSignature && why === 'change') return;
  settingsSignature = signature;
  void simulatorApi.probeEvent('frontend_settings', snapshot).catch(() => undefined);
}

onMounted(() => {
  if (settings.value.enabled) warmVoice();
  syncPollRate();
  reminderTimer = window.setInterval(tickReminders, CLOCK_TICK_MS);
  reportSettingsSnapshot('mount');
});
// 换图、开始/停止统计、改白名单都要立刻重算，不必等下一轮低频 tick。
watch(
  () => [
    props.running,
    props.live,
    props.nest,
    settings.value.enabled,
    settings.value.nests.join(','),
  ],
  syncPollRate,
);
onUnmounted(() => { reset(); window.clearInterval(timer); window.clearInterval(reminderTimer); player.dispose(); });
</script>

<template>
  <button class="frost-entry voice-entry" type="button" @click="openSettings">语音播报<span class="voice-entry-state">{{ settings.enabled ? '开' : '关' }}</span></button>
  <Teleport to="body">
    <div v-if="open" class="voice-backdrop" @click.self="open = false" @keydown.esc="open = false">
      <section class="voice-dialog" role="dialog" aria-modal="true" aria-label="语音播报设置">
        <header><h2>语音播报</h2><button class="secondary-button" @click="open = false">关闭</button></header>
        <div class="voice-enable-row">
          <label><input v-model="settings.enabled" type="checkbox" :disabled="!supported"> 开启实时语音播报</label>
          <label :title="'黑龙硬核／沙龙硬核分边打两只 Boss 时，只播报自己正在打的那一只；与悬浮窗倒计时的同名开关相互独立'">
            <input v-model="settings.splitBossOnly" type="checkbox"> 只播报当前分组 Boss 技能
          </label>
        </div>
        <p>配置自动保存。仅实时统计播报，历史抓包不播报。只播报已识别目标的职业，例如“注意影舞者点名”；职业未知时不猜测。同职业队员无法仅靠语音区分。不保证提前预警。</p>
        <fieldset><legend>巢穴（多选）</legend>
          <label v-for="nest in nestChoices" :key="nest.key"><input v-model="settings.nests" type="checkbox" :value="nest.key"> {{ nest.name }}</label>
        </fieldset>
        <fieldset v-for="nest in nestChoices.filter(n => settings.nests.includes(n.key))" :key="nest.key"><legend>{{ nest.name }} · 技能（多选）</legend>
          <p class="voice-help">每条技能可分别开启释放播报和预警播报（冷却／血量）；下拉框仅选择试听内容。折叠不会关闭播报。</p>
          <section v-for="group in groupsByNest[nest.key]" :key="group.key" :class="{ 'voice-boss-group': group.name }">
            <div v-if="group.name" class="voice-boss-heading">
              <button type="button" class="voice-boss-toggle" :aria-expanded="!!expandedGroups[group.key]" :aria-controls="`voice-group-${group.key}`" @click="expandedGroups[group.key] = !expandedGroups[group.key]">
                <span aria-hidden="true">{{ expandedGroups[group.key] ? '▾' : '▸' }}</span>
                <strong>{{ group.name }}</strong>
                <span class="voice-count">已开启 {{ enabledCount(group.rows.map(r => r.key)) }}/{{ group.rows.length }}</span>
              </button>
              <label class="voice-group-all"><input type="checkbox" :aria-label="`${group.name}全部播报`" :checked="voiceGroupState(settings,group.rows.map(r => r.key)).checked" :indeterminate="voiceGroupState(settings,group.rows.map(r => r.key)).indeterminate" @change="selectGroup(group.rows.map(r => r.key), $event)">全选</label>
            </div>
            <div v-show="!group.name || expandedGroups[group.key]" :id="`voice-group-${group.key}`" class="voice-group-content">
          <div v-for="rule in group.rows" :key="rule.key" class="voice-rule">
            <label><input v-model="settings.skills" type="checkbox" :value="rule.key"> {{ rule.label }}
              <small v-if="rule.key === 'sea-manticore-flight-slam'" class="voice-rule-note">听到“跳”后 1s 左右起跳</small>
              <small v-if="rule.key === 'sea-apocalypse-rotating-laser'" class="voice-rule-note">两个旋转方向均播报，共用冷却提醒</small>
            </label>
            <div class="voice-modes">
              <template v-if="voiceModes(rule).release && voiceModes(rule).warning">
              <label v-if="voiceModes(rule).release"><input v-model="settings.releaseSkills" type="checkbox" :value="rule.key" :disabled="!settings.skills.includes(rule.key)"> {{ ['sea-minotaur','classic-bomb-add'].includes(rule.key) ? '出现播报' : '技能释放' }}</label>
              <label v-if="voiceModes(rule).warning"><input v-model="settings.warningSkills" type="checkbox" :value="rule.key" :disabled="!settings.skills.includes(rule.key)"> {{ rule.key === 'classic-bomb-add' ? '全部消失播报' : rule.key === 'sea-minotaur' ? '即将出现' : ['sea-breath','sea-hp-flight'].includes(rule.key) ? '血量预警' : '冷却／血量预警' }}</label>
              </template>
            </div>
            <div class="voice-preview">
              <select v-if="rule.previews.length > 1" :value="previewIndex[rule.key] ?? 0" :aria-label="`${rule.name}提示内容`" @change="previewIndex[rule.key] = Number(($event.target as HTMLSelectElement).value)">
                <option v-for="(item,index) in rule.previews" :key="item.label" :value="index">{{ item.label }}</option>
              </select>
              <span v-else class="voice-preview-text">{{ rule.previews[0].text }}</span>
              <button class="secondary-button" :aria-label="`试听${rule.name}`" @click="speak(rule.previews[previewIndex[rule.key] ?? 0].text, true)">试听</button>
            </div>
          </div>
            </div>
          </section>
        </fieldset>
        <label class="voice-field">中文音色<select v-model="settings.voice"><option v-if="!voiceChoices.length" value="">暂无中文音色</option><option v-for="v in voiceChoices" :key="v.key" :value="v.key">{{ v.label }}</option></select></label>
        <p>四种音色，默认 Vivian（Vivi2.0 与小何2.0 来自豆包 TTS，Vivian / Serena 来自 Qwen3-TTS）。提示语随程序保存，战斗中离线播放，无需 Azure 账号或安装 Windows 音色。新机制提示语需要随版本更新音频。</p>
        <details v-if="settings.nests.includes('sea-nightmare')" class="voice-mechanic-notes">
          <summary>查看播报规则</summary>
        <p>冰弹播报统一以“注意拉冰”开头。第一阶段：右向冰弹 → 注意拉冰，三连啄后前往右侧安全区；左向冰弹 → 注意拉冰，三连啄后前往左侧安全区。其他已识别阶段保留原方向规则，阶段未知不播报。按已识别的技能或已验证动作触发；阶段未知不猜测方向。</p>
        <p>均从技能释放时计时：地面激光35秒；召唤触手75秒，天启血量低于20%改为55秒，60%时另报“召唤触手已好”；六芒星75秒，低于20%改为55秒，42%时另报“六芒星已好”；连续追踪85秒。冰捆115秒，海龙三阶段血量到30%改为55秒，61%另报“冰捆已好”。跨过血量阈值时，按原释放时间重算；缩短后的提醒时间已到则立即提示一次。龙息50秒、冰眼55秒；狮蝎飞天85秒，旋风／砸地／黑洞55秒。三阶段62/52/42/32/22%提醒拉冰；66/56%提醒躲龙息消灭暗影；46/36/26/16/6%提醒上炮。地狱犬85/65/45/25%提醒“牛头怪即将出现”。血量按实体满血计算。“已好”是预估提醒，不是服务端冷却。</p>
        </details>
        <details v-if="settings.nests.includes('hard')" class="voice-mechanic-notes">
          <summary>黑龙硬核 · 相关播报规则</summary>
          <p>甩尾（734009／735009）释放后，一阶段55秒、二阶段65秒提醒“甩尾已好”。二阶段首次点名在进入阶段后55秒提醒，首次上升重力65秒提醒；后续点名以734017释放为起点，115秒提醒“点名已好”；上升重力以735010／735011释放为起点，起手播报“注意上升重力”，115秒提醒“上升重力已好”。点名目标的职业提示仍按实际识别的状态播报。</p>
          <p>“已好”是提前准备的预估提醒，不代表服务端冷却已结束，也不保证Boss马上释放。</p>
        </details>
        <details v-if="settings.nests.includes('sand-hard')" class="voice-mechanic-notes">
          <summary>沙龙硬核 · 相关播报规则</summary>
          <p>石头人三连砸、狮子三连冲首次在血量下降跨过97%时提醒“已好”；蝎子旋转仍为92%。后续均从对应技能释放后25秒提醒。首次读到的血量仅建立基线，不补播已经跨过的血线。</p>
          <p>东西安全区按实际起手指路：730009播报“注意砸地，前往9点”；730010播报“注意砸地，前往3点”。冷却预警仍提示“叹息之墙已好，前往点位”，未识别起手前不预猜方向。“已好”是预估提醒，不保证Boss马上释放。</p>
        </details>
        <label class="voice-field">音量 {{ Math.round(settings.volume * 100) }}%<input v-model.number="settings.volume" type="range" min="0" max="1" step="0.05"></label>
        <button class="secondary-button" @click="speak('语音播报试听，请注意战斗机制', true)">试听当前音色与音量</button>
        <p role="status">{{ message }}</p>
      </section>
    </div>
  </Teleport>
</template>
<style scoped>
.voice-entry-state{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:16px;margin-left:7px;padding:0 6px;border-radius:999px;background:rgba(94,134,168,.14);color:var(--frost-ink-2,#53616f);font-size:9.5px;font-weight:600;letter-spacing:.02em}
.voice-preview-text{flex:1;min-width:0;align-self:center;overflow-wrap:anywhere}
.voice-backdrop{position:fixed;inset:0;z-index:12000;background:#102b3766;display:grid;place-items:center;padding:24px}
.voice-dialog{background:var(--panel-bg,#fff);color:#23434b;border-radius:16px;padding:24px;width:min(960px,100%);max-height:88vh;overflow:auto;box-shadow:0 16px 70px #102b3740;box-sizing:border-box}
.voice-rule-note{display:block;margin:4px 0 0 22px;font-size:13px;line-height:1.5;color:#46636b}
.voice-boss-group{border:1px solid #d5e4e5;border-radius:10px;overflow:hidden;margin-top:10px}.voice-boss-heading{display:flex;align-items:center;gap:12px;padding:10px 12px;background:#eff7f7}.voice-boss-toggle{display:flex;align-items:center;gap:10px;flex:1;min-width:0;border:0;background:transparent;text-align:left;color:#23434b;padding:4px;cursor:pointer;font:inherit}.voice-boss-toggle:focus-visible{outline:2px solid #087f83;outline-offset:2px}.voice-count{font-size:12px;color:#46636b}.voice-group-all{display:flex;align-items:center;gap:5px;white-space:nowrap;font-size:13px}.voice-group-content{padding:0 10px}.voice-mechanic-notes{margin:14px 0;padding:10px 12px;border:1px solid #d5e4e5;border-radius:8px}.voice-mechanic-notes summary{cursor:pointer;color:#28525a;font-size:14px}
.voice-rule{display:grid!important;grid-template-columns:minmax(170px,1fr) minmax(135px,.8fr) minmax(0,1.5fr);border-bottom:1px solid #e4eeee;padding:10px 0!important}.voice-rule:last-child{border-bottom:0}.voice-modes{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:13px}.voice-modes label{display:flex;align-items:center;gap:4px}.voice-preview{display:flex;gap:8px;min-width:0}.voice-preview select{flex:1;min-width:0;width:0;color:#23434b;border:1px solid #b8cdcf;border-radius:6px;background:var(--panel-bg,#fff);text-overflow:ellipsis}.voice-preview button{flex:none;white-space:nowrap}.voice-help{margin:0 0 8px}@media(max-width:700px){.voice-rule{grid-template-columns:1fr}.voice-backdrop{padding:10px}.voice-dialog{padding:16px}}
header,.voice-rule{display:flex;align-items:center;justify-content:space-between;gap:12px}h2{margin:0 0 16px}p{font-size:13px;line-height:1.7;color:#46636b}fieldset{border:1px solid #cadcde;border-radius:8px;margin:16px 0;padding:12px}fieldset>label{margin-right:20px}.voice-rule{padding:5px 0}.voice-field{display:flex;align-items:center;gap:16px;margin:16px 0}.voice-field select,.voice-field input{flex:1;min-width:0}select{padding:8px}
.voice-enable-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px;margin:4px 0}
</style>
