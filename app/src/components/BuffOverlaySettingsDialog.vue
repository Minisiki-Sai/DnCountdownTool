<script setup lang="ts">
import SkillCountdownPanel from './SkillCountdownPanel.vue';
import { computed, reactive, ref, watch } from "vue";
import { ChevronDown, ChevronUp, Download, GripVertical, RotateCcw, Upload, X } from "@lucide/vue";
import type { BuffOverlaySettings, BuffOverlayStatus } from "../types/api";
import { APP_VERSION_LABEL } from "../appVersion";
import { SKILL_COUNTDOWN_NESTS, SKILL_COUNTDOWN_OPTIONS } from '../utils/skillCountdowns';
import { selectStatusPreviewRows } from "../utils/overlayStatusPreview";
import {
  DEFAULT_BUFF_OVERLAY_SETTINGS,
  normalizeBuffOverlaySettings,
} from "../utils/buffOverlaySettings";

type OverlayTone = keyof BuffOverlaySettings["toneColors"];
type PreviewRow = {
  key: string;
  name: string;
  provider: string;
  detail: string;
  tone: OverlayTone;
  progress: number;
  child?: boolean;
  expandable?: boolean;
  expanded?: boolean;
  iconIndex?: number;
  compactEffect?: boolean;
};
type SettingsPage = "status" | "skills";
type DebuffOrderKey = BuffOverlaySettings["debuffOrder"][number];
type StatusSortRow = { key: string; kind: "buff" | "debuff"; label: string; provider: string };
type StatusIconColorKind = "background" | "name" | "time";

const SKILL_ICON_SHEET_WIDTH = 512;
const SKILL_ICON_CELL_SIZE = 50;
const STATUS_ICON_HORIZONTAL_PADDING = 20;

const props = defineProps<{
  open: boolean;
  options: BuffOverlayStatus["buffOptions"];
  modelValue: BuffOverlaySettings;
}>();
const emit = defineEmits<{ close: []; save: [value: BuffOverlaySettings] }>();
const draft = reactive<BuffOverlaySettings>(normalizeBuffOverlaySettings(props.modelValue));
const buffSearch = ref("");
const importInput = ref<HTMLInputElement | null>(null);
const transferMessage = ref("");
const transferFailed = ref(false);
const activePage = ref<SettingsPage>("status");
function toggleCountdownGroup(keys: string[], checked: boolean) {
  draft.selectedSkillCountdowns = checked ? [...new Set([...draft.selectedSkillCountdowns, ...keys])]
    : draft.selectedSkillCountdowns.filter(key => !keys.includes(key));
}
const countdownPreview = computed(() => SKILL_COUNTDOWN_OPTIONS.filter(row => draft.selectedSkillCountdowns.includes(row.key)).slice(0, 2));
const dragging = ref<string | null>(null);
const expandedPreviewDebuffs = ref(new Set<string>());
const SETTINGS_FILE_FORMAT = "dragon-nest-v420-buff-overlay-settings";
const toneMeta: Array<{ key: OverlayTone; label: string }> = [
  { key: "speed", label: "加速/冷却" }, { key: "power", label: "攻击/增伤" },
  { key: "element", label: "属性/减抗" }, { key: "guard", label: "防御/治疗" },
  { key: "critical", label: "致命" }, { key: "freeze", label: "叠冰" },
  { key: "general", label: "其他" },
];
const debuffMeta: Array<{ key: DebuffOrderKey; label: string }> = [
  { key: "damage", label: "增伤" },
  { key: "resistance", label: "减抗" },
  { key: "defense", label: "减防御" },
  { key: "critical", label: "减致命抗" },
  { key: "freeze", label: "叠冰层数" },
];
const debuffLabels = new Map(debuffMeta.map(row => [row.key, row.label]));
const previewIconBySkill = new Map<string, number>([
  ["勇敢", 19],
  ["体能暴涨", 98],
  ["旋风斩EX", 88],
]);

function replaceDraft(value: BuffOverlaySettings) {
  const normalized = normalizeBuffOverlaySettings(value);
  Object.assign(draft, normalized, {
    selectedBuffs: [...normalized.selectedBuffs],
    buffOrder: [...normalized.buffOrder], debuffOrder: [...normalized.debuffOrder], statusOrder: [...normalized.statusOrder],
    effectColors: { ...normalized.effectColors }, toneColors: { ...normalized.toneColors },
    statusIconBackgroundColors: { ...normalized.statusIconBackgroundColors },
    statusIconNameColors: { ...normalized.statusIconNameColors },
    statusIconTimeColors: { ...normalized.statusIconTimeColors },
  });
}
watch(() => props.open, open => {
  if (open) {
    replaceDraft(props.modelValue);
    activePage.value = "status";
    transferMessage.value = "";
    transferFailed.value = false;
  }
});

const visibleOptions = computed(() => {
  const query = buffSearch.value.trim().toLocaleLowerCase("zh-CN");
  if (!query) return props.options;
  return props.options.filter(row => [row.skill, row.label, row.category, row.provider]
    .some(value => String(value || "").toLocaleLowerCase("zh-CN").includes(query)));
});
const teamOptions = computed(() => visibleOptions.value.filter(row => row.scope === "team")
  .sort((a, b) => a.providerOrder - b.providerOrder || a.displayOrder - b.displayOrder || a.skill.localeCompare(b.skill, "zh-CN")));
const personalOptions = computed(() => visibleOptions.value.filter(row => row.scope !== "team")
  .sort((a, b) => a.providerOrder - b.providerOrder || a.displayOrder - b.displayOrder || a.skill.localeCompare(b.skill, "zh-CN")));

const uniqueBuffOptions = computed(() => {
  const byKey = new Map<string, BuffOverlayStatus["buffOptions"][number]>();
  for (const row of props.options) if (row.key && !byKey.has(row.key)) byKey.set(row.key, row);
  return [...byKey.values()].sort((a, b) => a.providerOrder - b.providerOrder || a.displayOrder - b.displayOrder || a.skill.localeCompare(b.skill, "zh-CN"));
});
function completeBuffOrder(): string[] {
  const defaults = props.options.map(row => row.key);
  const available = new Set(defaults);
  const unified = draft.statusOrder
    .filter(key => key.startsWith("buff:"))
    .map(key => key.slice("buff:".length));
  const stored = [...new Set([...unified, ...draft.buffOrder])].filter(key => available.has(key));
  return [...stored, ...defaults.filter(key => !stored.includes(key))];
}
const orderedSelectedBuffOptions = computed(() => {
  const byKey = new Map(props.options.map(row => [row.key, row]));
  const enabled = draft.selectedBuffs.length === 0
    ? new Set(props.options.map(row => row.key))
    : new Set(draft.selectedBuffs);
  return completeBuffOrder().filter(key => enabled.has(key)).map(key => byKey.get(key)).filter(row => row !== undefined);
});
const visibleIconColorOptions = computed(() => {
  const visible = new Set(visibleOptions.value.map(row => row.key));
  return orderedSelectedBuffOptions.value.filter(row => visible.has(row.key));
});
function debuffEnabled(key: DebuffOrderKey): boolean {
  return key === "damage" ? draft.showDamage
    : key === "resistance" ? draft.showResistance
      : key === "defense" ? draft.showDefense
        : key === "critical" ? draft.showCritical : draft.showFreeze;
}
const enabledDebuffOrder = computed(() => draft.debuffOrder.filter(debuffEnabled));
function debuffLabel(key: DebuffOrderKey): string { return debuffLabels.get(key) || key; }
const availableStatusRows = computed<StatusSortRow[]>(() => [
  ...orderedSelectedBuffOptions.value.map(row => ({
    key: `buff:${row.key}`,
    kind: "buff" as const,
    label: row.skill,
    provider: row.provider || "职业待确认",
  })),
  ...enabledDebuffOrder.value.map(key => ({
    key: `debuff:${key}`,
    kind: "debuff" as const,
    label: debuffLabel(key),
    provider: "Debuff",
  })),
]);
function completeStatusOrder(): string[] {
  const defaults = availableStatusRows.value.map(row => row.key);
  const available = new Set(defaults);
  const stored = draft.statusOrder.filter(key => available.has(key));
  return [...stored, ...defaults.filter(key => !stored.includes(key))];
}
const orderedStatusRows = computed(() => {
  const byKey = new Map(availableStatusRows.value.map(row => [row.key, row]));
  return completeStatusOrder().map(key => byKey.get(key)).filter(row => row !== undefined);
});
function saveStatusOrder(keys: string[]) {
  draft.statusOrder = keys;
  draft.buffOrder = keys.filter(key => key.startsWith("buff:")).map(key => key.slice("buff:".length));
  const debuffs = keys.filter(key => key.startsWith("debuff:")).map(key => key.slice("debuff:".length)) as DebuffOrderKey[];
  draft.debuffOrder = [...debuffs, ...DEFAULT_BUFF_OVERLAY_SETTINGS.debuffOrder.filter(key => !debuffs.includes(key))];
}
function moveSortItem(key: string, direction: -1 | 1) {
  const order = completeStatusOrder();
  const current = order.indexOf(key);
  const neighbor = order[current + direction];
  if (current < 0 || !neighbor) return;
  [order[current], order[current + direction]] = [order[current + direction], order[current]];
  saveStatusOrder(order);
}
function beginSortDrag(key: string, event: DragEvent) {
  dragging.value = key;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", key);
  }
}
function dropSortItem(targetKey: string) {
  const source = dragging.value;
  dragging.value = null;
  if (!source || source === targetKey) return;
  const order = completeStatusOrder();
  const sourceIndex = order.indexOf(source);
  if (sourceIndex < 0 || !order.includes(targetKey)) return;
  order.splice(sourceIndex, 1);
  order.splice(order.indexOf(targetKey), 0, source);
  saveStatusOrder(order);
}

