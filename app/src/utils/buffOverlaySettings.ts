import type { BuffOverlaySettings } from "../types/api";
import { SKILL_COUNTDOWN_OPTIONS } from './skillCountdowns.ts';
import {
  captureToggleAllows,
  type CaptureToggleState,
} from './captureToggles.ts';

export const BUFF_OVERLAY_STORAGE_KEY = "dragon-nest-v420-buff-overlay-settings";

export const OVERLAY_WINDOW_LABELS = {
  status: "buff-overlay",
  skill: "skill-countdown-overlay",
} as const;
export const ALL_OVERLAY_WINDOW_LABELS = Object.values(OVERLAY_WINDOW_LABELS);
export const BUFF_OVERLAY_CATALOG_STORAGE_KEY = "dragon-nest-v4521-overlay-catalog-v1";

export type OverlayWindowKind = keyof typeof OVERLAY_WINDOW_LABELS;

/**
 * 当前应该显示哪些悬浮窗。
 *
 * 两层门槛，缺一不可：
 *   1. 首页「总开关」（buffers / boss_actions）——关掉即整类悬浮窗不显示，
 *      但语音播报照旧，所以它只是显示开关，不是采集开关。
 *   2. 悬浮窗设置里的分项（Buff 状态 / Debuff 状态 / Boss 技能倒计时）。
 *
 * `toggles` 省略时读 localStorage 缓存，方便窗口显隐、热键、发布器这些
 * 没有网络上下文的调用点共用同一份判断。
 */
export function enabledOverlayWindowLabels(
  settings: BuffOverlaySettings,
  toggles?: CaptureToggleState,
): string[] {
  const labels: string[] = [];
  const statusEnabled = captureToggleAllows("buffs", toggles)
    && (settings.showBuffOverlay || settings.showDebuffOverlay);
  if (statusEnabled) labels.push(OVERLAY_WINDOW_LABELS.status);
  const skillEnabled = captureToggleAllows("boss_actions", toggles)
    && settings.showSkillCountdownOverlay;
  if (skillEnabled) labels.push(OVERLAY_WINDOW_LABELS.skill);
  return labels;
}

export const DEFAULT_BUFF_OVERLAY_SETTINGS: BuffOverlaySettings = {
  overlaySurfaceVersion: 2,
  selectedBuffs: [],
  buffOrder: [],
  debuffOrder: ["damage", "resistance", "defense", "critical", "freeze"],
  statusOrder: [],
  showBuffOverlay: true,
  showDebuffOverlay: true,
  showSkillCountdownOverlay: true,
  // 分边副本默认只显示自己正在打的那只 Boss 的倒计时；关掉就两边都显示。
  splitBossOnlyCurrent: true,
  selectedSkillCountdowns: SKILL_COUNTDOWN_OPTIONS.map(row => row.key),
  showDamage: true,
  showResistance: true,
  showCritical: true,
  showDefense: true,
  showFreeze: true,
  damageMode: "auto",
  resistanceMode: "auto",
  defenseMode: "auto",
  debuffBreakdownMode: "summary",
  buffLayoutMode: "rows",
  statusLayoutMode: "rows",
  statusIconSize: 42,
  statusIconBackgroundColor: "#eff8f9",
  statusIconBackgroundOpacity: 0,
  statusIconNameColor: "#17323a",
  statusIconTimeColor: "#315963",
  statusIconNameFontSize: 9,
  statusIconTimeFontSize: 9,
  statusIconBackgroundColors: {},
  statusIconNameColors: {},
  statusIconTimeColors: {},
  effectColors: {},
  toneColors: {
    speed: "#40a3dc",
    power: "#e0992a",
    element: "#8f5ecf",
    guard: "#36aa6c",
    critical: "#c95d89",
    freeze: "#299ac5",
    general: "#3eb3b8",
  },
  columnWidth: 320,
  maxRowsPerColumn: 16,
  rowHeight: 28,
  fontSize: 12,
  gap: 3,
  borderRadius: 3,
  backgroundOpacity: 50,
  showProvider: true,
  timeFormat: "subsecond",
};

