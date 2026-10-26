import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { QWEN_VOICES, announcementParts } from '../src/utils/qwenVoicePlayer.ts';
import { SEA_MECHANICS, seaBreathText, CLASSIC_ADD_TEXTS } from '../src/utils/voiceAnnouncements.ts';
import { CLASSIC_RULES } from '../src/utils/classicMechanics.ts';
import { HARD_RULES } from '../src/utils/hardMechanics.ts';
import { SAND_RULES } from '../src/utils/sandMechanics.ts';
const root = new URL('../public/voices/qwen3/', import.meta.url);

function pcmLevels(bytes: Buffer) {
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const size = bytes.readUInt32LE(offset + 4);
    if (bytes.toString('ascii', offset, offset + 4) === 'data') {
      let squares = 0, peak = 0;
      for (let i = offset + 8; i < offset + 8 + size; i += 2) {
        const sample = bytes.readInt16LE(i);
        squares += sample * sample; peak = Math.max(peak, Math.abs(sample));
      }
      return { rms: Math.sqrt(squares / (size / 2)), peak, size };
    }
    offset += 8 + size + (size % 2);
  }
  throw new Error('missing PCM data');
}

test('jump is louder than every other phrase without clipping or changing timing', () => {
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', root), 'utf8'));
  const original = createHash('sha256').update('跳').digest('hex').slice(0, 16);
  assert.notEqual(manifest.phrases['跳'], original, 'new URL avoids stale audio cache');
  for (const voice of QWEN_VOICES) {
    const levels = (id: string) => pcmLevels(readFileSync(new URL(`${voice.key}/${id}.wav`, root)));
    const jump = levels(manifest.phrases['跳']);
    assert.equal(jump.size, levels(original).size, 'no padding, repeats or timing shift');
    assert.ok(jump.peak <= 32767 * 10 ** (-1 / 20), 'at least 1 dB peak headroom');
    for (const [text, id] of Object.entries(manifest.phrases)) {
      if (text !== '跳') assert.ok(jump.rms >= levels(id as string).rms, `${voice.key}: ${text}`);
    }
  }
});

test('all speakers contain real PCM audio for every announcement, not renamed duplicates', () => {
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', root), 'utf8'));
  const phrases = manifest.phrases as Record<string, string>;
  for (const text of [...CLASSIC_RULES.flatMap(r=>r.texts),...HARD_RULES.flatMap(r=>r.texts),...SAND_RULES.flatMap(r=>r.texts),'爆炸兵出现','所有爆炸兵消失']) assert.ok(phrases[text],text);
  for (const text of Object.values(CLASSIC_ADD_TEXTS)) assert.ok(phrases[text], text);
  for (const { text, upcoming } of Object.values(SEA_MECHANICS)) {
    if (text) assert.ok(phrases[text], text);
    if (upcoming) assert.ok(phrases[upcoming], upcoming);
  }
  for (const template of [4237003, 4237023]) for (const skill of [325430, 325431])
    assert.ok(phrases[seaBreathText(skill, template)]);
  for (const text of ['黑影即将爆炸', '速击杀牛头怪', '牛头怪即将出现', '马上拉冰', '马上飞天、注意上炮', '马上飞天、注意躲避龙息消灭暗影', '注意影舞者点名'])
    assert.ok(phrases[text], text);
  assert.equal(announcementParts('冰捆已好，马上拉冰', phrases).length, 2);
  for (const id of Object.values(phrases)) {
    const hashes = QWEN_VOICES.map(voice => {
      const bytes = readFileSync(new URL(`${voice.key}/${id}.wav`, root));
      assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
      assert.ok(bytes.length > 12000, 'nonempty speech audio');
      return createHash('sha256').update(bytes).digest('hex');
    });
    assert.equal(new Set(hashes).size, QWEN_VOICES.length, 'each speaker must have its own synthesis');
  }
});
