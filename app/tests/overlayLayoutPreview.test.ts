import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { DEFAULT_BUFF_OVERLAY_SETTINGS } from "../src/utils/buffOverlaySettings.ts";
import { buildOverlayLayoutPreview } from "../src/utils/overlayLayoutPreview.ts";

test("idle positioning has complete Buff and Debuff examples", () => {
  const preview = buildOverlayLayoutPreview(DEFAULT_BUFF_OVERLAY_SETTINGS, null);
  assert.equal(preview.buffs.length, 6);
  assert.equal(preview.debuffs.length, 5);
  assert.ok(preview.buffs.every(row => Number(row.end_seconds) > preview.elapsedSeconds));
});

test("explicit Buff selections are respected during positioning", () => {
  const settings = { ...DEFAULT_BUFF_OVERLAY_SETTINGS, selectedBuffs: ["custom-buff"] };
  const before = JSON.stringify(settings);
  const preview = buildOverlayLayoutPreview(settings, null);
  assert.deepEqual(preview.buffs.map(row => row.catalog_key), ["custom-buff"]);
  assert.equal(JSON.stringify(settings), before);
});

test("available real rows retain their exact shape without mutating capture state", () => {
  const real = buildOverlayLayoutPreview(DEFAULT_BUFF_OVERLAY_SETTINGS, null);
  real.state = "stopped";
  const before = JSON.stringify(real);
  const preview = buildOverlayLayoutPreview(DEFAULT_BUFF_OVERLAY_SETTINGS, real);
  assert.deepEqual(preview.buffs, real.buffs);
  assert.deepEqual(preview.debuffs, real.debuffs);
  assert.equal(JSON.stringify(real), before);
});

const source = (path: string) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

test("positioning uses gameplay geometry for both remaining windows", () => {
  const presentation = source("composables/overlay/useBuffOverlayPresentation.ts");
  assert.match(presentation, /if \(layoutMode\.value\) return payload\.value\?\.elapsedSeconds/);
  assert.match(presentation, /if \(isSkillView\.value\)/);
  const template = source("components/BuffOverlay.vue");
  assert.match(template, /v-show="layoutMode \|\| statusRows\.length \|\| skillCountdownRows\.length/);
  assert.match(template, /isSkillView && skillCountdownRows\.length/);
});

test("preview clock stops while positioning and completion restores visibility", () => {
  const runtime = source("composables/overlay/useBuffOverlay.ts");
  assert.match(runtime, /function startTimers\(\) \{\s*if \(layoutMode\.value\) \{ stopTimers\(\); return; \}/);
  assert.match(runtime, /function finishLayoutMode\(\) \{[\s\S]*?set_buff_overlay_visible/);
  assert.match(source("composables/overlay/useBuffOverlayPresentation.ts"), /includes\(livePayload\.value\?\.state/);
});

test("no teammate or Boss-health previews remain", () => {
  const preview = source("utils/overlayLayoutPreview.ts");
  assert.doesNotMatch(preview, /TEAMMATE_LAYOUT_PREVIEW|HISTORY_LAYOUT_PREVIEW/);
  const settings = source("utils/buffOverlaySettings.ts");
  assert.doesNotMatch(settings, /bossHealth:/);
  const labels = settings.match(/OVERLAY_WINDOW_LABELS = \{[\s\S]*?\}/)?.[0] || "";
  assert.doesNotMatch(labels, /teammate|boss/i);
});
