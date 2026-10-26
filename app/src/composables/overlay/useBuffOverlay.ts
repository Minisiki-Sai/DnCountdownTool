import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { emitTo, listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useBuffOverlayPresentation } from "./useBuffOverlayPresentation";
import type { BuffOverlaySettings, BuffOverlayStatus } from "../../types/api";
import {
  BUFF_OVERLAY_STORAGE_KEY,
  ALL_OVERLAY_WINDOW_LABELS,
  OVERLAY_WINDOW_LABELS,
  enabledOverlayWindowLabels,
  normalizeBuffOverlaySettings,
  readBuffOverlaySettings,
  type OverlayWindowKind,
} from "../../utils/buffOverlaySettings";
import {
  overlayCountdownIntervalMs,
  overlayDomWorkNeeded,
  overlayViewConfiguredForLiveUpdates,
} from "../../utils/overlayRuntimePolicy";
import {
  requestOverlayEventSnapshot,
  subscribeOverlayEvent,
  type OverlayEventUnlisten,
} from "../../utils/overlayEventBus";
import { OVERLAY_EVENT_TOPICS } from "../../utils/overlayEventProtocol";
import { browserLayoutPreviewRequested } from "../../utils/overlayLayoutPreview";
import { onOverlayLayoutModeChange, readOverlayLayoutMode } from "../../utils/overlayLayoutFlag";
import { visibleSkillCountdowns, SKILL_COUNTDOWN_OPTIONS, type SkillCountdownSnapshot } from "../../utils/skillCountdowns";
import { resolveSkillIdentity } from "../../utils/overlayIdentity";
import { VOICE_KEY, normalizeVoiceSettings } from "../../utils/voiceAnnouncements";

type OverlayTone = keyof BuffOverlaySettings["toneColors"];
type OverlayRow = { key: string; colorKey: string; name: string; sourceJob: string; detail: string; remaining: number; duration: number; permanent: boolean; tone: OverlayTone; groupOrder: number; sortOrder: number };

export type BuffOverlayProps = { view: OverlayWindowKind };

