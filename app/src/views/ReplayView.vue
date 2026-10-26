<script setup lang="ts">
/**
 * 开发者模式 · 回放实战录像。
 *
 * 导入一个 .dpscap，按真实节奏把证据行喂进**和实战完全同一条链路**：
 * 主窗口的语音播报 + 悬浮窗倒计时。没有「回放专用分支」，所以听到看到
 * 什么，就跟打这一场时一模一样。
 */
import { computed, onActivated, onDeactivated, onUnmounted, ref } from 'vue';
import { invoke } from '@tauri-apps/api/core';
import { ArrowLeft, FolderOpen, Pause, Play, Square } from '@lucide/vue';
import { simulatorApi } from '../services/api';
import {
  pauseReplay, replayActive, replayElapsedMs, replayFinished, replaySession,
  resumeReplay, seekReplay, setReplaySpeed, startReplay, stopReplay,
  type ReplayPayload,
} from '../services/replaySource';
import {
  OVERLAY_WINDOW_LABELS,
  enabledOverlayWindowLabels,
  readBuffOverlaySettings,
} from '../utils/buffOverlaySettings';
import { subscribeOverlayEvent } from '../utils/overlayEventBus';
import { OVERLAY_EVENT_TOPICS } from '../utils/overlayEventProtocol';
import type { SkillCountdownSnapshot } from '../utils/skillCountdowns';
import type { BuffOverlayStatus } from '../types/api';

const props = defineProps<{ tauriAvailable: boolean }>();
const emit = defineEmits<{ back: [] }>();

const OVERLAY_ENABLED_KEY = 'dragon-nest-v420-buff-overlay-enabled';

type CaptureEntry = { path: string; name: string; sizeBytes: number; modifiedAt: number };

const captures = ref<CaptureEntry[]>([]);
const manualPath = ref('');
const selected = ref('');
const loading = ref(false);
const progress = ref(0);
const message = ref('');
const errorText = ref('');

const ready = ref<ReplayPayload | null>(null);
const job = ref('');
const playing = ref(false);
const speed = ref(1);
const cursorMs = ref(0);
const seekSeconds = ref(0);
const overlayOn = ref(localStorage.getItem(OVERLAY_ENABLED_KEY) === '1');

let pollTimer: number | undefined;
let clockTimer: number | undefined;

