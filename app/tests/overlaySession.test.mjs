import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { effectScope, ref } from 'vue';

const values = new Map();
globalThis.localStorage = {
  getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};
const native = [];
globalThis.window = {
  __TAURI_INTERNALS__: { invoke: async (cmd, args) => { native.push({ cmd, args }); } },
};
const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
  server: { middlewareMode: true, watch: null }, optimizeDeps: { noDiscovery: true, include: [] },
});
after(() => server.close());
const session = await server.ssrLoadModule('/src/utils/overlaySession.ts');
const { simulatorApi } = await server.ssrLoadModule('/src/services/api.ts');
const { useLiveCapture } = await server.ssrLoadModule('/src/composables/liveDps/useLiveCapture.ts');
const key = session.OVERLAY_VISIBLE_KEY;
const settingsKey = 'dragon-nest-v420-buff-overlay-settings';

function setup() {
  session.beginOverlaySession(localStorage, false);
  native.length = 0;
  let starts = 0;
  simulatorApi.startCapture = async () => { starts++; return { state: 'running' }; };
  simulatorApi.captureStatus = async () => ({ state: 'idle' });
  const context = {
    snapshot: ref(null), tab: ref('damage'), selectedPlayer: ref(0), loading: ref(false),
    progress: ref(0), progressLabel: ref(''), error: ref(''), capture: ref(null),
    displaySource: ref(null), liveTimelineEvents: ref([]), players: ref([]), totalDps: ref(0),
    captureRunning: ref(false), captureStopping: ref(false), captureBusy: ref(false),
    captureScoreReady: ref(false), score: ref(null), scoreDetail: ref(null),
    selectedScoreSource: ref(''), selectedScoreBoss: ref(''), calculateScore: async () => {},
    isCaptureActiveState: state => ['starting', 'running'].includes(state),
  };
  const scope = effectScope();
  // No component is mounted: don't run polling, cloud work or native capture.
  const live = scope.run(() => useLiveCapture(context));
  return { live, context, scope, starts: () => starts };
}

test('main startup/exit clear only visibility; auxiliary windows preserve it', () => {
  values.set(settingsKey, '{"fontSize":20,"showBuffOverlay":true}');
  values.set('overlay-position', '128,256');
  values.set(key, '1');
  session.beginOverlaySession(localStorage, false);
  assert.equal(values.get(key), '0');
  values.set(key, '1');
  session.beginOverlaySession(localStorage, true);
  assert.equal(values.get(key), '1');
  session.endOverlaySession(localStorage);
  assert.equal(values.get(key), '0');
  assert.equal(session.overlaySessionEnding(), true);
  assert.equal(values.get(settingsKey), '{"fontSize":20,"showBuffOverlay":true}');
  assert.equal(values.get('overlay-position'), '128,256');
});

test('automatic show while idle cannot start capture or expose an empty window', async () => {
  const t = setup();
  try {
    assert.equal(await t.live.setOverlayVisible(true), false);
    assert.equal(t.starts(), 0);
    assert.equal(native.length, 0);
    assert.equal(values.get(key), '0');
  } finally { t.scope.stop(); }
});

test('explicit manual show still starts capture; real running session shows without restart', async () => {
  const t = setup();
  try {
    assert.equal(await t.live.setOverlayVisible(true, true), true);
    assert.equal(t.starts(), 1);
    simulatorApi.captureStatus = async () => ({ state: 'running' });
    assert.equal(await t.live.setOverlayVisible(true), true);
    assert.equal(t.starts(), 1);
    assert.equal(values.get(key), '1');
    assert.ok(native.some(r => r.cmd === 'set_buff_overlay_visible' && r.args.visible));
  } finally { t.scope.stop(); }
});

test('missing game returns error without showing windows or sending a start notice', async () => {
  const t = setup();
  try {
    simulatorApi.startCapture = async () => ({ state: 'error', error: '未找到游戏进程' });
    assert.equal(await t.live.setOverlayVisible(true, true), false);
    assert.match(t.context.error.value, /未找到游戏进程/);
    assert.equal(t.live.overlayEnabled.value, false);
    assert.equal(values.get(key), '0');
    assert.ok(!native.some(r => r.cmd === 'set_buff_overlay_visible' && r.args.visible));
    assert.ok(!native.some(r => r.args?.event === 'buff-overlay-capture-notice'));
  } finally { t.scope.stop(); }
});

test('a delayed state response cannot reopen overlays after exit begins', async () => {
  const t = setup();
  try {
    let resolve;
    simulatorApi.captureStatus = () => new Promise(done => { resolve = done; });
    const pending = t.live.setOverlayVisible(true, true);
    session.endOverlaySession(localStorage);
    resolve({ state: 'running' });
    assert.equal(await pending, false);
    assert.equal(t.starts(), 0);
    assert.equal(native.length, 0);
    assert.equal(values.get(key), '0');
  } finally { t.scope.stop(); }
});

test('startup reset precedes child mount; page mount has no visibility-triggered capture', () => {
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8');
  const live = readFileSync(new URL('../src/composables/liveDps/useLiveCapture.ts', import.meta.url), 'utf8');
  assert.ok(app.indexOf('beginOverlaySession(localStorage,') < app.indexOf('onMounted(async'));
  assert.match(app, /exitSaveInProgress = true;\s*endOverlaySession\(localStorage\)/);
  const mount = live.slice(live.indexOf('onMounted(() =>'), live.indexOf('function handleGlobalHotkeyState'));
  assert.doesNotMatch(mount, /setOverlayVisible\(true/);
  const native = readFileSync(new URL('../src-tauri/src/lib.rs', import.meta.url), 'utf8');
  const teardown = native.slice(native.indexOf('fn stop_desktop_runtime'), native.indexOf('fn stop_desktop_runtime') + 1700);
  assert.match(teardown, /TEAMMATE_INFO_WINDOW_LABEL/);
  assert.match(teardown, /GAME_TIMER_OVERLAY_WINDOW_LABEL/);
  assert.match(teardown, /window\.destroy\(\)/);
});
