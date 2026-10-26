import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { computed, nextTick, ref, watch } from "vue";

// Execute the actual composable's functions/watch sources. No native window or
// copied scheduler implementation is needed to reproduce the timing race.
const source = readFileSync(new URL("../src/composables/overlay/useBuffOverlay.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const ast = ts.createSourceFile("useBuffOverlay.js", js, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS);
const functions = new Map<string, string>();
let geometrySources = "";
function visit(node: ts.Node) {
  if (ts.isFunctionDeclaration(node) && node.name) functions.set(node.name.text, node.getText(ast));
  if (ts.isCallExpression(node) && node.expression.getText(ast) === "watch"
    && node.arguments[1]?.getText(ast) === "scheduleOverlayMeasurement") {
    geometrySources = node.arguments[0].getText(ast);
  }
  ts.forEachChild(node, visit);
}
visit(ast);

type Size = { width: number; height: number };
async function settle() {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}
function createScheduler(initial: Size = { width: 350, height: 100 }, deferred = false) {
  let now = 0;
  let timerId = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const calls: Array<Size & { at: number }> = [];
  const requests: Array<{ resolve: () => void; reject: (error: Error) => void }> = [];
  let measurements = 0;
  const context = vm.createContext({
    overlayDomWorkActive: true,
    overlayResizeInFlight: false,
    overlayResizeRequest: null,
    overlayResizeRetryTimer: null,
    overlayNativeResizeTimer: null,
    pendingOverlaySize: null,
    appliedOverlaySize: { ...initial },
    overlayMeasureFrame: null,
    overlayMeasureGeneration: 0,
    overlayResizeObserver: null,
    lastSlowNativeResizeReportAt: Number.NEGATIVE_INFINITY,
    OVERLAY_NATIVE_RESIZE_DEBOUNCE_MS: 100,
    isTauri: () => true,
    windowLabel: { value: "buff-status-overlay" },
    enabled: true,
    layoutMode: { value: false },
    performance: { now: () => now },
    window: {
      setTimeout(fn: () => void, delay: number) {
        const id = ++timerId;
        timers.set(id, { at: now + delay, fn });
        return id;
      },
      clearTimeout(id: number) { timers.delete(id); },
      cancelAnimationFrame() {},
      removeEventListener() {},
    },
    document: { fonts: { removeEventListener() {} } },
    handleOverlayViewportChange() {},
    scheduleOverlayMeasurement() { measurements += 1; },
    invoke(command: string, args: Size) {
      if (command !== "set_buff_overlay_size") return Promise.resolve();
      calls.push({ at: now, width: args.width, height: args.height });
      if (!deferred) return Promise.resolve();
      return new Promise<void>((resolve, reject) => requests.push({ resolve, reject }));
    },
  });
  for (const name of ["sizesAreEquivalent", "scheduleNativeOverlayResize", "flushNativeOverlayResize", "resizeOverlay", "suspendOverlayDomWork"]) {
    assert.ok(functions.has(name), `missing production function ${name}`);
    vm.runInContext(functions.get(name)!, context);
  }
  return {
    calls, requests, context,
    resize(width: number, height: number) { context.resizeOverlay(width, height); },
    flush(): Promise<void> { return context.flushNativeOverlayResize(); },
    suspend() { context.suspendOverlayDomWork(); },
    get measurements() { return measurements; },
    get timerCount() { return timers.size; },
    async advance(target: number) {
      await settle();
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= target)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].fn();
        await settle();
      }
      now = target;
      await settle();
    },
  };
}

test("growth is dispatched immediately despite three seconds of repeated measurements", async () => {
  const scheduler = createScheduler();
  for (let at = 0; at <= 3_000; at += 90) {
    await scheduler.advance(at);
    scheduler.resize(350, 190);
  }
  await scheduler.advance(3_200);
  assert.deepEqual(scheduler.calls, [{ at: 0, width: 350, height: 190 }]);
  assert.equal(scheduler.timerCount, 0);
});

test("width growth is also immediate even when height shrinks", () => {
  const scheduler = createScheduler();
  scheduler.resize(450, 70);
  assert.deepEqual(scheduler.calls, [{ at: 0, width: 450, height: 70 }]);
});

test("identical pending shrink never restarts its 100 ms deadline", async () => {
  const scheduler = createScheduler({ width: 350, height: 200 });
  for (let at = 0; at <= 3_000; at += 90) {
    await scheduler.advance(at);
    scheduler.resize(350, 100);
  }
  assert.deepEqual(scheduler.calls, [{ at: 100, width: 350, height: 100 }]);
});

test("changing shrink targets uses the newest target at the bounded deadline", async () => {
  const scheduler = createScheduler({ width: 350, height: 200 });
  scheduler.resize(350, 180);
  await scheduler.advance(90);
  scheduler.resize(350, 130);
  assert.equal(scheduler.calls.length, 0);
  await scheduler.advance(100);
  assert.deepEqual(scheduler.calls, [{ at: 100, width: 350, height: 130 }]);
});

test("returning to the applied size cancels a stale shrink", async () => {
  const scheduler = createScheduler({ width: 350, height: 200 });
  scheduler.resize(350, 100);
  await scheduler.advance(90);
  scheduler.resize(350, 200);
  await scheduler.advance(300);
  assert.equal(scheduler.calls.length, 0);
  assert.equal(scheduler.context.pendingOverlaySize, null);
});

