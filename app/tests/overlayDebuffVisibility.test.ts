import assert from "node:assert/strict";
import test from "node:test";

import { ref } from "vue";
import { createServer, type ViteDevServer } from "vite";

import { DEFAULT_BUFF_OVERLAY_SETTINGS } from "../src/utils/buffOverlaySettings.ts";

let server: ViteDevServer;
let useBuffOverlayPresentation: typeof import("../src/composables/overlay/useBuffOverlayPresentation.ts")["useBuffOverlayPresentation"];

test.before(async () => {
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  ({ useBuffOverlayPresentation } = await server.ssrLoadModule(
    "/src/composables/overlay/useBuffOverlayPresentation.ts",
  ));
});

test.after(async () => {
  await server.close();
});

function debuff(category: string, mode: string, label: string) {
  return {
    key: `boss|${category}|${mode}`,
    category,
    mode,
    label,
    target_id: "boss",
    current_value: 30,
    next_value: 0,
    start_seconds: 0,
    end_seconds: 95,
    duration_seconds: 60,
    remaining_seconds: 55,
    contributors: [],
    value_source: "packet",
    duration_source: "packet",
    data_source: "boss_status",
  };
}

function buff() {
  return {
    key: "player|brave",
    catalog_key: "brave",
    target_id: "player",
    source_id: "player",
    skill_name: "勇敢",
    status_kind: "buff",
    // payload 的 elapsedSeconds 是 40：区间要覆盖当前时刻，否则会被判为已过期。
    start_seconds: 35,
    end_seconds: 60,
    duration_seconds: 25,
    icon_index: 19,
  };
}

function presentation(
  debuffs: ReturnType<typeof debuff>[],
  settings: Partial<typeof DEFAULT_BUFF_OVERLAY_SETTINGS> = {},
  buffs: ReturnType<typeof buff>[] = [],
) {
  // 火舞：自身只有 fire 元素、magic 攻击；队伍名单为空（轻量抓包不解伤害事件）。
  const payload = ref({
    state: "running",
    status: "",
    elapsedSeconds: 40,
    selfSourceId: "player",
    players: [{
      source_id: "player",
      job: "火舞",
      name: "玩家",
      damage: 0,
      dps: 0,
      damage_series: [],
      attack_types: ["magic"],
      elements: ["fire"],
    }],
    buffs,
    debuffs,
    cooldowns: [],
    summons: [],
    playerHealth: [],
    buffOptions: [],
    summonOptions: [],
  });
  return useBuffOverlayPresentation({
    props: { view: "status" },
    payload,
    settings: ref({
      ...DEFAULT_BUFF_OVERLAY_SETTINGS,
      selectedBuffs: ["brave"],
      ...settings,
    }),
    fetchedAt: ref(0),
    clock: ref(0),
    baseSeconds: ref(40),
    layoutMode: ref(false),
    statusPanelGap: 4,
  });
}

test("auto 模式不再按自身元素/攻击类型过滤减益", () => {
  const view = presentation([
    debuff("damage", "physical", "物理增伤"),
    debuff("resistance", "light", "减光抗"),
    debuff("defense", "physical", "减物防"),
    debuff("critical", "critical", "减致命抗"),
  ]);

  const labels = view.debuffRows.value.map(row => row.name.split(" ")[0]);
  assert.deepEqual(labels, ["物理增伤", "减光抗", "减物防", "减致命抗"]);
});

test("显式模式仍然过滤", () => {
  const view = presentation(
    [debuff("resistance", "light", "减光抗"), debuff("resistance", "fire", "减火抗")],
    { resistanceMode: "fire" },
  );

  assert.deepEqual(view.debuffRows.value.map(row => row.name.split(" ")[0]), ["减火抗"]);
});

test("showDebuffOverlay 关闭时不显示", () => {
  const view = presentation([debuff("damage", "physical", "物理增伤")], {
    showDebuffOverlay: false,
  });

  assert.deepEqual(view.debuffRows.value, []);
});

test("关掉 Debuff 只影响减益行，Buff 行继续渲染", () => {
  // 真实悬浮窗渲染用的是 statusRows / standardStatusRows，不是 debuffRows。
  // 这条保证「分项开关」确实作用到渲染路径上。
  const view = presentation([debuff("damage", "physical", "物理增伤")], {
    showDebuffOverlay: false,
  }, [buff()]);

  const names = view.statusRows.value.map(row => row.name.split(" ")[0]);
  assert.deepEqual(names, ["勇敢"]);
  assert.deepEqual(
    view.standardStatusRows.value.map(row => row.name.split(" ")[0]),
    ["勇敢"],
  );
});

test("关掉 Buff 只影响 Buff 行，Debuff 行继续渲染", () => {
  const view = presentation([debuff("damage", "physical", "物理增伤")], {
    showBuffOverlay: false,
  }, [buff()]);

  assert.deepEqual(
    view.statusRows.value.map(row => row.name.split(" ")[0]),
    ["物理增伤"],
  );
});

test("两个分项都关时渲染行为空（窗口随后由总开关隐藏）", () => {
  const view = presentation([debuff("damage", "physical", "物理增伤")], {
    showBuffOverlay: false,
    showDebuffOverlay: false,
  }, [buff()]);

  assert.deepEqual(view.statusRows.value, []);
  assert.deepEqual(view.standardStatusRows.value, []);
  assert.deepEqual(view.compactEffectRows.value, []);
});
