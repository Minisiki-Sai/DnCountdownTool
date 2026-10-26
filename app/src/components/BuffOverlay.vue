<script setup lang="ts">
import SkillCountdownPanel from './SkillCountdownPanel.vue';
import { Activity, CheckCircle2, ChevronDown, ChevronRight, GripVertical, Move } from "@lucide/vue";
import { useBuffOverlay } from "../composables/overlay/useBuffOverlay";
import type { OverlayWindowKind } from "../utils/buffOverlaySettings";

const props = withDefaults(defineProps<{ view?: OverlayWindowKind }>(), { view: "status" });
const {
  skillCountdownRows,
  settings, captureNotice,
  layoutMode, overlayShell, isStatusView, isSkillView, layoutTitle,
  statusRows, standardStatusRows, compactEffectRows,
  statusListStyle, statusIconListStyle, shellStyle, rowStyle, rowWidth, remainingText,
  rowIconStyle, rowIconProgressStyle, rowIconLabel,
  beginDrag, finishLayoutMode, toggleDebuffBreakdown,
  showEmptySection, emptyStateText, hiddenSelectedBuffCount,
  skillIdentity, skillIdentityVisible,
} = useBuffOverlay({ view: props.view });
</script>

<template>
  <main
    v-show="layoutMode || statusRows.length || skillCountdownRows.length || captureNotice || showEmptySection || skillIdentityVisible"
    ref="overlayShell"
    class="buff-overlay-shell"
    :class="[view === 'skill' ? 'is-skill' : 'is-status', { 'layout-mode': layoutMode }]"
    :style="shellStyle"
    @pointerdown.left="layoutMode && beginDrag()"
  >
    <section v-if="layoutMode" class="layout-adjust-panel" :title="`${layoutTitle} · 按住窗口拖动，悬停显示完成按钮`">
      <Move :size="19" />
      <div><strong>{{ layoutTitle }}</strong><span>按住此框拖动到需要的位置</span></div>
      <button @pointerdown.stop @click.stop="finishLayoutMode"><CheckCircle2 :size="13" />完成调整</button>
    </section>

    <button v-if="!layoutMode" class="drag-handle" title="按住拖动悬浮窗" @pointerdown.left="beginDrag">
      <GripVertical :size="12" />
    </button>

    <!-- 第一、二行：当前副本与 Boss 的身份。它替代了原来那句「采集中」，
         让窗口在没有任何技能倒计时时也能说明自己在看什么。 -->
    <section v-if="skillIdentityVisible" class="skill-identity" aria-label="副本与 Boss 识别结果">
      <p class="skill-identity-line">
        <span class="skill-identity-key">{{ skillIdentity.nest.prefix }}:</span>
        <strong>{{ skillIdentity.nest.name || "未识别" }}</strong>
        <small
          v-if="skillIdentity.nest.label"
          class="skill-identity-tag"
          :class="skillIdentity.nest.state"
        >{{ skillIdentity.nest.label }}</small>
      </p>
      <p class="skill-identity-line">
        <span class="skill-identity-key">{{ skillIdentity.boss.prefix }}:</span>
        <strong>{{ skillIdentity.boss.name || "未识别" }}</strong>
        <small
          v-if="skillIdentity.boss.label"
          class="skill-identity-tag"
          :class="skillIdentity.boss.state"
        >{{ skillIdentity.boss.label }}</small>
      </p>
    </section>

    <SkillCountdownPanel
      v-if="isSkillView && skillCountdownRows.length"
      :rows="skillCountdownRows"
    />

    <section v-if="!layoutMode && captureNotice" class="capture-notice" :class="captureNotice.tone" role="status" aria-live="polite">
      <Activity :size="15" /><strong>{{ captureNotice.message }}</strong>
    </section>

    <section v-if="isStatusView && standardStatusRows.length" class="overlay-panel status-panel buff-list" :style="statusListStyle(standardStatusRows)">
      <article
        v-for="row in standardStatusRows"
        :key="row.key"
        class="buff-row"
        :class="[row.tone, { 'effect-summary-row': row.expandable, 'effect-skill-row': row.rowKind === 'debuff-skill', 'effect-skill-indented': row.depth > 0 }]"
        :style="rowStyle(row)"
        :title="`${row.name}${row.sourceJob ? ` · ${row.sourceJob}` : ''} · ${remainingText(row)} · ${row.sourceDetail}${row.expandable ? ' · 点击展开贡献技能' : ''}`"
        @click="toggleDebuffBreakdown(row)"
      >
        <i class="buff-progress" :style="{ width: rowWidth(row) }" />
        <div class="buff-copy">
          <span v-if="row.expandable" class="effect-expand-icon"><ChevronDown v-if="row.expanded" :size="12" /><ChevronRight v-else :size="12" /></span>
          <strong>{{ row.name }}<small v-if="settings.showProvider && row.sourceJob"> · {{ row.sourceJob }}</small></strong>
          <span class="buff-remaining">{{ remainingText(row) }}</span>
        </div>
      </article>
    </section>

    <section v-if="isStatusView && compactEffectRows.length" class="overlay-panel status-panel buff-list status-icon-list" :style="statusIconListStyle(compactEffectRows)">
      <article
        v-for="row in compactEffectRows"
        :key="row.key"
        class="status-icon-card"
        :class="[{ 'effect-summary-card': row.expandable, 'effect-skill-card': row.rowKind === 'debuff-skill' }]"
        :style="[rowStyle(row), rowIconProgressStyle(row)]"
        :title="`${row.name}${row.sourceJob ? ` · ${row.sourceJob}` : ''} · ${remainingText(row)} · ${row.sourceDetail}${row.expandable ? ' · 点击展开贡献技能' : ''}`"
        @click="toggleDebuffBreakdown(row)"
      >
        <span class="status-icon-face">
          <i v-if="row.iconIndex >= 0" class="status-skill-icon" :style="rowIconStyle(row)" />
          <b v-else class="status-icon-fallback">{{ rowIconLabel(row) }}</b>
          <i class="status-icon-shade" />
          <i class="status-icon-ring" />
          <span v-if="row.expandable" class="status-icon-expand"><ChevronDown v-if="row.expanded" :size="10" /><ChevronRight v-else :size="10" /></span>
        </span>
        <strong class="status-icon-name">{{ row.name }}</strong>
        <small class="status-icon-time">{{ remainingText(row) }}</small>
      </article>
    </section>

    <section
      v-if="showEmptySection"
      class="overlay-empty"
      :title="view === 'skill' ? '采集进行中，暂无需要预告的 Boss 技能' : '采集进行中；若已挂上增益仍显示此项，请到悬浮窗设置里勾选对应增益'"
    >
      <Activity :size="14" />
      <span>{{ emptyStateText }}</span>
    </section>

    <!-- Rows are up, but the saved whitelist still hides some active Buffs.
         Without this chip the overlay looked incomplete with no explanation. -->
    <section
      v-if="!layoutMode && isStatusView && statusRows.length && hiddenSelectedBuffCount > 0"
      class="overlay-hidden-hint"
      title="这些增益在「悬浮窗设置 → Buff」里没有勾选，点全选即可全部显示"
    >
      <span>另有 {{ hiddenSelectedBuffCount }} 项增益未勾选</span>
    </section>
  </main>