function bounded(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
}

function normalizedColor(value: unknown, fallback: string): string {
  const color = String(value || "").trim();
  return /^#[0-9a-f]{6}$/i.test(color) ? color.toLowerCase() : fallback;
}

function normalizedColorRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const result: Record<string, string> = {};
  for (const [rawKey, rawColor] of Object.entries(value)) {
    const key = String(rawKey || "").trim();
    const color = String(rawColor || "").trim();
    if (key && /^#[0-9a-f]{6}$/i.test(color)) result[key] = color.toLowerCase();
  }
  return result;
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(row => String(row || "").trim()).filter(Boolean))];
}

export function normalizeBuffOverlaySettings(raw: Partial<BuffOverlaySettings> | null | undefined): BuffOverlaySettings {
  const value = raw || {};
  const legacyValue = value as unknown as Record<string, unknown>;
  const adjustableSurfaceVersion = Number(legacyValue.overlaySurfaceVersion || 0) >= 2;
  const storedDebuffOrder = uniqueStrings(value.debuffOrder).filter(
    (row): row is BuffOverlaySettings["debuffOrder"][number] =>
      DEFAULT_BUFF_OVERLAY_SETTINGS.debuffOrder.includes(row as BuffOverlaySettings["debuffOrder"][number]),
  );
  const storedBuffOrder = uniqueStrings(value.buffOrder);
  const storedStatusOrder = Array.isArray(value.statusOrder)
    ? uniqueStrings(value.statusOrder)
    : storedBuffOrder.length
      ? [
          ...storedBuffOrder.map(key => `buff:${key}`),
          ...storedDebuffOrder.map(key => `debuff:${key}`),
        ]
      : [];
  const normalized: BuffOverlaySettings = {
    ...DEFAULT_BUFF_OVERLAY_SETTINGS,
    ...value,
    overlaySurfaceVersion: 2,
    selectedBuffs: uniqueStrings(value.selectedBuffs),
    buffOrder: storedBuffOrder,
    debuffOrder: [...storedDebuffOrder, ...DEFAULT_BUFF_OVERLAY_SETTINGS.debuffOrder.filter(row => !storedDebuffOrder.includes(row))],
    statusOrder: storedStatusOrder,
    toneColors: { ...DEFAULT_BUFF_OVERLAY_SETTINGS.toneColors, ...(value.toneColors || {}) },
    showBuffOverlay: value.showBuffOverlay !== false,
    showDebuffOverlay: value.showDebuffOverlay !== false,
    showSkillCountdownOverlay: value.showSkillCountdownOverlay !== false,
    splitBossOnlyCurrent: value.splitBossOnlyCurrent !== false,
    selectedSkillCountdowns: Array.isArray(value.selectedSkillCountdowns)
      ? uniqueStrings(value.selectedSkillCountdowns).filter(key => SKILL_COUNTDOWN_OPTIONS.some(row => row.key === key))
      : SKILL_COUNTDOWN_OPTIONS.map(row => row.key),
    debuffBreakdownMode: value.debuffBreakdownMode === "skills"
      ? "skills"
      : value.debuffBreakdownMode === "summary_and_skills"
        ? "summary_and_skills"
        : "summary",
    buffLayoutMode: value.buffLayoutMode === "icons" ? "icons" : "rows",
    statusLayoutMode: value.statusLayoutMode === "icons" ? "icons" : "rows",
    statusIconSize: Math.round(bounded(value.statusIconSize, 42, 28, 64)),
    statusIconBackgroundColor: normalizedColor(value.statusIconBackgroundColor, "#eff8f9"),
    statusIconBackgroundOpacity: Math.round(bounded(value.statusIconBackgroundOpacity, 0, 0, 100)),
    statusIconNameColor: normalizedColor(value.statusIconNameColor, "#17323a"),
    statusIconTimeColor: normalizedColor(value.statusIconTimeColor, "#315963"),
    statusIconNameFontSize: Math.round(bounded(value.statusIconNameFontSize, 9, 8, 18)),
    statusIconTimeFontSize: Math.round(bounded(value.statusIconTimeFontSize, 9, 8, 18)),
    statusIconBackgroundColors: normalizedColorRecord(value.statusIconBackgroundColors),
    statusIconNameColors: normalizedColorRecord(value.statusIconNameColors),
    statusIconTimeColors: normalizedColorRecord(value.statusIconTimeColors),
    effectColors: { ...(value.effectColors || {}) },
    columnWidth: bounded(value.columnWidth, 320, 260, 520),
    maxRowsPerColumn: Math.round(bounded(value.maxRowsPerColumn, 16, 4, 24)),
    rowHeight: Math.round(bounded(value.rowHeight, 28, 22, 42)),
    fontSize: Math.round(bounded(value.fontSize, 12, 10, 18)),
    gap: Math.round(bounded(value.gap, 3, 0, 10)),
    borderRadius: Math.round(bounded(value.borderRadius, 3, 0, 12)),
    // V4.50 initially forced transparent surfaces. V2 introduces a user-
    // adjustable default of 50%; once saved, every later adjustment is kept.
    backgroundOpacity: adjustableSurfaceVersion
      ? Math.round(bounded(value.backgroundOpacity, 50, 0, 100))
      : 50,
    showProvider: value.showProvider !== false,
    // Legacy "auto"/"integer" settings migrate to the compact format.
    timeFormat: String(value.timeFormat) === "decimal" ? "decimal" : "subsecond",
  };
  // Removed modules (Boss HP, teammate panels, live score, effect/DPS curves,
  // key-Buff CD, alerts and summons) must not survive import/export through the
  // permissive object spread above. Only the Buff/Debuff status and the Boss
  // skill countdown windows remain.
  const legacy = normalized as unknown as Record<string, unknown>;
  for (const key of [
    "showBossHealthOverlay", "showTeammateInfoOverlay",
    "hidePersonalScoreHistoryOverlay", "hidePersonalScoreHistorySoftware",
    "showLiveScoreOverlay",
    "teammateInfoBackgroundOpacity", "teammateHistoryBackgroundOpacity",
    "showAlertOverlay", "showDebuffRefreshAlert", "reminderLeadSeconds", "missingBuffRules",
    "showEffectCurveOverlay", "showPersonalDpsOverlay", "showCooldownOverlay", "showSummonOverlay",
    "showMissingBuffAlert", "showClearedBuffAlert", "showLowHealthAlert",
    "selectedEffectCurves", "selectedDpsPlayers", "selectedCooldownBuffs", "selectedMissingBuffs",
    "selectedSummons", "summonSelectionMode", "summonOrder", "summonScope",
    "missingBuffScope", "showTeamMissingBuffs", "lowHealthThreshold",
    "curveHistorySeconds", "dpsWindowSeconds",
    "curveColumnWidth", "curveChartHeight", "curveFontSize", "curveGap", "curveBorderRadius",
    "curveBackgroundOpacity", "alertColumnWidth", "alertMaxRowsPerColumn", "alertRowHeight",
    "alertFontSize", "alertGap", "alertBorderRadius", "alertBackgroundOpacity",
    "overlayPerformanceMode",
  ]) delete legacy[key];
  return normalized;
}

export function readBuffOverlaySettings(): BuffOverlaySettings {
  try {
    return normalizeBuffOverlaySettings(JSON.parse(localStorage.getItem(BUFF_OVERLAY_STORAGE_KEY) || "{}"));
  } catch {
    return normalizeBuffOverlaySettings({});
  }
}
