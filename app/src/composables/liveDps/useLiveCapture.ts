import { computed, onMounted, onUnmounted, ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { emitTo } from "@tauri-apps/api/event";
import { simulatorApi } from "../../services/api";
import type { BuffOverlaySettings, BuffOverlayStatus, CaptureStatus } from "../../types/api";
import { overlaySessionEnding } from "../../utils/overlaySession";
import { writeOverlayLayoutMode } from "../../utils/overlayLayoutFlag";
import {
  ALL_OVERLAY_WINDOW_LABELS,
  BUFF_OVERLAY_STORAGE_KEY,
  enabledOverlayWindowLabels,
  readBuffOverlaySettings,
} from "../../utils/buffOverlaySettings";
import { writeCaptureToggles } from "../../utils/captureToggles";

const CAPTURE_NOTICE_MS = 900;
const TARGET_SELECTION_STORAGE_KEY = "dragon-nest-v420-buff-overlay-enabled";
/** 悬浮窗设置里 Buff 目录接口的等待上限，超时就当没有目录，不能卡住弹窗。 */
const OVERLAY_CATALOG_TIMEOUT_MS = 4_000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer = 0;
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(() => reject(new Error("请求超时")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => window.clearTimeout(timer));
}

export function isCaptureActiveState(state: unknown): boolean {
  return ["starting", "running"].includes(String(state || ""));
}

export function useLiveCapture() {
  const capture = ref<CaptureStatus | null>(null);
  const loading = ref(false);
  const error = ref("");
  const captureToggles = ref<Record<string, boolean>>({});
  const captureToggleLabels = ref<Record<string, string>>({});
  const overlaySettingsOpen = ref(false);
  const overlayOptions = ref<BuffOverlayStatus["buffOptions"]>([]);
  const overlaySettings = ref<BuffOverlaySettings>(readBuffOverlaySettings());
  let captureTimer: number | null = null;
  let refreshInFlight = false;
  let refreshPending = false;
  let requestGeneration = 0;

  const captureRunning = computed(() => isCaptureActiveState(capture.value?.state));
  const captureStopping = computed(() => capture.value?.state === "stopping");
  const captureBusy = computed(() => captureRunning.value || captureStopping.value);

  function isTauri(): boolean {
    return "__TAURI_INTERNALS__" in window;
  }

  async function refreshCapture() {
    if (refreshInFlight) {
      refreshPending = true;
      return;
    }
    refreshInFlight = true;
    try {
      do {
        refreshPending = false;
        const generation = requestGeneration;
        const wasRunning = captureRunning.value;
        try {
          const next = await simulatorApi.captureStatus(false);
          if (generation !== requestGeneration) continue;
          capture.value = next;
          if (!wasRunning && isCaptureActiveState(next.state)) await showStartedOverlay();
          if (wasRunning && !isCaptureActiveState(next.state)) await hideAfterStop();
          if (next.error) error.value = next.error;
        } catch (reason) {
          if (generation !== requestGeneration) continue;
          const message = reason instanceof Error ? reason.message : String(reason);
          if (capture.value && isCaptureActiveState(capture.value.state)) {
            capture.value = { ...capture.value, state: "error", status: "解析核心连接中断", error: message };
          }
          // Before the first successful status the engine is simply still
          // starting; the window-level banner already says so. Showing a red
          // "连接已断开" here made every launch look like a failure.
          if (capture.value) error.value = message;
        }
      } while (refreshPending);
    } finally {
      refreshInFlight = false;
    }
  }

  async function emitNotice(message: string, tone: "start" | "stop") {
    if (!isTauri()) return;
    const labels = enabledOverlayWindowLabels(overlaySettings.value);
    await Promise.allSettled(labels.map(label => emitTo(label, "buff-overlay-capture-notice", {
      message, tone, durationMs: CAPTURE_NOTICE_MS,
    })));
  }

  async function applyOverlayVisibility(visible: boolean): Promise<boolean> {
    if (!isTauri()) return true;
    const labels = enabledOverlayWindowLabels(overlaySettings.value);
    try {
      if (visible && !labels.length) {
        // 首页两个总开关都关掉（或分项全关）时没有任何窗口该出现，显式隐藏
        // 残留窗口；否则上次采集留下的 WebView 会一直挂在那里。
        await invoke("set_buff_overlay_visible", {
          visible: false,
          overlayLabels: [...ALL_OVERLAY_WINDOW_LABELS],
        });
        return true;
      }
      await invoke("set_buff_overlay_visible", {
        visible,
        overlayLabels: labels,
      });
      return true;
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
      return false;
    }
  }

  async function showStartedOverlay(): Promise<void> {
    localStorage.setItem(TARGET_SELECTION_STORAGE_KEY, "1");
    if (await applyOverlayVisibility(true)) await emitNotice("开始采集", "start");
  }

  let stoppingNotice: Promise<void> | null = null;
  function hideAfterStop(): Promise<void> {
    if (localStorage.getItem(TARGET_SELECTION_STORAGE_KEY) !== "1") return Promise.resolve();
    if (stoppingNotice) return stoppingNotice;
    stoppingNotice = (async () => {
      await emitNotice("关闭采集", "stop");
      await new Promise(resolve => window.setTimeout(resolve, CAPTURE_NOTICE_MS));
      if (!isCaptureActiveState(capture.value?.state)) await applyOverlayVisibility(false);
    })().finally(() => { stoppingNotice = null; });
    return stoppingNotice;
  }

  async function startCapture() {
    requestGeneration += 1;
    loading.value = true;
    error.value = "";
    try {
      capture.value = await simulatorApi.startCapture(0);
      await showStartedOverlay();
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
      loading.value = false;
      await refreshCapture();
    }
  }

  async function stopCapture() {
    requestGeneration += 1;
    refreshPending = false;
    if (capture.value) capture.value = { ...capture.value, state: "stopping", status: "正在结束统计" };
    try {
      await hideAfterStop();
      capture.value = await simulatorApi.stopCapture();
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
    } finally {
      await refreshCapture();
    }
  }

  async function loadCaptureToggles() {
    try {
      const payload = await simulatorApi.captureToggles();
      captureToggles.value = payload.toggles || {};
      captureToggleLabels.value = payload.labels || {};
      // 窗口显隐、发布器、热键都读这份缓存（它们拿不到网络上下文）。
      writeCaptureToggles(captureToggles.value);
    } catch {
      // Keep the panel usable while the core is still starting.
    }
  }

  async function setCaptureToggle(key: string, enabled: boolean) {
    const previous = captureToggles.value[key];
    captureToggles.value = { ...captureToggles.value, [key]: enabled };
    try {
      const payload = await simulatorApi.setCaptureToggles(captureToggles.value);
      captureToggles.value = payload.toggles || captureToggles.value;
      captureToggleLabels.value = payload.labels || captureToggleLabels.value;
      writeCaptureToggles(captureToggles.value);
      // 总开关只管「显示」：关掉立即隐藏该类悬浮窗，重新打开立即恢复。
      // 语音播报走独立事件流，不受这里影响。
      if (localStorage.getItem(TARGET_SELECTION_STORAGE_KEY) === "1") {
        await applyOverlayVisibility(true);
      }
    } catch (reason) {
      captureToggles.value = { ...captureToggles.value, [key]: previous };
      error.value = reason instanceof Error ? reason.message : String(reason);
    }
  }

  /**
   * 打开悬浮窗设置。
   *
   * 弹窗必须**先**打开再去拉 Buff 目录：目录接口只是一个可选增强，但以前是
   * `await` 在前，后端重启中 / 连接半开时请求会一直挂着（`request()` 没有
   * 超时），表现出来就是「点悬浮窗设置没反应」。用户实际遇到过这个现象。
   */
  async function openOverlaySettings() {
    overlaySettings.value = readBuffOverlaySettings();
    overlaySettingsOpen.value = true;
    try {
      overlayOptions.value = (await withTimeout(
        simulatorApi.buffOverlayStatus(true),
        OVERLAY_CATALOG_TIMEOUT_MS,
      )).buffOptions || [];
    } catch {
      // 目录拉不到就沿用上一次的选项；预览仍可用内置示例。
    }
  }

  function saveOverlaySettings(value: BuffOverlaySettings) {
    overlaySettings.value = value;
    localStorage.setItem(BUFF_OVERLAY_STORAGE_KEY, JSON.stringify(value));
    if (isTauri()) {
      void Promise.allSettled(ALL_OVERLAY_WINDOW_LABELS.map(label =>
        emitTo(label, "buff-overlay-settings-changed", value)));
      if (localStorage.getItem(TARGET_SELECTION_STORAGE_KEY) === "1") void applyOverlayVisibility(true);
    }
    overlaySettingsOpen.value = false;
  }

  /**
   * Overlay windows are created on demand, so the first "layout mode" message
   * was often delivered before the fresh WebView had registered its listener.
   * The window then stayed visible without the drag panel and with its old
   * (too small) size - that is why a second click "fixed" it. Re-send the state
   * a few times while the window boots; the handler is idempotent.
   */
  // A cold overlay WebView can take a couple of seconds to register its
  // listeners, so keep re-sending until it is definitely awake.
  const LAYOUT_MODE_ENABLE_RETRY_MS = [0, 150, 400, 900, 1600, 2400, 3600, 5000];
  const LAYOUT_MODE_DISABLE_RETRY_MS = [0, 200];

  let layoutModeTimers: number[] = [];

  function broadcastLayoutMode(enabled: boolean) {
    // Cancel pending deliveries first: finishing the adjustment must not be
    // undone by a late "enter layout mode" retry.
    for (const timer of layoutModeTimers) window.clearTimeout(timer);
    layoutModeTimers = [];
    const delays = enabled ? LAYOUT_MODE_ENABLE_RETRY_MS : LAYOUT_MODE_DISABLE_RETRY_MS;
    for (const delay of delays) {
      layoutModeTimers.push(window.setTimeout(() => {
        void Promise.allSettled(ALL_OVERLAY_WINDOW_LABELS.map(label =>
          emitTo(label, "buff-overlay-layout-mode", enabled)));
      }, delay));
    }
  }

  async function adjustOverlayLayout(): Promise<boolean> {
    if (!isTauri()) {
      error.value = "悬浮窗位置调整仅支持桌面版。";
      return false;
    }
    try {
      // The overlay WebView reads this on its first frame; the event broadcast
      // below only reaches windows that are already listening.
      writeOverlayLayoutMode(true);
      broadcastLayoutMode(true);
      await invoke("set_buff_overlay_visible", {
        visible: true,
        overlayLabels: [...ALL_OVERLAY_WINDOW_LABELS],
      });
      broadcastLayoutMode(true);
      return true;
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason);
      return false;
    }
  }

  function finishOverlayLayout() {
    if (!isTauri()) return;
    writeOverlayLayoutMode(false);
    broadcastLayoutMode(false);
    const visible = localStorage.getItem(TARGET_SELECTION_STORAGE_KEY) === "1";
    // 走统一入口：总开关关掉的那一侧在调整完位置后也要继续隐藏。
    void applyOverlayVisibility(visible).catch(() => undefined);
  }

  const onHotkeyState = () => { void refreshCapture(); };

  onMounted(() => {
    void refreshCapture();
    void loadCaptureToggles();
    captureTimer = window.setInterval(() => void refreshCapture(), 1000);
    window.addEventListener("dragon-nest-live-hotkey-state", onHotkeyState);
  });

  onUnmounted(() => {
    if (captureTimer !== null) window.clearInterval(captureTimer);
    window.removeEventListener("dragon-nest-live-hotkey-state", onHotkeyState);
  });

  return {
    capture, loading, error,
    captureRunning, captureStopping, captureBusy,
    captureToggles, captureToggleLabels, setCaptureToggle,
    overlaySettingsOpen, overlayOptions, overlaySettings,
    startCapture, stopCapture, refreshCapture,
    openOverlaySettings, saveOverlaySettings,
    adjustOverlayLayout, finishOverlayLayout,
  };
}
