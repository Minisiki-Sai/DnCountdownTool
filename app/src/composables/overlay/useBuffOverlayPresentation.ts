import { computed, ref } from "vue";
import { buildOverlayLayoutPreview } from "../../utils/overlayLayoutPreview";
import { skillCountdownGeometry } from "../../utils/skillCountdowns";
import type { BuffOverlaySettings, BuffOverlayStatus } from "../../types/api";
import { OVERLAY_WINDOW_LABELS, type OverlayWindowKind } from "../../utils/buffOverlaySettings";
import {
  buildOverlayProfessionLabels,
  overlayProfessionLabel,
  uniqueOverlayPartyPlayers,
} from "../../utils/overlayPartyPresentation";

type OverlayTone = keyof BuffOverlaySettings["toneColors"];
type OverlayRow = {
  key: string;
  appearanceKey: string;
  colorKey: string;
  name: string;
  sourceJob: string;
  sourceDetail: string;
  detail: string;
  remaining: number;
  duration: number;
  permanent: boolean;
  tone: OverlayTone;
  groupOrder: number;
  sortOrder: number;
  rowKind: "buff" | "debuff-summary" | "debuff-skill";
  expandable: boolean;
  expanded: boolean;
  depth: number;
  iconIndex: number;
  compactEffect: boolean;
};
type ReadonlyValue<T> = { readonly value: T };
type MutableValue<T> = { value: T };

const SKILL_ICON_SHEET_WIDTH = 512;
const SKILL_ICON_CELL_SIZE = 50;
const STATUS_ICON_HORIZONTAL_PADDING = 20;
// Leave enough room below the native 760px window ceiling for borders,
// WebView text rasterisation and non-100% display scaling. A tall row layout
// is split into extra columns before content reaches the clipped area.
const STATUS_CONTENT_MAX_HEIGHT = 600;
const STATUS_CONTENT_MAX_WIDTH = 1180;
// Keep the status window comfortably below the native 1280px width ceiling,
// even with the largest configurable icon and gap. Additional effects wrap
// onto another row instead of being clipped beyond the right window edge.
const STATUS_ICON_MAX_COLUMNS = 12;

function statusIconGridShape(count: number): { columns: number; rows: number } {
  const itemCount = Math.max(0, Math.floor(count));
  const columns = Math.max(1, Math.min(STATUS_ICON_MAX_COLUMNS, itemCount || 1));
  return {
    columns,
    rows: Math.max(1, Math.ceil(itemCount / columns)),
  };
}

