/**
 * 设置页的「退出路径」结构约束。
 *
 * 用户反馈过两次同一个问题：点完保存、滚下去之后回不到最上面，结果退不出设置页。
 * 根因不是滚动本身，而是**返回入口被放在滚动容器里**——滚动一旦异常，唯一的出口
 * 就跟着滚走了。这几条断言把修复后的结构钉住，防止以后又被改回去。
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(
  new URL('../src/views/SettingsView.vue', import.meta.url),
  'utf8',
);

test('页头是滚动容器的兄弟节点，不是它的子节点', () => {
  const headerAt = source.indexOf('class="frost-page-head"');
  const scrollerAt = source.indexOf('class="npcap-page"');

  assert.ok(headerAt > 0, '设置页必须有页头');
  assert.ok(scrollerAt > 0, '设置页必须有滚动容器');
  assert.ok(headerAt < scrollerAt, '页头必须排在滚动容器之前');

  const headerCloseAt = source.indexOf('</header>', headerAt);
  assert.ok(headerCloseAt > headerAt, '页头必须正常闭合');
  assert.ok(
    headerCloseAt < scrollerAt,
    '页头必须在滚动容器开始之前闭合——否则返回按钮会跟着表单一起滚走',
  );
});

test('返回按钮带文字，不是一个只有图标的小圆点', () => {
  const backAt = source.indexOf('class="frost-back"');
  assert.ok(backAt > 0, '设置页必须有返回按钮');
  const button = source.slice(backAt, source.indexOf('</button>', backAt));
  assert.match(button, /<span>返回<\/span>/, '返回按钮要有文字标签，便于一眼看到');
});

test('「回到顶部」是常驻兜底，不依赖 v-if 或滚动事件', () => {
  const topAt = source.indexOf('class="npcap-top"');
  assert.ok(topAt > 0, '必须有回到顶部按钮');

  const button = source.slice(topAt, source.indexOf('</button>', topAt));
  assert.doesNotMatch(button, /v-if/, '兜底按钮不能靠条件显隐：它失效时就正好不在');
  assert.match(button, /@click="scrollToTop"/, '兜底按钮必须直接调用 scrollToTop');

  assert.doesNotMatch(
    source,
    /@scroll/,
    '不要用滚动事件驱动这个按钮——滚动事件本身就是可能失效的那一环',
  );
});

test('设置页监听 Esc 作为键盘出口', () => {
  assert.match(source, /keydown/, '必须监听 keydown');
  assert.match(source, /Escape/, '必须有 Escape 分支');
  assert.match(source, /onDeactivated\(/, 'KeepAlive 缓存下切页要摘监听');
});

test('页面级永不滚动：滚动只发生在页面内部的滚动容器里', () => {
  const css = readFileSync(
    new URL('../src/styles/frost.css', import.meta.url),
    'utf8',
  );
  const anchor = css.indexOf('body.frost-scene #app');
  assert.ok(anchor > 0, '必须存在 #app 的根样式');
  const block = css.slice(anchor, css.indexOf('}', anchor));

  assert.doesNotMatch(
    block,
    /min-height:\s*\d{2,}px/,
    '根节点不能有固定最小高度：窗口比它矮时文档会高过视口，整页可以滚，' +
      '滚下去之后页头连同「← 返回」一起被滚出视口——这正是「滚到最上也退不出设置」',
  );
  assert.doesNotMatch(
    block,
    /min-width:\s*\d{2,}px/,
    '同理，固定最小宽度会让整页横向溢出',
  );
  assert.match(block, /overflow:\s*hidden/, '根节点必须 overflow:hidden');
});

test('设置页滚动容器自己处理滚动链，不把滚轮传给页面', () => {
  const css = readFileSync(
    new URL('../src/styles/frost.css', import.meta.url),
    'utf8',
  );
  const anchor = css.indexOf('.npcap-page {');
  assert.ok(anchor > 0, '必须存在 .npcap-page 规则');
  const block = css.slice(anchor, css.indexOf('}', anchor));
  assert.match(block, /overflow-y:\s*auto/);
  assert.match(block, /overflow-anchor:\s*none/, '保存后插入提示条不能让滚动位置自己跳');
});

test('面板不能被滚动：focus 引发的 scrollIntoView 不能把页头滚走', () => {
  const css = readFileSync(
    new URL('../src/styles/frost.css', import.meta.url),
    'utf8',
  );

  // .frost-panel / .frost-rail 共享的那块
  const shared = css.indexOf('backdrop-filter: var(--frost-blur)');
  assert.ok(shared > 0, '必须存在玻璃面板规则');
  const sharedBlock = css.slice(css.lastIndexOf('.frost-rail {', shared), css.indexOf('}', shared));
  assert.match(
    sharedBlock,
    /overflow:\s*clip/,
    '必须用 clip —— hidden 仍然是可滚动容器，面板里内容一溢出，' +
      '点复选框时 scrollIntoView 就会把整块面板滚上去，页头跟着移出视口',
  );
  assert.doesNotMatch(sharedBlock, /overflow:\s*hidden/, '不能退回 hidden');

  // grid item 的 min-height:auto 会按内容撑高面板，必须压回 0
  const panelAt = css.indexOf('.frost-panel {');
  assert.ok(panelAt > 0, '必须存在 .frost-panel 规则');
  const panelBlock = css.slice(panelAt, css.indexOf('}', panelAt));
  assert.match(panelBlock, /min-height:\s*0/, '面板高度要由 grid 行决定，不能被内容撑高');
});
