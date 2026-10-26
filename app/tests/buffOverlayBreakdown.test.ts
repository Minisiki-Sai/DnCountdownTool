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

function presentation(
  mode: "summary" | "skills" | "summary_and_skills",
  statusLayoutMode: "rows" | "icons" = "rows",
  includeNonCompact = false,
  buffLayoutMode: "rows" | "icons" = "rows",
  appearance: Partial<typeof DEFAULT_BUFF_OVERLAY_SETTINGS> = {},
  extraBuffCount = 0,
) {
  const payload = ref({
    state: "running",
    status: "",
    elapsedSeconds: 5,
    selfSourceId: "player",
    players: [{
      source_id: "player",
      job: "剑皇",
      name: "玩家",
      damage: 0,
      dps: 0,
      damage_series: [],
      attack_types: ["physical"],
      elements: ["fire"],
    }],
    buffs: [...(includeNonCompact ? [{
      key: "player|brave",
      catalog_key: "brave",
      target_id: "player",
      source_id: "player",
      skill_name: "勇敢",
      status_kind: "buff",
      start_seconds: 1,
      end_seconds: 13,
      duration_seconds: 12,
      icon_index: 19,
    }] : []), ...Array.from({ length: extraBuffCount }, (_, index) => ({
      key: `player|test-buff-${index}`,
      catalog_key: `test-buff-${index}`,
      target_id: "player",
      source_id: "player",
      skill_name: `测试 Buff ${index + 1}`,
      status_kind: "buff",
      start_seconds: 1,
      end_seconds: 13,
      duration_seconds: 12,
      icon_index: index,
    }))],
    debuffs: [{
      key: "boss|damage|physical",
      category: "damage",
      mode: "physical",
      label: "物理增伤",
      current_value: 60,
      next_value: 20,
      start_seconds: 1,
      end_seconds: 11,
      duration_seconds: 10,
      contributors: [
        {
          key: "heavy",
          skill_name: "重锤",
          source_id: "sword",
          source_job: "剑皇",
          current_value: 40,
          icon_index: 49,
          start_seconds: 1,
          end_seconds: 11,
          duration_seconds: 10,
        },
        {
          key: "armor-break",
          skill_name: "破甲",
          source_id: "paladin",
          source_job: "圣骑士",
          current_value: 20,
          icon_index: 630,
          start_seconds: 2,
          end_seconds: 8,
          duration_seconds: 6,
        },
      ],
    }, ...(includeNonCompact ? [{
      key: "boss|defense|physical",
      category: "defense",
      mode: "physical",
      label: "减物防",
      current_value: 20,
      next_value: 0,
      start_seconds: 1,
      end_seconds: 9,
      duration_seconds: 8,
      contributors: [],
    }] : [])],
    cooldowns: [],
    summons: [],
    playerHealth: [],
    buffOptions: [],
    summonOptions: [],
  });
  const settings = ref({
    ...DEFAULT_BUFF_OVERLAY_SETTINGS,
    ...appearance,
    debuffBreakdownMode: mode,
    buffLayoutMode,
    statusLayoutMode,
    damageMode: "physical" as const,
    showResistance: false,
    showCritical: false,
    showDefense: includeNonCompact,
    showFreeze: false,
  });
  return useBuffOverlayPresentation({
    props: { view: "status" },
    payload,
    settings,
    fetchedAt: ref(1_000),
    clock: ref(1_000),
    baseSeconds: ref(5),
    effectHistory: ref([]),
    effectHoverIndex: ref(null),
    layoutMode: ref(false),
    observedFullDurationTeamBuffs: new Set<string>(),
    statusPanelGap: 4,
  });
}

test("summary mode expands a total into its contributing skills", () => {
  const view = presentation("summary");

  assert.deepEqual(view.statusRows.value.map(row => row.name), [
    "物理增伤 60%→20%",
  ]);
  assert.equal(view.statusRows.value[0].expandable, true);

  view.toggleDebuffBreakdown(view.statusRows.value[0]);

  assert.deepEqual(view.statusRows.value.map(row => row.name), [
    "物理增伤 60%→20%",
    "重锤 40%",
    "破甲 20%",
  ]);
  assert.deepEqual(view.statusRows.value.slice(1).map(row => row.depth), [1, 1]);
});

