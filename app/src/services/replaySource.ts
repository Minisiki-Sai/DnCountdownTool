/**
 * 开发者模式「回放实战录像」的数据源。
 *
 * 核心思路：**不新建链路，只换数据源**。抓包解码出来的证据行按真实墙钟节奏
 * 一批批吐给 `simulatorApi.captureVoiceEvents()`，主窗口的语音播报、悬浮窗
 * 倒计时、观测日志全部走原路 —— 所以回放时听到的、看到的，和实战用的是
 * 同一份代码，不存在「回放专用分支」。
 *
 * 时间语义：
 *   - `elapsedSeconds` = 回放游标（不是真实战斗时长）
 *   - `observedAt`     = 当前墙钟（保证 5 秒新鲜度门和实战一致）
 *   - `sequence`       = 单调递增
 */

export type ReplayVoiceRow = {
  t?: number;
  timestamp: number;
  kind?: string;
  evidenceKind?: string;
  skill: number;
  templateId?: number;
  source: string;
  hpPercent?: number;
  threshold?: number;
  confirmedEndTimestamp?: number;
  recipients?: { id?: string; job?: string }[];
};

export type ReplayPayload = {
  capture: string;
  nestName: string;
  nestKey: string;
  durationMs: number;
  rows: ReplayVoiceRow[];
  /** 整场绑定过的 Boss（模板号 → 名字）。证据行只有模板号，悬浮窗要名字。 */
  bosses?: ReplayBoss[];
  /** 解析耗时，给界面显示用。 */
  elapsedMs?: number;
};

export type ReplayBoss = {
  templateId: number;
  actorId?: string;
  targetId?: string;
  name?: string;
};

export type ReplaySession = {
  job: string;
  capture: string;
  nestName: string;
  nestKey: string;
  durationMs: number;
  rows: ReplayVoiceRow[];
  bosses: ReplayBoss[];
  /** 已消费到的行下标。 */
  cursor: number;
  /** 起点（毫秒，相对抓包）。 */
  offsetMs: number;
  /** 播放倍速。 */
  speed: number;
  /** 真实墙钟起点。 */
  startedAtWall: number;
  paused: boolean;
  pausedAtMs: number;
  /** 第一次走到结尾的墙钟，用来给尾部提醒留时间。 */
  finishedAtWall: number;
};

/** 放完之后再保持「统计中」多久，等最后几条到期提醒念完。 */
const REPLAY_TAIL_MS = 90_000;

let session: ReplaySession | null = null;

export function replayActive(): boolean {
  return session !== null;
}

export function replaySession(): ReplaySession | null {
  return session;
}

export function startReplay(
  job: string,
  payload: ReplayPayload,
  options: { offsetMs?: number; speed?: number } = {},
): void {
  const offsetMs = Math.max(0, Math.min(payload.durationMs, Number(options.offsetMs) || 0));
  const rows = [...payload.rows].sort((left, right) => left.timestamp - right.timestamp);
  session = {
    job,
    capture: payload.capture,
    nestName: payload.nestName,
    nestKey: payload.nestKey,
    durationMs: payload.durationMs,
    rows,
    bosses: [...(payload.bosses ?? [])],
    cursor: rows.filter((row) => row.timestamp < offsetMs).length,
    offsetMs,
    speed: Math.max(0.25, Math.min(4, Number(options.speed) || 1)),
    startedAtWall: Date.now(),
    paused: false,
    pausedAtMs: offsetMs,
    finishedAtWall: Number.POSITIVE_INFINITY,
  };
}

export function stopReplay(): void {
  session = null;
}

export function pauseReplay(): void {
  if (!session || session.paused) return;
  session.pausedAtMs = replayElapsedMs();
  session.paused = true;
}

export function resumeReplay(): void {
  if (!session || !session.paused) return;
  session.startedAtWall = Date.now();
  session.offsetMs = session.pausedAtMs;
  session.paused = false;
}

export function seekReplay(offsetMs: number): void {
  if (!session) return;
  const target = Math.max(0, Math.min(session.durationMs, Number(offsetMs) || 0));
  session.offsetMs = target;
  session.pausedAtMs = target;
  session.startedAtWall = Date.now();
  session.cursor = session.rows.filter((row) => row.timestamp < target).length;
}

export function setReplaySpeed(speed: number): void {
  if (!session) return;
  const elapsed = replayElapsedMs();
  session.offsetMs = elapsed;
  session.startedAtWall = Date.now();
  session.speed = Math.max(0.25, Math.min(4, Number(speed) || 1));
}

/** 当前回放游标（毫秒）。 */
export function replayElapsedMs(): number {
  if (!session) return 0;
  if (session.paused) return Math.min(session.durationMs, session.pausedAtMs);
  const advanced = (Date.now() - session.startedAtWall) * session.speed;
  return Math.min(session.durationMs, session.offsetMs + advanced);
}

