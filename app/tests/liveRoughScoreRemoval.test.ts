import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const liveView = readFileSync(new URL("../src/views/LiveDpsView.vue", import.meta.url), "utf8");
const overlaySettings = readFileSync(
  new URL("../src/utils/buffOverlaySettings.ts", import.meta.url),
  "utf8",
);
const tauriConfig = readFileSync(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8");
const nativeWindows = readFileSync(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");

test("the live page carries no scoring controls or replay workspace", () => {
  assert.doesNotMatch(liveView, /实时模糊评分|liveRoughScore/);
  assert.doesNotMatch(liveView, /scoreLoading|score-workspace|workspace-tabs live-tabs/);
  assert.doesNotMatch(liveView, /尚未载入实战记录/);
});

test("removed live-score and teammate windows cannot be recreated", () => {
  assert.doesNotMatch(tauriConfig, /live-score-overlay|view=live-score/);
  assert.doesNotMatch(nativeWindows, /live-score-overlay|view:\s*"live-score"/);
  assert.doesNotMatch(overlaySettings, /OVERLAY_WINDOW_LABELS\.(score|teammate|bossHealth)/);
  // Removed keys stay in the one-way deletion list so stale saves cannot revive them.
  assert.match(overlaySettings, /"showLiveScoreOverlay"/);
  assert.match(overlaySettings, /"showTeammateInfoOverlay"/);
});

test("the window set is exactly main plus the two countdown overlays", () => {
  const labels = JSON.parse(tauriConfig).app.windows.map((row: { label: string }) => row.label);
  assert.deepEqual(labels, ["main", "buff-overlay", "skill-countdown-overlay"]);
  assert.doesNotMatch(nativeWindows, /teammate-info-overlay|teammate-history-overlay|game-timer-overlay|boss-health-overlay/);
});
