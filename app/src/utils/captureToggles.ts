/**
 * 首页两个「总开关」的共享状态：增益/减益（buffs）与 Boss 技能（boss_actions）。
 *
 * 这两个开关只决定**是否显示悬浮窗**，不再参与事件采集：语音播报走解码器里
 * 独立的 `voice_signal_feed`，关掉开关后依然要出声，所以采集侧不能把它们丢弃。
 *
 * 权威值来自后端 `/api/capture/toggles`；主窗口读到后写入 localStorage 缓存，
 * 悬浮窗发布器、热键、窗口显隐这些没有网络上下文的地方读缓存即可。
 */

export const CAPTURE_TOGGLES_STORAGE_KEY = "dragon-nest-v420-capture-toggles";

export type CaptureToggleKey = "buffs" | "boss_actions";

export type CaptureToggleState = Record<string, boolean | undefined>;

export function readCaptureToggles(): CaptureToggleState {
  try {
    const raw = localStorage.getItem(CAPTURE_TOGGLES_STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const state: CaptureToggleState = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof value === "boolean") state[key] = value;
    }
    return state;
  } catch {
    return {};
  }
}

let cachedRaw: string | null | undefined;
let cachedState: CaptureToggleState = {};

export function writeCaptureToggles(state: CaptureToggleState): void {
  const raw = JSON.stringify(state);
  try {
    localStorage.setItem(CAPTURE_TOGGLES_STORAGE_KEY, raw);
  } catch {
    // A quota/private-mode failure must not break the switch itself.
  }
  cachedRaw = raw;
  cachedState = state;
}

/**
 * 150ms 轮询里每次都要问一次开关，重复 JSON.parse 没必要：和
 * `countdownSettings()` 一样按原始字符串比较，值没变就复用。
 */
export function readCaptureTogglesCached(): CaptureToggleState {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(CAPTURE_TOGGLES_STORAGE_KEY);
  } catch {
    return {};
  }
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedState = readCaptureToggles();
  }
  return cachedState;
}

/**
 * 某个总开关是否允许显示对应的悬浮窗。
 *
 * 缓存还没加载时（`undefined`）按「开启」处理：宁可多显示一个窗口，也不能
 * 因为一次读缓存失败就让用户的悬浮窗凭空消失。
 */
export function captureToggleAllows(
  key: CaptureToggleKey,
  state?: CaptureToggleState,
): boolean {
  return (state ?? readCaptureTogglesCached())[key] !== false;
}