test("Buff icon layout is independent from Debuff skill layout", () => {
  const view = presentation("summary", "rows", true, "icons", {
    statusIconBackgroundColors: { brave: "#112233" },
    statusIconNameColors: { brave: "#445566" },
    statusIconTimeColors: { brave: "#778899" },
  });

  assert.deepEqual(view.compactEffectRows.value.map(row => row.name), ["勇敢"]);
  assert.deepEqual(view.standardStatusRows.value.map(row => row.name), [
    "物理增伤 60%→20%",
    "减物防 20%→0%",
  ]);
  assert.equal(view.rowIconStyle(view.compactEffectRows.value[0]).backgroundPosition, "-378px -42px");
  const rowAppearance = view.rowStyle(view.compactEffectRows.value[0]);
  assert.equal(rowAppearance["--status-icon-background-color"], "#112233");
  assert.equal(rowAppearance["--status-icon-name-color"], "#445566");
  assert.equal(rowAppearance["--status-icon-time-color"], "#778899");
});

test("skill and combined modes project the requested rows without a click", () => {
  const skills = presentation("skills");
  assert.deepEqual(skills.statusRows.value.map(row => row.name), [
    "重锤 40%",
    "破甲 20%",
  ]);
  assert.deepEqual(skills.statusRows.value.map(row => row.depth), [0, 0]);

  const combined = presentation("summary_and_skills");
  assert.deepEqual(combined.statusRows.value.map(row => row.name), [
    "物理增伤 60%→20%",
    "重锤 40%",
    "破甲 20%",
  ]);
});

test("icon layout keeps totals in rows and compacts only contributing skills", () => {
  const view = presentation("summary", "icons", true);

  assert.deepEqual(view.compactEffectRows.value, []);
  assert.deepEqual(view.standardStatusRows.value.map(row => row.name), [
    "勇敢",
    "物理增伤 60%→20%",
    "减物防 20%→0%",
  ]);

  const summary = view.standardStatusRows.value.find(row => row.rowKind === "debuff-summary");
  assert.ok(summary);
  view.toggleDebuffBreakdown(summary);

  assert.deepEqual(view.compactEffectRows.value.map(row => row.name), [
    "重锤 40%",
    "破甲 20%",
  ]);
  assert.equal(view.rowIconStyle(view.compactEffectRows.value[0]).backgroundPosition, "-378px -168px");
  assert.equal(view.rowIconStyle(view.compactEffectRows.value[0]).backgroundSize, "430.08px auto");
  assert.deepEqual(view.statusIconListStyle(view.compactEffectRows.value), {
    gridAutoFlow: "row",
    gridTemplateColumns: "repeat(2, 62px)",
    gridTemplateRows: "repeat(1, 77px)",
    gap: "3px",
  });
  const manyIcons = Array.from({ length: 22 }, () => view.compactEffectRows.value[0]);
  assert.deepEqual(view.statusIconListStyle(manyIcons), {
    gridAutoFlow: "row",
    gridTemplateColumns: "repeat(12, 62px)",
    gridTemplateRows: "repeat(2, 77px)",
    gap: "3px",
  });
  assert.equal(view.rowIconStyle(view.standardStatusRows.value[0]).backgroundPosition, "-378px -42px");
});

test("22 status icons wrap into 12 plus 10 with every icon inside the overlay", () => {
  const view = presentation("summary", "rows", false, "icons", {
    showDamage: false,
  }, 22);

  assert.equal(view.compactEffectRows.value.length, 22);
  assert.equal(view.standardStatusRows.value.length, 0);
  assert.deepEqual(view.statusIconListStyle(view.compactEffectRows.value), {
    gridAutoFlow: "row",
    gridTemplateColumns: "repeat(12, 62px)",
    gridTemplateRows: "repeat(2, 77px)",
    gap: "3px",
  });
  assert.deepEqual(view.overlayLayout.value, {
    columns: 12,
    rowsPerColumn: 1,
    width: 799,
    height: 169,
  });
});

test("22 default-height status rows split into 16 plus 6 without clipping", () => {
  const view = presentation("summary", "rows", false, "rows", {
    showDamage: false,
  }, 22);

  assert.equal(view.standardStatusRows.value.length, 22);
  assert.deepEqual(view.statusListStyle(view.standardStatusRows.value), {
    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
    gridTemplateRows: "repeat(16, 28px)",
    gap: "3px",
  });
  assert.deepEqual(view.overlayLayout.value, {
    columns: 2,
    rowsPerColumn: 16,
    width: 661,
    height: 505,
  });
});

test("22 large status rows split into 11 plus 11 before later debuffs are clipped", () => {
  const view = presentation("summary", "rows", false, "rows", {
    showDamage: false,
    columnWidth: 520,
    maxRowsPerColumn: 24,
    rowHeight: 42,
    gap: 10,
  }, 22);

  assert.equal(view.standardStatusRows.value.length, 22);
  assert.deepEqual(view.statusListStyle(view.standardStatusRows.value), {
    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
    gridTemplateRows: "repeat(11, 42px)",
    gap: "10px",
  });
  assert.deepEqual(view.overlayLayout.value, {
    columns: 2,
    rowsPerColumn: 11,
    width: 1068,
    height: 574,
  });
});
