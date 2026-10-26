import test from 'node:test';
import assert from 'node:assert/strict';
import { LiveReminderTime } from '../src/utils/liveReminderTime.ts';
import { SeaReminderClock } from '../src/utils/voiceAnnouncements.ts';

test('8 second producer stall does not stall an already armed warning', () => {
  let mono = 0;
  const time = new LiveReminderTime(() => mono);
  const reminders = new SeaReminderClock();
  reminders.started(50456, 100000, 'apocalypse', 4235447);
  time.observe(174000);
  mono = 1000;
  assert.deepEqual(reminders.due(time.now()!), ['召唤触手已好']);
  mono = 8000;
  time.observe(174000); // stale HTTP sample must not reset progress
  assert.equal(time.now(), 182000);
  assert.deepEqual(reminders.due(time.now()!), []);
  time.clear();
  assert.equal(time.now(), undefined);
  time.observe(100);
  assert.equal(time.now(), 100); // new capture starts from zero independently
});

test('death cancels pending warnings even with independently advancing time', () => {
  const reminders = new SeaReminderClock();
  reminders.started(50456, 100000, 'apocalypse', 4235447);
  reminders.health(4235447, 0, 174500, 'apocalypse');
  assert.deepEqual(reminders.due(175000), []);
});
