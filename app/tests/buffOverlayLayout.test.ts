import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/BuffOverlay.vue", import.meta.url),
  "utf8",
);
const presentation = readFileSync(
  new URL("../src/composables/overlay/useBuffOverlayPresentation.ts", import.meta.url),
  "utf8",
);

test("expand arrow stays at the left edge and does not inherit timer width", () => {
  assert.match(source, /class="buff-remaining"/);
  assert.match(source, /\.buff-copy \.buff-remaining\s*\{/);
  assert.doesNotMatch(source, /\.buff-copy span\s*\{/);
  assert.match(source, /\.effect-summary-row\s*\{\s*padding-left:\s*6px;/);
  assert.match(source, /\.effect-expand-icon\s*\{[^}]*flex:\s*0 0 12px;/s);
});

test("Buff rows keep the frosted capsule surface and the light-blue tint", () => {
  assert.match(source, /--ov-surface:\s*rgba\(226, 238, 252/);
  assert.match(source, /\.buff-overlay-shell\.is-skill\s*\{[\s\S]*?--ov-surface:\s*rgba\(252, 238, 225/);
  assert.match(source, /border-radius:\s*20px/);
});

test("status geometry never grows for skill countdowns, which own a separate window", () => {
  assert.match(presentation, /const overlayLayout = computed\(\(\) => baseOverlayLayout\.value\);/);
  assert.match(presentation, /if \(isSkillView\.value\)[\s\S]*?skillCountdownGeometry/);
  assert.match(presentation, /const standardPanel = standardStatusGridShape/);
});

test("native sizing accepts the configured width at half zoom", () => {
  const native = readFileSync(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");
  const minimum = Number(native.match(/let width = width\.clamp\(([\d.]+), 1280\.0\)/)?.[1]);
  assert.ok(minimum > 0 && minimum <= 260 * 0.5);
});
