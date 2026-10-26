import type {
  BuffOverlayStatus,
  CaptureStatus,
  LiveTimelineStatus,
} from "../types/api";
import {
  replayActive,
  replayBuffOverlayStatus,
  replayCaptureStatus,
  takeReplayBatch,
} from "./replaySource";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:48710";

export interface NpcapConfigPayload {
  configuredPath: string;
  configuredValid: boolean;
  configPath: string;
  resolvedPath: string;
  source: "configured" | "portable" | "system" | "missing";
  installed: boolean;
  adapterCount: number;
  message: string;
}

export interface NpcapDetectPayload {
  candidates: Array<{ path: string; source: string }>;
  resolvedPath: string;
  installed: boolean;
  message: string;
}

export interface NpcapTestPayload {
  ok: boolean;
  message: string;
  resolvedPath: string;
  adapterCount: number;
  failedStage: "" | "load" | "permission" | "adapter" | "unknown";
}

/** 观测日志开关状态（只记录，不影响任何统计/播报判定）。 */
export interface ProbeState {
  enabled: boolean;
  source: "runtime" | "env";
  path: string;
  rows: number;
  bytes: number;
  startedAt: string;
  envDefault: boolean;
}

/** 设置页「调试记录」：一个开关同时控制观测日志与保留本轮抓包。 */
export interface DebugRecordingState {
  enabled: boolean;
  configuredDir: string;
  outputDir: string;
  defaultDir: string;
  probeDir: string;
  captureDir: string;
  configPath: string;
  probeEnabled: boolean;
  probePath: string;
  probeRows: number;
  probeBytes: number;
  probeStartedAt: string;
  lastCapturePath: string;
  lastCaptureAt: string;
  lastCaptureBytes: number;
  keptCaptures: number;
  error: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response | null = null;
  let lastError: unknown = null;
  const attempts = path === "/api/health" ? 16 : 1;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      response = await fetch(`${API_BASE}${path}`, {
        ...init,
        headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      });
      break;
    } catch (reason) {
      lastError = reason;
      if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
  if (!response) {
    if (lastError instanceof TypeError) {
      throw new Error("本机解析核心连接已断开，请稍候重试；若持续失败请重新启动程序。");
    }
    throw lastError instanceof Error ? lastError : new Error("本机解析核心尚未启动");
  }
  if (!response.ok) {
    let detail = await response.text();
    try {
      const payload = JSON.parse(detail) as { message?: string };
      detail = payload.message || detail;
    } catch { /* plain text error */ }
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body: unknown) => request<T>(path, {
  method: "POST",
  body: JSON.stringify(body),
});

export const simulatorApi = {
  health: () => request<{ ok: boolean; version: string }>("/api/health"),
  captureStatus: async (includeSnapshot = false) => {
    const status = await request<CaptureStatus>(
      `/api/capture/status?includeSnapshot=${includeSnapshot ? "1" : "0"}`,
    );
    // 回放时只换数据源：状态字段照旧，只把「在不在统计」和「哪个副本」换成回放的。
    // 这样语音组件和悬浮窗不需要任何「回放中」分支。
    return replayActive() ? replayCaptureStatus(status as unknown as Record<string, unknown>) as unknown as CaptureStatus : status;
  },
  startCapture: (duration = 0) => post<CaptureStatus>("/api/capture/start", { duration }),
  stopCapture: () => post<CaptureStatus>("/api/capture/stop", {}),
  captureToggles: () =>
    request<{ toggles: Record<string, boolean>; labels: Record<string, string> }>("/api/capture/toggles"),
  setCaptureToggles: (toggles: Record<string, boolean>) =>
    post<{ toggles: Record<string, boolean>; labels: Record<string, string> }>("/api/capture/toggles", { toggles }),
  captureVoiceEvents: async () => {
    if (replayActive()) {
      const batch = takeReplayBatch();
      if (batch) return batch as never;
    }
    return request<Pick<LiveTimelineStatus, "state" | "sessionId" | "elapsedSeconds" | "voiceEvents"> & {
      clockRunning?: boolean;
      /** 播报白名单提示；副本未识别或选不出本机主目标时为 null（不过滤）。 */
      focusGroup?: { current: number; others: number[]; focus?: number[]; scene?: number[] } | null;
      /** 旧字段名，语义已并入 focusGroup（值相同）。 */
      splitGroup?: { current: number; others: number[]; focus?: number[]; scene?: number[] } | null;
      /** 血量贴地后再也没有 0 包、判定为已离场的 Boss（前端据此清倒计时）。 */
      retiredBossIds?: string[];
    }>(
      "/api/capture/voice-events",
    );
  },
  /** 开发者模式：列出可回放的录像。 */
  replayCaptures: () =>
    request<{ ok: boolean; captures: { path: string; name: string; sizeBytes: number; modifiedAt: number }[] }>(
      "/api/replay/captures",
    ),
  /** 开发者模式：开始解析一个录像，返回后台作业号。 */
  replayOpen: (path: string) =>
    post<{ ok: boolean; job?: string; cached?: boolean; error?: string }>("/api/replay/open", { path }),
  /** 开发者模式：查询解析进度，完成后带回证据行。 */
  replayStatus: (job: string) =>
    request<{
      ok: boolean;
      job?: string;
      state?: "loading" | "ready" | "error";
      progress?: number;
      message?: string;
      error?: string;
      capture?: string;
      nestName?: string;
      nestKey?: string;
      durationMs?: number;
      rows?: unknown[];
      /** 整场 Boss 名单（模板号 → 名字），悬浮窗 Boss 行要用。 */
      bosses?: unknown[];
      drops?: Record<string, number>;
      elapsedMs?: number;
    }>(`/api/replay/status?job=${encodeURIComponent(job)}`),
  /** 把前端这一批事件的处理结果写进观测流水，回放时用来逐条对账。 */
  probeEvent: (kind: string, fields: Record<string, unknown>) =>
    post<{ ok: boolean }>("/api/debug/probe-event", { kind, fields }),
  buffOverlayStatus: async (includeCatalog = false) => {
    const status = await request<BuffOverlayStatus>(
      `/api/capture/buff-overlay?includeCatalog=${includeCatalog ? "1" : "0"}`,
    );
    // 悬浮窗窗口只吃主窗口推来的这份快照。回放时若不在这里冒充「正在统计」，
    // 窗口会一直显示「统计已停止」，倒计时行也不会渲染。
    return replayActive()
      ? replayBuffOverlayStatus(status as unknown as Record<string, unknown>) as unknown as BuffOverlayStatus
      : status;
  },
  waitBuffOverlayChange: (afterRevision = 0, timeoutMs = 15_000) =>
    request<import("../types/api").BuffOverlayChangeStatus>(
      `/api/capture/buff-overlay/wait?afterRevision=${Math.max(0, Math.floor(afterRevision))}&timeoutMs=${Math.max(0, Math.min(25_000, Math.floor(timeoutMs)))}`,
    ),
  npcapConfig: () => request<NpcapConfigPayload>("/api/npcap/config"),
  saveNpcapConfig: (path: string) => post<NpcapConfigPayload>("/api/npcap/config", { path }),
  detectNpcap: () => post<NpcapDetectPayload>("/api/npcap/detect", {}),
  testNpcap: () => post<NpcapTestPayload>("/api/npcap/test", {}),
  probe: () => request<ProbeState>("/api/probe"),
  setProbe: (enabled: boolean) => post<ProbeState>("/api/probe", { enabled }),
  debugRecording: () => request<DebugRecordingState>("/api/debug-recording"),
  saveDebugRecording: (payload: { enabled?: boolean; outputDir?: string }) =>
    post<DebugRecordingState>("/api/debug-recording", payload),
};
