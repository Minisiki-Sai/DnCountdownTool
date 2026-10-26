import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const liveCaptureSource = readFileSync(
  new URL("../src/composables/liveDps/useLiveCapture.ts", import.meta.url),
  "utf8",
);
const overlaySource = readFileSync(
  new URL("../src/composables/overlay/useBuffOverlay.ts", import.meta.url),
  "utf8",
);

test("start shows the overlays only after the backend accepted the capture", () => {
  const start = liveCaptureSource.slice(
    liveCaptureSource.indexOf("async function startCapture"),
    liveCaptureSource.indexOf("async function stopCapture"),
  );
  const backendIndex = start.indexOf("await simulatorApi.startCapture(0)");
  const showIndex = start.indexOf("await showStartedOverlay()");
  assert.ok(backendIndex >= 0 && showIndex > backendIndex,
    "overlay visibility must follow a successful start");
});

test("main-window delayed hide rechecks the current capture state", () => {
  const stopPath = liveCaptureSource.slice(
    liveCaptureSource.indexOf("function hideAfterStop"),
    liveCaptureSource.indexOf("async function startCapture"),
  );
  assert.match(stopPath, /if \(!isCaptureActiveState\(capture\.value\?\.state\)\) await applyOverlayVisibility\(false\)/);
  assert.match(stopPath, /stoppingNotice = null/);
});

test("overlay-webview delayed hide is capture-generation guarded", () => {
  const closeFunction = overlaySource.slice(
    overlaySource.indexOf("async function closeAfterCaptureStop"),
    overlaySource.indexOf("function applyPayload"),
  );
  assert.match(closeFunction, /const generation = \+\+captureLifecycleGeneration/);
  assert.match(closeFunction, /generation !== captureLifecycleGeneration/);
  assert.match(closeFunction, /\["starting", "running"\]\.includes/);
});

test("stopping sends its notice before the backend stop settles", () => {
  const stop = liveCaptureSource.slice(liveCaptureSource.indexOf("async function stopCapture"));
  assert.match(stop, /await hideAfterStop\(\)/);
});