export function useBuffOverlay(props: BuffOverlayProps) {
  const payload = ref<BuffOverlayStatus | null>(null);
  const settings = ref<BuffOverlaySettings>(readBuffOverlaySettings());
  const fetchedAt = ref(performance.now());
  const clock = ref(performance.now());
  const skillSnapshot = ref<SkillCountdownSnapshot>({ rows: [], publishedAt: 0 });
  let skillUnlisten: OverlayEventUnlisten | null = null;
  const skillCountdownRows = computed(() => {
    void clock.value;
    if (props.view !== 'skill' || !settings.value.showSkillCountdownOverlay) return [];
    if (layoutMode.value) return SKILL_COUNTDOWN_OPTIONS
      .filter(row => settings.value.selectedSkillCountdowns.includes(row.key)).slice(0, 2)
      .map((row, index) => ({ key: row.key, label: row.name, ready: index === 0, remaining: index ? 32 : 0, progress: index ? 43 : 0 }));
    return captureActive.value ? visibleSkillCountdowns(skillSnapshot.value, Date.now(), payload.value?.endedBossIds)
      .filter(row => !!row.ruleKey && settings.value.selectedSkillCountdowns.includes(row.ruleKey)) : [];
  });
  const skillReadyRows = computed(() => skillCountdownRows.value.filter(row => row.ready));
  const skillPendingRows = computed(() => skillCountdownRows.value.filter(row => !row.ready));
  const skillPanelRowCount = computed(() => skillPendingRows.value.length + (skillReadyRows.value.length ? 1 : 0));
  const baseSeconds = ref(0);
  let clockTimer: number | null = null;
  let clockTimerDelay = 0;
  let enabledTimer: number | null = null;
  let enabled = false;
  // Mirrors `enabled` as reactive state: the shell now stays on screen for the
  // whole capture session, so a plain variable could not drive `v-show`.
  const shellVisible = ref(false);
  // The close notice is asynchronous. Guard it with a capture generation so
  // a stale stopped snapshot cannot hide a window reopened by a newer Start.
  let captureLifecycleGeneration = 0;
  let settingsSignature = "";
  let closingAfterCaptureStop = false;
  let visibilityUnlisten: UnlistenFn | null = null;
  let settingsUnlisten: UnlistenFn | null = null;
  let captureNoticeUnlisten: UnlistenFn | null = null;
  let layoutModeUnlisten: UnlistenFn | null = null;
  let layoutFlagUnlisten: (() => void) | null = null;
  let snapshotUnlisten: OverlayEventUnlisten | null = null;
  let snapshotRequestTimer: number | null = null;
  let snapshotRequestAttempts = 0;
  let captureNoticeTimer: number | null = null;
  const captureNotice = ref<{ message: string; tone: "start" | "stop" } | null>(null);
  const layoutMode = ref(false);
  const overlayShell = ref<HTMLElement | null>(null);
  let overlayResizeObserver: ResizeObserver | null = null;
  let overlayMeasureFrame: number | null = null;
  let overlayMeasureGeneration = 0;
  let overlayResizeRetryTimer: number | null = null;
  let overlayNativeResizeTimer: number | null = null;
  let overlayResizeInFlight = false;
  let overlayResizeRequest: Promise<void> | null = null;
  let pendingOverlaySize: { width: number; height: number } | null = null;
  let appliedOverlaySize = { width: 0, height: 0 };
  let overlayDomWorkActive = false;
  let componentMounted = false;
  let lastSlowNativeResizeReportAt = Number.NEGATIVE_INFINITY;
  let lastDevicePixelRatio = window.devicePixelRatio || 1;
  let cachedMonitorScaleFactor = 0;
  const OVERLAY_NATIVE_RESIZE_DEBOUNCE_MS = 100;
  const OVERLAY_NATIVE_RESIZE_TIMEOUT_MS = 1_500;
  const STATUS_PANEL_GAP = 4;
  const {
    windowLabel, isStatusView, isSkillView, layoutTitle,
    textValue, numberValue, captureActive,
    rows, buffRows, debuffRows, statusRows, standardStatusRows, compactEffectRows,
    hiddenSelectedBuffCount,
    overlayLayout, statusListStyle, statusIconListStyle, shellStyle, rowStyle,
    rowWidth, remainingText, rowIconStyle, rowIconProgressStyle, rowIconLabel,
    toggleDebuffBreakdown,
  } = useBuffOverlayPresentation({
    props,
    payload,
    settings,
    fetchedAt,
    clock,
    baseSeconds,
    layoutMode,
    statusPanelGap: STATUS_PANEL_GAP,
    skillCountdownCount: () => skillPanelRowCount.value,
    // 技能窗顶部两行身份要计入原生窗口高度，否则第一行会被裁掉。
    identityRowCount: () => (skillIdentityVisible.value ? 2 : 0),
  });
  function loadSettings() {
    const raw = localStorage.getItem(BUFF_OVERLAY_STORAGE_KEY) || "";
    if (raw === settingsSignature) return;
    settingsSignature = raw;
    settings.value = readBuffOverlaySettings();
  }
  // 语音播报白名单：只认「副本」这一层（设置页里勾了哪几个巢穴）。
  // 每次快照刷新都重读一次，主界面改完配置后这里最多晚一个刷新周期。
  const voiceNestWhitelist = computed(() => {
    void payload.value;
    try {
      const normalized = normalizeVoiceSettings(JSON.parse(localStorage.getItem(VOICE_KEY) || "{}"));
      return normalized.nests;
    } catch {
      return ["classic", "hard", "sand-hard", "sea-nightmare"];
    }
  });
  // 悬浮窗前两行的身份：副本 / Boss。判定规则集中在 resolveSkillIdentity 里，
  // 方便单独回归测试（已识别 / 不播报 / 非副本 / 未识别 四种状态）。
  // sceneName 由后端从地图历史给出，包含城镇，因此「抓到包就不会显示未识别」。
  const skillIdentity = computed(() => resolveSkillIdentity({
    nestName:
      textValue(payload.value?.sceneName) ||
      textValue(payload.value?.nestLabel) ||
      textValue(payload.value?.nestName),
    bossName: textValue(payload.value?.bossName),
    whitelist: voiceNestWhitelist.value,
    sceneKind: textValue(payload.value?.sceneKind),
    sample: layoutMode.value,
  }));
  // 身份两行（副本 / Boss）属于**技能播报窗**：进副本后显示在技能列表上方。
  // 增益/减益窗不显示，那里保持原来那句「采集中」。
  // 只有真正识别到副本（announced/muted）才显示；主城、庭院、未识别时让位给「采集中」。
  const skillIdentityVisible = computed(() => isSkillView.value
    && shellVisible.value
    && (skillIdentity.value.nest.state === "announced"
      || skillIdentity.value.nest.state === "muted"));
    function isTauri(): boolean { return "__TAURI_INTERNALS__" in window; }
  async function beginDrag() { if (isTauri()) try { await invoke("begin_buff_overlay_drag", { windowLabel: windowLabel.value }); } catch { /* Browser preview. */ } }
  async function setCurrentOverlayVisible(visible: boolean) {
    if (!isTauri()) return;
    try {
      await invoke("set_buff_overlay_window_visible", {
        windowLabel: windowLabel.value,
        visible,
      });
    } catch { /* The main window also controls global visibility. */ }
    finally { syncOverlayDomWork(); }
  }
  let layoutMeasureTimers: number[] = [];
  function clearLayoutMeasureTimers() {
    for (const timer of layoutMeasureTimers) window.clearTimeout(timer);
    layoutMeasureTimers = [];
  }
  function setLayoutMode(next: boolean) {
    layoutMode.value = next;
    clearLayoutMeasureTimers();
    syncOverlayDomWork();
    // A cold overlay WebView reports an empty shell on the first frame, so a
    // single measurement right after nextTick used to leave the preview
    // clipped until the user clicked "调整位置" a second time. Re-measure on a
    // short schedule until the preview DOM (and its fonts) have settled.
    const scheduleMeasure = (delay: number) => {
      layoutMeasureTimers.push(window.setTimeout(() => {
        if (!layoutMode.value || !componentMounted) return;
        // Native positioning may restore the previous (empty) window bounds.
        appliedOverlaySize = { width: 0, height: 0 };
        void measureAndResizeOverlay(true, true);
      }, delay));
    };
    void nextTick().then(() => scheduleMeasure(0));
    scheduleMeasure(120);
    if (!next) return; // Leaving layout mode only needs the window to shrink back.
    scheduleMeasure(400);
    scheduleMeasure(900);
  }
  async function finishLayoutMode() {
    if (!isTauri()) { setLayoutMode(false); return; }
    await Promise.all(ALL_OVERLAY_WINDOW_LABELS.map(label =>
      emitTo(label, "buff-overlay-layout-mode", false)));
    const visible = localStorage.getItem("dragon-nest-v420-buff-overlay-enabled") === "1";
    await invoke("set_buff_overlay_visible", {
      visible,
      overlayLabels: enabledOverlayWindowLabels(settings.value),
    });
  }
  function sizesAreEquivalent(
    left: { width: number; height: number },
    right: { width: number; height: number },
  ) {
    return Math.abs(left.width - right.width) <= 1
      && Math.abs(left.height - right.height) <= 1;
  }
  function expectedLocalEnabled(): boolean {
    if (layoutMode.value) return true;
    return localStorage.getItem("dragon-nest-v420-buff-overlay-enabled") === "1"
      && overlayViewConfiguredForLiveUpdates(props.view, settings.value);
  }
  function eventOverlayHasVisibleContent(): boolean {
    // Every remaining overlay (status, skill countdown, Boss health) is a
    // persistent window rather than an on-demand event alert.
    return true;
  }
  function shouldRunOverlayDomWork(): boolean {
    return overlayDomWorkNeeded(
      props.view,
      componentMounted,
      enabled,
      layoutMode.value,
      eventOverlayHasVisibleContent(),
    );
  }
  function suspendOverlayDomWork() {
    if (!overlayDomWorkActive && overlayMeasureFrame === null
      && overlayResizeRetryTimer === null && overlayNativeResizeTimer === null) return;
    overlayDomWorkActive = false;
    overlayMeasureGeneration += 1;
    pendingOverlaySize = null;
    if (overlayMeasureFrame !== null) window.cancelAnimationFrame(overlayMeasureFrame);
    if (overlayResizeRetryTimer !== null) window.clearTimeout(overlayResizeRetryTimer);
    if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
    overlayMeasureFrame = null;
    overlayResizeRetryTimer = null;
    overlayNativeResizeTimer = null;
    overlayResizeObserver?.disconnect();
    window.removeEventListener("resize", handleOverlayViewportChange);
    window.visualViewport?.removeEventListener("resize", handleOverlayViewportChange);
    document.fonts?.removeEventListener("loadingdone", scheduleOverlayMeasurement);
  }
  function resumeOverlayDomWork() {
    if (!shouldRunOverlayDomWork()) return;
    if (!overlayDomWorkActive) {
      overlayDomWorkActive = true;
      if (typeof ResizeObserver !== "undefined" && overlayShell.value) {
        overlayResizeObserver ||= new ResizeObserver(scheduleOverlayMeasurement);
        overlayResizeObserver.observe(overlayShell.value);
      }
      window.addEventListener("resize", handleOverlayViewportChange);
      window.visualViewport?.addEventListener("resize", handleOverlayViewportChange);
      document.fonts?.addEventListener("loadingdone", scheduleOverlayMeasurement);
      void document.fonts?.ready.then(() => {
        if (overlayDomWorkActive) scheduleOverlayMeasurement();
      });
    }
    scheduleOverlayMeasurement();
  }
  function syncOverlayDomWork() {
    if (shouldRunOverlayDomWork()) resumeOverlayDomWork();
    else suspendOverlayDomWork();
    if (shouldRunOverlayDomWork()) startTimers();
    else stopTimers();
  }

  function countdownRemainingSeconds(): number[] {
    if (!captureActive.value) return [];
    const values: number[] = [];
    values.push(...skillCountdownRows.value.map(row => row.remaining));
    if (isStatusView.value && (settings.value.showBuffOverlay || settings.value.showDebuffOverlay)) {
      values.push(...statusRows.value
        .filter(row => !row.permanent && row.duration > 0)
        .map(row => row.remaining));
    }
    return values;
  }
  function scheduleNativeOverlayResize() {
    if (!overlayDomWorkActive || !isTauri() || overlayResizeInFlight || overlayResizeRetryTimer !== null) return;
    const next = pendingOverlaySize;
    if (!next || sizesAreEquivalent(next, appliedOverlaySize)) {
      pendingOverlaySize = null;
      if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
      overlayNativeResizeTimer = null;
      return;
    }
    // Growth must not wait for countdown/ResizeObserver notifications to stop:
    // until the native viewport grows, otherwise valid rows are clipped.
    if (next.width > appliedOverlaySize.width + 1 || next.height > appliedOverlaySize.height + 1) {
      if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
      overlayNativeResizeTimer = null;
      void flushNativeOverlayResize();
      return;
    }
    // Coalesce shrinking against a bounded deadline. Repeated measurements,
    // including identical pending sizes, must never postpone that deadline.
    if (overlayNativeResizeTimer !== null) return;
    overlayNativeResizeTimer = window.setTimeout(() => {
      overlayNativeResizeTimer = null;
      void flushNativeOverlayResize();
    }, OVERLAY_NATIVE_RESIZE_DEBOUNCE_MS);
  }
  async function flushNativeOverlayResize(): Promise<void> {
    if (!overlayDomWorkActive || !isTauri()) return;
    if (overlayResizeInFlight) {
      // Event-only alerts await this before showing. An immediate growth may
      // already be in flight; wait for it (and any newer size), not just enqueue.
      await overlayResizeRequest?.catch(() => undefined);
      return flushNativeOverlayResize();
    }
    if (overlayResizeRetryTimer !== null) return;
    if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
    overlayNativeResizeTimer = null;
    const next = pendingOverlaySize;
    pendingOverlaySize = null;
    if (!next || sizesAreEquivalent(next, appliedOverlaySize)) return;
    overlayResizeInFlight = true;
    const resizeStartedAt = performance.now();
    let timedOut = false;
    try {
      overlayResizeRequest = invoke<void>("set_buff_overlay_size", {
        width: next.width,
        height: next.height,
        windowLabel: windowLabel.value,
      });
      // A native size request runs on Tauri's GUI thread.  When that thread is
      // busy (a WebView2 creation, a tray/drag operation) the promise can stay
      // pending for tens of seconds; measured 49.7 s in the field.  Waiting
      // here blocked every later resize, so rows arrived while the window kept
      // its old (tiny) height and the overlay looked empty.  Bound the wait and
      // retry through the normal measurement path instead.
      await Promise.race([        overlayResizeRequest,
        new Promise<void>(resolve => {
          window.setTimeout(() => { timedOut = true; resolve(); }, OVERLAY_NATIVE_RESIZE_TIMEOUT_MS);
        }),
      ]);
      if (!timedOut) appliedOverlaySize = next;
    } catch {
      // A WebView can finish mounting just before its native window becomes
      // available. Retry the newest stable DOM measurement, not every
      // intermediate ResizeObserver notification.
      if (overlayDomWorkActive) {
        if (overlayResizeRetryTimer !== null) window.clearTimeout(overlayResizeRetryTimer);
        overlayResizeRetryTimer = window.setTimeout(() => {
          overlayResizeRetryTimer = null;
          scheduleOverlayMeasurement();
        }, 160);
      }
    } finally {
      const resizeLatencyMs = performance.now() - resizeStartedAt;
      if (resizeLatencyMs > 500 && performance.now() - lastSlowNativeResizeReportAt > 5_000) {
        lastSlowNativeResizeReportAt = performance.now();
        void invoke("write_frontend_diagnostic", {
          kind: "overlay-native-resize-delay",
          message: `window=${windowLabel.value}\nlatency_ms=${resizeLatencyMs.toFixed(1)}\nwidth=${next.width}\nheight=${next.height}\nenabled=${enabled}\nlayout_mode=${layoutMode.value}`,
          stack: "",
          location: "useBuffOverlay.flushNativeOverlayResize",
        }).catch(() => undefined);
      }
      overlayResizeInFlight = false;
      overlayResizeRequest = null;
      if (timedOut && overlayDomWorkActive && overlayResizeRetryTimer === null) {
        // The native call is still queued; re-measure soon so the window catches
        // up as soon as the GUI thread responds.
        overlayResizeRetryTimer = window.setTimeout(() => {
          overlayResizeRetryTimer = null;
          scheduleOverlayMeasurement();
        }, 600);
      }
      scheduleNativeOverlayResize();
    }
  }
  function resizeOverlay(width: number, height: number) {
    if (!overlayDomWorkActive || !isTauri()) return;
    const next = {
      width: Math.max(1, Math.ceil(width)),
      height: Math.max(1, Math.ceil(height)),
    };
    if (sizesAreEquivalent(next, appliedOverlaySize) && !overlayResizeInFlight) {
      // Content may grow back while a shrink is pending. Discard the old shrink
      // rather than applying it later and cutting off the restored rows.
      pendingOverlaySize = null;
      if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
      overlayNativeResizeTimer = null;
      return;
    }
    pendingOverlaySize = next;
    scheduleNativeOverlayResize();
  }
  async function measureAndResizeOverlay(flushImmediately = false, authoritative = false) {
    if (!overlayDomWorkActive) return;
    const generation = ++overlayMeasureGeneration;
    await nextTick();
    if (!overlayDomWorkActive) return;
    const shell = overlayShell.value;
    if (!shell) return;
    const rect = shell.getBoundingClientRect();
    const cssWidth = Math.max(1, rect.width, shell.offsetWidth, shell.scrollWidth, overlayLayout.value.width);
    const cssHeight = Math.max(1, rect.height, shell.offsetHeight, shell.scrollHeight, overlayLayout.value.height);
    let zoomFactor = 1;
    if (isTauri()) {
      try {
        if (!(cachedMonitorScaleFactor > 0)) {
          cachedMonitorScaleFactor = await getCurrentWindow().scaleFactor();
        }
        const monitorScaleFactor = cachedMonitorScaleFactor;
        if (monitorScaleFactor > 0) {
          zoomFactor = Math.max(
            0.5,
            Math.min(4, (window.devicePixelRatio || monitorScaleFactor) / monitorScaleFactor),
          );
        }
      } catch { /* Logical CSS pixels are the safe fallback. */ }
    }
    if (!authoritative && generation !== overlayMeasureGeneration) return;
    // The rendered geometry (including overflow) remains authoritative.
    const edgeGuard = 1;
    resizeOverlay(
      Math.ceil((cssWidth + edgeGuard) * zoomFactor),
      Math.ceil((cssHeight + edgeGuard) * zoomFactor),
    );
    if (flushImmediately) {
      if (overlayNativeResizeTimer !== null) window.clearTimeout(overlayNativeResizeTimer);
      overlayNativeResizeTimer = null;
      await flushNativeOverlayResize();
    }
  }
  function scheduleOverlayMeasurement() {
    if (!overlayDomWorkActive) return;
    overlayMeasureGeneration += 1;
    if (overlayMeasureFrame !== null) return;
    overlayMeasureFrame = window.requestAnimationFrame(() => {
      overlayMeasureFrame = null;
      void measureAndResizeOverlay();
    });
  }
  function handleOverlayViewportChange() {
    if (!overlayDomWorkActive) return;
    lastDevicePixelRatio = window.devicePixelRatio || 1;
    // Moving between monitors can change the native scale independently from
    // the DOM size. Refresh it once for that transition, not on every row or
    // ResizeObserver callback.
    cachedMonitorScaleFactor = 0;
    scheduleOverlayMeasurement();
  }
  async function closeAfterCaptureStop() {
    if (!enabled || closingAfterCaptureStop) return;
    closingAfterCaptureStop = true;
    const generation = ++captureLifecycleGeneration;
    showCaptureNotice({ message: "关闭采集", tone: "stop", durationMs: 900 });
    await new Promise<void>(resolve => window.setTimeout(resolve, 900));
    if (
      generation !== captureLifecycleGeneration
      || ["starting", "running"].includes(payload.value?.state || "")
    ) {
      closingAfterCaptureStop = false;
      return;
    }
    localStorage.setItem("dragon-nest-v420-buff-overlay-enabled", "0");
    setOverlayEnabled(false);
    stopTimers();
    if (isTauri()) {
      try { await invoke("set_buff_overlay_visible", { visible: false }); }
      catch { /* The main window performs the same close operation. */ }
    }
    closingAfterCaptureStop = false;
  }
  function applyPayload(incoming: BuffOverlayStatus) {
      if (snapshotRequestTimer !== null) window.clearTimeout(snapshotRequestTimer);
      snapshotRequestTimer = null;
      const next: BuffOverlayStatus = {
        ...incoming,
        buffOptions: incoming.buffOptions
          ?? payload.value?.buffOptions
          ?? [],
      };
      const wasActive = ["starting", "running"].includes(payload.value?.state || "");
      const nextActive = ["starting", "running"].includes(next.state);
      const authoritativeSeconds = next.elapsedSeconds || 0;
      const newCapture = nextActive && (
        !wasActive
        || (next.state === "starting" && payload.value?.state !== "starting")
        || authoritativeSeconds + 0.05 < baseSeconds.value
      );
      // Re-anchor on every response.  Preserving a locally extrapolated clock
      // while multiplayer decoding lagged made finite effects expire against a
      // different clock than the backend and could display them as permanent.
      baseSeconds.value = authoritativeSeconds;
      payload.value = next; fetchedAt.value = performance.now(); clock.value = fetchedAt.value;
      // Keep the snapshot even while the shell is closed: a payload that lands
      // in the window between "capture started" and "overlay shown" used to be
      // dropped, and the overlay then waited for the next buff/debuff change.
      if (!enabled) return;
      syncOverlayDomWork();
      if (wasActive && !nextActive && props.view === "status") {
        void closeAfterCaptureStop();
      }
  }
  function requestInitialSnapshot() {
    if (!componentMounted || payload.value || snapshotRequestAttempts >= 12) return;
    snapshotRequestAttempts += 1;
    void requestOverlayEventSnapshot(
      OVERLAY_EVENT_TOPICS.combatSnapshot,
      windowLabel.value,
    );
    // The main window normally predates every on-demand WebView. A short,
    // bounded retry also covers a shell/main-window restart without restoring
    // permanent polling in hidden overlays.
    snapshotRequestTimer = window.setTimeout(requestInitialSnapshot, 500);
  }
  function startTimers() {
    if (layoutMode.value) { stopTimers(); return; }
    const nextDelay = overlayCountdownIntervalMs(
      settings.value.timeFormat,
      countdownRemainingSeconds(),
    );
    if (clockTimer !== null && clockTimerDelay === nextDelay) return;
    if (clockTimer !== null) window.clearTimeout(clockTimer);
    clockTimer = null;
    clockTimerDelay = nextDelay;
    if (!nextDelay) return;
    // Authoritative snapshots arrive through the shared publisher. Use a
    // one-shot clock so the cadence can fall from 1 s to 200 ms only near an
    // expiring timer, and stop completely as soon as no countdown is visible.
    clockTimer = window.setTimeout(() => {
      clockTimer = null;
      clock.value = performance.now();
      startTimers();
    }, nextDelay);
  }
  function stopTimers() {
    if (clockTimer !== null) window.clearTimeout(clockTimer);
    clockTimer = null;
    clockTimerDelay = 0;
  }
  function syncLocalState() {
    loadSettings();
    const currentDevicePixelRatio = window.devicePixelRatio || 1;
    if (Math.abs(currentDevicePixelRatio - lastDevicePixelRatio) > 1e-6) {
      lastDevicePixelRatio = currentDevicePixelRatio;
      cachedMonitorScaleFactor = 0;
      scheduleOverlayMeasurement();
    }
    const next = expectedLocalEnabled();
    if (next === enabled) return;
    setOverlayEnabled(next);
  }
  function setOverlayEnabled(next: boolean) {
    shellVisible.value = next;
    if (next === enabled) {
      syncOverlayDomWork();
      return;
    }
    enabled = next;
    syncOverlayDomWork();
  }
  function showCaptureNotice(value: unknown) {
    const row = value && typeof value === "object" ? value as Record<string, unknown> : {};
    const message = textValue(row.message);
    if (!message) return;
    const tone = textValue(row.tone) === "stop" ? "stop" : "start";
    if (tone === "start") captureLifecycleGeneration += 1;
    const duration = Math.max(300, numberValue(row.durationMs) || 900);
    captureNotice.value = { message, tone };
    if (captureNoticeTimer !== null) window.clearTimeout(captureNoticeTimer);
    captureNoticeTimer = window.setTimeout(() => {
      captureNotice.value = null;
      captureNoticeTimer = null;
    }, duration);
  }
  // The overlay stays on screen for the whole session, so an empty list still
  // has to say what the window is doing. "采集中 · 暂无增益" proves the capture
  // is alive; the whitelist variant points at the actual reason nothing shows.
  // 空态只在没有身份行可读时解释窗口在干什么。身份行（副本/Boss）已经把
  // 两个窗都在没有内容时显示「采集中」，让窗口常驻、能证明抓包还活着。
  // 增益窗一旦识别到副本，就换成「副本 / Boss」两行，不再显示这句。
  const emptyStateText = computed(() => (
    !payload.value || captureActive.value ? "采集中" : "统计已停止"
  ));
  const showEmptySection = computed(() => {
    if (!shellVisible.value || layoutMode.value || captureNotice.value) return false;
    if (props.view === "skill") {
      // 技能窗：进了副本就让位给两行身份；没进副本仍然显示「采集中」。
      return !skillIdentityVisible.value && !skillCountdownRows.value.length;
    }
    return !statusRows.value.length;
  });
  onMounted(() => {
    document.documentElement.classList.add("buff-overlay-mode"); document.body.classList.add("buff-overlay-mode");
    componentMounted = true;
    if (browserLayoutPreviewRequested()) setLayoutMode(true);
    // A window created for "调整位置" may finish loading after the main window
    // stopped re-sending the event; the shared flag is always already set.
    if (isTauri() && readOverlayLayoutMode()) setLayoutMode(true);
    // 技能倒计时事件只属于「技能播报」窗口（view='skill'）。这里原本守的是
    // view === 'status'，于是技能窗从不订阅、status 窗订阅了却没人用它渲染：
    // 倒计时行恒为空。数据一直在（语音同一条流能出声），只是窗口没接上。
    if (isSkillView.value) void subscribeOverlayEvent<SkillCountdownSnapshot>(
      OVERLAY_EVENT_TOPICS.skillCountdowns, next => {
        if (!Array.isArray(next?.rows) || !Number.isFinite(next.publishedAt)) return;
        skillSnapshot.value = next;
        clock.value = performance.now();
        syncOverlayDomWork();
      }, windowLabel.value,
    ).then(unlisten => { if (!componentMounted) unlisten(); else skillUnlisten = unlisten; });
    loadSettings(); setOverlayEnabled(expectedLocalEnabled());
    syncOverlayDomWork();
    void subscribeOverlayEvent<BuffOverlayStatus>(
      OVERLAY_EVENT_TOPICS.combatSnapshot,
      next => applyPayload(next),
      windowLabel.value,
    ).then(unlisten => {
      if (!componentMounted) unlisten();
      else {
        snapshotUnlisten = unlisten;
        // This WebView may have been created after the last state change. Ask
        // the main process for one targeted replay instead of polling the
        // backend independently or waiting indefinitely for another change.
        requestInitialSnapshot();
      }
    });
    if (isTauri()) {
      void listen<boolean>("buff-overlay-visible", event => setOverlayEnabled(Boolean(event.payload)))
        .then(unlisten => { visibilityUnlisten = unlisten; });
      void listen<BuffOverlaySettings>("buff-overlay-settings-changed", event => {
        settings.value = normalizeBuffOverlaySettings(event.payload);
        setOverlayEnabled(expectedLocalEnabled());
      }).then(unlisten => { settingsUnlisten = unlisten; });
      void listen<unknown>("buff-overlay-capture-notice", event => {
        showCaptureNotice(event.payload);
      }).then(unlisten => { captureNoticeUnlisten = unlisten; });
      void listen<boolean>("buff-overlay-layout-mode", event => {
        setLayoutMode(Boolean(event.payload));
      }).then(unlisten => { layoutModeUnlisten = unlisten; });
      layoutFlagUnlisten = onOverlayLayoutModeChange(enabled => setLayoutMode(enabled));
    }
    // Tauri windows receive explicit visibility/settings events. A hidden
    // on-demand WebView therefore needs no periodic JavaScript timer at all;
    // retain polling only for the browser preview where native events do not
    // exist.
    if (!isTauri()) enabledTimer = window.setInterval(syncLocalState, 500);
  });
  watch(
    // Separate scalar sources let Vue compare geometry/content, rather than a
    // new array identity on every 100 ms countdown tick.
    [
      () => overlayLayout.value.width,
      () => overlayLayout.value.height,
      () => buffRows.value.length,
      () => skillPanelRowCount.value,
      // Ready chips wrap: equal item counts can still have different heights.
      () => skillReadyRows.value.map(row => `${row.key}:${row.label}`).join('|'),
      () => debuffRows.value.length,
      () => Boolean(captureNotice.value),
      () => layoutMode.value,
    ],
    scheduleOverlayMeasurement,
    { immediate: true, flush: "post" },
  );
  watch(
    () => skillCountdownRows.value.length,
    () => { syncOverlayDomWork(); },
    { flush: 'post' },
  );
  onUnmounted(() => {
    componentMounted = false;
    skillUnlisten?.();
    document.documentElement.classList.remove("buff-overlay-mode"); document.body.classList.remove("buff-overlay-mode");
    stopTimers(); if (enabledTimer !== null) window.clearInterval(enabledTimer);
    if (captureNoticeTimer !== null) window.clearTimeout(captureNoticeTimer);
    if (snapshotRequestTimer !== null) window.clearTimeout(snapshotRequestTimer);
    snapshotRequestTimer = null;
    suspendOverlayDomWork();
    if (visibilityUnlisten) visibilityUnlisten();
    if (settingsUnlisten) settingsUnlisten();
    if (captureNoticeUnlisten) captureNoticeUnlisten();
    if (layoutModeUnlisten) layoutModeUnlisten();
    if (layoutFlagUnlisten) { layoutFlagUnlisten(); layoutFlagUnlisten = null; }
    snapshotUnlisten?.();
    snapshotUnlisten = null;
  });
  return {
    skillCountdownRows,
    skillReadyRows,
    skillPendingRows,
    payload, settings, captureNotice, layoutMode, overlayShell, windowLabel,
    isStatusView, isSkillView, layoutTitle, captureActive,
    rows, buffRows, debuffRows, statusRows, standardStatusRows,
    compactEffectRows, hiddenSelectedBuffCount, overlayLayout, statusListStyle, statusIconListStyle, shellStyle,
    rowStyle, rowWidth, remainingText, beginDrag,
    rowIconStyle, rowIconProgressStyle, rowIconLabel,
    finishLayoutMode, toggleDebuffBreakdown,
    showEmptySection, emptyStateText,
    skillIdentity, skillIdentityVisible,
  };
}
