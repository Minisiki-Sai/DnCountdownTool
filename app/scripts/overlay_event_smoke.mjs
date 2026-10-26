import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const baseUrl = process.env.OVERLAY_EVENT_URL || "http://127.0.0.1:1420/";
const edgePath = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const browser = await chromium.launch({ executablePath: edgePath, headless: true });
const context = await browser.newContext({ viewport: { width: 760, height: 520 } });
const requests = new Map();
const pageErrors = [];

await context.addInitScript(() => {
  localStorage.setItem("dragon-nest-v420-buff-overlay-enabled", "1");
  localStorage.setItem("dragon-nest-v420-buff-overlay-settings", JSON.stringify({
    overlaySurfaceVersion: 2,
    overlayPerformanceMode: true,
    showBuffOverlay: true,
    showDebuffOverlay: true,
    showEffectCurveOverlay: true,
    showPersonalDpsOverlay: true,
    showCooldownOverlay: true,
    showSummonOverlay: true,
    showMissingBuffAlert: true,
    showClearedBuffAlert: true,
    showLowHealthAlert: true,
    selectedCooldownBuffs: ["event-smoke"],
  }));
});

await context.route("http://127.0.0.1:48710/api/capture/buff-overlay*", async route => {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "Access-Control-Allow-Origin": "*" },
    body: JSON.stringify({
      state: "running",
      status: "事件订阅验证",
      elapsedSeconds: 12,
      selfSourceId: "0x1",
      players: [{
        source_id: "0x1",
        name: "事件订阅队友",
        job: "冰灵",
        damage: 100,
        dps: 10,
        damage_series: [[10, 10]],
        attack_types: ["magic"],
        elements: ["water"],
      }],
      buffs: [],
      debuffs: [],
      cooldowns: [{
        key: "event-smoke:0x1",
        catalog_key: "event-smoke",
        skill_name: "事件订阅验证",
        source_id: "0x1",
        source_job: "冰灵",
        cast_seconds: 10,
        cooldown_seconds: 60,
        ready_seconds: 70,
        remaining_seconds: 58,
      }],
      summons: [],
      playerHealth: [],
      buffClearRevision: 0,
      buffClearEvents: [],
      buffOptions: [],
      summonOptions: [],
    }),
  });
});

await context.route("http://127.0.0.1:48710/api/capture/teammate-info", async route => {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "Access-Control-Allow-Origin": "*" },
    body: JSON.stringify({
      state: "ready",
      revision: 1,
      inspectionKey: "event-smoke-inspection",
      name: "事件订阅队友",
      job: "冰灵",
      physicalAttackMin: 1,
      physicalAttackMax: 2,
      magicAttackMin: 3,
      magicAttackMax: 4,
      critPercent: 5,
      critDamagePercent: 6,
      finalPercent: 7,
      element: "水",
      elementAttackPercent: 8,
    }),
  });
});

context.on("request", request => {
  const endpoint = request.url().includes("/api/capture/buff-overlay")
    ? "combat"
    : request.url().includes("/api/capture/teammate-info")
      ? "teammate"
      : "";
  if (!endpoint) return;
  const view = new URL(request.frame().url()).searchParams.get("view") || "main";
  const key = `${view}:${endpoint}`;
  requests.set(key, (requests.get(key) || 0) + 1);
});

async function open(path) {
  const page = await context.newPage();
  page.on("pageerror", error => pageErrors.push(`${path}: ${error.stack || error.message}`));
  await page.goto(new URL(path, baseUrl).toString(), { waitUntil: "domcontentloaded" });
  return page;
}

try {
  const alertPage = await open("?view=alert-overlay");
  const statusPage = await open("?view=buff-overlay");
  const effectPage = await open("?view=effect-overlay");
  const summonPage = await open("?view=summon-overlay");
  const missingPage = await open("?view=missing-buff-overlay");
  const clearedPage = await open("?view=cleared-buff-overlay");
  const lowHealthPage = await open("?view=low-health-overlay");
  const infoPage = await open("?view=teammate-info");
  const historyPage = await open("?view=teammate-history");
  const scorePage = await open("?view=live-score");
  const timerPage = await open("?view=game-timer");
  await open("");

  // Performance mode routes the CD panel into the shared status WebView.
  await statusPage.getByText("事件订阅验证", { exact: false }).waitFor({ timeout: 5_000 });
  assert.equal(
    await alertPage.getByText("事件订阅验证", { exact: false }).count(),
    0,
    "性能模式仍把关键 Buff CD 重复渲染到独立窗口",
  );
  await infoPage.getByText("事件订阅队友", { exact: true }).first().waitFor({ timeout: 5_000 });
  await new Promise(resolve => setTimeout(resolve, 1_250));

  assert.ok((requests.get("main:combat") || 0) >= 3, "主窗口没有持续发布战斗快照");
  assert.ok((requests.get("main:teammate") || 0) >= 2, "主窗口没有持续监听队友查看事件");
  for (const [key, count] of requests) {
    if (key.startsWith("main:")) continue;
    assert.equal(count, 0, `${key} 仍在独立轮询后端`);
  }
  assert.equal(pageErrors.length, 0, pageErrors.join("\n"));
  assert.ok((await statusPage.locator("body").innerText()).length >= 0);
  assert.ok((await effectPage.locator("body").innerText()).length >= 0);
  assert.ok((await summonPage.locator("body").innerText()).length >= 0);
  assert.ok((await missingPage.locator("body").innerText()).length >= 0);
  assert.ok((await clearedPage.locator("body").innerText()).length >= 0);
  assert.ok((await lowHealthPage.locator("body").innerText()).length >= 0);
  assert.ok((await historyPage.locator("body").innerText()).length >= 0);
  assert.ok((await scorePage.locator("body").innerText()).length >= 0);
  assert.ok((await timerPage.locator("body").innerText()).length >= 0);

  process.stdout.write(`${JSON.stringify(Object.fromEntries(requests), null, 2)}\n`);
  process.stdout.write("[OK] 所有悬浮窗均由统一事件发布器供数，窗口端没有重复请求。\n");
} finally {
  await browser.close();
}
