import test from 'node:test';
import assert from 'node:assert/strict';
import { ShadowExplosionClock } from '../src/utils/shadowExplosion.ts';

test('first cast anchors 15 seconds; second cast and duplicates do not reset it', () => {
  const c = new ShadowExplosionClock();
  c.started('a', 1000); c.started('b', 2000); c.started('a', 3000);
  assert.deepEqual(c.due(15999), []);
  assert.deepEqual(c.due(16000), ['黑影即将爆炸']);
  assert.deepEqual(c.due(17000), []);
});
test('two distinct rescues cancel; old wave actors cannot cancel next wave', () => {
  const c = new ShadowExplosionClock();
  c.started('a', 1000); c.started('b', 2000);
  c.rescued('r1', 3000); c.rescued('r2', 16000);
  assert.deepEqual(c.due(16000), []);
  c.started('c', 40000); c.started('d', 41000);
  c.rescued('r1', 42000); c.rescued('r2', 42001);
  c.rescued('r3', 43000); c.rescued('r3', 44000);
  assert.deepEqual(c.due(55000), ['黑影即将爆炸']);
});
test('two NEW rescues in second wave cancel; late rescue cannot cancel', () => {
  const c = new ShadowExplosionClock();
  c.started('a', 0); c.rescued('r1', 1000); c.rescued('r2', 2000);
  c.started('b', 30000); c.rescued('r3', 31000); c.rescued('r4', 32000);
  assert.deepEqual(c.due(45000), []);
  c.clear(); c.started('a', 0); c.rescued('r1', 1000); c.rescued('r2', 15001);
  assert.deepEqual(c.due(15001), ['黑影即将爆炸']);
});
test('1018 observed cast and rescue timings all retain warning (only one rescue each)', () => {
  for (const [start, other, rescue] of [[637193,638190,654341],[1060972,1061176,1083131],[1249181,1249181,1261387]]) {
    const c = new ShadowExplosionClock();
    c.started('a', start); c.started('b', other); c.rescued('r1', rescue);
    assert.deepEqual(c.due(start + 15000), ['黑影即将爆炸']);
  }
});
