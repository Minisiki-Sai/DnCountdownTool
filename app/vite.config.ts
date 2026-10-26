import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const APP_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(APP_DIR, "..");
const VERSION_FILE = resolve(REPO_ROOT, "version.json");

/**
 * 版本真源是仓库根的 version.json。
 * 前端在 dev/build 时直接读它，不经过 package.json，因此改完 version.json 立即生效。
 */
function readVersionSource(): { product: string; runtime: string } {
  try {
    const payload = JSON.parse(readFileSync(VERSION_FILE, "utf-8")) as {
      product?: unknown;
      engine_protocol?: unknown;
    };
    const product = String(payload.product ?? "").trim();
    const runtime = String(payload.engine_protocol ?? "").trim() || product;
    if (product || runtime) {
      return { product: product || runtime, runtime: runtime || product };
    }
  } catch {
    // 文件缺失或格式错误时走下面的兜底，不阻断构建。
  }
  console.warn(`[version] 读不到 ${VERSION_FILE}，前端版本号回退 0.0.0`);
  return { product: "0.0.0", runtime: "0.0.0" };
}

const VERSION = readVersionSource();
// 只在三段式 x.y.0 时去掉末尾 .0（4.88.0 -> 4.88）；否则 5.0 会被削成 5。
const APP_VERSION = VERSION.runtime.replace(/^(\d+\.\d+)\.0$/, "$1");

/**
 * 安装包版本取自 package.json / Cargo.toml / tauri.conf.json，
 * 需要 tools/sync_version.py 落盘。这里只提示漂移，不阻断构建。
 */
function warnOnManifestDrift(): void {
  const manifests: Array<[string, RegExp]> = [
    ["package.json", /"version"\s*:\s*"([^"]+)"/],
    ["src-tauri/Cargo.toml", /^version\s*=\s*"([^"]+)"/m],
    ["src-tauri/tauri.conf.json", /"version"\s*:\s*"([^"]+)"/],
  ];
  for (const [relPath, pattern] of manifests) {
    try {
      const found = readFileSync(resolve(APP_DIR, relPath), "utf-8").match(pattern)?.[1];
      if (found && found !== VERSION.product) {
        console.warn(
          `[version] ${relPath} 是 ${found}，与 version.json 的 ${VERSION.product} 不一致；` +
            `安装包版本会错。请运行 python tools/sync_version.py`,
        );
      }
    } catch {
      // manifest 不存在时忽略，交由打包流程自行报错。
    }
  }
}
warnOnManifestDrift();

export default defineConfig({
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    // Cargo continuously replaces locked DLL/PDB files under this directory.
    // Watching it is unnecessary for the Vue dev server and can raise EBUSY
    // on Windows while `tauri dev` is compiling the Rust application.
    watch: {
      ignored: ["**/src-tauri/target/**"],
    },
  },
  envPrefix: ["VITE_", "TAURI_"],
  build: {
    target: process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    minify: process.env.TAURI_ENV_DEBUG ? false : "esbuild",
    sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
  },
});