const formatSeconds = (value: number) => {
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`;
};

const durationLabel = computed(() =>
  ready.value ? formatSeconds(ready.value.durationMs / 1000) : '--:--',
);
const cursorLabel = computed(() => formatSeconds(cursorMs.value / 1000));
const rowCount = computed(() => ready.value?.rows.length ?? 0);

/** 游标已经走过的事件：让人看得见「这一刻到底喂给了前端什么」。 */
const playedRows = computed(() => {
  if (!ready.value) return [];
  const cursor = cursorMs.value;
  return ready.value.rows
    .filter((row) => row.timestamp <= cursor)
    .slice(-16)
    .reverse();
});
const totalRows = computed(() => ready.value?.rows.length ?? 0);
const playedCount = computed(() => {
  if (!ready.value) return 0;
  const cursor = cursorMs.value;
  return ready.value.rows.filter((row) => row.timestamp <= cursor).length;
});

async function loadCaptures() {
  try {
    const payload = await simulatorApi.replayCaptures();
    captures.value = payload.captures || [];
    if (!selected.value && captures.value.length) selected.value = captures.value[0].path;
  } catch (error) {
    errorText.value = `读取录像列表失败：${(error as Error)?.message || error}`;
  }
}

/**
 * 回放自检：把「悬浮窗这一刻收到什么」直接显示出来。
 *
 * 语音和倒计时都走同一条事件链，但断点可能在发布端、也可能在窗口侧。
 * 这里订阅和悬浮窗**完全相同**的两个主题，所以这一块显示什么，
 * 悬浮窗就该显示什么 —— 不一致时，一眼能看出断在哪一段。
 */
const probeState = ref('');
const probeNest = ref('');
const probeBoss = ref('');
const probeOverlayAt = ref(0);
const probeRows = ref<{ key: string; left: number; ready: boolean }[]>([]);
const probeCountdownAt = ref(0);
const probeTick = ref(Date.now());
let probeOverlayUnsub: (() => void) | null = null;
let probeCountdownUnsub: (() => void) | null = null;
let probeTimer: number | undefined;

async function startProbe() {
  if (probeOverlayUnsub) return;
  const target = OVERLAY_WINDOW_LABELS.skill;
  probeOverlayUnsub = await subscribeOverlayEvent<BuffOverlayStatus>(
    OVERLAY_EVENT_TOPICS.combatSnapshot,
    (next) => {
      const row = (next || {}) as unknown as Record<string, unknown>;
      probeState.value = String(row.state || '');
      probeNest.value = String(row.nestName || row.sceneName || '');
      probeBoss.value = String(row.bossName || '');
      probeOverlayAt.value = Date.now();
    },
    target,
  );
  probeCountdownUnsub = await subscribeOverlayEvent<SkillCountdownSnapshot>(
    OVERLAY_EVENT_TOPICS.skillCountdowns,
    (next) => {
      probeRows.value = (next?.rows || []).map((row) => ({
        key: String(row.ruleKey || '?'),
        left: Math.max(0, Math.round(Number(row.remainingMs || 0) / 1000)),
        ready: row.ready === true,
      }));
      probeCountdownAt.value = Number(next?.publishedAt || 0);
    },
    target,
  );
  if (probeTimer) window.clearInterval(probeTimer);
  probeTimer = window.setInterval(() => { probeTick.value = Date.now(); }, 500);
}

function stopProbe() {
  probeOverlayUnsub?.();
  probeCountdownUnsub?.();
  probeOverlayUnsub = null;
  probeCountdownUnsub = null;
  if (probeTimer) window.clearInterval(probeTimer);
  probeTimer = undefined;
}

const probeOverlayAge = computed(() => {
  void probeTick.value;
  return probeOverlayAt.value ? Math.max(0, Math.round((probeTick.value - probeOverlayAt.value) / 1000)) : null;
});
const probeCountdownAge = computed(() => {
  void probeTick.value;
  return probeCountdownAt.value ? Math.max(0, Math.round((probeTick.value - probeCountdownAt.value) / 1000)) : null;
});
const probeActive = computed(() => ['starting', 'running'].includes(probeState.value));

function stopTimers() {
  if (pollTimer) window.clearInterval(pollTimer);
  if (clockTimer) window.clearInterval(clockTimer);
  pollTimer = undefined;
  clockTimer = undefined;
}

async function openReplay() {
  const path = (manualPath.value || selected.value || '').trim();
  if (!path) {
    errorText.value = '先选一个录像';
    return;
  }
  stopTimers();
  stopReplay();
  playing.value = false;
  ready.value = null;
  errorText.value = '';
  loading.value = true;
  progress.value = 0;
  message.value = '正在打开...';
  try {
    const opened = await simulatorApi.replayOpen(path);
    if (!opened.ok || !opened.job) {
      errorText.value = opened.error || '打不开这个录像';
      loading.value = false;
      return;
    }
    job.value = opened.job;
    pollTimer = window.setInterval(async () => {
      try {
        const status = await simulatorApi.replayStatus(job.value);
        progress.value = status.progress ?? 0;
        message.value = status.message || '';
        if (status.state === 'error') {
          errorText.value = status.error || '解析失败';
          loading.value = false;
          stopTimers();
          return;
        }
        if (status.state !== 'ready') return;
        stopTimers();
        loading.value = false;
        ready.value = {
          capture: status.capture || '',
          nestName: status.nestName || '',
          nestKey: status.nestKey || '',
          durationMs: status.durationMs || 0,
          rows: (status.rows || []) as ReplayPayload['rows'],
          bosses: (status.bosses || []) as ReplayPayload['bosses'],
          elapsedMs: status.elapsedMs,
        };
        const dropped = Object.values(status.drops || {}).reduce((sum, value) => sum + value, 0);
        message.value = dropped
          ? `解析完成，但有 ${dropped} 条 Boss 技能被丢弃（${JSON.stringify(status.drops)}）`
          : `解析完成，${ready.value.rows.length} 行`;
      } catch (error) {
        stopTimers();
        loading.value = false;
        errorText.value = `查询进度失败：${(error as Error)?.message || error}`;
      }
    }, 300);
  } catch (error) {
    loading.value = false;
    errorText.value = `打开失败：${(error as Error)?.message || error}`;
  }
}

async function startPlayback(fromMs = 0) {
  if (!ready.value) return;
  // 回放会接管 status / voice-events。真实统计同时跑的话，抓包还在干活，
  // 界面却全是回放内容 —— 先停下再放，避免两边抢同一组接口。
  if (!replayActive()) {
    try {
      const live = await simulatorApi.captureStatus(false);
      if (['starting', 'running'].includes(live.state)) await simulatorApi.stopCapture();
    } catch {
      /* 后端没起时照常回放，让下面的报错去说明 */
    }
    if (!ready.value) return;
  }
  startReplay(job.value, ready.value, { offsetMs: fromMs, speed: speed.value });
  playing.value = true;
  stopTimers();
  clockTimer = window.setInterval(() => {
    if (!replayActive()) return;
    cursorMs.value = replayElapsedMs();
    if (replayFinished()) {
      playing.value = false;
      if (clockTimer) window.clearInterval(clockTimer);
      clockTimer = undefined;
    }
  }, 200);
}

function togglePlay() {
  if (!ready.value) return;
  if (!replayActive()) {
    startPlayback(seekSeconds.value * 1000);
    return;
  }
  if (playing.value) {
    pauseReplay();
    playing.value = false;
  } else {
    resumeReplay();
    playing.value = true;
  }
}

function stopPlayback() {
  stopReplay();
  playing.value = false;
  cursorMs.value = 0;
  if (clockTimer) window.clearInterval(clockTimer);
  clockTimer = undefined;
}

function jumpTo() {
  const target = Math.max(0, seekSeconds.value * 1000);
  if (!ready.value) return;
  if (!replayActive()) {
    startPlayback(target);
    return;
  }
  seekReplay(target);
  cursorMs.value = replayElapsedMs();
}

function changeSpeed(next: number) {
  speed.value = next;
  setReplaySpeed(next);
}

async function toggleOverlay() {
  overlayOn.value = !overlayOn.value;
  localStorage.setItem(OVERLAY_ENABLED_KEY, overlayOn.value ? '1' : '0');
  if (!props.tauriAvailable) return;
  try {
    await invoke('set_buff_overlay_visible', {
      visible: overlayOn.value,
      overlayLabels: enabledOverlayWindowLabels(readBuffOverlaySettings()),
    });
  } catch {
    /* 窗口会在下一次抓包开始时重建，这里失败不影响回放本身 */
  }
}

onActivated(() => {
  void startProbe();
  void loadCaptures();
  if (replaySession()) {
    playing.value = !replaySession()!.paused;
    cursorMs.value = replayElapsedMs();
    stopTimers();
    clockTimer = window.setInterval(() => {
      if (!replayActive()) return;
      cursorMs.value = replayElapsedMs();
    }, 200);
  }
});
onDeactivated(() => {
  stopTimers();
  stopProbe();
});
onUnmounted(() => {
  stopTimers();
  stopProbe();
  stopPlayback();
});
</script>

<template>
  <section class="replay-view">
    <header class="replay-head">
      <button class="replay-back" type="button" @click="emit('back')">
        <ArrowLeft :size="16" /><span>返回</span>
      </button>
      <div class="replay-title">
        <p class="replay-eyebrow">开发者模式</p>
        <h1>回放实战录像</h1>
      </div>
      <label class="replay-overlay">
        <input type="checkbox" :checked="overlayOn" @change="toggleOverlay" />
        <span>打开悬浮窗</span>
      </label>
    </header>

    <div class="replay-body">
      <p class="replay-hint">
        导入一场抓包，下面会按当时的节奏重放：语音照常播报、悬浮窗照常倒计时。
        走的是和实战同一套规则，所以听起来、看起来对不对，就是实战的结果。
      </p>

      <div class="replay-row">
        <label class="replay-label">选择录像</label>
        <div class="replay-select-line">
          <select v-model="selected" class="replay-select" :disabled="loading">
            <option value="">（从列表里选）</option>
            <option v-for="item in captures" :key="item.path" :value="item.path">
              {{ item.name }} · {{ (item.sizeBytes / 1048576).toFixed(1) }}MB
            </option>
          </select>
          <button class="replay-btn" type="button" :disabled="loading" @click="loadCaptures">
            <FolderOpen :size="14" /><span>刷新</span>
          </button>
        </div>
      </div>

      <div class="replay-row">
        <label class="replay-label">或手动填路径</label>
        <input
          v-model="manualPath"
          class="replay-input"
          type="text"
          spellcheck="false"
          placeholder="C:\Users\...\capture_xxx.dpscap"
          :disabled="loading"
        />
      </div>

      <div class="replay-actions">
        <button class="replay-btn primary" type="button" :disabled="loading" @click="openReplay">
          {{ loading ? '解析中…' : '载入录像' }}
        </button>
        <button class="replay-btn" type="button" :disabled="!ready" @click="togglePlay">
          <Pause v-if="playing" :size="14" /><Play v-else :size="14" />
          <span>{{ playing ? '暂停' : replayActive() ? '继续' : '播放' }}</span>
        </button>
        <button class="replay-btn" type="button" :disabled="!replayActive()" @click="stopPlayback">
          <Square :size="14" /><span>停止</span>
        </button>
        <div class="replay-speed">
          <button
            v-for="value in [1, 2]"
            :key="value"
            class="replay-speed-btn"
            :class="{ active: speed === value }"
            type="button"
            @click="changeSpeed(value)"
          >
            {{ value }}x
          </button>
        </div>
      </div>

      <div v-if="loading" class="replay-progress">
        <div class="replay-progress-bar"><i :style="{ width: `${Math.round(progress * 100)}%` }" /></div>
        <span>{{ Math.round(progress * 100) }}% · {{ message }}</span>
      </div>

      <div v-if="ready" class="replay-meta">
        <span>副本：<b>{{ ready.nestName || '未识别' }}</b></span>
        <span>时长：<b>{{ durationLabel }}</b></span>
        <span>证据行：<b>{{ rowCount }}</b></span>
        <span>解析耗时：<b>{{ ((ready.elapsedMs || 0) / 1000).toFixed(1) }}s</b></span>
      </div>

      <div v-if="ready" class="replay-seek">
        <span class="replay-time">{{ cursorLabel }} / {{ durationLabel }}</span>
        <input
          v-model.number="seekSeconds"
          class="replay-input short"
          type="number"
          min="0"
          :max="Math.floor(ready.durationMs / 1000)"
        />
        <span class="replay-unit">秒</span>
        <button class="replay-btn" type="button" @click="jumpTo">跳到此处</button>
      </div>

      <p v-if="message && !loading" class="replay-note">{{ message }}</p>
      <p v-if="errorText" class="replay-error">{{ errorText }}</p>

      <div class="replay-probe">
        <div class="replay-probe-head">
          <strong>悬浮窗自检</strong>
          <span>订阅的是悬浮窗那两个主题，这里显示什么，窗口就该显示什么</span>
        </div>
        <div class="replay-probe-row">
          <span class="k">快照</span>
          <b :class="probeActive ? 'is-ok' : 'is-bad'">{{ probeState || '未收到' }}</b>
          <span class="v">{{ probeNest || '副本未识别' }}</span>
          <span class="v">{{ probeBoss || 'Boss 未识别' }}</span>
          <span class="t">{{ probeOverlayAge === null ? '' : probeOverlayAge + 's 前' }}</span>
        </div>
        <div class="replay-probe-row">
          <span class="k">倒计时</span>
          <b :class="probeRows.length ? 'is-ok' : 'is-bad'">{{ probeRows.length }} 行</b>
          <span class="v">
            {{ probeRows.map((row) => `${row.key} ${row.ready ? '已好' : row.left + 's'}`).join(' · ') || '—' }}
          </span>
          <span class="t">{{ probeCountdownAge === null ? '' : probeCountdownAge + 's 前' }}</span>
        </div>
        <p v-if="probeCountdownAge !== null && probeCountdownAge > 3" class="replay-probe-warn">
          距上次发布已 {{ probeCountdownAge }} 秒，超过 3 秒冻结门 —— 悬浮窗会把整表清空。
        </p>
      </div>

      <div v-if="ready" class="replay-feed">
        <div class="replay-feed-head">
          <strong>已播事件</strong>
          <span>{{ playedCount }} / {{ totalRows }}</span>
          <span class="replay-feed-hint">游标走到哪，这些行就按当时节奏喂给语音与悬浮窗</span>
        </div>
        <table class="replay-feed-table">
          <thead>
            <tr><th>时间</th><th>技能</th><th>模板</th><th>来源</th><th>类型</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in playedRows" :key="`${row.timestamp}-${row.skill}-${index}`">
              <td>{{ formatSeconds(row.timestamp / 1000) }}</td>
              <td>{{ row.skill || '—' }}</td>
              <td>{{ row.templateId || '—' }}</td>
              <td class="replay-feed-src">{{ row.source || '—' }}</td>
              <td>{{ row.kind || row.evidenceKind || '技能' }}</td>
            </tr>
            <tr v-if="!playedRows.length">
              <td colspan="5" class="replay-feed-empty">还没走到任何事件</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.replay-view {
  display: flex;
  flex-direction: column;
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
.replay-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 18px 10px;
  flex: 0 0 auto;
}
.replay-title { flex: 1 1 auto; min-width: 0; }
.replay-eyebrow {
  margin: 0;
  font-size: 11px;
  letter-spacing: .08em;
  text-transform: uppercase;
  opacity: .6;
}
.replay-title h1 { margin: 2px 0 0; font-size: 18px; font-weight: 600; }
.replay-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border-radius: 999px;
  border: 1px solid var(--frost-line-strong, rgba(29, 39, 51, .16));
  background: var(--frost-glass, rgba(255, 255, 255, .6));
  color: var(--frost-ink, inherit);
  cursor: pointer;
}
.replay-overlay { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.replay-probe {
  margin: 14px 0 4px;
  padding: 10px 14px;
  border: 1px solid rgba(29, 39, 51, .10);
  border-radius: 12px;
  background: rgba(255, 255, 255, .5);
  font-size: 12.5px;
}
.replay-probe-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.replay-probe-head strong { font-size: 13px; }
.replay-probe-head span { color: rgba(29, 39, 51, .5); font-size: 11.5px; }
.replay-probe-row {
  display: flex; align-items: baseline; gap: 10px;
  padding: 3px 0; border-top: 1px dashed rgba(29, 39, 51, .08);
  font-variant-numeric: tabular-nums;
}
.replay-probe-row .k { width: 48px; color: rgba(29, 39, 51, .55); }
.replay-probe-row .v { color: rgba(29, 39, 51, .78); }
.replay-probe-row .t { margin-left: auto; color: rgba(29, 39, 51, .45); font-size: 11.5px; }
.replay-probe-row .is-ok { color: #1f7a4d; }
.replay-probe-row .is-bad { color: #a8442a; }
.replay-probe-warn { margin: 6px 0 0; color: #a8442a; font-size: 11.8px; }

.replay-feed {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin: 14px 0 16px;
  border: 1px solid rgba(29, 39, 51, .10);
  border-radius: 12px;
  overflow: hidden;
  background: rgba(255, 255, 255, .5);
}
.replay-feed-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 9px 14px;
  border-bottom: 1px solid rgba(29, 39, 51, .08);
  font-size: 12.5px;
}
.replay-feed-head strong { font-size: 13px; }
.replay-feed-head span { color: rgba(29, 39, 51, .5); font-variant-numeric: tabular-nums; }
.replay-feed-hint { margin-left: auto; font-size: 11.5px; }
.replay-feed-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  overflow: auto;
  display: block;
  flex: 1 1 auto;
  min-height: 0;
}
.replay-feed-table thead th {
  position: sticky;
  top: 0;
  text-align: left;
  font-weight: 600;
  font-size: 11.5px;
  color: rgba(29, 39, 51, .55);
  background: rgba(249, 251, 254, .96);
  padding: 7px 12px;
  border-bottom: 1px solid rgba(29, 39, 51, .08);
}
.replay-feed-table td {
  padding: 6px 12px;
  border-bottom: 1px solid rgba(29, 39, 51, .05);
  font-variant-numeric: tabular-nums;
}
.replay-feed-src { color: rgba(29, 39, 51, .55); font-family: ui-monospace, Consolas, monospace; }
.replay-feed-empty { text-align: center; color: rgba(29, 39, 51, .4); padding: 18px 0; }

.replay-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-anchor: none;
  padding: 4px 18px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.replay-hint { margin: 0; font-size: 13px; line-height: 1.6; opacity: .78; }
.replay-row { display: flex; flex-direction: column; gap: 6px; }
.replay-label { font-size: 12px; opacity: .66; }
.replay-select-line { display: flex; gap: 8px; }
.replay-select, .replay-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--frost-line-strong, rgba(29, 39, 51, .16));
  background: var(--frost-glass-strong, rgba(255, 255, 255, .72));
  color: var(--frost-ink, inherit);
  font-size: 13px;
}
.replay-input.short { flex: 0 0 110px; }
.replay-unit { font-size: 12px; opacity: .6; align-self: center; }
.replay-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.replay-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--frost-line-strong, rgba(29, 39, 51, .16));
  background: var(--frost-glass, rgba(255, 255, 255, .6));
  color: var(--frost-ink, inherit);
  cursor: pointer;
  font-size: 13px;
}
.replay-btn:disabled { opacity: .42; cursor: not-allowed; }
.replay-btn.primary { background: rgba(120, 170, 255, .22); border-color: rgba(120, 170, 255, .5); }
.replay-speed { display: inline-flex; border-radius: 8px; overflow: hidden; border: 1px solid rgba(255, 255, 255, .16); }
.replay-speed-btn {
  padding: 7px 12px;
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  font-size: 12px;
}
.replay-speed-btn.active { background: rgba(120, 170, 255, .28); }
.replay-progress { display: flex; flex-direction: column; gap: 6px; font-size: 12px; opacity: .86; }
.replay-progress-bar { height: 6px; border-radius: 999px; background: rgba(255, 255, 255, .12); overflow: hidden; }
.replay-progress-bar i { display: block; height: 100%; background: rgba(120, 170, 255, .8); transition: width .2s; }
.replay-meta { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; }
.replay-meta b { font-weight: 600; }
.replay-seek { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.replay-time { font-variant-numeric: tabular-nums; font-size: 14px; }
.replay-note { margin: 0; font-size: 12px; opacity: .7; }
.replay-error { margin: 0; font-size: 12px; color: #ff9a9a; }
</style>
