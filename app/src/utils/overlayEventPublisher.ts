import { simulatorApi } from "../services/api";
import type { BuffOverlayStatus } from "../types/api";
import {
  BUFF_OVERLAY_CATALOG_STORAGE_KEY,
  OVERLAY_WINDOW_LABELS,
  enabledOverlayWindowLabels,
  readBuffOverlaySettings,
} from "./buffOverlaySettings";
import {
  publishOverlayEvent,
  subscribeOverlaySnapshotRequests,
  type OverlayEventUnlisten,
} from "./overlayEventBus";
import { OVERLAY_EVENT_TOPICS } from "./overlayEventProtocol";
import {
  combatPayloadFingerprint,
  combatOverlaySnapshotNeeded,
  overlayRefreshPolicy,
} from "./overlayRuntimePolicy";

export type OverlayEventPublisherDiagnostics = {
  combatRequests: number;
  combatCoalescedTicks: number;
  combatPublishCount: number;
  combatDuplicateSkips: number;
  combatDegradedTicks: number;
  lastCombatRequestMs: number;
  lastError: string;
};

export type OverlayEventPublisher = {
  start: () => void;
  stop: () => void;
  diagnostics: () => Readonly<OverlayEventPublisherDiagnostics>;
};

// Buff/Debuff lifecycle changes are rare but immediately visible; the status
// window is the only consumer that needs the immediate push.
const BUFF_LIFECYCLE_OVERLAY_LABELS = new Set<string>([OVERLAY_WINDOW_LABELS.status]);

function withoutStaticCatalog(status: BuffOverlayStatus): BuffOverlayStatus {
  const dynamic: Partial<BuffOverlayStatus> = { ...status };
  delete dynamic.buffOptions;
  delete dynamic.summonOptions;
  return dynamic as BuffOverlayStatus;
}

function targetSignature(labels: readonly string[]): string {
  return [...labels].sort().join("|");
}

