<script setup lang="ts">
import { computed } from "vue";
import { AlertTriangle, Move, Play, Settings2, ShieldCheck, Square } from "@lucide/vue";
import BuffOverlaySettingsDialog from "../components/BuffOverlaySettingsDialog.vue";
import VoiceAnnouncementSettings from "../components/VoiceAnnouncementSettings.vue";
import { useLiveCapture } from "../composables/liveDps/useLiveCapture";
import type { BuffOverlaySettings } from "../types/api";

const emit = defineEmits<{ openNpcap: [] }>();

const {
  capture, loading, error,
  captureRunning, captureStopping, captureBusy,
  captureToggles, captureToggleLabels, setCaptureToggle,
  overlaySettingsOpen, overlayOptions, overlaySettings,
  startCapture, stopCapture,
  openOverlaySettings, saveOverlaySettings,
  adjustOverlayLayout, finishOverlayLayout,
} = useLiveCapture();

const stateKind = computed(() => {
  const state = String(capture.value?.state || "idle");
  if (state === "running") return "running";
  if (state === "starting") return "starting";
  if (state === "error") return "error";
  return "idle";
});

const stateTitle = computed(() => ({
  running: "抓包中",
  starting: "正在连接游戏",
  error: "抓包出错",
  idle: capture.value?.state === "stopped" ? "已停止" : "未连接",
}[stateKind.value]));

function readableError(value: string): string {
  return value.replace(/^(RuntimeError|NpcapError|Error):\s*/, "").trim();
}

const stateDetail = computed(() => {
  if (error.value) return readableError(error.value);
  if (stateKind.value === "idle") {
    return "点击「开始统计」连接游戏，解析增益/减益与 Boss 技能倒计时。";
  }
  return readableError(capture.value?.status || "") || "正在准备抓包环境…";
});

const nestLabel = computed(() => capture.value?.nestLabel || capture.value?.nestName || "");
// 进本后才开始统计时，副本不是从场景包读到的，而是按「正在打的 Boss」反推的。
// 标一下，避免用户以为识别出了确定值。
const nestInferred = computed(() => String(capture.value?.nestConfidence || "") === "inferred");

function toggleClass(key: string) {
  return key === "boss_actions" ? "is-warm" : "is-blue";
}

function toggleHint(key: string) {
  // 关闭即停止该类事件的聚合与推送（省掉后续计算），但解码不停：语音播报
  // 用的是同一批包，关掉悬浮窗后仍然照常出声。
  return key === "boss_actions"
    ? "关闭后停止该类事件的解析与推送，语音播报照常"
    : "关闭后停止增益/减益事件的解析与推送，语音播报照常";
}

function onToggle(key: string, event: Event) {
  void setCaptureToggle(key, (event.target as HTMLInputElement).checked);
}

function saveSettings(value: BuffOverlaySettings) {
  saveOverlaySettings(value);
  finishOverlayLayout();
}

async function adjustLayout() {
  if (await adjustOverlayLayout()) {
    window.setTimeout(() => finishOverlayLayout(), 60_000);
  }
}
</script>

<template>
  <div class="control-page">
    <header class="frost-status">
      <span class="frost-status-dot" :class="`is-${stateKind}`" />
      <div class="frost-status-copy">
        <h2 class="frost-status-title">{{ stateTitle }}</h2>
        <p class="frost-status-detail" :title="stateDetail">{{ stateDetail }}</p>
      </div>
      <div class="frost-status-meta">
        <span
          v-if="nestLabel"
          class="frost-chip"
          :title="nestInferred ? '进本后才开始统计，按你正在攻击的 Boss 反推的副本' : ''"
        >{{ nestLabel }}<small v-if="nestInferred"> · 推断</small></span>
      </div>
    </header>

    <section class="frost-switches" aria-label="采集内容开关">
      <label
        v-for="(label, key) in captureToggleLabels"
        :key="key"
        class="frost-switch"
        :class="[toggleClass(key), { 'is-busy': loading }]"
        :title="toggleHint(key)"
      >
        <span class="frost-switch-copy">
          <strong>{{ label }}</strong>
          <small>{{ toggleHint(key) }}</small>
        </span>
        <input
          type="checkbox"
          :checked="captureToggles[key] !== false"
          :disabled="loading"
          @change="onToggle(key, $event)"
        />
        <span class="frost-switch-control" aria-hidden="true" />
      </label>
    </section>

    <section class="frost-actions">
      <button
        v-if="!captureRunning && !captureStopping"
        class="frost-pill"
        type="button"
        :disabled="loading || capture?.npcapInstalled === false"
        @click="startCapture"
      >
        <Play :size="16" :stroke-width="2" />开始统计
      </button>
      <button v-else-if="captureStopping" class="frost-pill is-quiet" type="button" disabled>
        <Square :size="14" />正在结束
      </button>
      <button v-else class="frost-pill is-stop" type="button" @click="stopCapture">
        <Square :size="14" />停止统计
      </button>
      <p class="frost-pill-hint">
        {{ capture?.npcapInstalled === false ? "尚未检测到 Npcap，请先在右侧「Ncap 路径」中配置" : captureRunning ? "统计进行中，悬浮窗会随事件自动刷新" : "开始后自动打开已启用的悬浮窗" }}
      </p>
    </section>

    <div v-if="capture?.npcapInstalled === false" class="frost-note is-warn">
      <ShieldCheck :size="15" />
      <span>未检测到可用的 Npcap 抓包组件，抓包无法启动。</span>
      <button class="frost-link" type="button" @click="emit('openNpcap')">去配置 Ncap 路径</button>
    </div>

    <div v-if="error && stateKind !== 'error'" class="frost-note is-error">
      <AlertTriangle :size="15" />{{ error }}
    </div>

    <footer class="frost-entries">
      <button class="frost-entry" type="button" @click="openOverlaySettings">
        <Settings2 :size="15" :stroke-width="1.7" />悬浮窗设置
      </button>
      <span class="frost-entry-sep" />
      <VoiceAnnouncementSettings :running="captureRunning" :live="Boolean(capture)" :nest="nestLabel" />
      <span class="frost-entry-sep" />
      <button class="frost-entry" type="button" :disabled="captureBusy" @click="adjustLayout">
        <Move :size="15" :stroke-width="1.7" />调整位置
      </button>
    </footer>

    <BuffOverlaySettingsDialog
      :open="overlaySettingsOpen"
      :options="overlayOptions"
      :model-value="overlaySettings"
      @close="overlaySettingsOpen = false"
      @save="saveSettings"
    />
  </div>
</template>

<style scoped>
.control-page { display: flex; flex-direction: column; min-height: 0; height: 100%; }
.frost-link {
  margin-left: auto;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 11.5px;
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
}
</style>
