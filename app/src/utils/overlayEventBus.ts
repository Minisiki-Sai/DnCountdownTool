import { emit, emitTo, listen, type UnlistenFn } from "@tauri-apps/api/event";
import {
  OVERLAY_EVENT_NAME,
  OVERLAY_EVENT_TOPICS,
  acceptOverlayEvent,
  createOverlayEventEnvelope,
  isOverlayEventEnvelope,
  overlayEventName,
  targetedOverlayEventName,
  type OverlayEventCursor,
  type OverlayEventEnvelope,
  type OverlayEventTopic,
} from "./overlayEventProtocol";

export type OverlayEventUnlisten = () => void;
export type OverlayEventHandler<T> = (
  payload: T,
  envelope: OverlayEventEnvelope<T>,
) => void;

type BrowserListener = (envelope: OverlayEventEnvelope) => void;
export type OverlaySnapshotRequest = {
  kind: "overlay-snapshot-request-v1";
  topic: OverlayEventTopic;
  targetLabel: string;
};

const browserListeners = new Set<BrowserListener>();
const browserSnapshotRequestListeners = new Set<(
  request: OverlaySnapshotRequest,
) => void>();
const sequenceByTopic = new Map<OverlayEventTopic, number>();
const streamId = createStreamId();
const OVERLAY_SNAPSHOT_REQUEST_EVENT = "dragon-nest-overlay-snapshot-request-v1";
const browserChannel = tauriAvailable() || typeof BroadcastChannel === "undefined"
  ? null
  : new BroadcastChannel(OVERLAY_EVENT_NAME);

if (browserChannel) {
  browserChannel.onmessage = event => dispatchBrowserMessage(event.data);
}

function tauriAvailable(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function createStreamId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `overlay-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function dispatchBrowserEnvelope(value: unknown) {
  if (!isOverlayEventEnvelope(value)) return;
  for (const listener of [...browserListeners]) listener(value);
}

function isOverlaySnapshotRequest(value: unknown): value is OverlaySnapshotRequest {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<OverlaySnapshotRequest>;
  return row.kind === "overlay-snapshot-request-v1"
    && Object.values(OVERLAY_EVENT_TOPICS).includes(row.topic as OverlayEventTopic)
    && Boolean(String(row.targetLabel || "").trim());
}

function dispatchBrowserSnapshotRequest(value: unknown) {
  if (!isOverlaySnapshotRequest(value)) return;
  for (const listener of [...browserSnapshotRequestListeners]) listener(value);
}

function dispatchBrowserMessage(value: unknown) {
  if (isOverlaySnapshotRequest(value)) dispatchBrowserSnapshotRequest(value);
  else dispatchBrowserEnvelope(value);
}

export async function publishOverlayEvent<T>(
  topic: OverlayEventTopic,
  payload: T,
  targetLabels?: readonly string[],
): Promise<OverlayEventEnvelope<T>> {
  const sequence = (sequenceByTopic.get(topic) || 0) + 1;
  sequenceByTopic.set(topic, sequence);
  const envelope = createOverlayEventEnvelope(topic, payload, streamId, sequence);
  if (tauriAvailable()) {
    const targets = [...new Set(
      (targetLabels || []).map(label => String(label || "").trim()).filter(Boolean),
    )];
    if (targets.length) {
      // On-demand WebViews may not exist yet (for example saving timer
      // settings before its first Show). Delivery to existing targets is
      // independent; a missing disabled window must not turn the user action
      // into an error or fall back to an application-wide broadcast.
      await Promise.allSettled(
        targets.map(label => emitTo(label, targetedOverlayEventName(topic, label), envelope)),
      );
    } else {
      await emit(overlayEventName(topic), envelope);
    }
  } else {
    // BroadcastChannel does not echo to its sender, so deliver locally as well.
    dispatchBrowserEnvelope(envelope);
    browserChannel?.postMessage(envelope);
  }
  return envelope;
}

export async function subscribeOverlayEvent<T>(
  topic: OverlayEventTopic,
  handler: OverlayEventHandler<T>,
  targetLabel?: string,
): Promise<OverlayEventUnlisten> {
  let cursor: OverlayEventCursor = null;
  let disposed = false;
  const deliver = (value: unknown) => {
    if (disposed || !isOverlayEventEnvelope(value, topic)) return;
    const decision = acceptOverlayEvent(value, topic, cursor);
    if (!decision.accepted) return;
    cursor = decision.cursor;
    handler(value.payload as T, value as OverlayEventEnvelope<T>);
  };

  if (tauriAvailable()) {
    let nativeUnlisten: UnlistenFn | null = await listen<OverlayEventEnvelope>(
      targetedOverlayEventName(topic, String(targetLabel || "").trim()),
      event => deliver(event.payload),
    );
    return () => {
      disposed = true;
      nativeUnlisten?.();
      nativeUnlisten = null;
    };
  }

  browserListeners.add(deliver);
  return () => {
    disposed = true;
    browserListeners.delete(deliver);
  };
}

/**
 * Ask the main publisher to replay its latest state to a newly-created WebView.
 * Dynamic windows can otherwise miss the snapshot emitted just before their
 * first navigation completes and remain empty until combat state changes.
 */
export async function requestOverlayEventSnapshot(
  topic: OverlayEventTopic,
  targetLabel: string,
): Promise<void> {
  const request: OverlaySnapshotRequest = {
    kind: "overlay-snapshot-request-v1",
    topic,
    targetLabel: String(targetLabel || "").trim(),
  };
  if (!request.targetLabel) return;
  if (tauriAvailable()) {
    await emitTo("main", OVERLAY_SNAPSHOT_REQUEST_EVENT, request);
    return;
  }
  dispatchBrowserSnapshotRequest(request);
  browserChannel?.postMessage(request);
}

export async function subscribeOverlaySnapshotRequests(
  handler: (request: OverlaySnapshotRequest) => void,
): Promise<OverlayEventUnlisten> {
  if (tauriAvailable()) {
    const unlisten = await listen<OverlaySnapshotRequest>(
      OVERLAY_SNAPSHOT_REQUEST_EVENT,
      event => {
        if (isOverlaySnapshotRequest(event.payload)) handler(event.payload);
      },
    );
    return unlisten;
  }
  browserSnapshotRequestListeners.add(handler);
  return () => {
    browserSnapshotRequestListeners.delete(handler);
  };
}