</template>

<style scoped>
/* 磨砂胶囊条：增益/减益浅蓝，Boss 技能暖橙。只改外皮，行内容与刷新逻辑不变。 */
.buff-overlay-shell {
  position: relative;
  box-sizing: border-box;
  min-height: 0;
  height: max-content;
  padding: 8px 8px 8px 19px;
  border-radius: 20px;
  border: 1px solid var(--ov-border);
  background: var(--ov-surface);
  -webkit-backdrop-filter: blur(18px) saturate(150%);
  backdrop-filter: blur(18px) saturate(150%);
  box-shadow: 0 12px 28px var(--ov-shadow), inset 0 1px 0 rgba(255, 255, 255, 0.55);
  user-select: none;
  overflow: hidden;
  color: var(--ov-ink);
  font-family: -apple-system, "SF Pro Text", "Segoe UI Variable Text", "Microsoft YaHei UI", sans-serif;
}

.buff-overlay-shell.is-status {
  --ov-surface: rgba(226, 238, 252, var(--overlay-background-opacity));
  --ov-row: rgba(255, 255, 255, calc(var(--overlay-row-background-opacity) * 0.72));
  --ov-ink: #2c4155;
  --ov-ink-soft: #5a7186;
  --ov-border: rgba(120, 160, 205, calc(0.16 + var(--overlay-background-opacity) * 0.22));
  --ov-shadow: rgba(38, 66, 96, calc(var(--overlay-background-opacity) * 0.16));
  --ov-grip: rgba(70, 100, 130, 0.32);
}

