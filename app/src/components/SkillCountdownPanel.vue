<script setup lang="ts">
import { computed } from 'vue';
const props = defineProps<{ rows: { key: string; label: string; ready: boolean; remaining: number; progress: number }[] }>();
const readyRows = computed(() => props.rows.filter(row => row.ready));
const pendingRows = computed(() => props.rows.filter(row => !row.ready));
</script>
<template>
  <section class="skill-countdown-panel" aria-label="Boss 技能倒计时">
    <header><strong>Boss 技能</strong><small>预计冷却</small></header>
    <div v-if="readyRows.length" class="skill-ready-row" aria-label="冷却已好（估算）">
      <span class="skill-ready-label">已好</span>
      <div class="skill-ready-list">
        <span v-for="row in readyRows" :key="row.key" :title="`${row.label}：预计冷却完成，等待实际释放`">{{ row.label }}</span>
      </div>
    </div>
    <article v-for="row in pendingRows" :key="row.key" class="skill-countdown-row" :class="{ urgent: row.remaining <= 5 }">
      <i :style="{ width: `${row.progress}%` }" />
      <strong>{{ row.label }}</strong>
      <b>{{ Math.ceil(row.remaining) }}s</b>
    </article>
    <p v-if="!rows.length" class="skill-empty">等待技能识别</p>
  </section>
</template>
<style scoped>
.skill-countdown-panel {
  box-sizing: border-box;
  padding: 3px 1px;
  color: var(--ov-ink, #6b4526);
  font-size: var(--overlay-font-size);
  font-family: inherit;
  line-height: 1.5;
}
.skill-countdown-panel header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  height: var(--skill-countdown-header-height);
  margin: 0 4px 2px;
}
.skill-countdown-panel header strong { font-size: calc(var(--overlay-font-size) - 1px); font-weight: 600; letter-spacing: 0.02em; }
.skill-countdown-panel header small { color: var(--ov-ink-soft, #8d6a4b); font-size: 10px; }

.skill-ready-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: var(--skill-countdown-row-height);
  margin-top: 4px;
  padding: 3px 9px;
  border-radius: 999px;
  background: rgba(255, 255, 255, calc(var(--overlay-row-background-opacity) * 0.86));
  color: #4f7d5f;
}
.skill-ready-label { flex: 0 0 auto; font-size: 10.5px; font-weight: 600; opacity: 0.85; }
.skill-ready-list { display: flex; flex: 1; flex-wrap: wrap; gap: 5px; min-width: 0; }
.skill-ready-list span {
  box-sizing: border-box;
  max-width: 100%;
  min-width: 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(122, 176, 140, 0.18);
  overflow-wrap: anywhere;
}

.skill-countdown-row {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  box-sizing: border-box;
  height: var(--skill-countdown-row-height);
  margin-top: 4px;
  padding: 0 12px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(255, 255, 255, calc(var(--overlay-row-background-opacity) * 0.9));
  color: var(--ov-ink, #6b4526);
}
.skill-countdown-row i {
  position: absolute;
  inset: 0 auto 0 0;
  background: rgba(223, 167, 113, 0.3);
  pointer-events: none;
}
.skill-countdown-row strong,
.skill-countdown-row b { position: relative; z-index: 1; }
.skill-countdown-row strong {
  overflow: hidden;
  font-weight: 550;
  letter-spacing: -0.005em;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.skill-countdown-row b {
  flex-shrink: 0;
  font-size: var(--overlay-detail-size, 11px);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.skill-countdown-row.urgent i { background: rgba(214, 122, 74, 0.4); }
.skill-countdown-row.urgent b { color: #b0603a; }
.skill-empty {
  margin: 4px 0;
  padding: 4px 10px;
  color: var(--ov-ink-soft, #8d6a4b);
  font-size: 11px;
  text-align: center;
}
</style>
