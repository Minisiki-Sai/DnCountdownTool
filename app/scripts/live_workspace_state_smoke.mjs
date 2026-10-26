import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname } from "node:path";
import { chromium } from "playwright-core";

// Exercise the real production Vue build without capturing, scoring or changing
// the user's running ACT. Every local API request is intercepted with fixtures.
const dist = new URL("../dist/", import.meta.url);
const server = createServer(async (req, res) => {
  const path = new URL(req.url, "http://localhost").pathname;
  const file = path === "/" ? "index.html" : path.slice(1);
  if (file.includes("..")) { res.writeHead(400); res.end(); return; }
  try {
    const body = await readFile(new URL(file, dist));
    res.setHeader("Content-Type", extname(file) === ".js" ? "text/javascript" : extname(file) === ".css" ? "text/css" : "text/html");
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
let page;
const requests = new Map();
const errors = [];
const source = "retained-record-one";
const player = { source_id: "0x10000001", name: "测试队员", job: "冰灵", damage: 120000,
  healing: 0, dps: 1000, incoming_damage: 0, critical_rate: 0.5, active_seconds: 100,
  hits: 10, critical_hits: 5, skills: [], damage_series: [[0, 0], [120, 1000]] };
const effect = { key: "减冰抗%", label: "减冰抗", status_kind: "Debuff", target_id: "boss", target_name: "测试Boss",
  skill_name: "冰刺", job: "冰灵", value: 20, coverage: 0.5, refresh_count: 1 };
const buff = { target_id: player.source_id, target_job: "冰灵", job: "冰灵", skill_name: "测试增益",
  status_kind: "Buff", label: "魔攻提高", key: "魔攻%", value: 20, coverage: 0.5 };
const replay = {
  sourceHash: source, sourceFile: "retained-one.dpscap", elapsed_seconds: 120, dps_elapsed_seconds: 100,
  total_damage: 120000, total_healing: 0, players: [player], damage_series: player.damage_series,
  damage_targets: [{ target_id: "boss", name: "测试Boss", damage: 120000, max_hp: 120000, first_ms: 0, last_ms: 120000 }],
  boss_encounters: [], boss_hp_tracks: [], boss_hp_curves: [], boss_action_events: [],
  boss_effects: [effect], boss_effect_events: [{ ...effect, timestamp_ms: 0, duration_seconds: 60 }],
  boss_effect_summary: [effect], boss_effect_series: [["减冰抗%", [[0, 20], [60, 0]]]], boss_effect_views: [],
  player_buffs: [buff], player_buff_events: [{ ...buff, timestamp_ms: 0, duration_seconds: 60 }], player_buff_series: [],
  incoming_damage_events: [], combat_events: [], death_reports: [], party_members: [], player_profiles: [], profile_panels: {},
  skill_cast_events: Array.from({ length: 605 }, (_, index) => ({ timestamp_ms: index * 190,
    source_id: player.source_id, job: player.job, skill_name: "冰刺", packet_kind: "skill_action" })),
};
const scoreRow = { sourceId: player.source_id, name: player.name, job: player.job, role: "输出", roleReason: "测试",
  grade: "A", totalScore: 80, outputScore: 80, supportScore: 80, specialtyScore: null, survivalPenalty: 0,
  outputWeight: 0.8, supportWeight: 0.2, specialtyWeight: 0, specialtyLabel: "", theoryDamageCompletion: 80,
  burstUsage: 0, hasBurstWindows: false, coverageCompletion: 80, contributionCompletion: 80,
  actualStandardPower: 800, theoryStandardPower: 1000, notes: [] };
const score = { scoreKey: "retained-score", sourceHash: source, rows: [scoreRow], bossTargetIds: [], diagnostics: {} };
const equipment = { sourceHash: source, sourceId: player.source_id, source: "standard_default", build: { job: "冰灵" }, summary: {},
  options: { equipmentTiers: [], jewelryTiers: [], glyphALevels: [], glyphSLevels: [], talismanGrades: [], engravingGrades: [], cardGrades: [] } };
const movement = { schema: "movement-ledger-v1", state: "ready", message: "", sourceHash: source,
  sourceFile: replay.sourceFile, durationSeconds: 120, segmentCount: 1, trackCount: 1, pointCount: 2, diagnostics: {},
  segments: [{ key: "test-map", mapId: 1, mapType: 3, mapName: "测试地图", officialName: "测试地图", contentKind: "dragon",
    startSeconds: 0, endSeconds: 120, pointCount: 2, trackCount: 1, bounds: { minX: 0, maxX: 100, minY: 0, maxY: 100 },
    map: { available: false }, tracks: [{ actorId: player.source_id, name: player.name, job: player.job,
      kind: "player", isLocal: true, pointCount: 2, points: [[0, 0, 0], [120, 100, 100]] }] }] };

try {
  browser = await chromium.launch({ executablePath: process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", headless: true });
  page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(10000);
  page.on("pageerror", error => errors.push(error.message));
  await page.route("http://127.0.0.1:48710/**", route => {
    const url = new URL(route.request().url());
    requests.set(url.pathname, (requests.get(url.pathname) || 0) + 1);
    const json = value => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
    switch (url.pathname) {
      case "/api/bootstrap": return json({ version: "4.76", title: "测试", engine: { status: "ready" }, modules: [],
        jobs: { base: ["法师"], first: ["元素师"], second: { "元素师": ["冰灵"] }, firstByBase: { "法师": ["元素师"] } } });
      case "/api/capture/status": case "/api/capture/auto": return json({ state: "stopped", status: "已停止",
        snapshot: url.searchParams.get("includeSnapshot") === "0" ? undefined : replay, npcapInstalled: true, scoreReady: true });
      case "/api/replay/movement": return json(movement);
      case "/api/replay/score-equipment": return json(equipment);
      case "/api/replay/score": return json({ jobId: "test-score" });
      case "/api/replay/upload": return json({ jobId: "test-load" });
      case "/api/job": return json({ done: true, progress: 100, error: "", state: "completed",
        result: url.searchParams.get("id") === "test-load" ? { ...replay, sourceHash: "retained-record-two", sourceFile: "retained-two.dpscap" } : score });
      case "/api/replay/score/detail": return json({ actual_timeline: [{ seconds: 1, end_seconds: 2, skill_name: "冰刺", power: 800 }],
        theory_timeline: [{ seconds: 1, end_seconds: 2, skill_name: "冰刺", power: 1000 }], skill_details: [], support_details: [] });
      case "/api/skill-build": return json({ glyphs: [] });
      default: return json({});
    }
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "实战统计", exact: true }).first().click();
  const tabs = page.locator(".live-tabs");
  const pane = name => page.locator(`[data-live-pane="${name}"]`);
  const select = async name => tabs.getByRole("button", { name, exact: true }).click();
  const remember = async (locator, key) => locator.evaluate((element, name) => {
    (window.retainedTestNodes ||= {})[name] = element;
  }, key);
  const sameNode = async (locator, key) => assert.equal(await locator.evaluate((element, name) => window.retainedTestNodes[name] === element, key), true, `${key} remounted`);
  await pane("damage").waitFor();
  assert.equal(await page.locator("[data-live-pane]").count(), 1, "Unvisited panes must not mount eagerly");
  const damageZoom = pane("damage").getByLabel("缩放", { exact: true });
  await damageZoom.fill("20");
  await remember(pane("damage"), "damage");

  await select("全队技能时间轴");
  const timeline = pane("timeline");
  await timeline.getByLabel("玩家").selectOption(player.source_id);
  await timeline.getByRole("button", { name: "下一页", exact: true }).click();
  await timeline.getByLabel("缩放", { exact: true }).fill("33");
  await timeline.getByRole("button", { name: "隐藏标签", exact: true }).click();
  const scroll = timeline.locator(".native-timeline-scroll");
  const expected = await scroll.evaluate(element => {
    element.scrollLeft = 600; element.dispatchEvent(new Event("scroll")); return element.scrollLeft;
  });
  await remember(timeline, "timeline");
  await select("全场伤害统计");
  await sameNode(pane("damage"), "damage");
  assert.equal(await damageZoom.inputValue(), "20");
  await select("全队技能时间轴");
  await sameNode(timeline, "timeline");
  assert.equal(await timeline.getByLabel("玩家").inputValue(), player.source_id);
  assert.equal(await timeline.getByLabel("缩放", { exact: true }).inputValue(), "33");
  assert.equal(await timeline.getByRole("button", { name: "显示标签", exact: true }).count(), 1);
  assert.match(await timeline.locator(".table-pager").textContent(), /2\s*\/\s*3/);
  await page.waitForFunction(value => Math.abs(document.querySelector('[data-live-pane="timeline"] .native-timeline-scroll').scrollLeft - value) < 2, expected);
  const vertical = await timeline.evaluate(element => {
    const shell = element.closest(".live-native-page"); shell.scrollTop = 450; return shell.scrollTop;
  });
  // DOM click avoids Playwright scrolling the navigation bar into view before
  // the app has a chance to save the previous pane's scroll position.
  await tabs.getByRole("button", { name: "全场伤害统计", exact: true }).evaluate(element => element.click());
  await pane("damage").waitFor();
  await tabs.getByRole("button", { name: "全队技能时间轴", exact: true }).evaluate(element => element.click());
  await timeline.waitFor();
  await page.waitForFunction(value => Math.abs(document.querySelector(".live-native-page").scrollTop - value) < 2, vertical);

  await select("Boss实时状态");
  const status = pane("status");
  await status.getByRole("button", { name: "状态明细", exact: true }).click();
  await status.getByLabel("来源技能").selectOption("冰刺");
  await remember(status, "status");
  await select("实时增益");
  const buffs = pane("buffs");
  await buffs.getByRole("button", { name: "增益明细", exact: true }).click();
  await buffs.getByLabel("目标职业").selectOption("冰灵");
  await remember(buffs, "buffs");
  await select("Boss实时状态");
  await sameNode(status, "status");
  assert.equal(await status.getByLabel("来源技能").inputValue(), "冰刺");
  assert.equal(await status.getByRole("button", { name: "状态明细", exact: true }).getAttribute("class"), "active");
  await select("实时增益");
  await sameNode(buffs, "buffs");
  assert.equal(await buffs.getByLabel("目标职业").inputValue(), "冰灵");
  assert.equal(await buffs.getByRole("button", { name: "增益明细", exact: true }).getAttribute("class"), "active");

  await select("Boss统计");
  const boss = pane("boss");
  await boss.locator(".boss-detail-tabs").getByRole("button", { name: "伤害曲线", exact: true }).click();
  const curve = boss.locator('[data-boss-pane="curve"]');
  await curve.getByLabel("缩放", { exact: true }).fill("18");
  await remember(curve, "boss-curve");
  await boss.locator(".boss-detail-tabs").getByRole("button", { name: "Boss实时状态", exact: true }).click();
  const bossStatus = boss.locator('[data-boss-pane="status"]');
  await bossStatus.getByRole("button", { name: "状态明细", exact: true }).click();
  await remember(bossStatus, "boss-status");
  await boss.locator(".boss-detail-tabs").getByRole("button", { name: "伤害曲线", exact: true }).click();
  await sameNode(curve, "boss-curve");
  assert.equal(await curve.getByLabel("缩放", { exact: true }).inputValue(), "18");
  await select("全场伤害统计");
  await select("Boss统计");
  assert.equal(await curve.isVisible(), true);
  await boss.locator(".boss-detail-tabs").getByRole("button", { name: "Boss实时状态", exact: true }).click();
  await sameNode(bossStatus, "boss-status");
  assert.equal(await bossStatus.getByRole("button", { name: "状态明细", exact: true }).getAttribute("class"), "active");

  await select("队伍轨迹");
  const map = pane("movement");
  await map.getByLabel("速度").selectOption("2");
  await remember(map, "movement");
  const mapRequests = requests.get("/api/replay/movement");
  await map.getByRole("button", { name: "播放", exact: true }).click();
  await select("全场伤害统计");
  await select("队伍轨迹");
  await sameNode(map, "movement");
  assert.equal(await map.getByLabel("速度").inputValue(), "2");
  assert.equal(requests.get("/api/replay/movement"), mapRequests, "Returning to movement must not fetch again");
  assert.equal(await map.getByRole("button", { name: "播放", exact: true }).count(), 1, "Hidden movement must pause");

  await select("评分装备");
  const gear = pane("score-equipment");
  await gear.getByRole("button", { name: "全队统一设置", exact: true }).click();
  await remember(gear, "equipment");
  const gearRequests = requests.get("/api/replay/score-equipment");
  await select("死亡报告");
  await select("评分装备");
  await sameNode(gear, "equipment");
  assert.equal(await gear.locator(".uniform-settings").isVisible(), true);
  assert.equal(requests.get("/api/replay/score-equipment"), gearRequests, "Returning to equipment must not fetch again");

  await select("全队评分");
  const rating = pane("rating");
  await rating.locator(".score-layout").waitFor();
  await rating.getByRole("button", { name: "输出时间轴", exact: true }).click();
  const axis = rating.locator('[data-score-detail-pane="timeline"]');
  await axis.getByLabel("缩放", { exact: true }).fill("30");
  await remember(axis, "score-axis");
  const scoreRequests = requests.get("/api/replay/score");
  const detailRequests = requests.get("/api/replay/score/detail");
  await select("全场伤害统计");
  await select("全队评分");
  await sameNode(axis, "score-axis");
  assert.equal(await axis.getByLabel("缩放", { exact: true }).inputValue(), "30");
  assert.equal(requests.get("/api/replay/score"), scoreRequests, "Tab navigation must not recalculate score");
  assert.equal(requests.get("/api/replay/score/detail"), detailRequests, "Tab navigation must not reload score detail");

  await select("全队技能时间轴");
  await page.getByTestId("replay-file-input").setInputFiles({ name: "retained-two.dpscap", mimeType: "application/octet-stream", buffer: Buffer.from("fixture") });
  await page.locator(".page-heading").getByText("retained-two.dpscap", { exact: true }).waitFor();
  assert.equal(await timeline.getByLabel("玩家").inputValue(), "", "A different record must clear old filters");
  assert.equal(await timeline.getByRole("button", { name: "隐藏标签", exact: true }).count(), 1);
  assert.equal(await pane("damage").count(), 0, "Old record panes must be released");
  assert.equal(await timeline.evaluate(element => window.retainedTestNodes.timeline === element), false);
  assert.deepEqual(errors, []);
  console.log("Live workspace state smoke passed: lazy panes, chart zoom/horizontal scroll, per-pane vertical scroll, filters/paging, nested Boss panes, movement pause/request reuse, equipment state, score reuse and record reset.");
} catch (error) {
  console.error("Fixture browser errors:", errors);
  console.error("Fixture request counts:", Object.fromEntries(requests));
  console.error("Fixture visible headings:", await page.locator("h1:visible,h2:visible,h3:visible").allTextContents());
  throw error;
} finally {
  await browser?.close();
  server.close();
}
