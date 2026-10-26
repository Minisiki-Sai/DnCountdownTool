export const OVERLAY_EVENT_NAME = "dragon-nest-overlay-event-v1";
export const OVERLAY_EVENT_SCHEMA_VERSION = 1 as const;

export const OVERLAY_EVENT_TOPICS = {
  combatSnapshot: "combat.snapshot",
  skillCountdowns: "skill.countdowns",
  teammateInspection: "teammate.inspection",
  liveScoreSnapshot: "live-score.snapshot",
  gameTimerConfig: "game-timer.config",
  gameTimerHotkey: "game-timer.hotkey",
  gameTimerControl: "game-timer.control",
} as const;

export type OverlayEventTopic = typeof OVERLAY_EVENT_TOPICS[keyof typeof OVERLAY_EVENT_TOPICS];

export function overlayEventName(topic: OverlayEventTopic): string {
  // Tauri event names intentionally exclude dots even though logical topic
  // names use them for readability inside the versioned envelope.
  return `${OVERLAY_EVENT_NAME}:${topic.replaceAll(".", "-")}`;
}

export function targetedOverlayEventName(
  topic: OverlayEventTopic,
  targetLabel: string,
): string {
  // Tauri 2.x JavaScript listeners are registered with EventTarget::Any.
  // emitTo(label, sameName, payload) can therefore still inject/parse that
  // event in every WebView that listens to the shared name.  The destination
  // is part of the event name as well, so only one WebView owns a matching
  // listener even on affected Tauri/WebView2 versions.
  const safeLabel = String(targetLabel || "")
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "-");
  return safeLabel
    ? `${overlayEventName(topic)}:to:${safeLabel}`
    : overlayEventName(topic);
}

export type OverlayEventEnvelope<T = unknown> = {
  schemaVersion: typeof OVERLAY_EVENT_SCHEMA_VERSION;
  topic: OverlayEventTopic;
  streamId: string;
  sequence: number;
  publishedAt: number;
  payload: T;
};

export type OverlayEventCursor = {
  streamId: string;
  sequence: number;
  publishedAt: number;
} | null;

export function createOverlayEventEnvelope<T>(
  topic: OverlayEventTopic,
  payload: T,
  streamId: string,
  sequence: number,
  publishedAt = Date.now(),
): OverlayEventEnvelope<T> {
  return {
    schemaVersion: OVERLAY_EVENT_SCHEMA_VERSION,
    topic,
    streamId,
    sequence,
    publishedAt,
    payload,
  };
}

export function isOverlayEventEnvelope(
  value: unknown,
  expectedTopic?: OverlayEventTopic,
): value is OverlayEventEnvelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Partial<OverlayEventEnvelope>;
  return row.schemaVersion === OVERLAY_EVENT_SCHEMA_VERSION
    && (!expectedTopic || row.topic === expectedTopic)
    && Object.values(OVERLAY_EVENT_TOPICS).includes(row.topic as OverlayEventTopic)
    && typeof row.streamId === "string"
    && row.streamId.length > 0
    && Number.isSafeInteger(row.sequence)
    && Number(row.sequence) > 0
    && Number.isFinite(Number(row.publishedAt));
}

export function acceptOverlayEvent(
  envelope: OverlayEventEnvelope,
  expectedTopic: OverlayEventTopic,
  cursor: OverlayEventCursor,
): { accepted: boolean; cursor: OverlayEventCursor } {
  if (!isOverlayEventEnvelope(envelope, expectedTopic)) {
    return { accepted: false, cursor };
  }
  if (!cursor) {
    return {
      accepted: true,
      cursor: {
        streamId: envelope.streamId,
        sequence: envelope.sequence,
        publishedAt: envelope.publishedAt,
      },
    };
  }
  const sameStream = envelope.streamId === cursor.streamId;
  const newer = sameStream
    ? envelope.sequence > cursor.sequence
    : envelope.publishedAt > cursor.publishedAt;
  if (!newer) return { accepted: false, cursor };
  return {
    accepted: true,
    cursor: {
      streamId: envelope.streamId,
      sequence: envelope.sequence,
      publishedAt: envelope.publishedAt,
    },
  };
}
