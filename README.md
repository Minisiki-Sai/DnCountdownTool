# DN倒计时工具

基于 Npcap 抓包的游戏倒计时工具，只有两个悬浮窗：**增益 / 减益倒计时** 与 **Boss 技能倒计时**。
界面为 macOS 毛玻璃极简风，设置导航只有「Ncap 路径」。

运行时只依赖 Python 标准库（`>=3.11`），没有任何第三方 Python 包。

## 目录导览

```
main.py                  引擎入口：解析 --host/--port 后交给 server.serve()
server.py                本地 HTTP 服务：抓包控制 / 悬浮窗数据 / Npcap 路径
engine/
  core.py                抓包与解析核心
  legacy_timeline.py     旧版时间轴实现（被 core 复用）
  live/                  报文解析、事件匹配、悬浮窗快照
    skill_identity.py      技能身份归一化（实时链路与评分链路共用的最小公共层，不依赖 scoring）
    protocol.py            抓包协议编解码
    score_sync.py          匿名队伍评分上报
    wire/                  协议分体解码器（actor / combat / profile / status）
  native/                业务服务层（采集、悬浮窗、回放评分、技能身份）
  data/modular/          配装与技能表
  data/profiles/         Boss 技能 / 动作 / 血量档位 / 客户端协议配置
  assets/                图标等静态资源
  build_support/         Windows 版本信息资源
tests/                   后端 unittest（stdlib，无 pytest）
tools/
  dev.ps1                  开发启动器（后端 + Vite/桌面壳，自动重启）
  sync_version.py          把 version.json 的版本号铺到 4 个打包清单
  probe_report.py          观测日志报告
  capture_replay.py        抓包回放
app/                     前端（Vue 3 + Vite + Tauri 桌面壳）
  src/App.vue              主面板 + 右侧 Ncap 导航
  src/views/               状态页 / 回放页 / 设置页
  src/components/          BuffOverlay.vue、SkillCountdownPanel.vue 两个悬浮窗
  public/voices/           语音播报资源（4 个音色）
  src-tauri/               Rust 壳：窗口、托盘、快捷键、位置记忆
version.json             唯一版本真源
```

## 版本号从哪来

**唯一需要手改的文件是 `version.json`。** 其它任何地方出现的版本号都是它的下游，
不要手改。

```
version.json                      ← 只有这里要人改
   product         = "4.88.0"     打包元数据（包名 / 安装器 / Cargo / pyproject）
   engine_protocol = "4.88"       运行时握手与前端显示（去掉尾部 .0 后与 product 等价）
        │
        ├── 构建期读取（前端）──> app/vite.config.ts 读文件 → define 注入 __APP_VERSION__
        │                          └─> app/src/appVersion.ts ──> 界面上的 V4.88
        │                          （不经过 package.json，改完 version.json 立即生效）
        │
        ├── python tools/sync_version.py ──> app/package.json
        │                                    app/src-tauri/Cargo.toml
        │                                    app/src-tauri/tauri.conf.json
        │                                    pyproject.toml
        │
        └── 运行时读取（后端）──> server.py  _version_from_file()
                                     └─> VERSION ──> GET /api/health 的 "version"
```

> 前端与后端是**两个独立读取方**，都直读 `version.json`：前端在 Vite 构建/开发时读（
> `define` 内联成编译期常量），后端在 `import server` 时读。两者都不经过
> `package.json`，所以改了 `version.json` 后**前端立刻显示新号**，不必先跑 sync。
> `sync_version.py` 只负责"安装包版本"那一层（NSIS 安装器与 npm/cargo 元数据）。


发版三步：

```bash
# 1. 只改 version.json（product + engine_protocol）
# 2. 铺到 4 个打包清单
python tools/sync_version.py
# 3. 确认没有漂移（可做 CI 门禁，有漂移退出码 1）
python tools/sync_version.py --check
```

`sync_version.py` 只做**行级替换**，不做 TOML/JSON 反序列化重写，所以引号风格、
键顺序、空行、注释都不会被动到；值已经正确时不写文件。`--check` 会打印每个文件的
`旧值 -> 新值` 并列出漂移清单。

### 前端显示的版本号（构建期直读，不写死）

`app/vite.config.ts` 在 dev / build 时**直接读**仓库根的 `version.json`：

- 取 `engine_protocol`（缺失回退 `product`），再去掉尾部 `.0`；
- 通过 Vite 的 `define` 内联成 `__APP_VERSION__`，`src/appVersion.ts` 用它拼 `V4.88`；
- 生产产物里是**普通字符串常量**（`grep dist/assets/*.js` 能直接看到），运行时不再读任何文件。

所以：**改 `version.json` → 重启 dev 服务器（或重新 build）就生效**，不需要先跑 sync，
也不经过 `package.json`。

构建时会顺带做一次漂移检查：若 `package.json` / `Cargo.toml` / `tauri.conf.json` 的
`version` 与 `version.json` 的 `product` 不一致，会打印
`[version] ... 不一致；安装包版本会错` 警告（**只警告、不阻断构建**）。看到这条就说明
该跑 `python tools/sync_version.py`，否则打出来的安装包版本号还是旧的。

### 运行时读取的优先级（从高到低，**不要调整这个顺序**）

| 优先级 | 来源 | 何时生效 |
|---|---|---|
| 1 | `DRAGON_NEST_APP_VERSION` 环境变量 | 有两个注入方：桌面壳启动引擎时（值来自 `Cargo.toml` 的 `version`，`lib.rs` 里 `env!("CARGO_PKG_VERSION")` → `app_display_version()` 去掉尾部 `.0`），以及 `tools/dev.ps1`（值直接取自 `version.json`）。**更新器靠它判断"装的到底是不是新版本"，必须保持最高优先级。** |
| 2 | `version.json` 的 `engine_protocol`（缺失时回退 `product`） | 裸启后端（`python main.py`）时使用 |
| 3 | `server.py` 的 `_FALLBACK_APP_VERSION` | 前两者都拿不到才有值，正常情况不该发生 |

