import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
try {
  const page=await browser.newPage({viewport:{width:1400,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/__countdown_preview*',route=>route.fulfill({contentType:'text/html',body:'<html><body><div id="app"></div></body></html>'}));
  await page.goto('http://127.0.0.1:5198/__countdown_preview');
  await page.evaluate(async()=>{
    const {createApp}=await import('/node_modules/.vite/deps/vue.js');
    const {default:Component}=await import('/src/components/BuffOverlaySettingsDialog.vue');
    const {normalizeBuffOverlaySettings}=await import('/src/utils/buffOverlaySettings.ts');
    window.testApp=createApp(Component,{open:true,options:[],summonOptions:[],players:[],
      modelValue:normalizeBuffOverlaySettings({showSkillCountdownOverlay:true}),onSave:value=>window.savedCountdownSettings=value});
    window.testApp.mount('#app');
  });
  await page.getByRole('button',{name:/技能倒计时\/Boss血量/}).click();
  const nests=page.locator('details.countdown-skill-group');
  assert.equal(await nests.count(),4);
  for(const nest of await nests.all())await nest.locator('summary').click();
  const hard=nests.filter({has:page.locator('summary',{hasText:'黑龙硬核'})});
  const sea=nests.filter({has:page.locator('summary',{hasText:'梦魇海龙'})});
  const before=await sea.locator('input:checked').count();
  await hard.getByRole('checkbox',{name:'本副本全选',exact:true}).uncheck();
  assert.equal(await hard.locator('input:checked').count(),0);
  assert.equal(await sea.locator('input:checked').count(),before);
  await hard.getByRole('checkbox',{name:'本副本全选',exact:true}).check();
  await hard.getByRole('checkbox',{name:'二阶段 · 上升重力',exact:true}).uncheck();
  assert.equal(await hard.getByRole('checkbox',{name:'本副本全选',exact:true}).evaluate(e=>e.indeterminate),true);
  await page.getByRole('button',{name:'保存并应用',exact:true}).click();
  const saved=await page.evaluate(()=>window.savedCountdownSettings.selectedSkillCountdowns);
  assert.ok(saved.includes('hard-claw'));assert.ok(!saved.includes('hard-gravity'));assert.ok(saved.includes('sea-ice-eye'));assert.ok(saved.includes('sand-wall'));
  await page.screenshot({path:'../outputs/verification/countdown_nest_settings.png',fullPage:true});
  await page.setViewportSize({width:600,height:900});
  assert.equal(await page.locator('.overlay-settings-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1),true);
  await page.evaluate(async()=>{
    window.testApp.unmount();
    const {createApp}=await import('/node_modules/.vite/deps/vue.js');
    const {default:Component}=await import('/src/components/BuffOverlay.vue');
    const {normalizeBuffOverlaySettings,BUFF_OVERLAY_STORAGE_KEY}=await import('/src/utils/buffOverlaySettings.ts');
    localStorage.setItem(BUFF_OVERLAY_STORAGE_KEY,JSON.stringify(normalizeBuffOverlaySettings({showBossHealthOverlay:true})));
    localStorage.setItem('dragon-nest-v420-buff-overlay-enabled','1');
    window.testApp=createApp(Component,{view:'bossHealth'});window.testApp.mount('#app');
    window.publishBoss=async (rows,endedBossIds=[])=>{
      const {publishOverlayEvent}=await import('/src/utils/overlayEventBus.ts');
      await publishOverlayEvent('combat.snapshot',{state:'running',elapsedSeconds:5,bossHealth:rows,endedBossIds,
        players:[],buffs:[],debuffs:[],cooldowns:[],summons:[],playerHealth:[],buffOptions:[],summonOptions:[]});
    };
  });
  await page.evaluate(()=>window.publishBoss([]));
  assert.equal(await page.locator('.buff-overlay-shell').isVisible(),false);
  const boss={instanceId:'a',targetId:'a',name:'测试Boss',currentHp:50,maxHp:100,confidence:'packet_hp'};
  await page.evaluate(b=>window.publishBoss([b]),boss);
  await page.locator('.boss-health-card').waitFor({state:'visible'});
  await page.evaluate(b=>window.publishBoss([b,{...b,instanceId:'b',targetId:'b',name:'存活Boss'}],['a']),boss);
  await page.getByText('存活Boss',{exact:true}).waitFor({state:'visible'});
  assert.equal(await page.locator('.boss-health-card').count(),1);
  assert.equal(await page.getByText('测试Boss',{exact:true}).count(),0);
  await page.evaluate(b=>window.publishBoss([b],['a']),boss);
  await page.locator('.buff-overlay-shell').waitFor({state:'hidden'});
  await page.evaluate(()=>window.publishBoss([]));
  await page.locator('.buff-overlay-shell').waitFor({state:'hidden'});
  await page.evaluate(b=>window.publishBoss([b]),boss);
  await page.locator('.boss-health-card').waitFor({state:'visible'});
  assert.deepEqual(errors,[]);
  console.log('PASS: four nests, nested selection, independent save, narrow layout, Boss idle hide / fight show / entity-scoped death / hide / reappear');
} finally {await browser.close();}
