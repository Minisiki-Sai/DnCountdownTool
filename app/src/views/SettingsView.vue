<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { ArrowLeft, ArrowUp, CheckCircle2, FolderOpen, LoaderCircle, PlayCircle, RefreshCw, ShieldAlert, XCircle } from "@lucide/vue";
import { simulatorApi, type DebugRecordingState, type NpcapConfigPayload } from "../services/api";
import {
  DEFAULT_CAPTURE_HOTKEY,
  DEFAULT_OVERLAY_HOTKEY,
  HOTKEY_STORAGE_KEYS,
  readGlobalHotkeyConfiguration,
} from "../utils/globalHotkeys";

const emit = defineEmits<{ back: []; 'open-replay': [] }>();
const tauriAvailable = Boolean((window as typeof window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);

// 开发者模式：默认关闭，开了才露出「回放实战录像」这类工具页。
// 存在 localStorage，跨启动保留，和抓包开关各自独立。
const DEV_MODE_KEY = "dragon-nest-v420-developer-mode";
const developerMode = ref(localStorage.getItem(DEV_MODE_KEY) === "1");
function toggleDeveloperMode() {
  developerMode.value = !developerMode.value;
  localStorage.setItem(DEV_MODE_KEY, developerMode.value ? "1" : "0");
}

const npcapPath = ref("");
const config = ref<NpcapConfigPayload | null>(null);
const busy = ref<"" | "detect" | "test" | "save" | "clear">("");
const message = ref("");
const messageTone = ref<"" | "is-ok" | "is-warn" | "is-error">("");
const adapterCount = ref(0);
const savedHint = ref("");

const captureHotkey = ref(localStorage.getItem(HOTKEY_STORAGE_KEYS.capture) || DEFAULT_CAPTURE_HOTKEY);
const overlayHotkey = ref(localStorage.getItem(HOTKEY_STORAGE_KEYS.overlay) || DEFAULT_OVERLAY_HOTKEY);
const hotkeyMessage = ref("");
const hotkeyError = ref(false);
const minimizeToTray = ref(localStorage.getItem("dragon-nest-v420-minimize-to-tray") !== "0");
const trayMessage = ref("");

const debug = ref<DebugRecordingState | null>(null);
const debugDir = ref("");
const debugBusy = ref(false);
const debugMessage = ref("");
const debugTone = ref<"" | "is-ok" | "is-warn" | "is-error">("");

const scrollHost = ref<HTMLElement | null>(null);

/* 硬跳，不做平滑动画：这个按钮的存在意义就是「无论如何都能到顶」，
   smooth 在系统关闭动态效果时不一定生效，也容易被后续的滚动事件打断。 */
function scrollToTop() {
  const host = scrollHost.value;
  if (host) host.scrollTop = 0;
}

function formatBytes(value: number) {
  if (!value) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

const probeSummary = computed(() => {
  if (!debug.value?.enabled) return "未开启";
  if (!debug.value?.probePath) return "已开启 · 等待本轮第一份数据";
  return `${debug.value.probeRows} 行 · ${formatBytes(debug.value.probeBytes)}`;
});

const captureSummary = computed(() => {
  if (!debug.value?.lastCapturePath) {
    return debug.value?.enabled
      ? "本轮结束后保存到上面的目录"
      : "未保存（关闭开关时抓包在统计结束后删除）";
  }
  return `${debug.value.lastCaptureAt} · ${formatBytes(debug.value.lastCaptureBytes)}`;
});

function setDebugMessage(text: string, tone: "" | "is-ok" | "is-warn" | "is-error") {
  debugMessage.value = text;
  debugTone.value = tone;
}

function applyDebug(next: DebugRecordingState) {
  debug.value = next;
  debugDir.value = next.configuredDir || "";
}

async function loadDebug() {
  try {
    applyDebug(await simulatorApi.debugRecording());
  } catch (reason) {
    setDebugMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  }
}

async function toggleDebug(event: Event) {
  const enabled = (event.target as HTMLInputElement).checked;
  debugBusy.value = true;
  setDebugMessage("", "");
  try {
    const next = await simulatorApi.saveDebugRecording({ enabled });
    applyDebug(next);
    setDebugMessage(
      enabled
        ? `已开启：观测日志与抓包都会保存到 ${next.outputDir}`
        : "已关闭：抓包在统计结束后删除，不再写观测日志。",
      enabled ? "is-ok" : "",
    );
  } catch (reason) {
    setDebugMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
    await loadDebug();
  } finally {
    debugBusy.value = false;
  }
}

async function saveDebugDir() {
  debugBusy.value = true;
  try {
    const next = await simulatorApi.saveDebugRecording({ outputDir: debugDir.value.trim() });
    applyDebug(next);
    setDebugMessage(
      next.error || `已保存到 ${next.outputDir}`,
      next.error ? "is-error" : "is-ok",
    );
  } catch (reason) {
    setDebugMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
    await loadDebug();
  } finally {
    debugBusy.value = false;
  }
}

async function chooseDebugDir() {
  if (!tauriAvailable) {
    const typed = window.prompt("调试记录保存目录", debugDir.value);
    if (typed !== null) debugDir.value = typed;
    return;
  }
  try {
    const picked = await invoke<string | null>("pick_record_folder", {
      current: debugDir.value || debug.value?.outputDir || "",
    });
    if (picked) debugDir.value = picked;
  } catch (reason) {
    setDebugMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  }
}

async function clearDebugDir() {
  debugDir.value = "";
  await saveDebugDir();
}

const sourceLabel = computed(() => ({
  configured: "手动指定",
  portable: "随包组件",
  system: "系统安装",
  missing: "未找到",
}[config.value?.source || "missing"]));
const configuredInvalid = computed(() =>
  Boolean(config.value?.configuredPath) && config.value?.configuredValid === false);

function setMessage(text: string, tone: "" | "is-ok" | "is-warn" | "is-error") {
  message.value = text;
  messageTone.value = tone;
}

function applyConfig(next: NpcapConfigPayload) {
  config.value = next;
  npcapPath.value = next.configuredPath || "";
  adapterCount.value = next.adapterCount || 0;
}

function describeConfig(next: NpcapConfigPayload) {
  if (!next.installed) {
    setMessage(next.message || "未找到 Npcap：请指定 wpcap.dll，或安装随包 / 官方 Npcap。", "is-warn");
    return;
  }
  if (next.configuredPath && next.configuredValid === false) {
    setMessage(next.message, "is-error");
    return;
  }
  setMessage(next.message, "is-ok");
}

async function loadConfig() {
  try {
    applyConfig(await simulatorApi.npcapConfig());
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  }
}

async function choose(kind: "file" | "folder") {
  if (!tauriAvailable) {
    const typed = window.prompt(kind === "file" ? "wpcap.dll 完整路径" : "Npcap 安装目录");
    if (typed) npcapPath.value = typed;
    return;
  }
  try {
    const picked = await invoke<string | null>(kind === "file" ? "pick_npcap_path" : "pick_npcap_folder");
    if (picked) {
      npcapPath.value = picked;
      savedHint.value = "已选择路径，点「保存」生效。";
    }
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  }
}

async function savePath() {
  busy.value = "save";
  savedHint.value = "";
  try {
    const next = await simulatorApi.saveNpcapConfig(npcapPath.value.trim());
    applyConfig(next);
    describeConfig(next);
    savedHint.value = next.configuredPath
      ? `已保存到 ${next.configPath}`
      : "已恢复自动检测。";
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  } finally {
    busy.value = "";
  }
}

async function clearPath() {
  npcapPath.value = "";
  busy.value = "clear";
  await savePath();
  busy.value = "";
  setMessage("已清除手动路径，改为自动检测随包组件与系统 Npcap。", "is-ok");
}

async function detect() {
  busy.value = "detect";
  setMessage("正在检查随包组件、系统目录与手动路径…", "");
  try {
    const result = await simulatorApi.detectNpcap();
    await loadConfig();
    if (result.installed) {
      setMessage(`已找到可用的 Npcap：${result.resolvedPath}`, "is-ok");
    } else {
      const hint = result.candidates.length
        ? `检测到 ${result.candidates.length} 个候选文件，但均不可加载。`
        : "未找到 Npcap：随包目录与系统目录均无 wpcap.dll。";
      setMessage(hint, "is-warn");
    }
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  } finally {
    busy.value = "";
  }
}

async function testConnection() {
  busy.value = "test";
  setMessage("正在加载 Npcap 并枚举抓包接口…", "");
  try {
    const result = await simulatorApi.testNpcap();
    adapterCount.value = result.adapterCount || 0;
    if (result.ok) {
      setMessage(`测试抓包成功：已加载 ${result.resolvedPath}，可用接口 ${result.adapterCount} 个。`, "is-ok");
    } else {
      const stage = {
        load: "无法加载 wpcap.dll（文件缺失或位数不匹配）",
        permission: "权限不足：请以管理员身份运行，或重新安装 Npcap 时取消“仅管理员访问”",
        adapter: "没有可用抓包接口：请确认网卡已启用并已连接游戏",
        unknown: "未知错误",
        "": "抓包测试失败",
      }[result.failedStage || "unknown"];
      setMessage(`${stage}${result.message ? `：${result.message}` : ""}`, "is-error");
    }
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  } finally {
    busy.value = "";
  }
}

async function installNpcap() {
  if (!tauriAvailable) return;
  try {
    await invoke("install_npcap", { installerPath: null });
    setMessage("已启动随包 Npcap 安装程序；完成后请点击「检测」。", "");
  } catch (reason) {
    setMessage(reason instanceof Error ? reason.message : String(reason), "is-error");
  }
}

function keyName(event: KeyboardEvent) {
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(event.key)) return event.key;
  if (event.key.length === 1 && /[a-z0-9]/i.test(event.key)) return event.key.toUpperCase();
  return "";
}

function recordHotkey(event: KeyboardEvent, target: "capture" | "overlay") {
  event.preventDefault();
  if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
  const key = keyName(event);
  if (!key) return;
  const parts = [event.ctrlKey ? "Ctrl" : "", event.altKey ? "Alt" : "", event.shiftKey ? "Shift" : "", event.metaKey ? "Super" : "", key].filter(Boolean);
  if (parts.length < 2) {
    hotkeyError.value = true;
    hotkeyMessage.value = "快捷键至少需要 Ctrl、Alt、Shift 或 Win 中的一个修饰键。";
    return;
  }
  if (target === "capture") captureHotkey.value = parts.join("+");
  else overlayHotkey.value = parts.join("+");
  hotkeyError.value = false;
  hotkeyMessage.value = "";
}

async function saveHotkeys() {
  hotkeyError.value = false;
  hotkeyMessage.value = "";
  if (captureHotkey.value === overlayHotkey.value) {
    hotkeyError.value = true;
    hotkeyMessage.value = "两个功能不能使用同一个快捷键。";
    return;
  }
  try {
    if (tauriAvailable) {
      await invoke("configure_global_hotkeys", {
        ...readGlobalHotkeyConfiguration(),
        startCaptureHotkey: captureHotkey.value,
        toggleOverlayHotkey: overlayHotkey.value,
      });
    }
    localStorage.setItem(HOTKEY_STORAGE_KEYS.capture, captureHotkey.value);
    localStorage.setItem(HOTKEY_STORAGE_KEYS.overlay, overlayHotkey.value);
    hotkeyMessage.value = "快捷键已保存并立即生效。";
  } catch (reason) {
    hotkeyError.value = true;
    hotkeyMessage.value = reason instanceof Error ? reason.message : String(reason);
  }
}

async function saveTray() {
  const enabled = minimizeToTray.value;
  try {
    if (tauriAvailable) await invoke("set_minimize_to_tray", { enabled });
    localStorage.setItem("dragon-nest-v420-minimize-to-tray", enabled ? "1" : "0");
    trayMessage.value = enabled ? "关闭窗口后继续在系统托盘运行。" : "关闭窗口即退出程序。";
  } catch (reason) {
    minimizeToTray.value = !enabled;
    trayMessage.value = reason instanceof Error ? reason.message : String(reason);
  }
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  await loadConfig();
  await loadDebug();
  if (!tauriAvailable) return;
  try {
    minimizeToTray.value = await invoke<boolean>("get_minimize_to_tray");
  } catch {
    // Older shells keep the stored value.
  }
});

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
});