> 唯一的纯装饰性字面量：`app/src-tauri/tauri.conf.json` 的窗口标题
> `"DN倒计时工具 V4.88"`。Tauri 配置不支持模板变量，这行只能手改；它不参与
> `/api/health`、不参与更新器判断，改漏了不影响功能，只是标题栏数字旧。

> 恢复 PyInstaller 打包时注意：`version.json` 在包目录外面，spec 的 `datas`
> 必须加上 `('version.json', '.')`。漏了这一行，冻结后的引擎读不到文件会悄悄
> 回落到兜底常量，表现为"新版本报旧版本号"。

## 如何运行

```bash
python main.py
```

后端（纯 JSON API，不含静态页）监听 `http://127.0.0.1:48710/`，健康检查：

```bash
curl http://127.0.0.1:48710/api/health
```

```json
{"ok":true,"ready":true,"product":"dragon-nest-battle-platform","version":"4.88",
 "apiRevision":"20261023-countdown-only","apiCapabilities":["capture-control-v1", "..."], "windowFree":true}
```

> 注意：直接访问 `http://127.0.0.1:48710/` 会返回 404 —— 这个端口只提供 `/api/*`，
> 界面必须由下面两种方式之一打开。

改端口：`python main.py --port 48799`。

## 如何运行（开发调试，不需要打包）

两种模式都**不产出 exe / 安装包**，直接跑源码。改完代码立刻见效，不用等打包。

### A. 浏览器模式 —— 最快，完全不碰 Rust

```powershell
powershell -ExecutionPolicy Bypass -File tools\dev.ps1 -BackendOnly
```

- 后端源码起在 `48710`，Vite 起在 `1420`，浏览器打开 `http://127.0.0.1:1420/`
- 悬浮窗页面：`http://127.0.0.1:1420/index.html?view=buff-overlay`
- 首次需在该页控制台执行
  `localStorage.setItem('dragon-nest-v420-buff-overlay-enabled','1')` 再刷新，
  否则主界面不会轮询悬浮窗数据
- **2~3 秒可用**；改 `.vue` / `.ts` 热更新，改 `engine/**.py` 自动重启后端
- 适合：调界面、调倒计时算法、跑录像回放

### B. 桌面壳模式 —— 真实悬浮窗（置顶 / 穿透 / 托盘）

```powershell
powershell -ExecutionPolicy Bypass -File tools\dev.ps1
```

- 额外跑 `tauri dev`，编译一份 **debug 二进制**（不是安装包），窗口行为与正式版一致
- 首次编译 4~10 分钟（378 个 crate），之后只改 Rust 30~90 秒、只改前端秒级
- 需要 MSVC + Windows SDK。脚本会自动探测（`vswhere` + Windows Kits），
  探测不到会打印警告；也可用 `DN_MSVC_ROOT` / `DN_SDK_ROOT` / `DN_SDK_VER` 手动指定
- 适合：调两个悬浮窗的窗口行为、托盘、全局快捷键

> **起不来怎么查**：完整输出在 `.dev\shell.log`；前端进程退出时脚本会把最后 15 行直接打到控制台。
> 两种常见故障：
> - `no @pnpm/exe.win32-x64 native binary` —— 全局 pnpm 装坏了。这种 pnpm `--version` 能过，
>   但任何 `run` 都会失败。脚本会自动跳过并改用可用的那个；也可 `$env:DN_PNPM = "<pnpm.cmd 路径>"` 指定。
> - `找不到 MSVC 链接器` —— 装 VS Build Tools（勾选「使用 C++ 的桌面开发」），
>   或用 `DN_MSVC_ROOT` / `DN_SDK_ROOT` / `DN_SDK_VER` 手动指定。

### 只起后端

```bash
python main.py                 # 默认 48710
python main.py --port 48799
```

不需要虚拟环境：本项目运行时**零第三方依赖**，任意 Python 3.11+ 即可
（若项目根存在 `.venv`，`dev.ps1` 会优先用它）。

两个模式的日志都落在 `.dev\backend.log` 与 `.dev\shell.log`。

## 测试

```bash
# 后端（stdlib unittest，无 pytest）
python -m unittest discover -s tests -p "test_*.py" -v

# 前端
cd app && pnpm test
```

## 调试记录（观测日志 + 保留抓包）

入口在 **设置 → 调试记录**。一个开关同时控制两件事：

| 开关打开时 | 落盘位置 |
|---|---|
| 观测日志（每轮一个 `.jsonl`） | `<保存目录>\probe\` |
| 本轮原始抓包（每轮一个 `.dpscap`） | `<保存目录>\capture\` |

开关关闭（默认）时行为与以前一致：抓包留在 `%TEMP%\DragonNestACT\working`，统计结束就删除。保存目录默认 `~/Documents/DragonNestDpsCapture/debug_records`，配置写在 `engine/data/debug_recording.json`。

打完看报告：

```bash
python tools/probe_report.py          # 最新一份
python tools/probe_report.py --all    # 所有文件概览
```

## 接口（仅这些）

```
GET  /api/health
GET  /api/capture/status | voice-events | timeline | buff-overlay | buff-overlay/wait | toggles
POST /api/capture/start | stop | auto | toggles
GET/POST /api/npcap/config
POST /api/npcap/detect | test
```

配装、配队、评分、排行、历史记录、导出 CSV、诊断上传等接口与页面已从代码中删除。
