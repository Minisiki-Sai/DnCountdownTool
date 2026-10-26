<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { Activity, AlertTriangle, LoaderCircle, Moon, Settings2, Sun } from "@lucide/vue";
import { getCurrentWindow } from "@tauri-apps/api/window";
import LiveDpsView from "./views/LiveDpsView.vue";
import SettingsView from "./views/SettingsView.vue";
import ReplayView from "./views/ReplayView.vue";
import BuffOverlay from "./components/BuffOverlay.vue";
import { simulatorApi } from "./services/api";
import { replayActive, stopReplay } from "./services/replaySource";
import { APP_NAME, APP_SHORT_NAME, APP_AUTHOR, APP_VERSION_LABEL } from "./appVersion";
import { readGlobalHotkeyConfiguration } from "./utils/globalHotkeys";
import {
  beginOverlaySession,
  endOverlaySession,
  overlaySessionEnding,
} from "./utils/overlaySession";
import { enabledOverlayWindowLabels, readBuffOverlaySettings, type OverlayWindowKind } from "./utils/buffOverlaySettings";
import { createOverlayEventPublisher, type OverlayEventPublisher } from "./utils/overlayEventPublisher";

type Theme = "light" | "dark";
// 历史键名：改了会清空用户已保存的主题偏好，故保留旧前缀，勿随包名调整。
const THEME_STORAGE_KEY = "dnact-frost-theme";

const params = new URLSearchParams(window.location.search);
const overlayViewName = params.get("view") || "";
const overlayKind = ({
  "buff-overlay": "status",
  "skill-countdown-overlay": "skill",
} as Record<string, OverlayWindowKind>)[overlayViewName] || null;
const isOverlayWindow = Boolean(overlayKind);
const tauriAvailable = Boolean((window as typeof window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);

beginOverlaySession(localStorage, isOverlayWindow);
if (isOverlayWindow) {
  document.documentElement.classList.add("dn-overlay-scene");
  document.body.classList.add("dn-overlay-scene");
} else {
  document.documentElement.classList.add("frost-scene");
  document.body.classList.add("frost-scene");
}

const theme = ref<Theme>(localStorage.getItem(THEME_STORAGE_KEY) === "dark" ? "dark" : "light");
watch(theme, value => {
  if (isOverlayWindow) return;
  document.documentElement.dataset.theme = value;
  localStorage.setItem(THEME_STORAGE_KEY, value);
}, { immediate: true });

const page = ref<"control" | "npcap" | "replay">("control");
const connected = ref(false);
const busy = ref(true);
const error = ref("");
let unlistenHotkey: UnlistenFn | null = null;
let unlistenExit: UnlistenFn | null = null;
let overlayPublisher: OverlayEventPublisher | null = null;

const themeLabel = computed(() => (theme.value === "dark" ? "深色" : "浅色"));
const appWindow = tauriAvailable ? getCurrentWindow() : null;
const closeApp = () => void appWindow?.close();
const minimizeApp = () => void appWindow?.minimize();
const toggleMaximize = () => void appWindow?.toggleMaximize();

async function connect(retries = 8) {
  busy.value = true;
  error.value = "";
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      await simulatorApi.health();
      connected.value = true;
      busy.value = false;
      return;
    } catch (reason) {
      if (attempt === retries) error.value = reason instanceof Error ? reason.message : String(reason);
      else await new Promise(resolve => window.setTimeout(resolve, 350));
    }
  }
  busy.value = false;
}

function toggleTheme() {
  theme.value = theme.value === "dark" ? "light" : "dark";
}

const OVERLAY_ENABLED_KEY = "dragon-nest-v420-buff-overlay-enabled";

async function setOverlayVisible(visible: boolean) {
  localStorage.setItem(OVERLAY_ENABLED_KEY, visible ? "1" : "0");
  if (!tauriAvailable || isOverlayWindow) return;
  try {
    await invoke("set_buff_overlay_visible", {
      visible,
      overlayLabels: enabledOverlayWindowLabels(readBuffOverlaySettings()),
    });
  } catch {
    // 悬浮窗窗口会在下一次抓包开始时重建，这里失败不影响统计本身。
  }
}

/**
 * 一键开始：统计 + 悬浮窗，两个一起开。
 *
 * 以前这里只有「开始统计」，悬浮窗要不要显示取决于上一次按 F11 的切换结果，
 * 所以经常出现「统计开了但看不到悬浮窗」。现在按一下就是完整的可战斗状态。
 */
async function startCaptureWithOverlay() {
  // 回放会接管 status/voice-events 两个接口。这里不先退出回放的话，
  // 真实统计其实已经在抓包，界面却一直显示回放内容 —— 看着像统计坏了。
  if (replayActive()) stopReplay();
  const current = await simulatorApi.captureStatus(false);
  if (!["starting", "running"].includes(current.state)) {
    await simulatorApi.startCapture(0);
  }
  await setOverlayVisible(true);
}

/**
 * 一键结束：停统计 + 关悬浮窗。
 *
 * 原先是「切换悬浮窗」，同一个键既可能开也可能关，取决于 localStorage 里上一次
 * 的状态；游戏里按一下没反应（其实是关掉了）的困惑就是这么来的。现在语义固定：
 * 这个键只负责关。
 */
async function stopCaptureWithOverlay() {
  // 回放中按「一键结束」要连回放一起停，否则窗口关了语音还在念。
  if (replayActive()) stopReplay();
  // 先关窗再停统计：反过来的话，统计停下时悬浮窗会先闪一遍「已好」。
  await setOverlayVisible(false);
  const current = await simulatorApi.captureStatus(false);
  if (["starting", "running"].includes(current.state)) {
    await simulatorApi.stopCapture();
  }
}

