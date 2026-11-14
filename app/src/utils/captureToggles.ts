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

/**
 * 后端 `/api/capture/toggles` 的 `labels` 拿不到时用的兜底文案。
 *
 * 首页这两张卡以前完全靠后端下发文字，请求一失败 `<section>` 就渲染成空的，
 * 看起来就是「两个开关不见了」。壳和引擎是并行启动的，首次启动（WebView2
 * 冷建配置目录 + 引擎还没 bind 48710）这个请求必然抢在后端前面，所以兜底
 * 不是可选项。文案必须与 `engine/live/capture_toggles.py::CAPTURE_TOGGLE_LABELS`
 * 逐字一致，否则后端应答前后会闪一下。
 */
export const CAPTURE_TOGGLE_FALLBACK_LABELS: Record<string, string> = {
  buffs: "增益 / 减益倒计时",
  boss_actions: "Boss 技能 / 动作倒计时",
};

/**
 * 合并后端标签与兜底标签。
 *
 * 永远从兜底开始铺，再用后端值覆盖：这样即使后端只回了一个 key，首页也还是
 * 两张卡，而不会缺一张。`authoritative` 表示后端确实应答过，轮询据此停止补拉。
 */
export function resolveCaptureToggleLabels(
  payload?: Record<string, string> | null,
): { labels: Record<string, string>; authoritative: boolean } {
  const labels: Record<string, string> = { ...CAPTURE_TOGGLE_FALLBACK_LABELS };
  let authoritative = false;
  for (const [key, value] of Object.entries(payload || {})) {
    if (typeof value !== "string" || !value.trim()) continue;
    labels[key] = value;
    authoritative = true;
  }
  return { labels, authoritative };
}

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
