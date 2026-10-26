#!/usr/bin/env node
/**
 * 语音资源构建期校验。
 *
 * 语音播报是产品核心资源（4 个音色 × 158 条短语 = 722 个 wav，约 53MB）。
 * 这些文件走 Git LFS，一旦没 pull 或在搬运时漏掉，前端只会静默降级成
 * 「无声版」——功能不报错、用户只觉得没声音。所以这里在构建前硬性校验：
 *
 *   1. manifest.json 可解析，voices / phrases 非空
 *   2. 每个 voice 目录下，每条 phrase 的 wav 都必须存在且非空
 *   3. 磁盘上 wav 总数必须等于 EXPECTED_WAV_COUNT（漏传/多传都算错）
 *
 * 任一不满足 → 打印完整缺失清单 → process.exit(1)。
 *
 * 用法：
 *   node scripts/verify_voice_assets.mjs
 *   # 或
 *   pnpm verify:voices
 *
 * 说明：manifest 里 158 条短语，但 Vivian / Serena 各多 44 个历史音频、
 * Vivi2 / Xiaohe 各多 1 个（`跳` 的旧版本，供 qwenVoiceAssets.test.ts 对比
 * 音量）。这些「多余文件」是刻意保留的，不算错误，只在 verbose 下提示。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const APP_ROOT = resolve(SCRIPT_DIR, '..');
const VOICES_ROOT = join(APP_ROOT, 'public', 'voices');
const MANIFEST_PATH = join(VOICES_ROOT, 'qwen3', 'manifest.json');

/** 语音资源应有的 wav 总数。搬运/检出的硬性期望值。 */
const EXPECTED_WAV_COUNT = 722;

/** 单条语音文件的最小体积（字节）。低于它基本可以断定是 LFS 指针或空文件。 */
const MIN_WAV_BYTES = 1024;

function fail(headline, details) {
  console.error('');
  console.error(`[verify:voices] 失败：${headline}`);
  for (const line of details) {
    console.error(`  - ${line}`);
  }
  console.error('');
  console.error('[verify:voices] 语音资源不完整。若是刚克隆的仓库，先执行 `git lfs pull`；');
  console.error('[verify:voices] 若文件确实丢了，产物会是无声音版本，禁止发布。');
  process.exit(1);
}

function readManifest() {
  let raw;
  try {
    raw = readFileSync(MANIFEST_PATH, 'utf8');
  } catch (error) {
    fail(`读不到 ${MANIFEST_PATH}`, [String(error.message)]);
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    return fail('manifest.json 不是合法 JSON', [String(error.message)]);
  }
}

function listWavs(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch (error) {
    fail(`读不到目录 ${dir}`, [String(error.message)]);
  }
  return entries
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith('.wav'))
    .map((entry) => entry.name.slice(0, -'.wav'.length));
}

function main() {
  const manifest = readManifest();

  const voices = Array.isArray(manifest.voices) ? manifest.voices : [];
  const phrases = manifest.phrases && typeof manifest.phrases === 'object' ? manifest.phrases : {};

  const problems = [];

  if (voices.length === 0) {
    problems.push('manifest.voices 为空');
  }
  const phraseEntries = Object.entries(phrases);
  if (phraseEntries.length === 0) {
    problems.push('manifest.phrases 为空');
  }
  if (problems.length > 0) {
    fail('manifest.json 结构不完整', problems);
  }

  const phraseIds = new Set();
  for (const [text, id] of phraseEntries) {
    if (typeof id !== 'string' || id.trim() === '') {
      problems.push(`短语「${text}」没有对应的音频 id`);
      continue;
    }
    phraseIds.add(id);
  }

  // --- 1) 每条短语 × 每个音色都必须有非空 wav ---
  let checked = 0;
  for (const voice of voices) {
    const voiceDir = join(VOICES_ROOT, 'qwen3', voice);
    const present = new Set(listWavs(voiceDir));

    for (const [text, id] of phraseEntries) {
      if (typeof id !== 'string' || id.trim() === '') {
        continue;
      }
      if (!present.has(id)) {
        problems.push(`缺文件：public/voices/qwen3/${voice}/${id}.wav（短语「${text}」）`);
        continue;
      }
      const size = statSync(join(voiceDir, `${id}.wav`)).size;
      if (size < MIN_WAV_BYTES) {
        problems.push(
          `文件过小：public/voices/qwen3/${voice}/${id}.wav（${size} 字节，疑似 LFS 指针或空文件）`,
        );
      }
      checked += 1;
    }
  }

  if (problems.length > 0) {
    fail('短语音频缺失或损坏', problems);
  }

  // --- 2) wav 总数必须等于约定值 ---
  let total = 0;
  const perVoice = new Map();
  for (const voice of voices) {
    const count = listWavs(join(VOICES_ROOT, 'qwen3', voice)).length;
    perVoice.set(voice, count);
    total += count;
  }

  if (total !== EXPECTED_WAV_COUNT) {
    const detail = [...perVoice.entries()].map(([voice, count]) => `${voice}: ${count}`);
    fail(`wav 总数 ${total} ≠ 期望 ${EXPECTED_WAV_COUNT}`, detail);
  }

  // --- 3) 报告 ---
  console.log(
    `[verify:voices] OK  音色 ${voices.length} 个 × 短语 ${phraseIds.size} 条 = 校验 ${checked} 个文件；磁盘 wav 总数 ${total}`,
  );
  const detail = [...perVoice.entries()]
    .map(([voice, count]) => `${voice}=${count}`)
    .join(' ');
  console.log(
    `[verify:voices] 各音色 wav 数：${detail}（短语 ${phraseIds.size} 条 + 历史冗余，冗余是刻意保留的）`,
  );
}

main();
