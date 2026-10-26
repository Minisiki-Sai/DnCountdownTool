import assert from "node:assert/strict";
import test from "node:test";

import {
  OVERLAY_EVENT_TOPICS,
  acceptOverlayEvent,
  createOverlayEventEnvelope,
  isOverlayEventEnvelope,
  overlayEventName,
  targetedOverlayEventName,
  type OverlayEventCursor,
} from "../src/utils/overlayEventProtocol.ts";

test("overlay subscribers accept each publisher sequence once", () => {
  const first = createOverlayEventEnvelope(
    OVERLAY_EVENT_TOPICS.combatSnapshot,
    { elapsedSeconds: 1 },
    "capture-a",
    1,
    1_000,
  );
  const accepted = acceptOverlayEvent(first, OVERLAY_EVENT_TOPICS.combatSnapshot, null);
  assert.equal(accepted.accepted, true);

  const duplicate = acceptOverlayEvent(first, OVERLAY_EVENT_TOPICS.combatSnapshot, accepted.cursor);
  assert.equal(duplicate.accepted, false);

  const stale = createOverlayEventEnvelope(
    OVERLAY_EVENT_TOPICS.combatSnapshot,
    { elapsedSeconds: 0.5 },
    "capture-a",
    0,
    900,
  );
  assert.equal(
    acceptOverlayEvent(stale, OVERLAY_EVENT_TOPICS.combatSnapshot, accepted.cursor).accepted,
    false,
  );
});

test("targeted overlay event names isolate WebView listeners", () => {
  assert.equal(
    targetedOverlayEventName(OVERLAY_EVENT_TOPICS.combatSnapshot, "buff-overlay"),
    "dragon-nest-overlay-event-v1:combat-snapshot:to:buff-overlay",
  );
  assert.notEqual(
    targetedOverlayEventName(OVERLAY_EVENT_TOPICS.combatSnapshot, "buff-overlay"),
    targetedOverlayEventName(OVERLAY_EVENT_TOPICS.combatSnapshot, "effect-overlay"),
  );
});

test("a newer publisher stream is accepted after the main window restarts", () => {
  const cursor: OverlayEventCursor = {
    streamId: "old-main",
    sequence: 400,
    publishedAt: 1_000,
  };
  const restarted = createOverlayEventEnvelope(
    OVERLAY_EVENT_TOPICS.combatSnapshot,
    { elapsedSeconds: 0 },
    "new-main",
    1,
    1_001,
  );
  assert.equal(
    acceptOverlayEvent(restarted, OVERLAY_EVENT_TOPICS.combatSnapshot, cursor).accepted,
    true,
  );
});

test("topics are isolated and malformed envelopes are ignored", () => {
  const score = createOverlayEventEnvelope(
    OVERLAY_EVENT_TOPICS.liveScoreSnapshot,
    { rows: [] },
    "main",
    3,
    2_000,
  );
  assert.equal(isOverlayEventEnvelope(score, OVERLAY_EVENT_TOPICS.liveScoreSnapshot), true);
  assert.equal(
    acceptOverlayEvent(score, OVERLAY_EVENT_TOPICS.combatSnapshot, null).accepted,
    false,
  );
  assert.equal(isOverlayEventEnvelope({ ...score, schemaVersion: 2 }), false);
  assert.equal(
    overlayEventName(OVERLAY_EVENT_TOPICS.liveScoreSnapshot),
    "dragon-nest-overlay-event-v1:live-score-snapshot",
  );
});
