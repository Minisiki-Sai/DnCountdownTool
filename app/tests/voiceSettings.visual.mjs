import { chromium } from 'playwright-core';
import assert from 'node:assert/strict';
const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
try {
  const page = await browser.newPage({viewport:{width:1200,height:1000}});
  await page.route('**/__voice_preview',route=>route.fulfill({contentType:'text/html',body:'<html><head></head><body><div id="app"></div></body></html>'}));
  await page.goto('http://127.0.0.1:5197/__voice_preview');
  await page.evaluate(async()=>{
    const {createApp} = await import('/node_modules/.vite/deps/vue.js');
    const {default:Component} = await import('/src/components/VoiceAnnouncementSettings.vue');
    createApp(Component,{running:false,live:false,nest:'梦魇海龙'}).mount('#app');
  });
  await page.getByRole('button',{name:'语音播报 · 已关闭',exact:true}).click();
  const groups = page.locator('.voice-boss-group');
  assert.equal(await groups.count(),3);
  assert.equal(await groups.nth(0).locator('.voice-rule:visible').count(),5);
  assert.equal(await groups.nth(1).locator('.voice-rule:visible').count(),0);
  await groups.nth(1).getByRole('button').first().click();
  assert.equal(await groups.nth(1).locator('.voice-rule:visible').count(),6);
  await page.getByRole('checkbox',{name:'天启全部播报',exact:true}).uncheck();
  assert.match(await groups.nth(1).innerText(),/已开启 0\/6/);
  assert.match(await groups.nth(0).innerText(),/已开启 5\/5/);
  await page.getByRole('checkbox',{name:'天启全部播报',exact:true}).check();
  await groups.nth(1).getByRole('button').first().click();
  await page.screenshot({path:'../outputs/verification/voice_settings_boss_groups.png',fullPage:true});
  await page.setViewportSize({width:560,height:850});
  assert.equal(await page.locator('.voice-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
  console.log('Three groups, collapse, group select isolation and narrow layout passed');
} finally { await browser.close(); }