test("new growth cancels a delayed shrink", async () => {
  const scheduler = createScheduler({ width: 350, height: 200 });
  scheduler.resize(350, 100);
  await scheduler.advance(90);
  scheduler.resize(350, 250);
  await scheduler.advance(300);
  assert.deepEqual(scheduler.calls, [{ at: 90, width: 350, height: 250 }]);
});

test("native calls stay serial and queued growth starts as soon as the first completes", async () => {
  const scheduler = createScheduler(undefined, true);
  scheduler.resize(350, 200);
  await scheduler.advance(50);
  scheduler.resize(350, 220);
  scheduler.resize(350, 260);
  assert.equal(scheduler.calls.length, 1);
  scheduler.requests[0].resolve();
  await settle();
  assert.deepEqual(scheduler.calls, [
    { at: 0, width: 350, height: 200 },
    { at: 50, width: 350, height: 260 },
  ]);
  scheduler.requests[1].resolve();
  await settle();
  assert.equal(scheduler.context.overlayResizeInFlight, false);
});

test("fit-before-show awaits both an in-flight resize and the newest geometry", async () => {
  const scheduler = createScheduler(undefined, true);
  scheduler.resize(350, 200);
  let finished = false;
  const fitting = scheduler.flush().then(() => { finished = true; });
  scheduler.resize(350, 240);
  await settle();
  assert.equal(finished, false);
  scheduler.requests[0].resolve();
  await settle();
  assert.equal(finished, false);
  assert.equal(scheduler.calls.length, 2);
  scheduler.requests[1].resolve();
  await fitting;
  assert.equal(finished, true);
  assert.equal(scheduler.context.appliedOverlaySize.height, 240);
});

test("duplicate measurements during an in-flight resize do not invoke twice", async () => {
  const scheduler = createScheduler(undefined, true);
  scheduler.resize(350, 200);
  for (let i = 0; i < 30; i += 1) scheduler.resize(350, 200);
  scheduler.requests[0].resolve();
  await settle();
  assert.equal(scheduler.calls.length, 1);
  assert.equal(scheduler.timerCount, 0);
  assert.equal(scheduler.context.pendingOverlaySize, null);
});

test("failed native calls retain retry backoff instead of spinning on immediate growth", async () => {
  const scheduler = createScheduler(undefined, true);
  scheduler.resize(350, 200);
  scheduler.resize(350, 240);
  scheduler.requests[0].reject(new Error("window still mounting"));
  await settle();
  await scheduler.advance(150);
  scheduler.resize(350, 240);
  assert.equal(scheduler.calls.length, 1);
  await scheduler.advance(160);
  assert.equal(scheduler.measurements, 1);
  scheduler.resize(350, 240);
  assert.equal(scheduler.calls.length, 2);
  scheduler.requests[1].resolve();
  await settle();
});

test("suspending cancels delayed resize and ignores later measurements", async () => {
  const scheduler = createScheduler({ width: 350, height: 200 });
  scheduler.resize(350, 100);
  scheduler.suspend();
  scheduler.resize(350, 300);
  await scheduler.advance(300);
  assert.equal(scheduler.calls.length, 0);
  assert.equal(scheduler.timerCount, 0);
});

test("hiding during a native request neither resizes again nor starts retries", async () => {
  const scheduler = createScheduler(undefined, true);
  scheduler.resize(350, 200);
  scheduler.resize(350, 240);
  const fitting = scheduler.flush();
  scheduler.suspend();
  scheduler.requests[0].reject(new Error("window hidden"));
  await fitting;
  assert.equal(scheduler.calls.length, 1);
  assert.equal(scheduler.timerCount, 0);
});

test("actual Vue geometry watcher ignores countdown ticks but responds to layout changes", async () => {
  assert.ok(geometrySources);
  const clock = ref(0);
  const count = ref(1);
  const width = ref(350);
  const buffRows = computed(() => Array.from({ length: count.value }, () => ({ remaining: 60 - clock.value / 1_000 })));
  const context = {
    overlayLayout: computed(() => ({ width: width.value, height: 46 + buffRows.value.length * 36 })),
    buffRows, debuffRows: ref([]),
    // The geometry watcher also tracks the skill-countdown panel. It is a
    // composable-internal computed, so the isolated context supplies static
    // stand-ins: countdown ticks must not trigger a measurement either way.
    skillPanelRowCount: ref(0),
    skillReadyRows: ref<Array<{ key: string; label: string }>>([]),
    captureNotice: ref<null | { message: string }>(null),
    layoutMode: ref(false),
  };
  let measurements = 0;
  const stop = watch(vm.runInNewContext(geometrySources, context), () => { measurements += 1; }, { flush: "post" });
  try {
    for (let i = 1; i <= 30; i += 1) {
      clock.value = i * 100;
      await nextTick();
    }
    assert.equal(measurements, 0, "countdown text alone must not request another measurement");
    count.value = 2;
    await nextTick();
    assert.equal(measurements, 1);
    width.value = 450;
    await nextTick();
    assert.equal(measurements, 2);
    context.captureNotice.value = { message: "开始采集" };
    await nextTick();
    assert.equal(measurements, 3);
    context.layoutMode.value = true;
    await nextTick();
    assert.equal(measurements, 4);
  } finally {
    stop();
  }
});
