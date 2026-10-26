import assert from "node:assert/strict";
import test from "node:test";

import {
  buildOverlayProfessionLabels,
  confirmedMissingBuffsByTarget,
  confirmedBuffPartyPlayers,
  overlayProfessionLabel,
  uniqueOverlayPartyPlayers,
  updateObservedFullDurationTeamBuffs,
} from "../src/utils/overlayPartyPresentation.ts";

test("overlay identities prefer exact names and number unresolved duplicate jobs", () => {
  const players = uniqueOverlayPartyPlayers([
    { source_id: "0x20", job: "冰灵", name: "不显示的角色名" },
    { source_id: "0x10", job: "冰灵", name: "另一个角色名" },
    { source_id: "0x30", job: "圣徒", name: "第三个角色名" },
    { source_id: "0x30", job: "圣徒", name: "重复协议行" },
    { source_id: "0x50", job: "黑暗女王", name: "角色 0x00000050" },
    { source_id: "0x60", job: "黑暗女王", name: "0x00000060" },
  ]);
  const labels = buildOverlayProfessionLabels(players);

  assert.equal(players.length, 5);
  assert.equal(overlayProfessionLabel(labels, "0x10", ""), "另一个角色名");
  assert.equal(overlayProfessionLabel(labels, "0x20", ""), "不显示的角色名");
  assert.equal(overlayProfessionLabel(labels, "0x30", ""), "第三个角色名");
  assert.equal(overlayProfessionLabel(labels, "0x50", ""), "黑暗女王1");
  assert.equal(overlayProfessionLabel(labels, "0x60", ""), "黑暗女王2");
  assert.equal(overlayProfessionLabel(labels, "0x40", "黑暗女王"), "黑暗女王");
  assert.equal(overlayProfessionLabel(labels, "0x40", ""), "未知职业");
});

test("automatic missing alerts ignore raw permanent bits on burst Buffs", () => {
  const observed = new Set(["已失效的旧分类"]);
  updateObservedFullDurationTeamBuffs(
    observed,
    [
      { key: "战吼", scope: "team", fullDurationTeam: true },
      { key: "精神爆发", scope: "team", fullDurationTeam: false },
      { key: "神圣化身", scope: "personal", fullDurationTeam: true },
    ],
    [
      { catalog_key: "战吼", permanent: false },
      { catalog_key: "精神爆发", permanent: true },
      { catalog_key: "神圣化身", permanent: true },
    ],
  );

  assert.deepEqual([...observed], ["战吼"]);
});

test("missing alerts require that recipient's ended lifetime, not another player's receipt", () => {
  const observations = [{ target_id: "p1", catalog_key: "无情打击", end_seconds: 10, permanent: false }];
  assert.equal(confirmedMissingBuffsByTarget([], [], 20).size, 0);
  assert.equal(confirmedMissingBuffsByTarget(observations, [], 9).size, 0);
  const missing = confirmedMissingBuffsByTarget(observations, [], 10);
  assert.deepEqual([...missing.get("p1") || []], ["无情打击"]);
  assert.equal(missing.has("p2"), false);
});

test("missing active display row does not cancel a still-valid or permanent receipt", () => {
  const observations = [
    { target_id: "p1", catalog_key: "鸡尾酒", end_seconds: 733.798, permanent: false },
    { target_id: "p1", catalog_key: "判定光环", end_seconds: 685.851, permanent: true },
  ];
  assert.equal(confirmedMissingBuffsByTarget(observations, [], 692).size, 0);
});

test("reapplication and another active provider win over an expired application", () => {
  const expired = { target_id: "p1", catalog_key: "无情打击", end_seconds: 10, permanent: false };
  assert.equal(confirmedMissingBuffsByTarget([expired], [
    { target_id: "p1", catalog_key: "无情打击" },
  ], 20).size, 0);
  assert.equal(confirmedMissingBuffsByTarget([
    expired, { ...expired, end_seconds: 50 },
  ], [], 20).size, 0);
});

test("malformed receipt timing does not produce a definite missing claim", () => {
  assert.equal(confirmedMissingBuffsByTarget([
    { target_id: "p1", catalog_key: "无情打击", end_seconds: NaN, permanent: false },
    { target_id: "p1", catalog_key: "鹰怒", end_seconds: 0, permanent: false },
  ], [], 20).size, 0);
});

test("Buff explorer uses independent identities and retains zero-damage teammates", () => {
  const players = confirmedBuffPartyPlayers([
    { source_id: "p1", job: "剑皇", name: "输出队友" },
    { source_id: "unknown", job: "未知职业", name: "缓存姓名" },
    { source_id: "boss", job: "Boss Lv70", name: "Boss" },
  ], [
    { actor_id: "idle", job: "圣徒", name: "未出伤队友", confidence: 0.95 },
    { actor_id: "weak", job: "剑皇", name: "弱身份", confidence: 0.35 },
    { actor_id: "missing", job: "剑皇", name: "没有身份置信度" },
    { actor_id: "p1", job: "剑皇", name: "重复身份", confidence: 0.95 },
  ]);
  assert.deepEqual(players.map(player => player.source_id), ["p1", "idle"]);
  assert.equal(players[0].name, "输出队友");
});
