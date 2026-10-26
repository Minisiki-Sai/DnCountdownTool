import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_BUFF_OVERLAY_SETTINGS,
  OVERLAY_WINDOW_LABELS,
  enabledOverlayWindowLabels,
  normalizeBuffOverlaySettings,
} from "../src/utils/buffOverlaySettings.ts";
import {
  combatPayloadFingerprint,
  combatOverlaySnapshotNeeded,
  overlayCountdownIntervalMs,
  overlayDomWorkNeeded,
  overlayViewConfiguredForLiveUpdates,
  overlayRefreshPolicy,
} from "../src/utils/overlayRuntimePolicy.ts";
import type { BuffOverlayStatus } from "../src/types/api";

test("combat publication keeps receipt evidence and lag transitions, not diagnostic churn", () => {
  const snapshot = {
    elapsedSeconds: 10, overlayRevision: 1, buffs: [],
    buffObservations: [{ target_id: "a", catalog_key: "brave", end_seconds: 9, permanent: false }],
    latency: { thresholdExceeded: false, pendingRawFrames: 0 },
  } as unknown as BuffOverlayStatus;
  const original = combatPayloadFingerprint(snapshot);
  assert.equal(combatPayloadFingerprint({ ...snapshot, elapsedSeconds: 11,
    overlayRevision: 2, latency: { thresholdExceeded: false, pendingRawFrames: 10 } }), original);
  const delayed = combatPayloadFingerprint({ ...snapshot, latency: { thresholdExceeded: true } });
  assert.notEqual(delayed, original);
  assert.equal(combatPayloadFingerprint({ ...snapshot, latency: { thresholdExceeded: false } }), original);
  assert.notEqual(combatPayloadFingerprint({ ...snapshot, buffObservations: [] }), original);
});

test("only the Buff/Debuff and Boss skill windows remain configurable", () => {
  assert.deepEqual(Object.values(OVERLAY_WINDOW_LABELS).sort(),
    ["buff-overlay", "skill-countdown-overlay"]);
});

test("status and skill windows follow their own switches", () => {
  const onlySkill = {
    ...DEFAULT_BUFF_OVERLAY_SETTINGS,
    showBuffOverlay: false,
    showDebuffOverlay: false,
    showSkillCountdownOverlay: true,
  };
  assert.deepEqual(enabledOverlayWindowLabels(onlySkill), [OVERLAY_WINDOW_LABELS.skill]);
  assert.equal(overlayViewConfiguredForLiveUpdates("skill", onlySkill), true);
  assert.equal(overlayViewConfiguredForLiveUpdates("status", onlySkill), false);

  const onlyStatus = { ...onlySkill, showSkillCountdownOverlay: false, showBuffOverlay: true };
  assert.deepEqual(enabledOverlayWindowLabels(onlyStatus), [OVERLAY_WINDOW_LABELS.status]);
  assert.equal(overlayViewConfiguredForLiveUpdates("status", onlyStatus), true);
  assert.equal(overlayViewConfiguredForLiveUpdates("skill", onlyStatus), false);
});

test("ordinary overlays stop all DOM work when disabled", () => {
  assert.equal(overlayDomWorkNeeded("status", true, false, false, true), false);
  assert.equal(overlayDomWorkNeeded("status", true, true, false, true), true);
  assert.equal(overlayDomWorkNeeded("skill", true, true, true, false), true);
});

test("one combat publisher serves every enabled combat overlay", () => {
  const hidden = {
    ...DEFAULT_BUFF_OVERLAY_SETTINGS,
    showBuffOverlay: false,
    showDebuffOverlay: false,
    showSkillCountdownOverlay: false,
  };
  assert.equal(combatOverlaySnapshotNeeded(false, DEFAULT_BUFF_OVERLAY_SETTINGS), false);
  assert.equal(combatOverlaySnapshotNeeded(true, hidden), false);
  assert.equal(combatOverlaySnapshotNeeded(true, { ...hidden, showSkillCountdownOverlay: true }), true);
});

test("countdown clock stops when idle and only accelerates for visible precision", () => {
  assert.equal(overlayCountdownIntervalMs("subsecond", []), 0);
  assert.equal(overlayCountdownIntervalMs("subsecond", [18, 7]), 1_000);
  assert.equal(overlayCountdownIntervalMs("subsecond", [18, 1.2]), 200);
  assert.equal(overlayCountdownIntervalMs("decimal", [18]), 200);
  assert.equal(overlayCountdownIntervalMs("decimal", [0, -1]), 0);
});

test("removed performance flag is discarded when reading or importing old settings", () => {
  for (const flag of [undefined, false, true]) {
    const settings = normalizeBuffOverlaySettings({ overlayPerformanceMode: flag });
    assert.equal('overlayPerformanceMode' in settings, false);
  }
});

test("decoder backpressure throttles ordinary overlays but not critical alerts", () => {
  assert.deepEqual(overlayRefreshPolicy("running", {}, true), {
    pollIntervalMs: 500,
    regularPublishIntervalMs: 500,
    criticalPublishIntervalMs: 500,
    degraded: false,
    severe: false,
  });
  assert.deepEqual(
    overlayRefreshPolicy("running", { decoderQueueDelayMs: 800 }, true),
    {
      pollIntervalMs: 500,
      regularPublishIntervalMs: 750,
      criticalPublishIntervalMs: 500,
      degraded: true,
      severe: false,
    },
  );
  assert.equal(
    overlayRefreshPolicy("running", { pendingRawFrames: 10_000 }, false).pollIntervalMs,
    1_000,
  );
  assert.equal(overlayRefreshPolicy("idle", {}, true).pollIntervalMs, 1_500);
});