.buff-overlay-shell.is-skill {
  --ov-surface: rgba(252, 238, 225, var(--overlay-background-opacity));
  --ov-row: rgba(255, 255, 255, calc(var(--overlay-row-background-opacity) * 0.9));
  --ov-ink: #6b4526;
  --ov-ink-soft: #8d6a4b;
  --ov-border: rgba(214, 158, 110, calc(0.18 + var(--overlay-background-opacity) * 0.24));
  --ov-shadow: rgba(112, 72, 36, calc(var(--overlay-background-opacity) * 0.16));
  --ov-grip: rgba(120, 80, 44, 0.32);
}

.drag-handle {
  position: absolute;
  z-index: 3;
  left: 2px;
  top: 50%;
  display: grid;
  place-items: center;
  width: 14px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 7px;
  transform: translateY(-50%);
  color: var(--ov-grip);
  background: transparent;
  cursor: move;
  touch-action: none;
  opacity: 0.55;
  transition: opacity 160ms ease, background 160ms ease;
}
.buff-overlay-shell:hover .drag-handle { opacity: 1; background: rgba(255, 255, 255, 0.4); }

.capture-notice {
  position: absolute;
  z-index: 5;
  inset: 6px 7px 6px 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border-radius: 13px;
  color: var(--ov-ink);
  background: var(--ov-surface);
  font-size: var(--overlay-font-size);
  font-weight: 550;
}

.overlay-panel + .overlay-panel { margin-top: var(--overlay-panel-gap); }
.buff-list { display: grid; grid-auto-flow: column; }

.buff-row {
  position: relative;
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  overflow: hidden;
  padding: 0 12px 0 13px;
  border: 1px solid color-mix(in srgb, var(--ov-border) 82%, transparent);
  border-radius: calc(var(--overlay-row-radius) + 6px);
  background: var(--ov-row);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--effect-color) 62%, transparent);
}
.buff-progress {
  position: absolute;
  inset: 0 auto 0 0;
  background: color-mix(in srgb, var(--effect-color) 10%, transparent);
}
.buff-copy {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  height: 100%;
}
.buff-copy strong {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ov-ink);
  font-size: var(--overlay-font-size);
  font-weight: 550;
  letter-spacing: -0.005em;
}
.buff-copy .buff-remaining {
  display: flex;
  flex: 0 0 74px;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  line-height: 1;
  color: var(--ov-ink-soft);
  font-size: var(--overlay-detail-size);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
  text-align: right;
  white-space: nowrap;
}
.buff-copy small { color: var(--ov-ink-soft); font-size: 10px; font-weight: 500; }

.effect-summary-row { padding-left: 6px; cursor: pointer; }
.effect-summary-row:hover { border-color: color-mix(in srgb, var(--effect-color) 46%, var(--ov-border)); }
.effect-expand-icon { display: grid; flex: 0 0 12px; place-items: center; color: color-mix(in srgb, var(--effect-color) 70%, var(--ov-ink)); }
.effect-skill-row { border-style: dashed; background: color-mix(in srgb, var(--ov-row) 86%, var(--effect-color) 8%); }
.effect-skill-indented .buff-copy { padding-left: 14px; }
.effect-skill-indented .buff-copy::before {
  position: absolute;
  left: 3px;
  width: 7px;
  height: 8px;
  border-left: 1px solid color-mix(in srgb, var(--effect-color) 45%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--effect-color) 45%, transparent);
  content: "";
}

