/** Capture time anchored to a monotonic clock, independent of HTTP polling.
 * A stale producer sample must never freeze or rewind an established clock.
 * Session/lifecycle reset is explicit; this does not manufacture missing casts.
 */
export class LiveReminderTime {
  private anchor?: { capture: number; mono: number };
  private readonly monotonic: () => number;
  constructor(monotonic: () => number = () => performance.now()) { this.monotonic = monotonic; }
  clear() { this.anchor = undefined; }
  observe(captureMs: number) {
    if (!Number.isFinite(captureMs) || captureMs < 0) return;
    const mono = this.monotonic();
    const projected = this.anchor
      ? this.anchor.capture + Math.max(0, mono - this.anchor.mono) : 0;
    this.anchor = { capture: Math.max(captureMs, projected), mono };
  }
  now(): number | undefined {
    return this.anchor
      ? this.anchor.capture + Math.max(0, this.monotonic() - this.anchor.mono)
      : undefined;
  }
}