/* 设置页被 KeepAlive 缓存：切走时组件不会卸载，Esc 监听必须跟着摘掉，
   否则在主面板按 Esc 还会继续触发「返回」。 */
onActivated(() => window.addEventListener("keydown", onKeydown));
onDeactivated(() => window.removeEventListener("keydown", onKeydown));

/* Esc 是第三个出口：窗口被拖到屏幕外、或者滚动容器出问题时，键盘仍然能把
   人送回主面板。快捷键录制框里的 Esc 表示「取消录制」，那种情况让给输入框。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const target = event.target as HTMLElement | null;
  if (target && target.classList.contains("frost-hotkey")) return;
  event.preventDefault();
  emit("back");
}
</script>

<template>
  <!-- 页头（含返回按钮）留在滚动容器**外面**：滚动位置再怎么变、滚轮再怎么失灵，
       返回入口都永远在同一处点得到。之前它和表单同在一个滚动容器里靠 sticky 吸顶，
       一旦滚动异常就退不出设置页。 -->
  <div class="npcap-view">
    <header class="frost-page-head">
      <button class="frost-back" type="button" title="返回主面板（Esc）" @click="emit('back')">
        <ArrowLeft :size="16" /><span>返回</span>
      </button>
      <div>
        <h1>设置</h1>
        <p>抓包组件、快捷键与窗口，以及调试记录的保存开关。</p>
      </div>
    </header>

    <div ref="scrollHost" class="npcap-page">
    <div class="frost-form">
      <label class="frost-field">
        <span>Npcap 库与驱动路径</span>
        <div class="frost-input-row">
          <input
            v-model="npcapPath"
            class="frost-input"
            type="text"
            spellcheck="false"
            autocomplete="off"
            :class="{ 'is-invalid': configuredInvalid }"
            placeholder="留空自动检测：随包 third_party/npcap → 系统 System32"
          />
          <button class="frost-btn is-primary" type="button" :disabled="busy !== ''" @click="savePath">
            <LoaderCircle v-if="busy === 'save'" :size="15" class="frost-spin" />保存
          </button>
        </div>
        <div class="frost-input-row is-secondary">
          <button class="frost-btn" type="button" :disabled="busy !== ''" @click="choose('file')">
            <FolderOpen :size="15" />选择 DLL
          </button>
          <button class="frost-btn" type="button" :disabled="busy !== ''" @click="choose('folder')">
            <FolderOpen :size="15" />选择目录
          </button>
          <button class="frost-btn" type="button" :disabled="busy !== '' || !config?.configuredPath" @click="clearPath">
            <LoaderCircle v-if="busy === 'clear'" :size="15" class="frost-spin" />恢复自动检测
          </button>
        </div>
        <small>支持 wpcap.dll 完整路径、Npcap 安装目录（如 C:\Windows\System32\Npcap）或随包组件目录；留空表示自动检测。</small>
        <small v-if="savedHint" class="is-ok">{{ savedHint }}</small>
      </label>

      <div class="frost-inline">
        <button class="frost-btn" type="button" :disabled="busy !== ''" @click="detect">
          <LoaderCircle v-if="busy === 'detect'" :size="15" class="frost-spin" />
          <RefreshCw v-else :size="15" />检测
        </button>
        <button class="frost-btn" type="button" :disabled="busy !== ''" @click="testConnection">
          <LoaderCircle v-if="busy === 'test'" :size="15" class="frost-spin" />
          <ShieldAlert v-else :size="15" />测试连接
        </button>
        <button v-if="config?.installed === false" class="frost-btn" type="button" @click="installNpcap">安装随包 Npcap</button>
      </div>

      <p v-if="message" class="frost-note" :class="messageTone">
        <CheckCircle2 v-if="messageTone === 'is-ok'" :size="15" />
        <XCircle v-else-if="messageTone === 'is-error'" :size="15" />
        <ShieldAlert v-else :size="15" />
        <span>{{ message }}</span>
      </p>

      <dl class="frost-kv">
        <dt>当前来源</dt>
        <dd>{{ sourceLabel }}</dd>
        <dt>实际加载</dt>
        <dd>{{ config?.resolvedPath || "—" }}</dd>
        <dt>抓包接口</dt>
        <dd>{{ !config?.installed ? "尚未就绪" : adapterCount ? `${adapterCount} 个可用接口` : "组件已找到，点「测试连接」可枚举接口" }}</dd>
        <dt>配置文件</dt>
        <dd class="is-mono">{{ config?.configPath || "—" }}</dd>
      </dl>

      <p class="frost-section-title">快捷键与窗口</p>
      <div class="frost-subpanel">
        <div class="frost-inline">
          <label class="frost-check">
            <span>一键开始：统计 + 悬浮窗</span>
            <input class="frost-hotkey" :value="captureHotkey" readonly @keydown="recordHotkey($event, 'capture')" @click="($event.target as HTMLInputElement).select()" />
          </label>
          <label class="frost-check">
            <span>一键结束：停统计 + 关悬浮窗</span>
            <input class="frost-hotkey" :value="overlayHotkey" readonly @keydown="recordHotkey($event, 'overlay')" @click="($event.target as HTMLInputElement).select()" />
          </label>
          <button class="frost-btn" type="button" @click="saveHotkeys">保存快捷键</button>
        </div>
        <p class="frost-hotkey-help">
          两个键都是确定的单向动作，不来回切换：开始键负责把统计和悬浮窗一起打开，结束键负责一起关掉。
          游戏独占键盘时热键可能被游戏吞掉，这时点界面上的按钮或托盘菜单同样生效。
        </p>
        <label class="frost-check">
          <input v-model="minimizeToTray" type="checkbox" @change="saveTray" />
          关闭窗口时最小化到系统托盘
        </label>
        <p v-if="hotkeyMessage" class="frost-note" :class="{ 'is-error': hotkeyError }">{{ hotkeyMessage }}</p>
        <p v-else-if="trayMessage" class="frost-note">{{ trayMessage }}</p>
      </div>

      <p class="frost-section-title">调试记录</p>
      <div class="frost-subpanel">
        <label class="frost-switch" :class="{ 'is-busy': debugBusy }">
          <span class="frost-switch-copy">
            <strong>保留本轮抓包与观测日志</strong>
            <small>一个开关同时控制两项：原始抓包（.dpscap，可事后离线复算）与解码观测日志（.jsonl，记录地图包、Boss 实体与技能号）。关闭时抓包在统计结束后立即删除。</small>
          </span>
          <input type="checkbox" :checked="debug?.enabled === true" :disabled="debugBusy" @change="toggleDebug" />
          <span class="frost-switch-control" aria-hidden="true" />
        </label>

        <label class="frost-field">
          <span>保存目录</span>
          <div class="frost-input-row">
            <input
              v-model="debugDir"
              class="frost-input"
              type="text"
              spellcheck="false"
              autocomplete="off"
              :placeholder="`留空使用默认：${debug?.defaultDir || ''}`"
            />
            <button class="frost-btn is-primary" type="button" :disabled="debugBusy" @click="saveDebugDir">
              <LoaderCircle v-if="debugBusy" :size="15" class="frost-spin" />保存
            </button>
          </div>
          <div class="frost-input-row is-secondary">
            <button class="frost-btn" type="button" :disabled="debugBusy" @click="chooseDebugDir">
              <FolderOpen :size="15" />选择目录
            </button>
            <button class="frost-btn" type="button" :disabled="debugBusy || !debug?.configuredDir" @click="clearDebugDir">
              恢复默认
            </button>
          </div>
          <small>抓包写到 {{ debug?.captureDir || "—" }}；观测日志写到 {{ debug?.probeDir || "—" }}。</small>
        </label>

        <dl class="frost-kv">
          <dt>开关状态</dt>
          <dd>{{ debug?.enabled ? "已开启" : "已关闭" }}</dd>
          <dt>观测日志</dt>
          <dd>{{ probeSummary }}</dd>
          <dt>日志文件</dt>
          <dd class="is-mono">{{ debug?.probePath || "—" }}</dd>
          <dt>最近保留的抓包</dt>
          <dd>{{ captureSummary }}</dd>
          <dt>配置文件</dt>
          <dd class="is-mono">{{ debug?.configPath || "—" }}</dd>
        </dl>

        <p v-if="debugMessage" class="frost-note" :class="debugTone">
          <CheckCircle2 v-if="debugTone === 'is-ok'" :size="15" />
          <XCircle v-else-if="debugTone === 'is-error'" :size="15" />
          <ShieldAlert v-else :size="15" />
          <span>{{ debugMessage }}</span>
        </p>

        <label class="frost-switch">
          <span class="frost-switch-copy">
            <strong>开发者模式</strong>
            <small>打开后可以导入抓包离线回放：按当时的节奏重放语音播报和悬浮窗倒计时，用来验证机制提示对不对，不用真去打一场。</small>
          </span>
          <input
            type="checkbox"
            :checked="developerMode"
            data-testid="developer-mode-toggle"
            @change="toggleDeveloperMode"
          />
          <span class="frost-switch-control" aria-hidden="true" />
        </label>

        <div v-if="developerMode" class="frost-input-row">
          <button
            class="frost-btn is-primary"
            type="button"
            data-testid="open-replay"
            @click="emit('open-replay')"
          >
            <PlayCircle :size="15" />回放实战录像
          </button>
        </div>
      </div>
    </div>
    </div>

    <!-- 不依赖滚轮的兜底出口：常驻在右下角，点一下直接回到最上面。
         这里刻意不用滚动事件控制显隐 —— 兜底按钮本身如果依赖滚动事件，
         滚动一出问题它就不出现了，正好在最需要它的时候失效。 -->
    <button class="npcap-top" type="button" title="回到最上面" @click="scrollToTop">
      <ArrowUp :size="14" />回到顶部
    </button>
  </div>
</template>

<style scoped>
.frost-form { padding-bottom: 56px; }
.frost-check span { color: var(--frost-ink-2); }
</style>
