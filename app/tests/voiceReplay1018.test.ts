import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { SeaReminderClock } from '../src/utils/voiceAnnouncements.ts';

// Optional full-capture integration fixture produced by the read-only audit.
const path = '../outputs/verification/sea_voice_1018_final.json';
const fifth = '../outputs/verification/sea_voice_1018_ranger5_death_fix.json';
test('fifth capture lethal delta cancels all manticore clocks and blocks immediate alerts',
  { skip: !fs.existsSync(fifth) }, () => {
    const { signals } = JSON.parse(fs.readFileSync(fifth, 'utf8'));
    const source = '0x000000B1@4235448';
    const death = signals.find((r: any) => r.source === source && r.hpPercent === 0);
    assert.equal(death.timestamp, 734208);
    const clock = new SeaReminderClock();
    for (const row of signals.filter((r: any) => r.source === source)) {
      if (row.hpPercent === 0) clock.health(row.templateId, 0, row.timestamp, source);
      else clock.started(row.skill, row.timestamp, source);
    }
    assert.equal(clock.isDead(source), true);
    clock.started(50318, 740000, source);
    for (const time of [734301,741108,765923,785905,825000]) assert.deepEqual(clock.due(time), []);
    assert.equal(clock.isDead('another-boss'), false);
    clock.clear();
    assert.equal(clock.isDead(source), false);
  });
test('1018 full capture: five jumps, six ice eyes, live clocks and death cleanup',
  { skip: !fs.existsSync(path) }, () => {
    const { signals, targets } = JSON.parse(fs.readFileSync(path, 'utf8'));
    assert.equal(signals.filter((r: any) => r.skill === 325423).length, 5);
    assert.equal(signals.filter((r: any) => r.skill === 325447).length, 6);
    assert.equal(signals.filter((r: any) => r.skill === 325450).length, 6);
    assert.equal(targets['0x0000007A@4237012'].max_hp, 700000000);
    assert.equal(targets['0x0000008E@4235453'].max_hp, 80000000);
    assert.equal(targets['0x0000008F@4235453'].max_hp, 80000000);
    const clock = new SeaReminderClock();
    const alerts: { time: number; text: string }[] = [];
    let i = 0;
    for (let time = 0; time <= 1364000; time += 100) {
      while (i < signals.length && signals[i].timestamp <= time) {
        const r = signals[i++];
        if (r.kind === 'boss_hp_state' || r.kind === 'boss_hp_threshold')
          clock.health(r.templateId, r.hpPercent, r.timestamp, r.source);
        else if (r.skill) clock.started(r.skill, r.timestamp, r.source);
      }
      alerts.push(...clock.due(time).map(text => ({time, text})));
    }
    assert.equal(alerts.filter(r => r.text === '即将冰眼').length, 6);
    assert.equal(alerts.filter(r => r.text === '即将龙息').length, 6);
    // Both bosses are dead at 566.257 s. Old timers used to leak into this gap.
    assert.deepEqual(alerts.filter(r => r.time > 566300 && r.time < 967000), []);
    for (const skill of [325447,325450]) {
      const text = skill === 325447 ? '即将冰眼' : '即将龙息';
      const delay = skill === 325447 ? 55000 : 50000;
      const casts = signals.filter((r: any) => r.skill === skill);
      const reminders = alerts.filter(r => r.text === text);
      for (let n=0; n<casts.length; n++)
        assert.ok(reminders[n]!.time >= casts[n].timestamp+delay && reminders[n]!.time < casts[n].timestamp+delay+100);
    }
  });
