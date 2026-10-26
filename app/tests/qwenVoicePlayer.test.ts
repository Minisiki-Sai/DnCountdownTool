import test from 'node:test';
import assert from 'node:assert/strict';
import { QWEN_VOICES, qwenVoiceKey, announcementParts, QwenVoicePlayer } from '../src/utils/qwenVoicePlayer.ts';

test('bundled Qwen speaker keys, migrate native preferences', () => {
  const keys = QWEN_VOICES.map(v => v.key);
  assert.deepEqual(keys, ['Vivi2', 'Xiaohe', 'Vivian', 'Serena']);
  for (const key of keys) assert.equal(qwenVoiceKey(key), key, `${key} must not fall back`);
  assert.equal(qwenVoiceKey('Microsoft Huihui'), 'Vivian');
  assert.equal(qwenVoiceKey(''), 'Vivian');
  assert.equal(qwenVoiceKey('Uncle_Fu'), 'Vivian', 'removed speaker falls back');
  assert.equal(qwenVoiceKey('Dylan'), 'Vivian', 'removed speaker falls back');
});

test('stop cancels pending load; warm cache plays without network; no backlog', async () => {
  const originalFetch = globalThis.fetch;
  const originalContext = globalThis.AudioContext;
  let starts = 0, stops = 0, requests = 0;
  const gains: { value: number }[] = [];
  let release!: () => void;
  const deferred = new Promise<void>(resolve => { release = resolve; });
  class Context {
    currentTime = 0; state = 'running'; destination = {};
    async resume() {}
    async close() {}
    async decodeAudioData() { return { duration: 1 }; }
    createGain() { const gain = { value: 0 }; gains.push(gain); return { gain, connect() {}, disconnect() {} }; }
    createBufferSource() {
      return { onended: null, connect() {}, disconnect() {}, start() { starts++; }, stop() { stops++; } };
    }
  }
  globalThis.AudioContext = Context as unknown as typeof AudioContext;
  globalThis.fetch = (async (url: string) => {
    requests++;
    if (url.endsWith('manifest.json')) return { ok: true, json: async () => ({ phrases: { '跳': 'a' } }) };
    await deferred;
    return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) };
  }) as typeof fetch;
  try {
    const player = new QwenVoicePlayer('/voices/');
    const pending = player.play(['跳'], 'Vivian', 0.4, true);
    await new Promise(resolve => setImmediate(resolve));
    player.stop(); release(); await pending;
    assert.equal(starts, 0);
    await player.warm('Vivian');
    const before = requests;
    await player.play(['跳'], 'Vivian', 0.4, true);
    assert.equal(starts, 1); assert.equal(requests, before);
    assert.equal(gains[0]?.value, 0.4);
    await player.play(['跳'], 'Vivian', 0.4, false);
    assert.equal(starts, 2);
    assert.equal(stops, 0); // Live alerts overlap without cancelling the first.
    player.dispose(); assert.equal(stops, 2);
  } finally { globalThis.fetch = originalFetch; globalThis.AudioContext = originalContext; }
});
test('preserve full phrases with punctuation and split combined reminders', () => {
  const phrases = { '注意拉冰，前往龙左侧安全区': 'a', '冰捆已好': 'b', '马上拉冰': 'c' };
  assert.deepEqual(announcementParts('注意拉冰，前往龙左侧安全区', phrases), ['注意拉冰，前往龙左侧安全区']);
  assert.deepEqual(announcementParts('冰捆已好，马上拉冰', phrases), ['冰捆已好', '马上拉冰']);
  assert.throws(() => announcementParts('缺少音频', phrases), /缺少本地语音/);
});

test('independent alerts overlap; fragments of one sentence stay ordered', async () => {
  const oldFetch = globalThis.fetch, oldContext = globalThis.AudioContext;
  const starts: number[] = [];
  let stops = 0;
  class Context {
    currentTime = 10; state = 'running'; destination = {};
    async resume() {}
    async close() {}
    async decodeAudioData() { return { duration: 1 }; }
    createGain() { return { gain: { value: 0 }, connect() {}, disconnect() {} }; }
    createBufferSource() {
      return { onended: null, connect() {}, disconnect() {},
        start(at: number) { starts.push(at); }, stop() { stops++; } };
    }
  }
  globalThis.AudioContext = Context as unknown as typeof AudioContext;
  globalThis.fetch = (async (url: string) => url.endsWith('manifest.json')
    ? { ok: true, json: async () => ({ phrases: {
      '注意拉冰': 'a', '前往龙左侧安全区': 'b', '跳': 'c',
    } }) }
    : { ok: true, arrayBuffer: async () => new ArrayBuffer(0) }) as typeof fetch;
  try {
    const player = new QwenVoicePlayer('/voices/');
    await player.warm('Vivian');
    await player.play(['注意拉冰，前往龙左侧安全区', '跳'], 'Vivian', 0.8, false);
    assert.deepEqual(starts.slice().sort((a,b) => a-b), [10, 10, 11]);
    assert.equal(stops, 0);
    await player.play(['跳'], 'Vivian', 0.8, false);
    assert.equal(starts.at(-1), 10);
    assert.equal(stops, 0);
    player.dispose();
  } finally { globalThis.fetch = oldFetch; globalThis.AudioContext = oldContext; }
});

test('an uncached slow phrase does not hold another independent alert', async () => {
  const oldFetch = globalThis.fetch, oldContext = globalThis.AudioContext;
  let release!: () => void;
  const deferred = new Promise<void>(resolve => { release = resolve; });
  const starts: number[] = [];
  class Context {
    currentTime = 10; state = 'running'; destination = {};
    async resume() {}
    async close() {}
    async decodeAudioData() { return { duration: 1 }; }
    createGain() { return { gain: { value: 0 }, connect() {}, disconnect() {} }; }
    createBufferSource() {
      return { onended: null, connect() {}, disconnect() {},
        start(at: number) { starts.push(at); }, stop() {} };
    }
  }
  globalThis.AudioContext = Context as unknown as typeof AudioContext;
  globalThis.fetch = (async (url: string) => {
    if (url.endsWith('manifest.json')) return { ok: true, json: async () => ({
      phrases: { '注意拉冰': 'slow', '跳': 'fast' },
    }) };
    if (url.includes('slow')) await deferred;
    return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) };
  }) as typeof fetch;
  const player = new QwenVoicePlayer('/voices/');
  try {
    const pending = player.play(['注意拉冰', '跳'], 'Vivian', .8, false);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(starts, [10]);
    release();
    await pending;
    assert.deepEqual(starts, [10, 10]);
  } finally {
    release();
    player.dispose();
    globalThis.fetch = oldFetch; globalThis.AudioContext = oldContext;
  }
});