.status-icon-list { align-items: start; justify-items: center; }
.status-icon-card {
  display: grid;
  grid-template-rows: calc(var(--status-icon-size) + 4px) auto auto;
  justify-items: center;
  align-content: start;
  box-sizing: border-box;
  width: var(--status-icon-card-width);
  height: var(--status-icon-card-height);
  padding: 0 1px;
  border-radius: max(9px, var(--overlay-row-radius));
  color: var(--status-icon-name-color);
  background: color-mix(in srgb, var(--status-icon-background-color) var(--status-icon-background-opacity), transparent);
  cursor: default;
}
.status-icon-card.effect-summary-card { cursor: pointer; }
.status-icon-face {
  position: relative;
  display: block;
  box-sizing: content-box;
  width: var(--status-icon-size);
  height: var(--status-icon-size);
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--effect-color) 62%, var(--ov-border));
  border-radius: 9px;
  background: var(--status-icon-background-color);
  box-shadow: 0 3px 9px rgba(38, 66, 96, 0.16);
}
.status-skill-icon, .status-icon-shade, .status-icon-ring { position: absolute; inset: 0; border-radius: 8px; }
.status-skill-icon { width: var(--status-icon-size); height: var(--status-icon-size); background-repeat: no-repeat; }
.status-icon-fallback {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--status-icon-name-color);
  font-size: calc(var(--status-icon-name-size) + 7px);
  text-shadow: 0 1px 3px rgba(255, 255, 255, 0.55);
}
.status-icon-shade { z-index: 1; background: conic-gradient(from -90deg, transparent 0 var(--remaining-angle), rgba(9, 18, 26, 0.55) var(--remaining-angle) 360deg); }
.status-icon-ring { z-index: 2; box-sizing: border-box; border: 1px solid rgba(255, 255, 255, 0.55); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--effect-color) 40%, transparent); pointer-events: none; }
.status-icon-expand {
  position: absolute;
  z-index: 3;
  right: -1px;
  bottom: -1px;
  display: grid;
  width: 15px;
  height: 15px;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, 0.85);
  border-radius: 50%;
  color: #fff;
  background: color-mix(in srgb, var(--effect-color) 72%, #33505f);
}
.status-icon-name {
  display: -webkit-box;
  width: calc(var(--status-icon-card-width) - 4px);
  margin-top: 2px;
  overflow: hidden;
  font-size: var(--status-icon-name-size);
  font-weight: 500;
  line-height: var(--status-icon-name-line-height);
  text-align: center;
  text-overflow: ellipsis;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.status-icon-time {
  color: var(--status-icon-time-color);
  font-size: var(--status-icon-time-size);
  font-variant-numeric: tabular-nums;
  line-height: var(--status-icon-time-line-height);
}

.overlay-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 32px;
  color: var(--ov-ink-soft);
  font-size: 11.5px;
}

/* 副本 / Boss 两行身份：字号与技能面板表头一致，不抢行内容的视觉重量。 */
.skill-identity {
  display: grid;
  gap: 1px;
  padding: 2px 5px 4px;
}

.skill-identity-line {
  display: flex;
  align-items: baseline;
  gap: 5px;
  margin: 0;
  min-width: 0;
  color: var(--ov-ink);
  font-size: calc(var(--overlay-font-size, 13px) - 1px);
  line-height: 1.45;
}

.skill-identity-key {
  flex: 0 0 auto;
  color: var(--ov-ink-soft);
}

.skill-identity-line strong {
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  letter-spacing: .01em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-identity-tag {
  flex: 0 0 auto;
  font-size: 10px;
  letter-spacing: .02em;
}

.skill-identity-tag.announced { color: #4f7d5f; }
.skill-identity-tag.muted { color: #a1653c; }
/* 城镇/庭院：识别到了但不是副本，用中性灰避免被读成「这个副本不播报」。 */
.skill-identity-tag.nonNest { color: var(--ov-ink-soft); opacity: .82; }

.overlay-hidden-hint {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 14px;
  padding: 2px 4px 0;
  color: var(--ov-ink-soft);
  font-size: 10px;
  letter-spacing: .02em;
  opacity: .72;
}

/* 位置调整：保持与外皮一致的盒模型，只加虚线提示 */
.buff-overlay-shell.layout-mode {
  outline: 1px dashed rgba(94, 134, 168, 0.55);
  outline-offset: -1px;
  cursor: move;
  overflow: visible;
}
.layout-adjust-panel {
  position: absolute;
  z-index: 6;
  left: 2px;
  top: 50%;
  display: flex;
  align-items: center;
  gap: 9px;
  width: 15px;
  height: 26px;
  padding: 0;
  transform: translateY(-50%);
  color: var(--ov-ink);
  cursor: move;
  touch-action: none;
}
.layout-adjust-panel > svg { width: 12px; height: 12px; flex: 0 0 auto; }
.layout-adjust-panel > div { display: none; }
.layout-adjust-panel button {
  position: absolute;
  left: 16px;
  top: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  width: max-content;
  height: 26px;
  padding: 0 9px;
  border: 1px solid var(--ov-border);
  border-radius: 8px;
  color: var(--ov-ink);
  background: rgba(255, 255, 255, 0.9);
  font-size: 10px;
  font-weight: 600;
  cursor: pointer;
  visibility: hidden;
}
.buff-overlay-shell.layout-mode:hover .layout-adjust-panel button,
.buff-overlay-shell.layout-mode:focus-within .layout-adjust-panel button { visibility: visible; }
</style>
