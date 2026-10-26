import type { BuffOverlaySettings, BuffOverlayStatus } from "../types/api";
import type { OverlayWindowKind } from "./buffOverlaySettings";
import { captureToggleAllows, type CaptureToggleState } from "./captureToggles.ts";

export type OverlayLatencySample = {
  captureToDecodeMs?: number;
  decoderQueueDelayMs?: number;
  pendingRawFrames?: number;
  pendingDecodedEvents?: number;
  thresholdExceeded?: boolean;
};

export type OverlayRefreshPolicy = {
  pollIntervalMs: number;
  regularPublishIntervalMs: number;
  criticalPublishIntervalMs: number;
  degraded: boolean;
  severe: boolean;
};

const VOLATILE_COMBAT_KEYS = new Set([
  "elapsedSeconds", "overlayRevision", "remaining_seconds",
]);

export function combatPayloadFingerprint(status: BuffOverlayStatus): string {
  // Ignore countdowns and changing diagnostic counters, but retain the lag
  // transition: missing-Buff alerts must suppress/recover without a new Buff.
  return JSON.stringify(status, (key, value) => {
    if (key === "latency") return { thresholdExceeded: value?.thresholdExceeded === true };
    return VOLATILE_COMBAT_KEYS.has(key) ? undefined : value;
  });
}

export function overlayViewConfiguredForLiveUpdates(
  view: OverlayWindowKind,
  settings: BuffOverlaySettings,
): boolean {
  if (view === "skill") return settings.showSkillCountdownOverlay;
  return settings.showBuffOverlay || settings.showDebuffOverlay;
}

/**
 * Pick the cheapest local countdown cadence that preserves the selected text
 * precision. Backend snapshots remain event-driven; a hidden/no-countdown
 * WebView needs no animation clock at all.
 */
export function overlayCountdownIntervalMs(
  timeFormat: BuffOverlaySettings["timeFormat"],
  remainingSeconds: readonly number[],
): number {
  const active = remainingSeconds.filter(value => Number.isFinite(value) && value > 0);
  if (!active.length) return 0;
  if (timeFormat === "decimal" || Math.min(...active) <= 1.25) return 200;
  return 1_000;
}

export function overlayDomWorkNeeded(
  view: OverlayWindowKind,
  mounted: boolean,
  enabled: boolean,
  layoutMode: boolean,
  eventHasVisibleContent: boolean,
): boolean {
  if (!mounted || !enabled) return false;
  if (layoutMode) return true;
  // Every remaining overlay is persistent, so DOM measurement always runs
  // while the window is enabled.
  void view;
  void eventHasVisibleContent;
  return true;
}

export function combatOverlaySnapshotNeeded(
  globallyVisible: boolean,
  settings: BuffOverlaySettings,
  toggles?: CaptureToggleState,
): boolean {
  if (!globallyVisible) return false;
  // 首页总开关关掉的那一类不用再拉快照：窗口已经隐藏，省一次 150~500ms 的轮询。
  // 语音播报不依赖这份快照（它自己读 /api/capture/voice-events）。
  const statusNeeded = captureToggleAllows("buffs", toggles)
    && (settings.showBuffOverlay || settings.showDebuffOverlay);
  const skillNeeded = captureToggleAllows("boss_actions", toggles)
    && settings.showSkillCountdownOverlay;
  return statusNeeded || skillNeeded;
}

function finite(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

/**
 * Keep safety alerts responsive while letting the decoder catch up.
 *
 * Polling is 500 ms during capture and 1.5 s while idle. When raw/decode
 * backlog grows, only ordinary status/curve/CD/summon publications are
 * throttled; Buff-clear and low-health consumers retain the 500 ms path.
 */
export function overlayRefreshPolicy(
  state: string,
  latency: OverlayLatencySample | null | undefined,
  hasCriticalSubscribers = false,
): OverlayRefreshPolicy {
  const running = state === "starting" || state === "running" || state === "stopping";
  const queueDelay = Math.max(
    finite(latency?.captureToDecodeMs),
    finite(latency?.decoderQueueDelayMs),
  );
  const rawFrames = finite(latency?.pendingRawFrames);
  const decodedEvents = finite(latency?.pendingDecodedEvents);
  const severe = queueDelay >= 2_000 || rawFrames >= 8_192 || decodedEvents >= 16_384;
  const degraded = severe
    || queueDelay >= 500
    || rawFrames >= 2_048
    || decodedEvents >= 4_096
    || latency?.thresholdExceeded === true;
  const regularPublishIntervalMs = severe ? 1_000 : degraded ? 750 : running ? 500 : 1_500;
  return {
    // Critical listeners require the shared compact snapshot at 500 ms. Keep
    // ordinary overlays at no worse than 1 s under backpressure so a short
    // Buff lifecycle never arrives several seconds late.
    pollIntervalMs: running
      ? (hasCriticalSubscribers ? 500 : regularPublishIntervalMs)
      : 1_500,
    regularPublishIntervalMs,
    criticalPublishIntervalMs: running ? 500 : 1_500,
    degraded,
    severe,
  };
}
