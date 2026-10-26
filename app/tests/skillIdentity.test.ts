import assert from "node:assert/strict";
import test from "node:test";

import { createServer, type ViteDevServer } from "vite";

let server: ViteDevServer;
let resolveSkillIdentity: typeof import("../src/utils/overlayIdentity.ts")["resolveSkillIdentity"];
let SKILL_IDENTITY_SAMPLE: typeof import("../src/utils/overlayIdentity.ts")["SKILL_IDENTITY_SAMPLE"];
let SKILL_IDENTITY_LABEL: typeof import("../src/utils/overlayIdentity.ts")["SKILL_IDENTITY_LABEL"];

test.before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  ({ resolveSkillIdentity, SKILL_IDENTITY_SAMPLE, SKILL_IDENTITY_LABEL } =
    await server.ssrLoadModule("/src/utils/overlayIdentity.ts"));
});

test.after(async () => {
  await server?.close();
});

const ALL_NESTS = ["classic", "hard", "sand-hard", "sea-nightmare"] as const;

function lineOf(identity: ReturnType<typeof resolveSkillIdentity>, key: "nest" | "boss") {
  const { name, state, prefix, label } = identity[key];
  return { name, state, prefix, label };
}

test("白名单内 ⇒ 副本与 Boss 都标「已识别」", () => {
  const identity = resolveSkillIdentity({
    nestName: "黑龙巢穴（一）",
    bossName: "百夫长食人魔巴欧帕",
    sceneKind: "nest",
    whitelist: ALL_NESTS,
  });
  assert.deepEqual(lineOf(identity, "nest"), {
    name: "黑龙巢穴（一）",
    state: "announced",
    prefix: "副本",
    label: "(已识别)",
  });
  assert.deepEqual(lineOf(identity, "boss"), {
    name: "百夫长食人魔巴欧帕",
    state: "announced",
    prefix: "Boss",
    label: "(已识别)",
  });
});

test("识别到副本但没勾选 ⇒ 「不播报」", () => {
  const identity = resolveSkillIdentity({
    nestName: "黑龙巢穴（一）",
    bossName: "百夫长食人魔巴欧帕",
    sceneKind: "nest",
    whitelist: ["sea-nightmare"],
  });
  assert.equal(identity.nest.state, "muted");
  assert.equal(identity.nest.label, "(不播报)");
  assert.equal(identity.nest.prefix, "副本");
  assert.equal(identity.boss.state, "muted");
});

test("不在任何已知规则集里的副本 ⇒ 「不播报」而不是未识别", () => {
  const identity = resolveSkillIdentity({
    nestName: "迷雾巢穴",
    bossName: "迷雾女王",
    sceneKind: "nest",
    whitelist: ALL_NESTS,
  });
  assert.equal(identity.nest.name, "迷雾巢穴");
  assert.equal(identity.nest.state, "muted");
  assert.equal(identity.boss.name, "迷雾女王");
  assert.equal(identity.boss.state, "muted");
});

// 用户报的核心问题：明明抓到了「神圣天堂」，界面却写「未识别」。
test("识别到城镇 ⇒ 显示位置名 + 「非副本」，不再说未识别", () => {
  const identity = resolveSkillIdentity({
    nestName: "神圣天堂",
    bossName: "",
    sceneKind: "hub",
    whitelist: ALL_NESTS,
  });
  assert.deepEqual(lineOf(identity, "nest"), {
    name: "神圣天堂",
    state: "nonNest",
    prefix: "位置",
    label: "(非副本)",
  });
  // 城镇里的怪（木桩等）也不该标成「已识别」。
  const withBoss = resolveSkillIdentity({
    nestName: "神圣天堂",
    bossName: "70级巢穴木桩",
    sceneKind: "hub",
    whitelist: ALL_NESTS,
  });
  assert.equal(withBoss.boss.state, "nonNest");
  assert.equal(withBoss.boss.label, "(非副本)");
});

test("真的什么都没有 ⇒ 两行才「未识别」", () => {
  const identity = resolveSkillIdentity({
    nestName: "",
    bossName: "",
    sceneKind: "unknown",
    whitelist: ALL_NESTS,
  });
  assert.deepEqual(lineOf(identity, "nest"), {
    name: "",
    state: "unknown",
    prefix: "副本",
    label: "",
  });
  assert.deepEqual(lineOf(identity, "boss"), {
    // Boss 行没有目标时明确显示「未识别」：停手超过 30 秒后后端会主动留空，
    // 界面上要能看出来「现在没有在打的 Boss」。
    name: "未识别",
    state: "unknown",
    prefix: "Boss",
    label: "",
  });
});

test("副本已识别但 Boss 还没出来 ⇒ 两种状态可以共存", () => {
  const identity = resolveSkillIdentity({
    nestName: "梦魇海龙",
    bossName: "",
    sceneKind: "nest",
    whitelist: ALL_NESTS,
  });
  assert.equal(identity.nest.state, "announced");
  assert.equal(identity.boss.state, "unknown");
  assert.equal(identity.boss.label, "");
});

test("后端没给 sceneKind 时按副本处理，不会误判成非副本", () => {
  const identity = resolveSkillIdentity({
    nestName: "黑龙巢穴（一）",
    bossName: "",
    whitelist: ALL_NESTS,
  });
  assert.equal(identity.nest.state, "announced");
  assert.equal(identity.nest.prefix, "副本");
});

test("位置调整模式用示例文案，避免预览全是「未识别」", () => {
  const identity = resolveSkillIdentity({
    nestName: "",
    bossName: "",
    whitelist: ALL_NESTS,
    sample: true,
  });
  assert.equal(identity.nest.name, SKILL_IDENTITY_SAMPLE.nest);
  assert.equal(identity.nest.state, "announced");
  assert.equal(identity.boss.name, SKILL_IDENTITY_SAMPLE.boss);
  assert.equal(identity.boss.state, "announced");
});

test("四种状态都有对应后缀文案", () => {
  assert.equal(SKILL_IDENTITY_LABEL.announced, "(已识别)");
  assert.equal(SKILL_IDENTITY_LABEL.muted, "(不播报)");
  assert.equal(SKILL_IDENTITY_LABEL.nonNest, "(非副本)");
  assert.equal(SKILL_IDENTITY_LABEL.unknown, "");
});
