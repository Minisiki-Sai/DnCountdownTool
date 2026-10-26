import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const configurationSource = readFileSync(
  new URL("../src/utils/globalHotkeys.ts", import.meta.url),
  "utf8",
);
const nativeSource = readFileSync(
  new URL("../src-tauri/src/lib.rs", import.meta.url),
  "utf8",
);

test("one-click disconnect is opt-in for new and upgraded installations", () => {
  assert.match(configurationSource, /disconnectGameEnabled:\s*localStorage\.getItem\([^)]+disconnectGameEnabled\)\s*===\s*"1"/);
  assert.doesNotMatch(configurationSource, /disconnectGameEnabled:\s*true/);
});

test("disabled one-click disconnect is not registered or exposed as a manual command", () => {
  assert.match(nativeSource, /disconnect_game_enabled:\s*bool/);
  assert.match(nativeSource, /let disconnect_shortcut = if disconnect_game_enabled/);
  assert.match(nativeSource, /bindings\.disconnect_game\s*=\s*disconnect_shortcut/);
  assert.match(nativeSource, /一键掉线尚未开启/);
});