export function replayFinished(): boolean {
  if (!session) return false;
  const done = !session.paused && replayElapsedMs() >= session.durationMs;
  if (done && !Number.isFinite(session.finishedAtWall)) session.finishedAtWall = Date.now();
  if (!done) session.finishedAtWall = Number.POSITIVE_INFINITY;
  return done;
}

export type ReplayBatch = {
  state: "running";
  sessionId: string;
  elapsedSeconds: number;
  clockRunning: boolean;
  focusGroup: null;
  retiredBossIds: string[];
  voiceEvents: (ReplayVoiceRow & { sequence: number; observedAt: number })[];
};

/**
 * 取「自上次调用以来应当到达」的那批行。
 *
 * 实战里后端也是这么攒批的：轮询间隔内新产生的证据一次给前端。
 */
export function takeReplayBatch(): ReplayBatch | null {
  if (!session) return null;
  // 放完还要留一段时间：最后几条到期提醒（「XX已好」）可能晚于最后一个事件。
  if (replayFinished() && Date.now() - session.finishedAtWall > REPLAY_TAIL_MS) {
    stopReplay();
    return null;
  }
  const elapsed = replayElapsedMs();
  const fresh: (ReplayVoiceRow & { sequence: number; observedAt: number })[] = [];
  while (session.cursor < session.rows.length && session.rows[session.cursor].timestamp <= elapsed) {
    const row = session.rows[session.cursor];
    session.cursor += 1;
    fresh.push({
      ...row,
      sequence: session.cursor,
      observedAt: Date.now() / 1000,
    });
  }
  return {
    state: "running",
    sessionId: `replay:${session.job}`,
    elapsedSeconds: elapsed / 1000,
    clockRunning: true,
    focusGroup: null,
    retiredBossIds: [],
    voiceEvents: fresh,
  };
}

/** 回放时冒充 captureStatus，让主窗口以为「正在统计」。 */
export function replayCaptureStatus<T extends Record<string, unknown>>(base: T): T {
  if (!session) return base;
  return {
    ...base,
    state: "running",
    status: `回放中：${session.capture}`,
    nestKey: session.nestKey,
    nestLabel: session.nestName,
    nestName: session.nestName,
    elapsedSeconds: replayElapsedMs() / 1000,
  } as T;
}

/**
 * 回放时冒充 `/api/capture/buff-overlay`。
 *
 * 悬浮窗窗口自己不访问后端，只接收主窗口推来的 combat.snapshot。这份快照就是
 * 从这里取的：`state` 不是 starting/running 时，悬浮窗会把整块内容判成
 * 「统计已停止」，连倒计时行都不渲染（`visibleSkillCountdowns` 外面那层门）。
 * 所以回放必须在这里也冒充「正在统计」，否则语音在念、悬浮窗却写着已停止。
 *
 * 真实抓包的战况字段要清掉：那些是上一场留下来的（玩家列表、血量行、
 * 已结束 Boss），回放时显示出来就是错的。
 */
export function replayBuffOverlayStatus<T extends Record<string, unknown>>(base: T): T {
  if (!session) return base;
  const boss = replayBossAt(replayElapsedMs());
  return {
    ...base,
    state: "running",
    status: `回放中：${session.capture}`,
    elapsedSeconds: replayElapsedMs() / 1000,
    sceneName: session.nestName,
    sceneKind: "nest",
    nestKey: session.nestKey,
    nestLabel: session.nestName,
    nestName: session.nestName,
    nestConfidence: "replay",
    // Boss 行：按游标取「这一刻在场的那只」，不用真实抓包的上一场残留。
    bossName: boss?.name ?? "",
    bossTemplateId: boss?.templateId ?? 0,
    bossTargetId: boss?.targetId ?? "",
    bossHealth: [],
    selfAttackTarget: null,
    // 上一场的「已结束 Boss」会把刚建起来的倒计时整行抹掉。
    endedBossIds: [],
    players: [],
  } as T;
}

/**
 * 游标这一刻「在打谁」。
 *
 * 证据行只带模板号，名字在 `session.bosses` 里。取游标之前最后一条带模板号的
 * 行（行按时间有序，从后往前第一个命中即答案），再按模板号查名字。
 * 查不到就回落到最后出场的那只 —— 宁可显示一个合理的名字，也不要突然变「未识别」。
 */
function replayBossAt(cursorMs: number): ReplayBoss | null {
  if (!session || !session.bosses.length) return null;
  let templateId = 0;
  for (let index = session.rows.length - 1; index >= 0; index -= 1) {
    const row = session.rows[index];
    if (row.timestamp > cursorMs) continue;
    if (row.templateId) {
      templateId = Number(row.templateId);
      break;
    }
  }
  const matched = templateId
    ? session.bosses.find((item) => Number(item.templateId) === templateId)
    : undefined;
  if (matched) return matched;
  return session.bosses[session.bosses.length - 1] ?? null;
}
