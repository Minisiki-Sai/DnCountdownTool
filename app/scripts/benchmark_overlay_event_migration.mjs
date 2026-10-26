import { chromium } from "playwright-core";

const baseUrl = process.argv[2] || "http://127.0.0.1:1420/";
const waitMs = Math.max(1_000, Number(process.argv[3] || 4_000));
const edgePath = process.env.EDGE_PATH
  || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

const browser = await chromium.launch({ executablePath: edgePath, headless: true });
const context = await browser.newContext({ viewport: { width: 760, height: 520 } });
const requests = new Map();
const pageErrors = [];

await context.addInitScript(() => {
  localStorage.setItem("dragon-nest-v420-buff-overlay-enabled", "1");
  localStorage.setItem("dragon-nest-v420-buff-overlay-settings", JSON.stringify({
    overlaySurfaceVersion: 2,
    showBuffOverlay: true,
    showDebuffOverlay: true,
    showEffectCurveOverlay: true,
    showPersonalDpsOverlay: true,
    showLiveScoreOverlay: true,
    showCooldownOverlay: true,
    showSummonOverlay: true,
    showMissingBuffAlert: true,
    showClearedBuffAlert: true,
    showLowHealthAlert: true,
    selectedCooldownBuffs: ["migration-benchmark"],
  }));
});

await context.route("http://127.0.0.1:48710/api/capture/buff-overlay*", async route => {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { "Access-Control-Allow-Origin": "*" },
    body: JSON.stringify({
      state: "running",
      status: "事件订阅性能实测",
      elapsedSeconds: 12,
      selfSourceId: "0x1",
      players: [{
        source_id: "0x1",
        name: "测试角色",
        job: "冰灵",
        damage: 100,
        dps: 10,
        damage_series: [[10, 10]],
        attack_types: ["magic"],
        elements: ["water"],
      }],
      buffs: [],
      debuffs: [],
      cooldowns: [],
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
      inspectionKey: "migration-benchmark",
      name: "测试角色",
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
  const frameUrl = request.frame().url();
  const view = frameUrl ? (new URL(frameUrl).searchParams.get("view") || "main") : "unknown";
  const key = `${view}:${endpoint}`;
  requests.set(key, (requests.get(key) || 0) + 1);
});

const views = [
  "buff-overlay",
  "effect-overlay",
  "alert-overlay",
  "summon-overlay",
  "missing-buff-overlay",
  "cleared-buff-overlay",
  "low-health-overlay",
  "teammate-info",
  "teammate-history",
  "live-score",
  "",
];

try {
  for (const view of views) {
    const page = await context.newPage();
    page.on("pageerror", error => {
      pageErrors.push(`${view || "main"}: ${error.stack || error.message}`);
    });
    const target = view ? `?view=${view}` : "";
    await page.goto(new URL(target, baseUrl).toString(), { waitUntil: "domcontentloaded" });
  }
  // Ignore startup races and measure a stable interval.
  await new Promise(resolve => setTimeout(resolve, 1_000));
  requests.clear();
  await new Promise(resolve => setTimeout(resolve, waitMs));

  const rows = Object.fromEntries([...requests.entries()].sort(([left], [right]) => left.localeCompare(right)));
  const combat = [...requests]
    .filter(([key]) => key.endsWith(":combat"))
    .reduce((total, [, count]) => total + count, 0);
  const teammate = [...requests]
    .filter(([key]) => key.endsWith(":teammate"))
    .reduce((total, [, count]) => total + count, 0);
  process.stdout.write(`${JSON.stringify({
    baseUrl,
    measuredSeconds: waitMs / 1_000,
    requests: rows,
    totals: {
      combat,
      teammate,
      all: combat + teammate,
      requestsPerSecond: (combat + teammate) / (waitMs / 1_000),
    },
    pageErrors,
  }, null, 2)}\n`);
} finally {
  await browser.close();
}
