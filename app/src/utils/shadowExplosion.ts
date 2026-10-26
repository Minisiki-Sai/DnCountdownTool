/** Each wave starts with its first cast, not the second shadow's cast. */
export class ShadowExplosionClock {
  private wave?: { start: number; deadline: number; rescued: Set<string>; done: boolean };
  private casters = new Set<string>();
  private seenRescued = new Set<string>();
  clear() { this.wave = undefined; this.casters.clear(); this.seenRescued.clear(); }
  started(source: string, timestamp: number) {
    if (!source || !Number.isFinite(timestamp) || this.casters.has(source)) return;
    this.casters.add(source);
    if (this.wave && timestamp <= this.wave.deadline) return;
    this.wave = { start: timestamp, deadline: timestamp + 15000, rescued: new Set(), done: false };
  }
  rescued(source: string, timestamp: number) {
    if (!source || !Number.isFinite(timestamp) || this.seenRescued.has(source)) return;
    this.seenRescued.add(source);
    const wave = this.wave;
    if (!wave || wave.done || !source || timestamp < wave.start || timestamp > wave.deadline) return;
    wave.rescued.add(source);
    if (wave.rescued.size >= 2) wave.done = true;
  }
  due(timestamp: number): string[] {
    const wave = this.wave;
    if (!wave || wave.done || timestamp < wave.deadline) return [];
    wave.done = true;
    return timestamp - wave.deadline <= 5000 ? ['黑影即将爆炸'] : [];
  }
  countdowns(timestamp: number) {
    const wave = this.wave;
    return wave && !wave.done && timestamp < wave.deadline
      ? [{ key: `shadow:${wave.start}`, ruleKey: 'sea-shadow-explosion', label: '海龙 · 黑影爆炸', remainingMs: wave.deadline - timestamp, durationMs: 15000 }]
      : [];
  }
}