async function handleHotkey(action: string) {
  if (overlaySessionEnding()) return;
  if (action === "start-capture") {
    await startCaptureWithOverlay();
    window.dispatchEvent(new CustomEvent("dragon-nest-live-hotkey-state", { detail: { action: "start-capture" } }));
    return;
  }
  // 原生层仍叫 toggle-overlay（改名要动 Rust），但语义已固定为「一键结束」。
  if (action === "toggle-overlay") {
    await stopCaptureWithOverlay();
    window.dispatchEvent(new CustomEvent("dragon-nest-live-hotkey-state", { detail: { action: "stop-capture" } }));
  }
}

onMounted(async () => {
  if (isOverlayWindow) return;
  void connect();
  overlayPublisher = createOverlayEventPublisher();
  overlayPublisher.start();
  if (!tauriAvailable) return;
  try {
    await invoke("configure_global_hotkeys", readGlobalHotkeyConfiguration());
    unlistenHotkey = await listen<string>("dragon-nest-global-hotkey", event => {
      void handleHotkey(event.payload).catch(reason => {
        error.value = reason instanceof Error ? reason.message : String(reason);
      });
    });
  } catch {
    // Browser preview has no native hotkey host.
  }
  try {
    // The native shell waits for this acknowledgement before exiting. Remove
    // the build-save delay: this build has no persistent editor state.
    unlistenExit = await listen("dragon-nest-save-builds-before-exit", () => {
      endOverlaySession(localStorage);
      overlayPublisher?.stop();
      void invoke("finish_exit_after_build_save").catch(() => undefined);
    });
  } catch {
    // Older shells fall back to their own exit timeout.
  }
});

onUnmounted(() => {
  if (unlistenHotkey) unlistenHotkey();
  if (unlistenExit) unlistenExit();
  overlayPublisher?.stop();
  overlayPublisher = null;
});
</script>

<template>
  <BuffOverlay v-if="overlayKind" :view="overlayKind" />
  <div v-else class="frost-app">
    <div class="frost-titlebar" data-tauri-drag-region>
      <button class="frost-traffic is-close" type="button" title="关闭" @click="closeApp" />
      <button class="frost-traffic is-min" type="button" title="最小化" @click="minimizeApp" />
      <button class="frost-traffic is-max" type="button" title="最大化" @click="toggleMaximize" />
    </div>
    <main class="frost-panel">
      <!-- The window paints the real panel immediately. Waiting behind a
           splash until the capture engine finished loading made the first few
           seconds of every launch feel frozen: the controls were already on
           screen but could not be clicked. -->
      <KeepAlive><LiveDpsView v-if="page === 'control'" @open-npcap="page = 'npcap'" /></KeepAlive>
      <KeepAlive><SettingsView v-if="page === 'npcap'" @back="page = 'control'" @open-replay="page = 'replay'" /></KeepAlive>
      <KeepAlive><ReplayView v-if="page === 'replay'" :tauri-available="tauriAvailable" @back="page = 'npcap'" /></KeepAlive>
      <div v-if="!connected" class="frost-note is-warm" role="status" style="margin-top: 12px">
        <LoaderCircle v-if="busy" :size="15" class="frost-spin" />
        <AlertTriangle v-else :size="15" />
        <span>{{ busy ? "正在唤醒解析核心，界面已可用…" : (error || "解析核心未响应，点此重试。") }}</span>
        <button v-if="!busy" class="frost-link" type="button" @click="connect()">重新连接</button>
      </div>
      <div v-if="error && connected" class="frost-note is-error" role="alert" style="margin-top: 12px">
        <AlertTriangle :size="15" />{{ error }}
      </div>
    </main>

    <aside class="frost-rail">
      <button class="frost-brand" type="button" :title="APP_NAME" @click="page = 'control'">
        <img src="/app-icon.png" alt="" />
        <span class="frost-brand-copy">
          <strong>{{ APP_SHORT_NAME }}</strong>
          <span class="frost-brand-meta">
            <span>{{ APP_VERSION_LABEL }}</span>
            <span class="frost-author">作者：{{ APP_AUTHOR }}</span>
          </span>
        </span>
      </button>

      <!-- 右侧栏常驻两个页面入口：从设置页回去不只有左上角那个返回箭头，
           窗口被拖歪、或者滚动容器不听话时，这里也点得到。 -->
      <p class="frost-rail-caption">页面</p>
      <nav class="frost-rail-nav" aria-label="页面导航">
        <button
          class="frost-rail-item"
          :class="{ 'is-active': page === 'control' }"
          type="button"
          @click="page = 'control'"
        >
          <Activity :size="17" :stroke-width="1.7" />
          <span>统计面板</span>
        </button>
        <button
          class="frost-rail-item"
          :class="{ 'is-active': page === 'npcap' }"
          type="button"
          @click="page = 'npcap'"
        >
          <Settings2 :size="17" :stroke-width="1.7" />
          <span>设置</span>
        </button>
      </nav>

      <div class="frost-rail-footer">
        <span>{{ connected ? "解析核心已连接" : "正在连接解析核心" }}</span>
        <button class="frost-theme-toggle" type="button" @click="toggleTheme">
          <Moon v-if="theme === 'light'" :size="13" />
          <Sun v-else :size="13" />
          {{ themeLabel }}主题
        </button>
      </div>
    </aside>
  </div>
</template>