export function createOverlayEventPublisher(): OverlayEventPublisher {
  let running = false;
  let combatTimer: number | null = null;
  let combatInFlight = false;
  let combatPending = false;
  let combatImmediatePending = false;
  let buffLifecycleWatchGeneration = 0;
  let seenBuffStateRevision = 0;
  let snapshotRequestUnlisten: OverlayEventUnlisten | null = null;
  let latestCombatPayload: BuffOverlayStatus | null = null;
  let catalogLoaded = false;
  let lastCombatState = "idle";
  let lastCombatLatency: BuffOverlayStatus["latency"] = {};
  let lastFingerprint = "";
  let lastTargets = "";
  let lastPublishAt = Number.NEGATIVE_INFINITY;
  const counters: OverlayEventPublisherDiagnostics = {
    combatRequests: 0,
    combatCoalescedTicks: 0,
    combatPublishCount: 0,
    combatDuplicateSkips: 0,
    combatDegradedTicks: 0,
    lastCombatRequestMs: 0,
    lastError: "",
  };

  function combatTargets(): string[] {
    const globallyVisible = localStorage.getItem("dragon-nest-v420-buff-overlay-enabled") === "1";
    const settings = readBuffOverlaySettings();
    if (!combatOverlaySnapshotNeeded(globallyVisible, settings)) return [];
    return enabledOverlayWindowLabels(settings);
  }

  function nextCombatDelay(): number {
    return overlayRefreshPolicy(lastCombatState, lastCombatLatency, false).pollIntervalMs;
  }

  function scheduleCombatPoll(delay = nextCombatDelay()) {
    if (!running) return;
    if (combatTimer !== null) window.clearTimeout(combatTimer);
    combatTimer = window.setTimeout(() => {
      combatTimer = null;
      void pollCombat();
    }, Math.max(100, delay));
  }

  async function publish(targets: string[], payload: BuffOverlayStatus) {
    if (!targets.length) return;
    await publishOverlayEvent(OVERLAY_EVENT_TOPICS.combatSnapshot, payload, targets);
    counters.combatPublishCount += 1;
  }

  async function pollCombat(immediateBuffLifecycle = false) {
    if (!running) return;
    if (immediateBuffLifecycle) combatImmediatePending = true;
    const targets = combatTargets();
    if (!targets.length) {
      scheduleCombatPoll(1_500);
      return;
    }
    if (combatInFlight) {
      combatPending = true;
      counters.combatCoalescedTicks += 1;
      return;
    }
    combatInFlight = true;
    try {
      do {
        combatPending = false;
        const forceBuffLifecycle = combatImmediatePending;
        combatImmediatePending = false;
        const startedAt = performance.now();
        try {
          counters.combatRequests += 1;
          const status = await simulatorApi.buffOverlayStatus(!catalogLoaded);
          counters.lastCombatRequestMs = performance.now() - startedAt;
          if (!running) return;
          lastCombatState = String(status.state || "idle");
          lastCombatLatency = status.latency || {};

          const hasCatalog = Array.isArray(status.buffOptions)
            && Array.isArray(status.summonOptions);
          if (hasCatalog) {
            localStorage.setItem(BUFF_OVERLAY_CATALOG_STORAGE_KEY, JSON.stringify({
              buffOptions: status.buffOptions,
              summonOptions: status.summonOptions,
            }));
            catalogLoaded = true;
          }

          const currentTargets = combatTargets();
          const dynamicStatus = withoutStaticCatalog(status);
          latestCombatPayload = dynamicStatus;
          const fingerprint = combatPayloadFingerprint(dynamicStatus);
          const now = performance.now();
          const policy = overlayRefreshPolicy(
            lastCombatState,
            lastCombatLatency,
            false,
          );
          if (policy.degraded) counters.combatDegradedTicks += 1;

          const signature = targetSignature(currentTargets);
          const changed = fingerprint !== lastFingerprint || signature !== lastTargets;
          const due = changed && now - lastPublishAt >= policy.regularPublishIntervalMs;
          const firstCatalogPayload = hasCatalog && !lastFingerprint;
          const payload = firstCatalogPayload ? status : dynamicStatus;

          const immediateTargets = forceBuffLifecycle
            ? currentTargets.filter(label => BUFF_LIFECYCLE_OVERLAY_LABELS.has(label))
            : [];
          if (immediateTargets.length) await publish(immediateTargets, payload);

          const immediateSet = new Set(immediateTargets);
          const deliveryTargets = due
            ? currentTargets.filter(label => !immediateSet.has(label))
            : [];
          if (deliveryTargets.length) await publish(deliveryTargets, payload);

          if (due) {
            lastFingerprint = fingerprint;
            lastTargets = signature;
            lastPublishAt = now;
          } else if (!immediateTargets.length) {
            counters.combatDuplicateSkips += 1;
          }
          counters.lastError = "";
        } catch (reason) {
          counters.lastError = reason instanceof Error ? reason.message : String(reason);
        }
      } while (running && combatPending);
    } finally {
      combatInFlight = false;
      scheduleCombatPoll(Math.max(100, nextCombatDelay() - counters.lastCombatRequestMs));
    }
  }

  async function watchBuffLifecycle(generation: number) {
    while (running && generation === buffLifecycleWatchGeneration) {
      try {
        const signal = await simulatorApi.waitBuffOverlayChange(seenBuffStateRevision, 20_000);
        if (!running || generation !== buffLifecycleWatchGeneration) return;
        const revision = Math.max(0, Number(signal.buffStateRevision || 0));
        if (signal.changed || revision !== seenBuffStateRevision) {
          seenBuffStateRevision = revision;
          await pollCombat(true);
        }
      } catch (reason) {
        if (!running || generation !== buffLifecycleWatchGeneration) return;
        counters.lastError = reason instanceof Error ? reason.message : String(reason);
        await new Promise(resolve => window.setTimeout(resolve, 2_000));
      }
    }
  }

  function start() {
    if (running) return;
    running = true;
    buffLifecycleWatchGeneration += 1;
    const lifecycleGeneration = buffLifecycleWatchGeneration;
    void subscribeOverlaySnapshotRequests(request => {
      if (!running || request.topic !== OVERLAY_EVENT_TOPICS.combatSnapshot) return;
      const allowed = new Set(combatTargets());
      if (!allowed.has(request.targetLabel)) return;
      if (!latestCombatPayload) {
        scheduleCombatPoll(0);
        return;
      }
      void publishOverlayEvent(
        OVERLAY_EVENT_TOPICS.combatSnapshot,
        latestCombatPayload,
        [request.targetLabel],
      ).then(() => { counters.combatPublishCount += 1; })
        .catch(reason => {
          counters.lastError = reason instanceof Error ? reason.message : String(reason);
        });
    }).then(unlisten => {
      if (!running) unlisten();
      else snapshotRequestUnlisten = unlisten;
    });
    void pollCombat();
    void watchBuffLifecycle(lifecycleGeneration);
  }

  function stop() {
    running = false;
    combatPending = false;
    combatImmediatePending = false;
    buffLifecycleWatchGeneration += 1;
    if (combatTimer !== null) window.clearTimeout(combatTimer);
    combatTimer = null;
    snapshotRequestUnlisten?.();
    snapshotRequestUnlisten = null;
  }

  return {
    start,
    stop,
    diagnostics: () => ({ ...counters }),
  };
}