export function useBuffOverlayPresentation(context: {
  props: { view: OverlayWindowKind };
  payload: ReadonlyValue<BuffOverlayStatus | null>;
  settings: MutableValue<BuffOverlaySettings>;
  fetchedAt: ReadonlyValue<number>;
  clock: ReadonlyValue<number>;
  baseSeconds: ReadonlyValue<number>;
  layoutMode: ReadonlyValue<boolean>;
  statusPanelGap: number;
  skillCountdownCount?: () => number;
  /** 技能窗顶部「副本 / Boss」占几行（0 表示不显示）。 */
  identityRowCount?: () => number;
}) {
  const {
    props, payload: livePayload, settings, fetchedAt, clock, baseSeconds,
    layoutMode,
    statusPanelGap: STATUS_PANEL_GAP,
  } = context;

  // Positioning freezes only presentation, never the capture/inspection bus.
  const layoutSnapshot = ref<BuffOverlayStatus | null>(null);
  if (layoutMode.value && livePayload.value) {
    layoutSnapshot.value = JSON.parse(JSON.stringify(livePayload.value)) as BuffOverlayStatus;
  }
  const payload = computed(() => layoutMode.value
    ? buildOverlayLayoutPreview(settings.value, layoutSnapshot.value) : livePayload.value);

  const windowLabel = computed(() => OVERLAY_WINDOW_LABELS[props.view]);
  const isStatusView = computed(() => props.view === "status");
  const isSkillView = computed(() => props.view === "skill");
  const layoutTitle = computed(() => (props.view === "skill" ? "Boss 技能倒计时" : "Buff / Debuff"));
  function textValue(value: unknown): string { return String(value ?? "").trim(); }
  function numberValue(value: unknown): number { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; }
  const overlayPlayers = computed(() => uniqueOverlayPartyPlayers(payload.value?.players || []));
  const professionLabels = computed(() => buildOverlayProfessionLabels(overlayPlayers.value));
  function professionLabel(sourceId: unknown, fallbackJob?: unknown): string {
    return overlayProfessionLabel(professionLabels.value, sourceId, fallbackJob);
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

  const captureActive = computed(() => ["starting", "running"].includes(livePayload.value?.state || ""));
  const currentSeconds = computed(() => {
    if (layoutMode.value) return payload.value?.elapsedSeconds || 60;
    return captureActive.value
      ? baseSeconds.value + Math.max(0, clock.value - fetchedAt.value) / 1000
      : baseSeconds.value;
  });
  const targetId = computed(() => {
    // Buff rows always describe the current local character.  Before the
    // packet decoder has confirmed that actor, fail closed: selecting a saved
    // teammate or the first party row made pre-combat Buffs look like our own.
    return textValue(payload.value?.selfSourceId);
  });
  const target = computed(() => (payload.value?.players || []).find(row => row.source_id === targetId.value));

  function buffSourceJob(raw: Record<string, unknown>): string {
    const direct = textValue(raw.job);
    if (direct) return direct;
    const sourceId = textValue(raw.source_id);
    return textValue((payload.value?.players || []).find(row => row.source_id === sourceId)?.job);
  }

  function allowedModes(category: string): string[] {
    // "auto" used to narrow rows to the *local* character's own attack types and
    // elements.  Debuff rows describe what the whole party applied, and the
    // lightweight capture profile no longer decodes damage events, so the party
    // roster stays empty and `elements` only holds one self element.  That made
    // "auto" hide real debuffs (减光抗 with a fire-only self).  Auto now means
    // "trust what the backend published"; explicit modes still filter.
    if (category === "damage") {
      const mode = settings.value.damageMode;
      return mode === "auto" || mode === "both" ? ["physical", "magic"] : [mode];
    }
    if (category === "defense") {
      const mode = settings.value.defenseMode;
      return mode === "auto" || mode === "both" ? ["physical", "magic"] : [mode];
    }
    if (category === "resistance") {
      const mode = settings.value.resistanceMode;
      return mode === "auto" || mode === "all"
        ? ["fire", "water", "light", "dark"]
        : [mode];
    }
    return [category];
  }
  function categoryEnabled(category: string): boolean {
    return category === "damage" ? settings.value.showDamage
      : category === "resistance" ? settings.value.showResistance
        : category === "critical" ? settings.value.showCritical
          : category === "defense" ? settings.value.showDefense
            : category === "freeze" ? settings.value.showFreeze : false;
  }
  function formatValue(value: number, category: string): string {
    const number = value.toFixed(value % 1 ? 1 : 0);
    return category === "freeze" ? `${number}层` : `${number}%`;
  }
  function formatRemainingSeconds(value: number): string {
    if (settings.value.timeFormat === "decimal") return `${Math.max(0, value).toFixed(1)}s`;
    return value < 1
      ? `${Math.max(0, value).toFixed(1)}s`
      : `${Math.ceil(Math.max(0, value))}s`;
  }
  function liveDataSourceDetail(raw: Record<string, unknown>, kind: "timing" | "effect"): string {
    const explicit = textValue(raw.data_source);
    if (explicit) return `${kind === "timing" ? "计时" : "效果"}：${explicit}`;
    const timing = textValue(raw.timing_source || raw.duration_source);
    const value = textValue(raw.value_source);
    const usesFallback = /fallback|skill_table|activation_rule/.test(`${timing} ${value}`);
    const usesPacket = /packet|server_status/.test(`${timing} ${value}`);
    const source = usesFallback
      ? (usesPacket ? "抓包优先 / 预设兜底" : "预设数据兜底")
      : usesPacket ? "抓包数据" : "来源待确认";
    return `${kind === "timing" ? "计时" : "效果"}：${source}`;
  }
  function statusIconSize(): number {
    return settings.value.statusIconSize;
  }
  function statusIconCardWidth(): number {
    return Math.max(54, statusIconSize() + STATUS_ICON_HORIZONTAL_PADDING);
  }
  function statusIconCardHeight(): number {
    return statusIconSize() + 4
      + (settings.value.statusIconNameFontSize + 1) * 2
      + settings.value.statusIconTimeFontSize + 2;
  }
  function statusIconPanelHeight(count: number): number {
    if (!count) return 0;
    const grid = statusIconGridShape(count);
    return grid.rows * statusIconCardHeight() + Math.max(0, grid.rows - 1) * settings.value.gap;
  }
  function standardStatusGridShape(count: number) {
    const maximumRows = Math.max(1, settings.value.maxRowsPerColumn);
    const gap = settings.value.gap;
    const iconHeight = statusIconPanelHeight(compactEffectRows.value.length);
    const reservedHeight = iconHeight + (count && iconHeight ? STATUS_PANEL_GAP : 0);
    const availableHeight = Math.max(settings.value.rowHeight, STATUS_CONTENT_MAX_HEIGHT - reservedHeight);
    const heightLimitedRows = Math.max(1, Math.floor(
      (availableHeight + gap) / (settings.value.rowHeight + gap),
    ));
    const rows = Math.max(1, Math.min(maximumRows, heightLimitedRows, count || 1));
    return {
      count,
      rows,
      columns: count ? Math.max(1, Math.ceil(count / rows)) : 0,
      height: count
        ? rows * settings.value.rowHeight + Math.max(0, rows - 1) * gap
        : 0,
    };
  }
  function standardStatusPanelWidth(columns: number): number {
    if (columns <= 1) return settings.value.columnWidth;
    return Math.min(
      STATUS_CONTENT_MAX_WIDTH,
      18 + columns * settings.value.columnWidth + (columns - 1) * settings.value.gap,
    );
  }
  const debuffModeOrder: Record<string, number> = {
    physical: 0, magic: 1, fire: 0, water: 1, light: 2, dark: 3,
    critical: 0, freeze: 0,
  };
  const expandedDebuffKeys = ref<Set<string>>(new Set());
  const iconIndexByBuffKey = computed(() => new Map(
    (payload.value?.buffOptions || []).map(row => [row.key, numberValue(row.iconIndex ?? -1)]),
  ));

  const rows = computed<OverlayRow[]>(() => {
    const seconds = currentSeconds.value;
    const selectedTarget = targetId.value;
    const unique = new Map<string, { row: OverlayRow; rank: number }>();
    const buffOrder = new Map(settings.value.buffOrder.map((key, index) => [key, index]));
    const debuffOrder = new Map(settings.value.debuffOrder.map((key, index) => [key, index]));
    const statusOrder = new Map(settings.value.statusOrder.map((key, index) => [key, index]));
    const keepBest = (identity: string, row: OverlayRow, rank: number) => {
      const current = unique.get(identity);
      if (!current || rank > current.rank) unique.set(identity, { row, rank });
    };
    const selectedBuffs = new Set(settings.value.selectedBuffs);
    for (const raw of payload.value?.buffs || []) {
      if (!selectedTarget || textValue(raw.target_id) !== selectedTarget) continue;
      if (selectedBuffs.size && !selectedBuffs.has(textValue(raw.catalog_key))) continue;
      const permanent = Boolean(raw.permanent);
      const start = numberValue(raw.start_seconds);
      const end = numberValue(raw.end_seconds);
      if (!permanent && (start > seconds + .05 || end <= seconds)) continue;
      const name = textValue(raw.skill_name) || textValue(raw.label) || "未命名增益";
      const sourceJob = buffSourceJob(raw);
      const duration = permanent ? 0 : Math.max(.1, numberValue(raw.duration_seconds) || end - start);
      const row: OverlayRow = {
        key: textValue(raw.key) || `${textValue(raw.target_id)}:${name}`,
        appearanceKey: textValue(raw.catalog_key),
        colorKey: textValue(raw.catalog_key),
        name, sourceJob, sourceDetail: liveDataSourceDetail(raw, "timing"), detail: permanent ? "常驻" : "", remaining: permanent ? 0 : Math.max(0, end - seconds),
        duration, permanent, tone: rowTone(textValue(raw.status_kind), name),
        groupOrder: 0,
        rowKind: "buff",
        expandable: false,
        expanded: false,
        depth: 0,
        iconIndex: numberValue(raw.icon_index ?? iconIndexByBuffKey.value.get(textValue(raw.catalog_key)) ?? -1),
        compactEffect: false,
        sortOrder: statusOrder.has(`buff:${textValue(raw.catalog_key)}`)
          ? numberValue(statusOrder.get(`buff:${textValue(raw.catalog_key)}`)) * 100
          : buffOrder.has(textValue(raw.catalog_key))
            ? 10_000 + numberValue(buffOrder.get(textValue(raw.catalog_key))) * 100
            : 20_000 + numberValue(raw.display_order) * 100,
      };
      // Different sources and packet variants can describe the same effective
      // status. The overlay only needs one row; prefer permanent, otherwise the
      // application with the longest remaining server interval.
      keepBest(`buff:${name}`, row, permanent ? Number.MAX_SAFE_INTEGER : row.remaining);
    }
    for (const raw of payload.value?.debuffs || []) {
      if (payload.value?.endedBossIds?.includes(textValue(raw.target_id))) continue;
      const category = textValue(raw.category);
      const mode = textValue(raw.mode);
      if (!categoryEnabled(category) || !allowedModes(category).includes(mode)) continue;
      const end = numberValue(raw.end_seconds);
      const remaining = end ? Math.max(0, end - seconds) : 0;
      if (end && remaining <= 0) continue;
      const current = numberValue(raw.current_value);
      const next = numberValue(raw.next_value);
      const label = textValue(raw.label);
      const nextText = end ? `→${formatValue(next, category)}` : "";
      const summaryIdentity = `debuff:${label}`;
      const breakdownEligible = category === "damage" || category === "resistance";
      const contributors = Array.isArray(raw.contributors)
        ? raw.contributors.filter(row => row && typeof row === "object") as Array<Record<string, unknown>>
        : [];
      const breakdownMode = settings.value.debuffBreakdownMode;
      const manuallyExpanded = expandedDebuffKeys.value.has(summaryIdentity);
      const showSummary = !breakdownEligible || breakdownMode !== "skills" || !contributors.length;
      const showSkills = breakdownEligible && contributors.length > 0 && (
        breakdownMode === "skills"
        || breakdownMode === "summary_and_skills"
        || manuallyExpanded
      );
      const baseSortOrder = statusOrder.has(`debuff:${category}`)
        ? numberValue(statusOrder.get(`debuff:${category}`)) * 100 + (debuffModeOrder[mode] ?? 99)
        : 30_000 + (debuffOrder.get(category as BuffOverlaySettings["debuffOrder"][number]) ?? 99) * 100
          + (debuffModeOrder[mode] ?? 99);
      const row: OverlayRow = {
        key: textValue(raw.key), appearanceKey: "", colorKey: `debuff:${category}:${mode}`, name: `${label} ${formatValue(current, category)}${nextText}`,
        sourceJob: "",
        sourceDetail: liveDataSourceDetail(raw, "effect"),
        detail: end ? formatRemainingSeconds(remaining) : "持续",
        remaining, duration: numberValue(raw.duration_seconds), permanent: !end, tone: rowTone(category, label),
        groupOrder: 1 + (debuffOrder.get(category as BuffOverlaySettings["debuffOrder"][number]) ?? 99),
        sortOrder: baseSortOrder,
        rowKind: "debuff-summary",
        expandable: breakdownEligible && breakdownMode === "summary" && contributors.length > 0,
        expanded: breakdownMode === "summary_and_skills" || manuallyExpanded,
        depth: 0,
        iconIndex: -1,
        // Totals keep the original horizontal row. Only concrete contributor
        // skills enter the compact icon strip.
        compactEffect: false,
      };
      // Same-name debuffs do not stack as rows. Keep the strongest current
      // value, then the longer remaining interval when strengths are equal.
      if (showSummary) {
        keepBest(summaryIdentity, row, Math.abs(current) * 1_000_000 + (end ? remaining : 999_999));
      }
      if (showSkills) {
        contributors.forEach((contributor, index) => {
          const contributorEnd = numberValue(contributor.end_seconds);
          const contributorRemaining = contributorEnd ? Math.max(0, contributorEnd - seconds) : 0;
          if (contributorEnd && contributorRemaining <= 0) return;
          const contributorValue = numberValue(contributor.current_value);
          const skillName = textValue(contributor.skill_name) || "待识别技能";
          const sourceJob = textValue(contributor.source_job)
            || professionLabel(contributor.source_id, contributor.source_job);
          const contributorKey = textValue(contributor.key)
            || `${summaryIdentity}:${textValue(contributor.source_id)}:${skillName}:${index}`;
          const child: OverlayRow = {
            key: contributorKey,
            appearanceKey: `debuff-skill:${textValue(contributor.skill_id) || skillName}`,
            colorKey: `debuff:${category}:${mode}`,
            name: `${skillName} ${formatValue(contributorValue, category)}`,
            sourceJob,
            sourceDetail: liveDataSourceDetail(contributor, "effect"),
            detail: contributorEnd ? formatRemainingSeconds(contributorRemaining) : "持续",
            remaining: contributorRemaining,
            duration: numberValue(contributor.duration_seconds),
            permanent: !contributorEnd,
            tone: rowTone(category, label),
            groupOrder: row.groupOrder,
            sortOrder: baseSortOrder + 0.001 + index / 10_000,
            rowKind: "debuff-skill",
            expandable: false,
            expanded: false,
            depth: breakdownMode === "skills" ? 0 : 1,
            iconIndex: numberValue(contributor.icon_index ?? -1),
            compactEffect: breakdownEligible,
          };
          keepBest(
            `debuff-skill:${summaryIdentity}:${contributorKey}`,
            child,
            Math.abs(contributorValue) * 1_000_000
              + (contributorEnd ? contributorRemaining : 999_999),
          );
        });
      }
    }
    const result = [...unique.entries()].map(([identity, item]) => ({
      ...item.row,
      // Packet variants can replace the selected source for the same effective
      // status. Keep the rendered identity stable so Vue does not remount the row.
      key: identity,
    }));
    return result.sort((left, right) => left.sortOrder - right.sortOrder || left.name.localeCompare(right.name, "zh-CN"));
  });
  /**
   * Active self-Buffs that the saved whitelist hides.
   *
   * The empty shell explains itself with this count: "采集中 · 3 项增益未勾选"
   * is actionable, while a bare "暂无增益" looked like a broken capture even
   * though the packets were decoded correctly.
   */
  const hiddenSelectedBuffCount = computed(() => {
    const selectedTarget = targetId.value;
    const whitelist = new Set(settings.value.selectedBuffs);
    if (!selectedTarget || !whitelist.size) return 0;
    const seconds = currentSeconds.value;
    let hidden = 0;
    for (const raw of payload.value?.buffs || []) {
      if (textValue(raw.target_id) !== selectedTarget) continue;
      if (whitelist.has(textValue(raw.catalog_key))) continue;
      if (!Boolean(raw.permanent) && numberValue(raw.end_seconds) <= seconds) continue;
      hidden += 1;
    }
    return hidden;
  });
  const buffRows = computed(() => settings.value.showBuffOverlay ? rows.value.filter(row => row.groupOrder === 0) : []);
  const debuffRows = computed(() => settings.value.showDebuffOverlay ? rows.value.filter(row => row.groupOrder > 0) : []);
  const statusRows = computed(() => rows.value.filter(row => row.groupOrder === 0 ? settings.value.showBuffOverlay : settings.value.showDebuffOverlay));
  function usesIconLayout(row: OverlayRow): boolean {
    if (row.rowKind === "buff") return settings.value.buffLayoutMode === "icons";
    return row.compactEffect && settings.value.statusLayoutMode === "icons";
  }
  const compactEffectRows = computed(() => statusRows.value.filter(usesIconLayout));
  const standardStatusRows = computed(() => statusRows.value.filter(row => !usesIconLayout(row)));

  function toggleDebuffBreakdown(row: OverlayRow): void {
    if (!row.expandable || row.rowKind !== "debuff-summary") return;
    const next = new Set(expandedDebuffKeys.value);
    if (next.has(row.key)) next.delete(row.key);
    else next.add(row.key);
    expandedDebuffKeys.value = next;
  }

  const baseOverlayLayout = computed(() => {
    if (isSkillView.value) {
      const count = context.skillCountdownCount?.() || 0;
      const panel = skillCountdownGeometry(
        count,
        settings.value.fontSize,
        context.identityRowCount?.() || 0,
      );
      return {
        columns: 1,
        rowsPerColumn: Math.max(1, count),
        width: 320,
        height: Math.max(46, 12 + panel.height),
      };
    }
    // Buff and Debuff share one ordered grid, allowing cross-type placement.
    // DOM measurement remains authoritative after the first render.
    const gap = settings.value.gap;
    const standardPanel = standardStatusGridShape(standardStatusRows.value.length);
    if (compactEffectRows.value.length) {
      const iconCount = compactEffectRows.value.length;
      const cardWidth = statusIconCardWidth();
      const cardHeight = statusIconCardHeight();
      const iconGrid = statusIconGridShape(iconCount);
      const standardColumns = Math.max(1, standardPanel.columns);
      const standardWidth = standardPanel.count
        ? standardStatusPanelWidth(standardColumns)
        : 0;
      const iconWidth = 22 + iconGrid.columns * cardWidth + Math.max(0, iconGrid.columns - 1) * gap;
      const iconHeight = iconGrid.rows * cardHeight + Math.max(0, iconGrid.rows - 1) * gap;
      const base = {
        columns: Math.max(iconGrid.columns, standardColumns),
        rowsPerColumn: standardPanel.rows,
        // Icon cards are ~56 px wide, so a single row used to shrink the whole
        // window to a ~120 px sliver and the list looked like it had vanished.
        // Keep the configured column width as the floor.
        width: Math.max(standardWidth, iconWidth, settings.value.columnWidth),
        height: Math.max(46, 12 + standardPanel.height
          + (standardPanel.count ? STATUS_PANEL_GAP : 0) + iconHeight),
      };
      return base;
    }
    const columns = Math.max(1, standardPanel.columns);
    const rowsPerColumn = standardPanel.rows;
    return ({
      columns,
      rowsPerColumn,
      width: standardStatusPanelWidth(columns),
      height: Math.max(46, 12 + standardPanel.height),
    });
  });
  function statusListStyle(source: OverlayRow[]) {
    const grid = standardStatusGridShape(source.length);
    return {
      gridTemplateColumns: `repeat(${Math.max(1, grid.columns)}, minmax(0, 1fr))`,
      gridTemplateRows: `repeat(${grid.rows}, ${settings.value.rowHeight}px)`,
      gap: `${settings.value.gap}px`,
    };
  }
  function statusIconListStyle(source: OverlayRow[]) {
    const cardWidth = statusIconCardWidth();
    const cardHeight = statusIconCardHeight();
    const grid = statusIconGridShape(source.length);
    return {
      gridAutoFlow: "row",
      gridTemplateColumns: `repeat(${grid.columns}, ${cardWidth}px)`,
      gridTemplateRows: `repeat(${grid.rows}, ${cardHeight}px)`,
      gap: `${settings.value.gap}px`,
    };
  }
  const overlayLayout = computed(() => baseOverlayLayout.value);
  const shellStyle = computed(() => {
    const opacity = settings.value.backgroundOpacity / 100;
    const radius = settings.value.borderRadius;
    const fontSize = settings.value.fontSize;
    const panelGap = STATUS_PANEL_GAP;
    return {
      width: `${overlayLayout.value.width}px`,
      minHeight: `${overlayLayout.value.height}px`,
      "--skill-countdown-row-height": `${skillCountdownGeometry(1, settings.value.fontSize).rowHeight}px`,
      "--skill-countdown-header-height": `${skillCountdownGeometry(1, settings.value.fontSize).headerHeight}px`,
      "--overlay-background-opacity": String(opacity),
      "--overlay-row-background-opacity": String(opacity * 0.86),
      "--overlay-handle-background-opacity": String(opacity * 0.68),
      "--overlay-border-opacity": String(0.08 + opacity * 0.24),
      "--overlay-shadow-opacity": String(opacity * 0.14),
      "--overlay-row-radius": `${radius}px`,
      "--overlay-font-size": `${fontSize}px`,
      "--overlay-detail-size": `${Math.max(9, fontSize - 1)}px`,
      "--overlay-panel-gap": `${panelGap}px`,
      "--status-icon-size": `${statusIconSize()}px`,
      "--status-icon-card-width": `${statusIconCardWidth()}px`,
      "--status-icon-card-height": `${statusIconCardHeight()}px`,
      "--status-icon-background-color": settings.value.statusIconBackgroundColor,
      "--status-icon-background-opacity": `${settings.value.statusIconBackgroundOpacity}%`,
      "--status-icon-name-color": settings.value.statusIconNameColor,
      "--status-icon-time-color": settings.value.statusIconTimeColor,
      "--status-icon-name-size": `${settings.value.statusIconNameFontSize}px`,
      "--status-icon-name-line-height": `${settings.value.statusIconNameFontSize + 1}px`,
      "--status-icon-time-size": `${settings.value.statusIconTimeFontSize}px`,
      "--status-icon-time-line-height": `${settings.value.statusIconTimeFontSize + 2}px`,
    };
  });

  function rowStyle(row: OverlayRow) {
    return {
      "--effect-color": settings.value.effectColors[row.colorKey] || settings.value.toneColors[row.tone] || settings.value.toneColors.general,
      "--status-icon-background-color": settings.value.statusIconBackgroundColors[row.appearanceKey]
        || settings.value.statusIconBackgroundColor,
      "--status-icon-name-color": settings.value.statusIconNameColors[row.appearanceKey]
        || settings.value.statusIconNameColor,
      "--status-icon-time-color": settings.value.statusIconTimeColors[row.appearanceKey]
        || settings.value.statusIconTimeColor,
    };
  }

  function rowIconStyle(row: OverlayRow) {
    if (row.iconIndex < 0) return {};
    const page = Math.floor(row.iconIndex / 200) + 1;
    const local = row.iconIndex % 200;
    const iconSize = statusIconSize();
    return {
      backgroundImage: `url(http://127.0.0.1:48710/api/assets/skill-icon-sheet?page=${page})`,
      backgroundPosition: `-${(local % 10) * iconSize}px -${Math.floor(local / 10) * iconSize}px`,
      // The source sheet is 512px wide rather than a tightly packed 500px.
      // Scale by the real sheet width so later columns do not drift sideways.
      backgroundSize: `${iconSize * SKILL_ICON_SHEET_WIDTH / SKILL_ICON_CELL_SIZE}px auto`,
    };
  }

  function rowIconProgressStyle(row: OverlayRow) {
    const progress = row.permanent || row.duration <= 0
      ? 100
      : Math.max(0, Math.min(100, row.remaining / row.duration * 100));
    return { "--remaining-angle": `${progress * 3.6}deg` };
  }

  function rowIconLabel(row: OverlayRow): string {
    if (row.rowKind === "debuff-summary") return "总";
    return [...row.name.replace(/\s+[-+]?\d+(?:\.\d+)?%.*$/, "").trim()][0] || "技";
  }

  function rowWidth(row: OverlayRow): string {
    return row.permanent || row.duration <= 0 ? "100%" : `${Math.max(0, Math.min(100, row.remaining / row.duration * 100))}%`;
  }
  function remainingText(row: OverlayRow): string {
    if (row.detail) return row.detail;
    if (row.permanent) return "常驻";
    return formatRemainingSeconds(row.remaining);
  }

  return {
    windowLabel, isStatusView, isSkillView,
    layoutTitle, textValue, numberValue, captureActive,
    currentSeconds, targetId, target, buffSourceJob, allowedModes,
    categoryEnabled, formatValue, formatRemainingSeconds, rows, buffRows,
    debuffRows, statusRows, standardStatusRows, compactEffectRows,
    hiddenSelectedBuffCount,
    overlayLayout, statusListStyle, statusIconListStyle,
    shellStyle, rowStyle, rowWidth, remainingText,
    rowIconStyle, rowIconProgressStyle, rowIconLabel,
    toggleDebuffBreakdown,
  };
}