function selected(key: string) { return draft.selectedBuffs.length === 0 || draft.selectedBuffs.includes(key); }
function toggleBuff(key: string, enabled: boolean) {
  if (draft.selectedBuffs.length === 0) draft.selectedBuffs = completeBuffOrder();
  const values = new Set(draft.selectedBuffs); if (enabled) values.add(key); else values.delete(key);
  draft.selectedBuffs = completeBuffOrder().filter(row => values.has(row));
}
function onBuffToggle(key: string, event: Event) { toggleBuff(key, (event.target as HTMLInputElement).checked); }
function selectAll() { draft.selectedBuffs = []; }
function clearAll() { draft.selectedBuffs = ["__none__"]; }
function setEffectColor(key: string, event: Event) { draft.effectColors[key] = (event.target as HTMLInputElement).value; }
function clearEffectColor(key: string) { delete draft.effectColors[key]; }
function statusIconColorMap(kind: StatusIconColorKind): Record<string, string> {
  return kind === "background" ? draft.statusIconBackgroundColors
    : kind === "name" ? draft.statusIconNameColors
      : draft.statusIconTimeColors;
}
function statusIconDefaultColor(kind: StatusIconColorKind): string {
  return kind === "background" ? draft.statusIconBackgroundColor
    : kind === "name" ? draft.statusIconNameColor
      : draft.statusIconTimeColor;
}
function statusIconSkillColor(kind: StatusIconColorKind, key: string): string {
  return statusIconColorMap(kind)[key] || statusIconDefaultColor(kind);
}
function setStatusIconSkillColor(kind: StatusIconColorKind, key: string, event: Event) {
  statusIconColorMap(kind)[key] = (event.target as HTMLInputElement).value;
}
function clearStatusIconSkillColors(key: string) {
  delete draft.statusIconBackgroundColors[key];
  delete draft.statusIconNameColors[key];
  delete draft.statusIconTimeColors[key];
}
function hasStatusIconSkillColors(key: string): boolean {
  return Boolean(
    draft.statusIconBackgroundColors[key]
    || draft.statusIconNameColors[key]
    || draft.statusIconTimeColors[key]
  );
}
function resetAppearance() {
  const d = DEFAULT_BUFF_OVERLAY_SETTINGS;
  Object.assign(draft, {
    columnWidth: d.columnWidth, maxRowsPerColumn: d.maxRowsPerColumn, rowHeight: d.rowHeight, fontSize: d.fontSize,
    gap: d.gap, borderRadius: d.borderRadius, backgroundOpacity: d.backgroundOpacity, showProvider: d.showProvider,
    buffLayoutMode: d.buffLayoutMode, statusLayoutMode: d.statusLayoutMode,
    statusIconSize: d.statusIconSize, statusIconBackgroundColor: d.statusIconBackgroundColor,
    statusIconBackgroundOpacity: d.statusIconBackgroundOpacity,
    statusIconNameColor: d.statusIconNameColor, statusIconTimeColor: d.statusIconTimeColor,
    statusIconNameFontSize: d.statusIconNameFontSize, statusIconTimeFontSize: d.statusIconTimeFontSize,
    statusIconBackgroundColors: {}, statusIconNameColors: {}, statusIconTimeColors: {},
    timeFormat: d.timeFormat, effectColors: {}, toneColors: { ...d.toneColors },
  });
}
function rowTone(kind: string, name: string): OverlayTone {
  const value = `${kind} ${name}`;
  if (/时间加速|精神爆发|动作速度|冷却/.test(value)) return "speed";
  if (/增伤|攻击|力量|敏捷|智力|四维/.test(value)) return "power";
  if (/减抗|火|水|冰|光|暗|属性/.test(value)) return "element";
  if (/防御|护盾|减伤|生命|治疗/.test(value)) return "guard";
  if (/致命/.test(value)) return "critical";
  if (/叠冰/.test(value)) return "freeze";
  return "general";
}
function previewTime(value: number): string {
  if (draft.timeFormat === "decimal") return `${value.toFixed(1)}s`;
  return value < 1 ? `${value.toFixed(1)}s` : `${Math.ceil(value)}s`;
}
function togglePreviewDebuff(key: string) {
  if (draft.debuffBreakdownMode !== "summary") return;
  const next = new Set(expandedPreviewDebuffs.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expandedPreviewDebuffs.value = next;
}
const previewRows = computed<PreviewRow[]>(() => {
  const debuffPreviews: Record<DebuffOrderKey, PreviewRow> = {
    damage: { key: "debuff:damage:physical", name: "物理增伤 40%", provider: "", detail: `${previewTime(6.8)} → 20%`, tone: "power", progress: 62, iconIndex: -1, compactEffect: false },
    resistance: { key: "debuff:resistance:dark", name: "减暗抗 30%", provider: "", detail: `${previewTime(9.2)} → 15%`, tone: "element", progress: 81, iconIndex: -1, compactEffect: false },
    defense: { key: "debuff:defense:physical", name: "减物防 20%", provider: "", detail: previewTime(7.4), tone: "guard", progress: 69, iconIndex: -1 },
    critical: { key: "debuff:critical:critical", name: "减致命抗 30%", provider: "", detail: previewTime(5.6), tone: "critical", progress: 58, iconIndex: -1 },
    freeze: { key: "debuff:freeze:freeze", name: "叠冰 2层", provider: "", detail: previewTime(.7), tone: "freeze", progress: 48, iconIndex: -1 },
  };
  const contributorPreviews: Record<DebuffOrderKey, PreviewRow[]> = {
    damage: [
      { key: "debuff-skill:damage:重锤", name: "重锤 25%", provider: "剑皇", detail: previewTime(6.8), tone: "power", progress: 62, child: true, iconIndex: 49, compactEffect: true },
      { key: "debuff-skill:damage:破甲", name: "破甲 15%", provider: "圣骑士", detail: previewTime(11.4), tone: "power", progress: 84, child: true, iconIndex: 630, compactEffect: true },
    ],
    resistance: [
      { key: "debuff-skill:resistance:灵魂之门", name: "灵魂之门 20%", provider: "噬魂者", detail: previewTime(9.2), tone: "element", progress: 81, child: true, iconIndex: 1634, compactEffect: true },
      { key: "debuff-skill:resistance:憎恶之境", name: "憎恶之境 10%", provider: "黑暗萨满", detail: previewTime(13.7), tone: "element", progress: 93, child: true, iconIndex: 1625, compactEffect: true },
    ],
    defense: [
      { key: "debuff-skill:defense:破甲", name: "破甲 20%", provider: "圣骑士", detail: previewTime(7.4), tone: "guard", progress: 69, child: true, iconIndex: 630 },
    ],
    critical: [
      { key: "debuff-skill:critical:精神爆破", name: "精神爆破 30%", provider: "雷神", detail: previewTime(5.6), tone: "critical", progress: 58, child: true, iconIndex: 627 },
    ],
    freeze: [
      { key: "debuff-skill:freeze:冰刃", name: "冰刃 1层", provider: "冰灵", detail: previewTime(.7), tone: "freeze", progress: 48, child: true, iconIndex: 420 },
      { key: "debuff-skill:freeze:冰刺", name: "冰刺 1层", provider: "冰灵", detail: previewTime(4.1), tone: "freeze", progress: 73, child: true, iconIndex: 437 },
    ],
  };
  const optionByStatusKey = new Map(orderedSelectedBuffOptions.value.map(row => [`buff:${row.key}`, row]));
  // 预览必须和真实悬浮窗同一套分项开关：关掉 Debuff 后，这里也不能再出现
  // 物理增伤/减抗这些示例行，否则用户会以为开关只影响预览以外的地方。
  const previewStatusRows = selectStatusPreviewRows(
    orderedStatusRows.value,
    draft.showBuffOverlay,
    draft.showDebuffOverlay,
  );
  return previewStatusRows.flatMap((sortRow, index) => {
    if (sortRow.kind === "debuff") {
      const debuffKey = sortRow.key.slice("debuff:".length) as DebuffOrderKey;
      const summary = {
        ...debuffPreviews[debuffKey],
        expandable: draft.debuffBreakdownMode === "summary",
        expanded: expandedPreviewDebuffs.value.has(debuffKey),
      };
      const contributors = contributorPreviews[debuffKey];
      if (draft.debuffBreakdownMode === "skills") return contributors;
      if (draft.debuffBreakdownMode === "summary_and_skills" || summary.expanded) return [summary, ...contributors];
      return [summary];
    }
    const row = optionByStatusKey.get(sortRow.key)!;
    return [{
      key: row.key, name: row.skill, provider: row.provider,
      detail: previewTime(index === 0 ? .8 : index % 2 ? 8.4 : 12),
      tone: rowTone(row.category, row.skill), progress: Math.max(30, 76 - index * 8),
      iconIndex: row.iconIndex ?? previewIconBySkill.get(row.skill) ?? -1,
      compactEffect: draft.buffLayoutMode === "icons",
    }];
  }).filter(row => row !== undefined).slice(0, 11);
});
function previewRowUsesIcon(row: PreviewRow): boolean {
  return Boolean(row.compactEffect && (!row.child || draft.statusLayoutMode === "icons"));
}
function previewStatusIconCardWidth(): number {
  return Math.max(54, draft.statusIconSize + STATUS_ICON_HORIZONTAL_PADDING);
}
function previewStatusIconCardHeight(): number {
  return draft.statusIconSize + 4
    + (draft.statusIconNameFontSize + 1) * 2
    + draft.statusIconTimeFontSize + 2;
}
const previewIconRows = computed(() => previewRows.value.filter(previewRowUsesIcon));
const previewStandardRows = computed(() => previewRows.value.filter(row => !previewRowUsesIcon(row)));
const previewStyle = computed(() => ({
  width: previewIconRows.value.length
    ? `${Math.min(720, Math.max(
      previewStandardRows.value.length ? draft.columnWidth : 0,
      22 + previewIconRows.value.length * previewStatusIconCardWidth() + Math.max(0, previewIconRows.value.length - 1) * draft.gap,
    ))}px`
    : `${Math.min(520, draft.columnWidth)}px`, "--preview-opacity": String(draft.backgroundOpacity / 100),
  "--preview-row-opacity": String(draft.backgroundOpacity / 100 * 0.86),
  "--preview-handle-opacity": String(draft.backgroundOpacity / 100 * 0.68),
  "--preview-border-opacity": String(0.08 + draft.backgroundOpacity / 100 * 0.24),
  "--preview-shadow-opacity": String(draft.backgroundOpacity / 100 * 0.14),
  "--preview-row-height": `${draft.rowHeight}px`, "--preview-font-size": `${draft.fontSize}px`,
  "--preview-detail-size": `${Math.max(9, draft.fontSize - 1)}px`, "--preview-gap": `${draft.gap}px`,
  "--preview-radius": `${draft.borderRadius}px`,
  "--preview-icon-size": `${draft.statusIconSize}px`,
  "--preview-icon-card-width": `${previewStatusIconCardWidth()}px`,
  "--preview-icon-card-height": `${previewStatusIconCardHeight()}px`,
  "--preview-icon-background-color": draft.statusIconBackgroundColor,
  "--preview-icon-background-opacity": `${draft.statusIconBackgroundOpacity}%`,
  "--preview-icon-name-color": draft.statusIconNameColor,
  "--preview-icon-time-color": draft.statusIconTimeColor,
  "--preview-icon-name-size": `${draft.statusIconNameFontSize}px`,
  "--preview-icon-name-line-height": `${draft.statusIconNameFontSize + 1}px`,
  "--preview-icon-time-size": `${draft.statusIconTimeFontSize}px`,
  "--preview-icon-time-line-height": `${draft.statusIconTimeFontSize + 2}px`,
}));
const previewHeading = computed<[string, string]>(() => activePage.value === "status"
  ? ["实时预览", `${previewRows.value.length} 项`]
  : ["Boss 技能倒计时", "独立悬浮窗"]);
/** 预览区要主动说明「哪一半被关掉了」，否则空列表会被当成渲染故障。 */
const previewScopeNote = computed(() => {
  if (draft.showBuffOverlay && draft.showDebuffOverlay) return "";
  if (!draft.showBuffOverlay && !draft.showDebuffOverlay) {
    return "Buff 与 Debuff 都已关闭，保存后这个悬浮窗不会再显示。";
  }
  return draft.showBuffOverlay ? "只显示 Buff，Debuff 已关闭。" : "只显示 Debuff，Buff 已关闭。";
});
function previewRowStyle(row: PreviewRow) {
  return {
    "--effect-color": draft.effectColors[row.key] || draft.toneColors[row.tone] || draft.toneColors.general,
    "--remaining-angle": `${Math.max(0, Math.min(100, row.progress)) * 3.6}deg`,
    "--preview-icon-background-color": statusIconSkillColor("background", row.key),
    "--preview-icon-name-color": statusIconSkillColor("name", row.key),
    "--preview-icon-time-color": statusIconSkillColor("time", row.key),
  };
}
function previewIconStyle(row: PreviewRow) {
  const index = Number(row.iconIndex ?? -1);
  if (index < 0) return {};
  const page = Math.floor(index / 200) + 1;
  const local = index % 200;
  const iconSize = draft.statusIconSize;
  return {
      backgroundImage: `url(http://127.0.0.1:48710/api/assets/skill-icon-sheet?page=${page})`,
      backgroundPosition: `-${(local % 10) * iconSize}px -${Math.floor(local / 10) * iconSize}px`,
      backgroundSize: `${iconSize * SKILL_ICON_SHEET_WIDTH / SKILL_ICON_CELL_SIZE}px auto`,
  };
}
function previewIconLabel(row: PreviewRow) {
  if (row.expandable || Number(row.iconIndex ?? -1) < 0 && row.key.startsWith("debuff:")) return "总";
  return [...row.name.replace(/\s+[-+]?\d+(?:\.\d+)?%.*$/, "").trim()][0] || "技";
}
function exportSettings() {
  const payload = {
    format: SETTINGS_FILE_FORMAT,
    version: 1,
    exportedAt: new Date().toISOString(),
    settings: normalizeBuffOverlaySettings(draft),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const date = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  anchor.href = url;
  anchor.download = `DN倒计时工具${APP_VERSION_LABEL}悬浮窗设置_${date}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  transferFailed.value = false;
  transferMessage.value = "个人设置已导出";
}
function openImportPicker() {
  if (!importInput.value) return;
  importInput.value.value = "";
  importInput.value.click();
}
async function importSettings(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  try {
    const parsed: unknown = JSON.parse(await file.text());
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("配置文件内容无效");
    const root = parsed as Record<string, unknown>;
    if (root.format && root.format !== SETTINGS_FILE_FORMAT) throw new Error("不是悬浮窗个人设置文件");
    const candidate = root.settings ?? root;
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) throw new Error("配置文件缺少设置内容");
    replaceDraft(candidate as BuffOverlaySettings);
    transferFailed.value = false;
    transferMessage.value = `已导入 ${file.name}`;
  } catch (reason) {
    transferFailed.value = true;
    transferMessage.value = reason instanceof Error ? reason.message : "导入失败";
  } finally {
    input.value = "";
  }
}
function save() { emit("save", normalizeBuffOverlaySettings(draft)); }
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay-settings-backdrop" @pointerdown.self="emit('close')">
      <section class="overlay-settings-dialog" role="dialog" aria-modal="true" aria-label="悬浮窗设置与预览">
        <header><div><h2>悬浮窗设置</h2><p>显示、颜色与版式</p></div><div class="header-actions"><span v-if="transferMessage" class="transfer-message" :class="{ error: transferFailed }">{{ transferMessage }}</span><button class="secondary-button compact-action" title="导出当前悬浮窗个人设置" @click="exportSettings"><Download :size="14" />导出</button><button class="secondary-button compact-action" title="导入悬浮窗个人设置" @click="openImportPicker"><Upload :size="14" />导入</button><input ref="importInput" class="settings-file-input" type="file" accept=".json,application/json" @change="importSettings"/><button class="icon-button" title="关闭" @click="emit('close')"><X :size="17" /></button></div></header>
        <nav class="settings-page-tabs" aria-label="悬浮窗设置分页">
          <button :class="{ active: activePage === 'status' }" @click="activePage='status'"><strong>Buff / Debuff</strong><span>状态、外观与筛选</span></button>
          <button :class="{ active: activePage === 'skills' }" @click="activePage='skills'"><strong>Boss 技能倒计时</strong><span>技能自选与独立窗口</span></button>
          </nav>
        <div class="dialog-body">
          <div class="settings-pane">
            <template v-if="activePage === 'status'">
              <section class="settings-section module-section page-pair-section">
                <div class="settings-title"><strong>Buff / Debuff 状态悬浮窗</strong><span>独立移动并保存位置</span></div>
                <div class="module-grid pair-module-grid">
                <label title="取消勾选后这个悬浮窗不再显示增益行；只影响悬浮窗，语音播报不受影响"><input v-model="draft.showBuffOverlay" type="checkbox"/><span>Buff 状态</span></label>
                <label title="取消勾选后这个悬浮窗不再显示减益行；只影响悬浮窗，语音播报不受影响"><input v-model="draft.showDebuffOverlay" type="checkbox"/><span>Debuff 状态</span></label>
                </div>
              </section>
              <section class="settings-section appearance-section">
              <div class="settings-title"><strong>状态悬浮窗版式</strong><button title="恢复默认外观" @click="resetAppearance"><RotateCcw :size="14" />恢复默认</button></div>
              <div class="layout-subtitle"><strong>Buff 显示方式</strong><span>与增伤、减抗技能独立设置</span></div>
              <div class="status-layout-choice" role="radiogroup" aria-label="Buff排列方式">
                <label :class="{ active: draft.buffLayoutMode === 'rows' }"><input v-model="draft.buffLayoutMode" type="radio" value="rows"/><span><strong>横向条目</strong><small>保持原来的名称与倒计时显示</small></span></label>
                <label :class="{ active: draft.buffLayoutMode === 'icons' }"><input v-model="draft.buffLayoutMode" type="radio" value="icons"/><span><strong>单行技能图标</strong><small>Buff 图标、名称和时间横向排列</small></span></label>
              </div>
              <div class="layout-subtitle"><strong>增伤 / 减抗技能</strong><span>合计始终保留横向条目</span></div>
              <div class="status-layout-choice" role="radiogroup" aria-label="增伤和减抗排列方式">
                <label :class="{ active: draft.statusLayoutMode === 'rows' }"><input v-model="draft.statusLayoutMode" type="radio" value="rows"/><span><strong>分行列表</strong><small>增伤、减抗继续使用横向明细</small></span></label>
                <label :class="{ active: draft.statusLayoutMode === 'icons' }"><input v-model="draft.statusLayoutMode" type="radio" value="icons"/><span><strong>单行技能图标</strong><small>仅贡献技能合并为一行</small></span></label>
              </div>
              <div class="layout-subtitle icon-appearance-title"><strong>图标外观</strong><span>同时用于 Buff 和贡献技能图标</span></div>
              <div class="range-grid icon-appearance-ranges">
                <label><span>图标大小</span><input v-model.number="draft.statusIconSize" type="range" min="28" max="64"/><output>{{ draft.statusIconSize }}px</output></label>
                <label><span>底色不透明度</span><input v-model.number="draft.statusIconBackgroundOpacity" type="range" min="0" max="100"/><output>{{ draft.statusIconBackgroundOpacity }}%</output></label>
                <label><span>技能名字号</span><input v-model.number="draft.statusIconNameFontSize" type="range" min="8" max="18"/><output>{{ draft.statusIconNameFontSize }}px</output></label>
                <label><span>时间字号</span><input v-model.number="draft.statusIconTimeFontSize" type="range" min="8" max="18"/><output>{{ draft.statusIconTimeFontSize }}px</output></label>
              </div>
              <div class="icon-color-grid">
                <label><span>图标背景色</span><input v-model="draft.statusIconBackgroundColor" type="color"/></label>
                <label><span>技能名颜色</span><input v-model="draft.statusIconNameColor" type="color"/></label>
                <label><span>时间颜色</span><input v-model="draft.statusIconTimeColor" type="color"/></label>
              </div>
              <div class="range-grid">
                <label><span>单列宽度</span><input v-model.number="draft.columnWidth" type="range" min="260" max="520" step="10"/><output>{{ draft.columnWidth }}px</output></label>
                <label><span>每列条数</span><input v-model.number="draft.maxRowsPerColumn" type="range" min="4" max="24"/><output>{{ draft.maxRowsPerColumn }}</output></label>
                <label><span>条目高度</span><input v-model.number="draft.rowHeight" type="range" min="22" max="42"/><output>{{ draft.rowHeight }}px</output></label>
                <label><span>字号</span><input v-model.number="draft.fontSize" type="range" min="10" max="18"/><output>{{ draft.fontSize }}px</output></label>
                <label><span>条目间距</span><input v-model.number="draft.gap" type="range" min="0" max="10"/><output>{{ draft.gap }}px</output></label>
                <label><span>圆角</span><input v-model.number="draft.borderRadius" type="range" min="0" max="12"/><output>{{ draft.borderRadius }}px</output></label>
                <label><span>背景不透明度</span><input v-model.number="draft.backgroundOpacity" type="range" min="0" max="100"/><output>{{ draft.backgroundOpacity }}%</output></label>
              </div>
              <p class="setting-note">0% 为完全透明，100% 为实色；这里只调整 Buff / Debuff 状态悬浮窗。</p>
              <div class="format-row"><label><input v-model="draft.showProvider" type="checkbox"/>显示来源职业</label><label>倒计时<select v-model="draft.timeFormat"><option value="decimal">始终显示1位小数</option><option value="subsecond">仅小于1秒显示1位小数</option></select></label></div>
              </section>
              <section class="settings-section color-section">
              <div class="settings-title"><strong>分类颜色</strong></div>
              <div class="tone-grid"><label v-for="tone in toneMeta" :key="tone.key"><input v-model="draft.toneColors[tone.key]" type="color"/><span>{{ tone.label }}</span></label></div>
              </section>
              <section class="settings-section">
              <div class="settings-title"><strong>Buff</strong><span>{{ props.options.length }} 项</span><button @click="selectAll">全选</button><button @click="clearAll">清空</button></div>
              <input v-model="buffSearch" class="buff-search" type="search" placeholder="搜索 Buff 或职业" />
              <div class="buff-scope-grid">
                <details class="buff-option-panel team-buff-panel" open><summary><strong>团队 Buff</strong><span>{{ teamOptions.length }}</span></summary><div class="buff-check-grid single-column">
                  <label v-for="row in teamOptions" :key="row.key" class="buff-option-row" :title="row.label || row.skill"><input type="checkbox" :checked="selected(row.key)" @change="onBuffToggle(row.key, $event)"/><span>{{ row.skill }} <small>（{{ row.provider || '职业待确认' }}）</small></span><input class="effect-color" type="color" :value="draft.effectColors[row.key] || draft.toneColors[rowTone(row.category, row.skill)]" :title="`${row.skill}横向条目颜色`" @input="setEffectColor(row.key, $event)"/><button v-if="draft.effectColors[row.key]" class="reset-color" title="跟随分类颜色" @click.prevent="clearEffectColor(row.key)"><RotateCcw :size="12"/></button></label>
                  <span v-if="!teamOptions.length" class="empty-options">没有符合条件的团队 Buff</span>
                </div></details>
                <details class="buff-option-panel personal-buff-panel" open><summary><strong>个人 Buff</strong><span>{{ personalOptions.length }}</span></summary><div class="buff-check-grid single-column">
                  <label v-for="row in personalOptions" :key="row.key" class="buff-option-row" :title="row.label || row.skill"><input type="checkbox" :checked="selected(row.key)" @change="onBuffToggle(row.key, $event)"/><span>{{ row.skill }} <small>（{{ row.provider || '职业待确认' }}）</small></span><input class="effect-color" type="color" :value="draft.effectColors[row.key] || draft.toneColors[rowTone(row.category, row.skill)]" :title="`${row.skill}横向条目颜色`" @input="setEffectColor(row.key, $event)"/><button v-if="draft.effectColors[row.key]" class="reset-color" title="跟随分类颜色" @click.prevent="clearEffectColor(row.key)"><RotateCcw :size="12"/></button></label>
                  <span v-if="!personalOptions.length" class="empty-options">没有符合条件的个人 Buff</span>
                </div></details>
              </div>
              <details v-if="draft.buffLayoutMode === 'icons'" class="buff-icon-color-panel" open>
                <summary><strong>逐技能图标配色</strong><span>未单独设置时跟随全局</span></summary>
                <div class="buff-icon-color-head"><span>技能</span><span>图标底色</span><span>技能名</span><span>时间</span><span></span></div>
                <div class="buff-icon-color-list">
                  <article v-for="row in visibleIconColorOptions" :key="`icon-color:${row.key}`">
                    <span><strong>{{ row.skill }}</strong><small>{{ row.provider || '职业待确认' }}</small></span>
                    <input type="color" :value="statusIconSkillColor('background', row.key)" :title="`${row.skill}图标背景色`" @input="setStatusIconSkillColor('background', row.key, $event)"/>
                    <input type="color" :value="statusIconSkillColor('name', row.key)" :title="`${row.skill}技能名颜色`" @input="setStatusIconSkillColor('name', row.key, $event)"/>
                    <input type="color" :value="statusIconSkillColor('time', row.key)" :title="`${row.skill}时间颜色`" @input="setStatusIconSkillColor('time', row.key, $event)"/>
                    <button type="button" :disabled="!hasStatusIconSkillColors(row.key)" title="恢复跟随全局" @click="clearStatusIconSkillColors(row.key)"><RotateCcw :size="12"/></button>
                  </article>
                  <span v-if="!visibleIconColorOptions.length" class="empty-options">没有符合当前筛选的已启用 Buff</span>
                </div>
              </details>
              </section>
              <section class="settings-section debuff-section">
              <div class="settings-title"><strong>Debuff</strong></div>
              <div class="debuff-setting-row"><label>增伤/减抗明细</label><select v-model="draft.debuffBreakdownMode"><option value="summary">合计（默认，可点击展开）</option><option value="skills">按技能</option><option value="summary_and_skills">合计 + 技能</option></select></div>
              <div class="debuff-setting-row"><label><input v-model="draft.showDamage" type="checkbox"/>增伤</label><select v-model="draft.damageMode"><option value="auto">按职业自动</option><option value="physical">物理增伤</option><option value="magic">魔法增伤</option><option value="both">物理 + 魔法</option></select></div>
              <div class="debuff-setting-row"><label><input v-model="draft.showResistance" type="checkbox"/>减抗</label><select v-model="draft.resistanceMode"><option value="auto">按职业属性自动</option><option value="fire">火抗</option><option value="water">水抗</option><option value="light">光抗</option><option value="dark">暗抗</option><option value="all">四属性</option></select></div>
              <div class="debuff-setting-row"><label><input v-model="draft.showDefense" type="checkbox"/>减防御</label><select v-model="draft.defenseMode"><option value="auto">按职业自动</option><option value="physical">物理防御</option><option value="magic">魔法防御</option><option value="both">物理 + 魔法</option></select></div>
              <div class="debuff-setting-row compact"><label><input v-model="draft.showCritical" type="checkbox"/>减致命抗</label><label><input v-model="draft.showFreeze" type="checkbox"/>叠冰层数</label></div>
              </section>
              <section class="settings-section">
              <div class="sort-editor status-sort-editor">
                <div class="sort-editor-heading"><strong>Buff / Debuff 统一显示顺序</strong><span>可跨类型拖动，或使用右侧上下按钮</span></div>
                <div v-if="orderedStatusRows.length" class="sort-list">
                  <article v-for="(row, index) in orderedStatusRows" :key="row.key" class="sort-row" :class="{ dragging: dragging === row.key }" draggable="true" @dragstart="beginSortDrag(row.key, $event)" @dragend="dragging=null" @dragover.prevent @drop.prevent="dropSortItem(row.key)">
                    <GripVertical :size="14" class="sort-grip"/><span><strong>{{ row.label }}</strong><small>{{ row.kind === 'buff' ? row.provider : 'Debuff' }}</small></span>
                    <button title="上移" :disabled="index === 0" @click="moveSortItem(row.key, -1)"><ChevronUp :size="13"/></button><button title="下移" :disabled="index === orderedStatusRows.length - 1" @click="moveSortItem(row.key, 1)"><ChevronDown :size="13"/></button>
                  </article>
                </div>
                <span v-else class="empty-sort-list">当前没有勾选的 Buff 或 Debuff</span>
              </div>
              </section>
            </template>
            <template v-else-if="activePage === 'skills'">
              <section class="settings-section module-section">
                <div class="settings-title"><strong>Boss 技能倒计时悬浮窗</strong><span>独立窗口，独立移动并保存位置</span></div>
                <label><input v-model="draft.showSkillCountdownOverlay" type="checkbox"/>启用 Boss 技能倒计时</label>
                <p class="setting-note">独立选择要显示的技能，可以静音使用。从已识别的技能起手按客户端 AI 冷却计时，不再扣除语音提前量。归零表示预计冷却完成，实际释放仍受血量、动作与 AI 选招影响。</p>
                <div class="countdown-selection-actions"><button class="secondary-button" @click="draft.selectedSkillCountdowns = SKILL_COUNTDOWN_OPTIONS.map(row => row.key)">全选</button><button class="secondary-button" @click="draft.selectedSkillCountdowns = []">全不选</button><span>已选 {{ draft.selectedSkillCountdowns.length }} / {{ SKILL_COUNTDOWN_OPTIONS.length }}</span><label class="countdown-group-scope" title="黑龙硬核／沙龙硬核分边打两只 Boss 时，只显示自己正在打的那一只的倒计时；与语音播报的同名开关相互独立"><input v-model="draft.splitBossOnlyCurrent" type="checkbox"/>只显示当前分组 Boss 技能</label></div>
                <details v-for="nest in SKILL_COUNTDOWN_NESTS" :key="nest.key" class="countdown-skill-group">
                  <summary><strong>{{ nest.name }}</strong> · 已选 {{ nest.rows.filter(row => draft.selectedSkillCountdowns.includes(row.key)).length }} / {{ nest.rows.length }}</summary>
                  <label><input type="checkbox" :checked="nest.rows.every(row => draft.selectedSkillCountdowns.includes(row.key))" :indeterminate="nest.rows.some(row => draft.selectedSkillCountdowns.includes(row.key)) && !nest.rows.every(row => draft.selectedSkillCountdowns.includes(row.key))" @change="toggleCountdownGroup(nest.rows.map(row => row.key), ($event.target as HTMLInputElement).checked)"/>本副本全选</label>
                <fieldset v-for="group in nest.groups" :key="group.key" class="countdown-skill-group">
                  <legend>{{ group.name }}</legend>
                  <template v-if="group.rows.length">
                  <label><input type="checkbox" :checked="group.rows.every(row => draft.selectedSkillCountdowns.includes(row.key))" :indeterminate="group.rows.some(row => draft.selectedSkillCountdowns.includes(row.key)) && !group.rows.every(row => draft.selectedSkillCountdowns.includes(row.key))" @change="toggleCountdownGroup(group.rows.map(row => row.key), ($event.target as HTMLInputElement).checked)"/>本组全选</label>
                  <div class="countdown-skill-options"><label v-for="rule in group.rows" :key="rule.key"><input v-model="draft.selectedSkillCountdowns" type="checkbox" :value="rule.key"/>{{ rule.label }}</label></div>
                  </template>
                  <p v-if="group.untimed.length" class="setting-note">仅语音提示（血量／出现事件／尚无确认的循环冷却）：{{ group.untimed.map(row => row.label).join('、') }}。</p>
                </fieldset>
                </details>
                <p v-if="!draft.selectedSkillCountdowns.length" class="setting-note">尚未选择技能，悬浮窗不会显示技能倒计时。</p>
                <p class="setting-note">仅显示当前副本、实际识别到释放的技能；各副本选择独立保存，与语音开关互不影响。标注“组冷却”的项目仅表示共享限制解除，不保证某个具体招式可释放。死亡、换阶段、停止采集或切换副本会清除相关计时。纯血量、出现事件和未确认冷却的提示不编造倒计时；黑影15秒仍仅作语音预警。</p>
                <p class="setting-note">独立的暖橙色磨砂胶囊条，与 Buff / Debuff 窗口分开拖动并各自记忆位置。</p>
              </section>
            </template>
          </div>
          <aside class="preview-pane">
            <div class="preview-heading"><strong>{{ previewHeading[0] }}</strong><span>{{ previewHeading[1] }}</span></div>
            <template v-if="activePage === 'status'">
            <div class="overlay-preview" :class="{ 'status-icon-preview': previewIconRows.length > 0 }" :style="previewStyle"><i class="preview-handle"/>
              <div v-if="previewStandardRows.length" class="preview-list"><article v-for="row in previewStandardRows" :key="row.key" class="preview-row" :class="{ 'preview-child-row': row.child, 'preview-expandable-row': row.expandable }" :style="previewRowStyle(row)" @click="row.expandable && togglePreviewDebuff(row.key.split(':')[1] || '')"><i class="preview-progress" :style="{ width: `${row.progress}%` }"/><div><ChevronDown v-if="row.expandable && row.expanded" :size="13" class="preview-chevron"/><ChevronUp v-else-if="row.expandable" :size="13" class="preview-chevron preview-chevron-collapsed"/><strong>{{ row.name }}<small v-if="draft.showProvider && row.provider"> · {{ row.provider }}</small></strong><span>{{ row.detail }}</span></div></article></div>
              <div v-if="!previewRows.length" class="preview-empty">{{ draft.showBuffOverlay || draft.showDebuffOverlay ? "采集中 · 暂无状态" : "已关闭" }}</div>
              <div v-if="previewIconRows.length" class="preview-icon-list">
                <article v-for="row in previewIconRows" :key="row.key" class="preview-icon-card" :class="{ 'preview-icon-expandable': row.expandable, 'preview-icon-child': row.child }" :style="previewRowStyle(row)" :title="`${row.name}${row.provider ? ` · ${row.provider}` : ''} · ${row.detail}`" @click="row.expandable && togglePreviewDebuff(row.key.split(':')[1] || '')">
                  <span class="preview-icon-face"><i v-if="Number(row.iconIndex ?? -1) >= 0" class="preview-skill-icon" :style="previewIconStyle(row)"/><b v-else>{{ previewIconLabel(row) }}</b><i class="preview-icon-shade"/><i class="preview-icon-ring"/><span v-if="row.expandable" class="preview-icon-expand"><ChevronDown v-if="row.expanded" :size="10"/><ChevronUp v-else :size="10"/></span></span>
                  <strong>{{ row.name }}</strong><small>{{ row.detail }}</small>
                </article>
              </div>
            </div>
            <p v-if="previewScopeNote" class="preview-scope-note">{{ previewScopeNote }}</p>
            </template>
            <div v-else-if="activePage === 'skills'" class="skill-preview-window" :style="{
              '--overlay-font-size': `${draft.fontSize}px`,
              '--overlay-background-opacity': String(draft.backgroundOpacity / 100),
              '--overlay-row-radius': `${draft.borderRadius}px`,
              '--skill-countdown-row-height': `${Math.ceil(draft.fontSize * 1.55) + 14}px`,
              '--skill-countdown-header-height': `${Math.ceil(draft.fontSize * 1.55)}px`
            }">
              <SkillCountdownPanel :rows="countdownPreview.map((rule, index) => ({ key: rule.key, label: rule.name, ready: index === 0, remaining: index ? 32 : 0, progress: index ? 43 : 0 }))" />
              <p class="setting-note">{{ draft.showSkillCountdownOverlay ? '已启用' : '未启用' }} · 示例：冷却归零后移入“冷却已好”，再次释放后重新倒计时。</p>
            </div>
          </aside>
        </div>
        <footer><button class="secondary-button" @click="emit('close')">取消</button><button class="primary-button" @click="save">保存并应用</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay-settings-backdrop{position:fixed;inset:0;z-index:2000;display:grid;place-items:center;padding:20px;background:rgba(22,34,39,.34);backdrop-filter:blur(2px)}.overlay-settings-dialog{display:flex;flex-direction:column;width:min(1120px,calc(100vw - 40px));max-height:min(860px,calc(100vh - 40px));padding:18px;overflow:hidden;border:1px solid #bfd4d9;border-radius:7px;background:#f8fbfc;box-shadow:0 22px 60px rgba(21,45,52,.22)}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:10px}h2{margin:0;color:#153944;font-size:19px}p{margin:3px 0 0;color:#617982;font-size:13px}.header-actions{display:flex;align-items:center;gap:7px;min-width:0}.compact-action{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:31px;padding:0 10px}.settings-file-input{display:none}.transfer-message{max-width:260px;overflow:hidden;color:#26745d;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.transfer-message.error{color:#b4444d}.dialog-body{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:18px;min-height:0;overflow:hidden}.settings-pane{min-height:0;overflow:auto;padding-right:6px}.preview-pane{min-width:0;border-left:1px solid #d8e5e8;padding-left:18px}
.settings-page-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-bottom:10px;padding:4px;border:1px solid #c7dadd;border-radius:6px;background:#edf4f5}.settings-page-tabs button{display:flex;flex-direction:column;align-items:flex-start;gap:2px;min-width:0;padding:8px 11px;border:1px solid transparent;border-radius:4px;background:transparent;color:#47646c;cursor:pointer;text-align:left}.settings-page-tabs button strong{overflow:hidden;width:100%;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.settings-page-tabs button span{overflow:hidden;width:100%;color:#789097;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.settings-page-tabs button.active{border-color:#96c6ce;background:#fff;color:#126f82;box-shadow:0 2px 7px rgba(35,80,88,.08)}.dialog-body.single-pane{grid-template-columns:minmax(0,1fr)}.dialog-body.single-pane .settings-pane{width:min(820px,100%);margin:0 auto;padding-right:0}
.settings-section{padding:12px 0;border-top:1px solid #d8e5e8}.settings-section:first-child{border-top:0}.settings-title{display:flex;align-items:center;gap:9px;margin-bottom:9px}.settings-title strong{color:#173c47;font-size:15px}.settings-title span{color:#70878e;font-size:12px}.settings-title button{display:inline-flex;align-items:center;gap:4px;margin-left:auto;border:0;background:transparent;color:#137d91;cursor:pointer}.settings-title button+button{margin-left:0}.range-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 14px}.range-grid label{display:grid;grid-template-columns:82px minmax(90px,1fr) 48px;align-items:center;gap:7px;color:#31535c;font-size:12px}.range-grid input{min-width:0;accent-color:#168ca0}.range-grid output{color:#537078;font-variant-numeric:tabular-nums;text-align:right}.format-row{display:flex;align-items:center;gap:20px;margin-top:10px;color:#31535c;font-size:12px}.format-row label{display:flex;align-items:center;gap:6px}.format-row select{height:28px;border:1px solid #bfd2d7;border-radius:4px;background:white;color:#294b54}
.status-layout-choice{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}.status-layout-choice label{display:grid;grid-template-columns:18px minmax(0,1fr);align-items:start;gap:7px;min-height:54px;padding:9px 10px;border:1px solid #cfe0e3;border-radius:5px;background:#f3f8f9;color:#294f59;cursor:pointer}.status-layout-choice label:has(input:checked){border-color:#58adba;background:#e8f7f9;box-shadow:inset 3px 0 #2698a9}.status-layout-choice input{margin-top:2px;accent-color:#168ca0}.status-layout-choice span{display:grid;gap:2px}.status-layout-choice strong{font-size:12px}.status-layout-choice small{color:#6e868d;font-size:10px;line-height:1.35}
.layout-subtitle{display:flex;align-items:center;gap:8px;margin-top:11px;color:#31535c}.layout-subtitle strong{font-size:12px}.layout-subtitle span{color:#789097;font-size:10px}.icon-appearance-title{margin-top:15px}.icon-appearance-ranges{margin-top:8px}.icon-color-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:8px 0 12px}.icon-color-grid label{display:flex;align-items:center;justify-content:space-between;gap:7px;min-height:34px;padding:0 8px;border:1px solid #d4e2e5;border-radius:4px;background:#f5f9fa;color:#31535c;font-size:11px}.icon-color-grid input{width:30px;height:24px;padding:0;border:0;background:transparent;cursor:pointer}
.module-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.module-grid label{display:flex;align-items:center;gap:7px;min-height:34px;padding:0 9px;border:1px solid #cfe0e3;border-radius:4px;background:#edf5f6;color:#264b55;font-size:12px}
.page-pair-section{padding-top:4px;border-top:0}.pair-module-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.pair-module-grid label{min-height:48px;padding:0 14px;background:#f2f8f9;font-size:13px}.missing-scope-picker label.active{border-color:#58adba;background:#e8f7f9;box-shadow:inset 3px 0 #2698a9}
  .tone-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}.tone-grid label{display:flex;align-items:center;gap:7px;min-width:0;padding:5px 7px;background:#edf4f5;color:#31535c;font-size:12px}.tone-grid input,.effect-color{width:25px;height:22px;padding:0;border:0;background:transparent;cursor:pointer}.buff-search{box-sizing:border-box;width:100%;height:34px;margin-bottom:8px;padding:0 10px;border:1px solid #bfd2d7;border-radius:4px;background:white;color:#244851}.buff-scope-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:8px}.buff-option-panel{min-width:0;border:1px solid #d4e2e5;border-radius:5px;background:#fff}.buff-option-panel summary{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 10px;color:#31535c;cursor:pointer;user-select:none}.buff-option-panel summary span{margin-left:auto;color:#71868c;font-size:12px}.team-buff-panel{border-color:#a9cfd5;background:#f6fbfc}.personal-buff-panel{border-color:#c9d5e5;background:#f8faff}
  .buff-icon-color-panel{margin-top:10px;border:1px solid #c9dce0;border-radius:5px;background:#fff}.buff-icon-color-panel>summary{display:flex;align-items:center;gap:8px;min-height:38px;padding:0 10px;color:#31535c;cursor:pointer}.buff-icon-color-panel>summary span{margin-left:auto;color:#789097;font-size:10px}.buff-icon-color-head,.buff-icon-color-list>article{display:grid;grid-template-columns:minmax(150px,1fr) 62px 62px 62px 28px;align-items:center;gap:5px}.buff-icon-color-head{padding:6px 9px;border-top:1px solid #e0e9eb;border-bottom:1px solid #e0e9eb;color:#789097;font-size:9px;text-align:center}.buff-icon-color-head span:first-child{text-align:left}.buff-icon-color-list{max-height:280px;overflow:auto;padding:5px 8px}.buff-icon-color-list>article{min-height:38px;border-bottom:1px solid #edf2f3}.buff-icon-color-list>article:last-child{border-bottom:0}.buff-icon-color-list>article>span{display:grid;min-width:0}.buff-icon-color-list strong,.buff-icon-color-list small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.buff-icon-color-list strong{color:#294d56;font-size:11px}.buff-icon-color-list small{color:#789097;font-size:9px}.buff-icon-color-list input{justify-self:center;width:28px;height:23px;padding:0;border:0;background:transparent;cursor:pointer}.buff-icon-color-list button{display:grid;width:24px;height:24px;padding:0;place-items:center;border:1px solid #d1e0e3;border-radius:3px;background:#f4f8f9;color:#47717b;cursor:pointer}.buff-icon-color-list button:disabled{opacity:.3;cursor:default}
  .buff-check-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 9px;padding:8px 10px 10px;border-top:1px solid #e1eaec}.buff-check-grid.single-column{grid-template-columns:minmax(0,1fr);max-height:310px;overflow:auto;align-content:start}.buff-option-row{display:grid;grid-template-columns:16px minmax(0,1fr) 26px 18px;align-items:center;gap:5px;min-width:0;color:#284a53;font-size:12px}.buff-option-row>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.buff-option-row small{color:#6d858c;font-size:10px;white-space:nowrap}.reset-color{display:grid;place-items:center;width:18px;height:18px;padding:0;border:0;color:#6a858c;background:transparent;cursor:pointer}.empty-options{grid-column:1/-1;color:#7a8f95;font-size:12px}.debuff-section{display:grid;gap:7px}.debuff-section .settings-title{margin-bottom:1px}.debuff-setting-row{display:grid;grid-template-columns:140px minmax(180px,250px);align-items:center;min-height:32px;padding:4px 8px;background:#edf4f5;border-radius:4px}.debuff-setting-row label{display:flex;align-items:center;gap:6px;color:#284a53;font-size:12px}.debuff-setting-row select{height:28px;border:1px solid #bfd2d7;border-radius:4px;background:white;color:#294b54}.debuff-setting-row.compact{grid-template-columns:140px 180px}
.sort-editor{margin-top:10px;padding:9px;border:1px solid #cbdde1;border-radius:5px;background:#f4f9fa}.sort-editor-heading{display:flex;align-items:center;gap:8px;margin-bottom:7px}.sort-editor-heading strong{color:#294f59;font-size:12px}.sort-editor-heading span{margin-left:auto;color:#789097;font-size:10px}.sort-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px;max-height:260px;overflow:auto;padding-right:3px}.sort-list.compact-sort-list{grid-template-columns:minmax(0,360px);max-height:none}.sort-row{display:grid;grid-template-columns:18px minmax(0,1fr) 24px 24px;align-items:center;gap:3px;min-width:0;min-height:34px;padding:3px 4px;border:1px solid #d4e2e5;border-radius:4px;background:#fff;color:#294d56;cursor:grab}.sort-row:active{cursor:grabbing}.sort-row.dragging{border-color:#48a6b5;background:#e8f6f8;opacity:.62}.sort-grip{color:#80969b}.sort-row>span{display:flex;min-width:0;flex-direction:column}.sort-row>span strong,.sort-row>span small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sort-row>span strong{font-size:11px;font-weight:600}.sort-row>span small{color:#7a9096;font-size:9px}.sort-row button{display:grid;place-items:center;width:23px;height:23px;padding:0;border:1px solid #d1e0e3;border-radius:3px;background:#f4f8f9;color:#47717b;cursor:pointer}.sort-row button:disabled{opacity:.3;cursor:default}.empty-sort-list{display:block;padding:8px;color:#7a8f95;font-size:11px;text-align:center}.teammate-info-toggle{grid-template-columns:minmax(0,1fr)}.teammate-info-toggle label{min-height:42px}.setting-note{margin:7px 0 0;color:#70878e;font-size:11px;line-height:1.5}
.preview-heading{display:flex;justify-content:space-between;margin:4px 0 10px;color:#31535c;font-size:13px}.preview-heading span{color:#71868c}.overlay-preview{position:sticky;top:0;box-sizing:border-box;max-width:100%;padding:5px 5px 5px 17px;background:rgba(239,248,249,var(--preview-opacity));border:1px solid rgba(79,143,153,var(--preview-border-opacity));border-radius:6px;box-shadow:0 7px 18px rgba(26,55,61,var(--preview-shadow-opacity))}.preview-empty{display:flex;align-items:center;justify-content:center;min-height:38px;color:#6d858c;font-size:12px}
.preview-scope-note{margin:8px 0 0;padding:6px 8px;border-radius:6px;background:#eef6f7;color:#41646d;font-size:11px;line-height:1.5}
.preview-handle{position:absolute;left:5px;top:50%;width:4px;height:26px;border-radius:2px;transform:translateY(-50%);background:rgba(125,161,168,var(--preview-handle-opacity))}.preview-list{display:grid;gap:var(--preview-gap)}.preview-row{position:relative;box-sizing:border-box;height:var(--preview-row-height);overflow:hidden;padding:0 7px;border:1px solid color-mix(in srgb,var(--effect-color) 30%,rgba(169,193,198,var(--preview-border-opacity)));border-radius:var(--preview-radius);background:rgba(255,255,255,var(--preview-row-opacity))}.preview-progress{position:absolute;inset:0 auto 0 0;background:color-mix(in srgb,var(--effect-color) 28%,transparent)}.preview-row>div{position:relative;display:flex;align-items:center;gap:7px;height:100%}.preview-row strong{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#17323a;font-size:var(--preview-font-size)}.preview-row small{color:#567078;font-size:max(9px,calc(var(--preview-font-size) - 2px))}.preview-row span{color:#385b63;font-size:var(--preview-detail-size);font-variant-numeric:tabular-nums;white-space:nowrap}
.status-icon-preview{overflow-x:auto;overflow-y:hidden;padding:7px 7px 7px 17px}.preview-list+.preview-icon-list{margin-top:var(--preview-gap)}.preview-icon-list{display:flex;align-items:flex-start;gap:var(--preview-gap);min-width:max-content}.preview-icon-card{position:relative;display:grid;box-sizing:border-box;width:72px;min-height:98px;justify-items:center;padding:2px 1px 0;color:#294f59;cursor:default}.preview-icon-expandable{cursor:pointer}.preview-icon-child .preview-icon-face{border-style:dashed}.preview-icon-face{position:relative;display:grid;box-sizing:content-box;width:50px;height:50px;overflow:hidden;place-items:center;border:1px solid color-mix(in srgb,var(--effect-color) 76%,#38565d);border-radius:5px;background:color-mix(in srgb,var(--effect-color) 72%,#263e45);box-shadow:0 2px 7px rgba(18,45,52,.24)}.preview-skill-icon{position:absolute;inset:0;width:50px;height:50px;background-repeat:no-repeat}.preview-icon-face>b{position:relative;z-index:1;color:#fff;font-size:18px;text-shadow:0 1px 3px rgba(0,0,0,.58)}.preview-icon-shade{position:absolute;z-index:2;inset:0;border-radius:4px;background:conic-gradient(from -90deg,transparent 0 var(--remaining-angle),rgba(4,11,15,.72) var(--remaining-angle) 360deg);pointer-events:none}.preview-icon-ring{position:absolute;z-index:3;inset:0;box-sizing:border-box;border:1px solid rgba(255,255,255,.62);border-radius:4px;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--effect-color) 52%,transparent);pointer-events:none}.preview-icon-expand{position:absolute;z-index:4;right:-1px;bottom:-1px;display:grid;width:17px;height:17px;place-items:center;border:1px solid rgba(255,255,255,.9);border-radius:50%;background:#247f8d;color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3)}.preview-icon-card>strong{display:-webkit-box;overflow:hidden;width:70px;height:26px;margin-top:3px;-webkit-box-orient:vertical;-webkit-line-clamp:2;color:#17323a;font-size:10px;font-weight:650;line-height:13px;text-align:center;text-overflow:ellipsis}.preview-icon-card>small{color:#365e67;font-size:10px;font-weight:700;font-variant-numeric:tabular-nums;line-height:14px}.preview-window{box-sizing:border-box;max-width:100%;gap:var(--preview-gap);padding:5px 5px 5px 17px;border:1px solid rgba(79,143,153,var(--preview-border-opacity));border-radius:6px;background:rgba(239,248,249,var(--preview-opacity));box-shadow:0 7px 18px rgba(26,55,61,var(--preview-shadow-opacity))}.preview-window .module-preview-card{border-color:rgba(79,143,153,var(--preview-border-opacity));border-radius:var(--preview-radius);background:rgba(255,255,255,var(--preview-row-opacity));box-shadow:none}.module-preview-card.disabled{opacity:.48}.preview-window .module-preview-card header strong{font-size:var(--preview-font-size)}.preview-window .curve-preview-card svg{height:var(--preview-chart-height)}.preview-window .curve-legend{font-size:max(9px,calc(var(--preview-font-size) - 3px))}.preview-window .sample-compact-rows,.preview-window .sample-alert-rows{gap:var(--preview-gap)}.preview-window .sample-compact-rows>div,.preview-window .sample-alert-rows>div{box-sizing:border-box;height:var(--preview-alert-row-height);min-height:0;border-radius:var(--preview-radius);font-size:var(--preview-font-size)}.sample-compact-rows b.ready{color:#168568}.sample-alert-rows .warning{border-color:#ead7aa;background:#fff9eb}.sample-alert-rows .warning b{color:#b47414}.sample-alert-rows .danger{border-color:#ebc2c5;background:#fff2f3}.sample-alert-rows .danger b{color:#bd4750}.sample-alert-rows .info{border-color:#bddde3;background:#effafd}.sample-alert-rows .info b{color:#238195}footer{display:flex;justify-content:flex-end;gap:8px;padding-top:13px;border-top:1px solid #d8e5e8}
.preview-expandable-row{cursor:pointer}.preview-child-row{width:calc(100% - 15px);margin-left:15px;border-left-width:3px;background:rgba(255,255,255,calc(var(--preview-row-opacity) * .82))}.preview-child-row strong{font-weight:600}.preview-chevron{flex:0 0 auto;color:#4c737c}.preview-chevron-collapsed{transform:rotate(90deg)}
.preview-icon-list{justify-content:flex-start}.preview-icon-card{grid-template-rows:calc(var(--preview-icon-size) + 4px) auto auto;width:var(--preview-icon-card-width);min-height:var(--preview-icon-card-height);align-content:start;border-radius:var(--preview-radius);color:var(--preview-icon-name-color);background:color-mix(in srgb,var(--preview-icon-background-color) var(--preview-icon-background-opacity),transparent)}.preview-icon-face{width:var(--preview-icon-size);height:var(--preview-icon-size);background:var(--preview-icon-background-color)}.preview-skill-icon{width:var(--preview-icon-size);height:var(--preview-icon-size)}.preview-icon-face>b{color:var(--preview-icon-name-color);font-size:calc(var(--preview-icon-name-size) + 7px);text-shadow:0 1px 3px rgba(255,255,255,.58)}.preview-icon-expand{width:15px;height:15px}.preview-icon-card>strong{width:calc(var(--preview-icon-card-width) - 2px);height:auto;max-height:calc(var(--preview-icon-name-line-height) * 2);margin-top:0;color:var(--preview-icon-name-color);font-size:var(--preview-icon-name-size);line-height:var(--preview-icon-name-line-height)}.preview-icon-card>small{color:var(--preview-icon-time-color);font-size:var(--preview-icon-time-size);line-height:var(--preview-icon-time-line-height)}
  @media(max-width:900px){.settings-page-tabs{grid-template-columns:1fr}.settings-page-tabs button span{display:none}.dialog-body{grid-template-columns:1fr;overflow:auto}.settings-pane{overflow:visible}.preview-pane{border-left:0;border-top:1px solid #d8e5e8;padding:14px 0 0}.tone-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.buff-scope-grid,.module-help-grid,.curve-selector-grid{grid-template-columns:1fr}}
@media(min-width:901px){.settings-page-tabs{grid-template-columns:repeat(2,minmax(0,1fr))}}
.countdown-selection-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0}.countdown-group-scope{display:flex;align-items:center;gap:5px;margin-left:auto;font-size:12px;white-space:nowrap}.countdown-skill-group{margin:10px 0;padding:10px;border:1px solid #c7dadd;border-radius:6px}.countdown-skill-group legend{padding:0 5px;font-weight:700}.countdown-skill-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}.countdown-skill-options label{display:flex;align-items:center;gap:5px}
</style>