test("removed teammate and score-privacy settings never survive normalization", () => {
  const normalized = normalizeBuffOverlaySettings({
    showTeammateInfoOverlay: true,
    hidePersonalScoreHistoryOverlay: true,
    hidePersonalScoreHistorySoftware: true,
    showBossHealthOverlay: true,
    teammateInfoBackgroundOpacity: 12,
  });
  const record = normalized as unknown as Record<string, unknown>;
  for (const key of [
    "showTeammateInfoOverlay", "hidePersonalScoreHistoryOverlay",
    "hidePersonalScoreHistorySoftware", "showBossHealthOverlay",
    "teammateInfoBackgroundOpacity", "teammateHistoryBackgroundOpacity",
  ]) assert.equal(key in record, false, `${key} must not be recreated`);
});

test("debuff skill breakdown defaults to totals and preserves explicit modes", () => {
  assert.equal(normalizeBuffOverlaySettings({}).debuffBreakdownMode, "summary");
  assert.equal(
    normalizeBuffOverlaySettings({ debuffBreakdownMode: "skills" }).debuffBreakdownMode,
    "skills",
  );
  assert.equal(
    normalizeBuffOverlaySettings({ debuffBreakdownMode: "summary_and_skills" }).debuffBreakdownMode,
    "summary_and_skills",
  );
  assert.equal(
    normalizeBuffOverlaySettings({ debuffBreakdownMode: "invalid" as never }).debuffBreakdownMode,
    "summary",
  );
});

test("status layout defaults to rows and preserves the compact icon mode", () => {
  assert.equal(normalizeBuffOverlaySettings({}).buffLayoutMode, "rows");
  assert.equal(
    normalizeBuffOverlaySettings({ buffLayoutMode: "icons" }).buffLayoutMode,
    "icons",
  );
  assert.equal(normalizeBuffOverlaySettings({}).statusLayoutMode, "rows");
  assert.equal(
    normalizeBuffOverlaySettings({ statusLayoutMode: "icons" }).statusLayoutMode,
    "icons",
  );
  assert.equal(
    normalizeBuffOverlaySettings({ statusLayoutMode: "invalid" as never }).statusLayoutMode,
    "rows",
  );
});

test("status icon appearance defaults, validates colors, and clamps sizes", () => {
  const defaults = normalizeBuffOverlaySettings({});
  assert.equal(defaults.statusIconSize, 42);
  assert.equal(defaults.statusIconBackgroundColor, "#eff8f9");
  assert.equal(defaults.statusIconNameColor, "#17323a");
  assert.equal(defaults.statusIconTimeColor, "#315963");
  assert.deepEqual(defaults.statusIconBackgroundColors, {});
  assert.deepEqual(defaults.statusIconNameColors, {});
  assert.deepEqual(defaults.statusIconTimeColors, {});

  const adjusted = normalizeBuffOverlaySettings({
    statusIconSize: 99,
    statusIconBackgroundOpacity: -4,
    statusIconNameFontSize: 5,
    statusIconTimeFontSize: 30,
    statusIconBackgroundColor: "#AABBCC",
    statusIconNameColor: "invalid",
    statusIconTimeColor: "#010203",
    statusIconBackgroundColors: { brave: "#112233", broken: "invalid" },
    statusIconNameColors: { brave: "#445566" },
    statusIconTimeColors: { brave: "#778899" },
  });
  assert.equal(adjusted.statusIconSize, 64);
  assert.equal(adjusted.statusIconBackgroundOpacity, 0);
  assert.equal(adjusted.statusIconNameFontSize, 8);
  assert.equal(adjusted.statusIconTimeFontSize, 18);
  assert.equal(adjusted.statusIconBackgroundColor, "#aabbcc");
  assert.equal(adjusted.statusIconNameColor, "#17323a");
  assert.equal(adjusted.statusIconTimeColor, "#010203");
  assert.deepEqual(adjusted.statusIconBackgroundColors, { brave: "#112233" });
  assert.deepEqual(adjusted.statusIconNameColors, { brave: "#445566" });
  assert.deepEqual(adjusted.statusIconTimeColors, { brave: "#778899" });
});

test("legacy overlay settings migrate once to the adjustable 50% default", () => {
  const settings = normalizeBuffOverlaySettings({ backgroundOpacity: 68 });
  assert.equal(settings.overlaySurfaceVersion, 2);
  assert.equal(settings.backgroundOpacity, 50);
});

test("versioned overlay settings preserve later visibility adjustments", () => {
  const settings = normalizeBuffOverlaySettings({
    overlaySurfaceVersion: 2,
    backgroundOpacity: 35,
  });
  assert.equal(settings.backgroundOpacity, 35);
});

test("removed alert/curve/summon settings never survive normalization", () => {
  const settings = normalizeBuffOverlaySettings({
    showEffectCurveOverlay: true,
    showPersonalDpsOverlay: true,
    showCooldownOverlay: true,
    showSummonOverlay: true,
    showMissingBuffAlert: true,
    showClearedBuffAlert: true,
    showLowHealthAlert: true,
    selectedSummons: ["spirit-gate"],
    selectedCooldownBuffs: ["brave"],
    selectedMissingBuffs: ["无情打击"],
  } as never);
  const raw = settings as unknown as Record<string, unknown>;
  for (const key of ["showEffectCurveOverlay", "showPersonalDpsOverlay", "showCooldownOverlay",
    "showSummonOverlay", "showMissingBuffAlert", "showClearedBuffAlert", "showLowHealthAlert",
    "selectedSummons", "selectedCooldownBuffs", "selectedMissingBuffs"]) {
    assert.equal(key in raw, false, key);
  }
});
