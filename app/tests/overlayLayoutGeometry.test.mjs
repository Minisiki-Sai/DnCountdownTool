import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';
import { effectScope, ref } from 'vue';

test('compact Boss health reserves two complete cards without the old blank space', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  const scope = effectScope();
  try {
    const { useBuffOverlayPresentation } = await server.ssrLoadModule('/src/composables/overlay/useBuffOverlayPresentation.ts');
    const { DEFAULT_BUFF_OVERLAY_SETTINGS } = await server.ssrLoadModule('/src/utils/buffOverlaySettings.ts');
    scope.run(() => {
      const p = useBuffOverlayPresentation({ props: { view: 'bossHealth' }, payload: ref(null),
        settings: ref({ ...DEFAULT_BUFF_OVERLAY_SETTINGS, showBossHealthOverlay: true }),
        fetchedAt: ref(0), clock: ref(0), baseSeconds: ref(0), effectHistory: ref([]), effectHoverIndex: ref(null),
        layoutMode: ref(true), observedFullDurationTeamBuffs: new Set(), statusPanelGap: 4 });
      assert.equal(p.bossHealthRows.value.length, 2);
      assert.equal(p.overlayLayout.value.width, 320);
      assert.equal(p.overlayLayout.value.height, 99);
      assert.ok(p.overlayLayout.value.height >= 42 * 2 + 3 + 12);
    });
  } finally { scope.stop(); await server.close(); }
});

test('countdown-only positioning reserves full panel height and tracks added/removed rows', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  try {
    const { useBuffOverlayPresentation } = await server.ssrLoadModule('/src/composables/overlay/useBuffOverlayPresentation.ts');
    const { DEFAULT_BUFF_OVERLAY_SETTINGS } = await server.ssrLoadModule('/src/utils/buffOverlaySettings.ts');
    const { skillCountdownGeometry } = await server.ssrLoadModule('/src/utils/skillCountdowns.ts');
    for (const fontSize of [12, 18, 24]) {
      const scope = effectScope();
      scope.run(() => {
        const count = ref(2), layoutMode = ref(true);
        const settings = ref({ ...DEFAULT_BUFF_OVERLAY_SETTINGS, fontSize,
          showBuffOverlay: false, showDebuffOverlay: false, showSkillCountdownOverlay: true });
        const p = useBuffOverlayPresentation({ props: { view: 'status' }, payload: ref(null), settings,
          fetchedAt: ref(0), clock: ref(0), baseSeconds: ref(0), effectHistory: ref([]), effectHoverIndex: ref(null),
          layoutMode, observedFullDurationTeamBuffs: new Set(), statusPanelGap: 4,
          skillCountdownCount: () => count.value });
        const height = p.overlayLayout.value.height;
        assert.equal(height, 12 + skillCountdownGeometry(2, fontSize).height);
        assert.ok(height > 100, 'two complete sample rows, not a 46px empty placeholder');
        assert.equal(p.shellStyle.value.minHeight, `${height}px`);
        layoutMode.value = false;
        assert.equal(p.overlayLayout.value.height, height, 'live and positioning share geometry');
        count.value = 4;
        assert.ok(p.overlayLayout.value.height > height);
        count.value = 0;
        assert.equal(p.overlayLayout.value.height, 46);
        count.value = 2; settings.value.showSkillCountdownOverlay = false;
        assert.equal(p.overlayLayout.value.height, 46);
      });
      scope.stop();
    }
  } finally { await server.close(); }
});

test('real presentation keeps identical row/icon/curve geometry during positioning', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  try {
    const { useBuffOverlayPresentation } = await server.ssrLoadModule('/src/composables/overlay/useBuffOverlayPresentation.ts');
    const { buildOverlayLayoutPreview } = await server.ssrLoadModule('/src/utils/overlayLayoutPreview.ts');
    const { DEFAULT_BUFF_OVERLAY_SETTINGS } = await server.ssrLoadModule('/src/utils/buffOverlaySettings.ts');
    for (const icons of [false, true]) for (const combined of [false, true]) {
      const settings = { ...DEFAULT_BUFF_OVERLAY_SETTINGS, overlayPerformanceMode: combined,
        buffLayoutMode: icons ? 'icons' : 'rows', statusLayoutMode: icons ? 'icons' : 'rows',
        showSummonOverlay: true, showCooldownOverlay: true, selectedCooldownBuffs: ['时间加速','精神爆发'],
        showEffectCurveOverlay: true, showPersonalDpsOverlay: true };
      for (const view of ['status','curves','alerts','summons','cleared','lowHealth']) {
        const scope = effectScope();
        scope.run(() => {
          const payload = ref(buildOverlayLayoutPreview(settings, null));
          const layoutMode = ref(false);
          const presentation = useBuffOverlayPresentation({ props:{ view }, payload, settings:ref(settings),
            fetchedAt:ref(1000),clock:ref(1000),baseSeconds:ref(60),effectHistory:ref([]),effectHoverIndex:ref(null),
            layoutMode,observedFullDurationTeamBuffs:new Set(),statusPanelGap:4 });
          const before = { ...presentation.overlayLayout.value };
          layoutMode.value = true;
          assert.deepEqual(presentation.overlayLayout.value, before, `${view}: icons=${icons}, combined=${combined}`);
          assert.equal(presentation.currentSeconds.value, 60);
          payload.value = { ...payload.value, state:'stopped',elapsedSeconds:90,buffs:[] };
          assert.equal(presentation.captureActive.value, false, 'samples must not override capture lifecycle');
          assert.equal(presentation.currentSeconds.value, 60, 'positioning snapshot stays frozen');
          layoutMode.value = false;
          assert.equal(presentation.buffRows.value.length, 0, 'no fake buffs survive completion');
        });
        scope.stop();
      }
    }
  } finally { await server.close(); }
});
