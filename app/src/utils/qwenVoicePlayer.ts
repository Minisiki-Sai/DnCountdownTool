export const QWEN_VOICES = [
  { key: 'Vivi2', label: 'Vivi2.0 · 清脆女声' },
  { key: 'Xiaohe', label: '小何2.0 · 清晰女声' },
  { key: 'Vivian', label: 'Vivian · 明亮女声' },
  { key: 'Serena', label: 'Serena · 温柔女声' },
] as const;
export function qwenVoiceKey(value: string): string {
  return QWEN_VOICES.some(v => v.key === value) ? value : 'Vivian';
}
export function announcementParts(text: string, phrases: Record<string, string>): string[] {
  const keys = Object.keys(phrases).sort((a, b) => b.length - a.length);
  const parts: string[] = [];
  let rest = text;
  while (rest) {
    const key = keys.find(k => rest.startsWith(k));
    if (!key) throw new Error(`缺少本地语音：${rest}`);
    parts.push(key);
    rest = rest.slice(key.length).replace(/^[，、。\s]+/, '');
  }
  return parts;
}
type Manifest = { phrases: Record<string, string> };

/** Offline assets only. No synthesis, capture-thread work or unbounded speech queue. */
export class QwenVoicePlayer {
  private context?: AudioContext;
  private manifest?: Promise<Manifest>;
  private buffers = new Map<string, Promise<AudioBuffer>>();
  private sources = new Set<AudioBufferSourceNode>();
  private gains = new Map<AudioBufferSourceNode, GainNode>();
  private generation = 0;
  private active = false;
  private voice = '';
  private readonly base: string;
  constructor(base: string) { this.base = base; }
  private audioContext() { return this.context ??= new AudioContext(); }
  private data() {
    if (!this.manifest) this.manifest = fetch(`${this.base}manifest.json`).then(async r => {
      if (!r.ok) throw new Error('本地 Qwen 音色资源缺失，请检查安装文件。');
      return await r.json() as Manifest;
    }).catch(e => { this.manifest = undefined; throw e; });
    return this.manifest;
  }
  private buffer(voice: string, id: string) {
    const key = `${voice}/${id}`;
    let pending = this.buffers.get(key);
    if (!pending) {
      pending = fetch(`${this.base}${key}.wav`).then(async r => {
        if (!r.ok) throw new Error('本地 Qwen 音频缺失，请检查安装文件。');
        return this.audioContext().decodeAudioData(await r.arrayBuffer());
      }).catch(e => { this.buffers.delete(key); throw e; });
      this.buffers.set(key, pending);
    }
    return pending;
  }
  async unlock() { await this.audioContext().resume(); }
  async warm(voice: string) {
    if (this.voice !== voice) { this.voice = voice; this.buffers.clear(); }
    const manifest = await this.data();
    const ids = Object.values(manifest.phrases);
    // Two requests at a time, not hundreds competing with the live event feed.
    for (let i = 0; i < ids.length && this.voice === voice; i += 2)
      await Promise.all(ids.slice(i, i + 2).map(id => this.buffer(voice, id)));
  }
  stop() {
    this.generation++;
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); this.gains.get(source)?.disconnect(); }
    this.gains.clear();
    this.sources.clear(); this.active = false;
  }
  async play(texts: string[], voice: string, volume: number, interrupt: boolean) {
    // Live alerts may overlap: no cancellation, no serial backlog. Only explicit
    // previews/reset stop playback. Pending loads still obey the generation guard.
    if (interrupt) this.stop();
    const token = this.generation;
    const requestedAt = performance.now();
    this.active = true;
    try {
      const context = this.audioContext();
      await context.resume();
      if (token !== this.generation) return;
      if (performance.now() - requestedAt > 2000) { this.active = false; return; }
      const manifest = await this.data();
      // Each text is an independent mechanism. Only fragments inside that
      // text are sequential. One cold/missing phrase must not hold other alerts.
      await Promise.all(texts.map(async text => {
      const parts = announcementParts(text, manifest.phrases);
      const buffers = await Promise.all(parts.map(part => this.buffer(voice, manifest.phrases[part]!)));
      if (token !== this.generation) return;
      if (performance.now() - requestedAt > 2000) { this.active = false; return; }
      if (context.state !== 'running') throw new Error('请先点击试听，允许播放声音。');
      let when = context.currentTime;
      buffers.forEach(buffer => {
        const source = context.createBufferSource();
        const gain = context.createGain();
        gain.gain.value = Math.max(0, Math.min(1, volume));
        source.buffer = buffer; source.connect(gain); gain.connect(context.destination);
        this.sources.add(source);
        this.gains.set(source, gain);
        source.onended = () => {
          source.disconnect(); gain.disconnect(); this.sources.delete(source); this.gains.delete(source);
          if (token === this.generation && !this.sources.size) this.active = false;
        };
        source.start(when); when += buffer.duration;
      });
      if (!buffers.length) this.active = false;
      }));
    } catch (error) {
      if (token !== this.generation) return;
      this.active = false; throw error;
    }
  }
  dispose() { this.stop(); this.voice = ''; this.buffers.clear(); void this.context?.close(); }
}
