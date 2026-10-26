use std::backtrace::Backtrace;
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    atomic::{AtomicIsize, AtomicU8, Ordering},
    Mutex, OnceLock,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, LogicalSize, Manager, PhysicalPosition, PhysicalSize, RunEvent, WindowEvent,
};
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
};

mod crash_diagnostics;
mod engine_runtime;
mod utility_assist;
use utility_assist::launch_bundled_utility;

static APP_DIAGNOSTIC_PATH: OnceLock<PathBuf> = OnceLock::new();
static NATIVE_WINDOW_OPERATION_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
static OVERLAY_NATIVE_WINDOW_STATE: OnceLock<Mutex<OverlayNativeWindowState>> = OnceLock::new();
static DESKTOP_RUNTIME_STOP_STATE: AtomicU8 = AtomicU8::new(0);
static INJECTED_MODULE_WARNING_SHOWN: AtomicU8 = AtomicU8::new(0);
#[cfg(windows)]
static SINGLE_INSTANCE_MUTEX_HANDLE: AtomicIsize = AtomicIsize::new(0);

const TRAY_SHOW_MENU_ID: &str = "tray-show-main";
const TRAY_EXIT_MENU_ID: &str = "tray-exit";
// 游戏独占键盘时全局热键会被游戏吞掉（实测 ALT+F9/F11/F12 在游戏前台都收不到
// WM_HOTKEY），托盘菜单是玩家在游戏窗口化时唯一稳定的入口。
const TRAY_START_MENU_ID: &str = "tray-start-capture";
const TRAY_STOP_MENU_ID: &str = "tray-stop-capture";
const MINIMIZE_TO_TRAY_FILE_NAME: &str = "minimize-to-tray.txt";

const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

fn app_display_version() -> &'static str {
    APP_VERSION.strip_suffix(".0").unwrap_or(APP_VERSION)
}

fn crash_diagnostics_dir() -> PathBuf {
    crash_diagnostics::diagnostics_dir()
}

fn prepare_crash_diagnostics_dir() -> PathBuf {
    let directory = crash_diagnostics_dir();
    if fs::create_dir_all(&directory).is_err() {
        return directory;
    }
    let readme = directory.join("说明.txt");
    if !readme.exists() {
        let _ = fs::write(
            &readme,
            "DN倒计时工具崩溃诊断目录\r\n\r\n程序会在检测到上次非正常退出后自动生成 packages\\dragon-nest-crash-*.zip。\r\n也可以在设置页点击“生成完整诊断包”。请把 ZIP 整包私下发给开发者，不必再手动整理 desktop/backend 日志。\r\n诊断包不包含 dpscap 抓包正文、完整配装、上传令牌或 API Key。诊断转储可能包含少量线程栈及间接引用内存。\r\n",
        );
    }
    let mut logs = fs::read_dir(&directory)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension().and_then(|value| value.to_str()) == Some("log"))
        .collect::<Vec<_>>();
    logs.sort_by_key(|entry| {
        entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .unwrap_or(UNIX_EPOCH)
    });
    let remove_count = logs.len().saturating_sub(60);
    for entry in logs.into_iter().take(remove_count) {
        let _ = fs::remove_file(entry.path());
    }
    directory
}

fn app_diagnostic_path() -> &'static PathBuf {
    APP_DIAGNOSTIC_PATH.get_or_init(|| {
        let epoch = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        crash_diagnostics_dir().join(format!("desktop_{epoch}_{}.log", std::process::id()))
    })
}

fn append_app_diagnostic(component: &str, message: &str) {
    let path = app_diagnostic_path();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) else {
        return;
    };
    let epoch_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let safe_component = component.replace(['\r', '\n'], " ");
    let safe_message: String = message.chars().take(32_000).collect();
    let _ = writeln!(file, "\n[{epoch_ms}] [{safe_component}]\n{safe_message}");
}

fn install_panic_diagnostics() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        #[cfg(windows)]
        unsafe {
            let _ = crash_diagnostics::write_minidump(std::ptr::null(), "desktop-panic");
        }
        append_app_diagnostic(
            "tauri-panic",
            &format!("{info}\n\nBacktrace:\n{}", Backtrace::force_capture()),
        );
        previous(info);
    }));
}

#[cfg(windows)]
unsafe extern "system" fn unhandled_windows_exception(
    exception_info: *const windows_sys::Win32::System::Diagnostics::Debug::EXCEPTION_POINTERS,
) -> i32 {
    use windows_sys::Win32::System::Diagnostics::Debug::EXCEPTION_CONTINUE_SEARCH;

    let message = if exception_info.is_null()
        || unsafe { (*exception_info).ExceptionRecord.is_null() }
    {
        "Windows reported an unhandled native exception without an exception record.".to_string()
    } else {
        let record = unsafe { &*(*exception_info).ExceptionRecord };
        format!(
            "exception_code=0x{:08X}\nexception_flags=0x{:08X}\nexception_address={:?}",
            record.ExceptionCode as u32, record.ExceptionFlags, record.ExceptionAddress,
        )
    };
    let dump_path =
        unsafe { crash_diagnostics::write_minidump(exception_info, "desktop-native-crash") };
    append_app_diagnostic(
        "windows-unhandled-exception",
        &format!(
            "{message}\nminidump={}",
            dump_path
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_default()
        ),
    );
    EXCEPTION_CONTINUE_SEARCH
}

#[cfg(windows)]
fn install_native_exception_diagnostics() {
    use windows_sys::Win32::System::Diagnostics::Debug::SetUnhandledExceptionFilter;

    unsafe {
        SetUnhandledExceptionFilter(Some(unhandled_windows_exception));
    }
}

#[cfg(not(windows))]
fn install_native_exception_diagnostics() {}

struct BackendChild {
    child: Child,
    #[cfg(windows)]
    kill_job: Option<isize>,
    stopped: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackendStopOutcome {
    AlreadyStopped,
    Graceful,
    Forced,
}

impl BackendChild {
    fn stop_with_timeout(&mut self, graceful_timeout: Duration) -> BackendStopOutcome {
        if self.stopped {
            return BackendStopOutcome::AlreadyStopped;
        }
        if matches!(self.child.try_wait(), Ok(Some(_))) {
            self.release_job(false);
            let _ = self.child.wait();
            self.stopped = true;
            return BackendStopOutcome::AlreadyStopped;
        }

        // Give Python a bounded graceful-exit window first.  It closes Npcap,
        // cancels queued calculations and explicitly joins/kills its score
        // process pool.  The Job Object below remains the hard safety net.
        request_backend_shutdown(self.child.id());
        let deadline = Instant::now() + graceful_timeout;
        let mut exited = false;
        while Instant::now() < deadline {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                exited = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        if exited {
            // Closing the kill-on-close Job Object also guarantees that no
            // orphan score worker survives after the Python parent exited.
            self.release_job(false);
        } else {
            // Only the process tree placed in this desktop instance's Job
            // Object is terminated. Never enumerate or kill unrelated Python
            // processes on the machine.
            self.release_job(true);
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
        self.stopped = true;
        if exited {
            BackendStopOutcome::Graceful
        } else {
            BackendStopOutcome::Forced
        }
    }

    fn stop(&mut self) {
        let _ = self.stop_with_timeout(Duration::from_millis(1500));
    }

    #[cfg(windows)]
    fn release_job(&mut self, terminate: bool) {
        let Some(job) = self.kill_job.take() else {
            return;
        };
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        unsafe {
            if terminate {
                let _ = TerminateJobObject(job as HANDLE, 0);
            }
            let _ = CloseHandle(job as HANDLE);
        }
    }

    #[cfg(not(windows))]
    fn release_job(&mut self, _terminate: bool) {}
}

fn request_backend_shutdown(process_id: u32) {
    let address = SocketAddr::from(([127, 0, 0, 1], 48710));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(250)));
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let request = format!(
        "POST /api/shutdown?pid={process_id} HTTP/1.1\r\nHost: 127.0.0.1:48710\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_ok() {
        let mut response = [0u8; 128];
        let _ = stream.read(&mut response);
    }
}

impl Drop for BackendChild {
    fn drop(&mut self) {
        self.stop();
    }
}

struct BackendProcess(Mutex<Option<BackendChild>>);

// Tauri's manager lookup returns a cloned WebviewWindow. Repeating that clone
// while another command is resizing/hiding a WebView can race the Wry/Tao
// dispatcher lifetime on Windows. Capture one stable handle per window during
// setup and only borrow those handles afterwards.
struct NativeWindowRegistry(Mutex<HashMap<String, tauri::WebviewWindow>>);

// 0 = idle, 1 = waiting for the Vue build store, 2 = safe to exit.
struct ExitSaveState(AtomicU8);

// 0 = close the application, 1 = hide the main window in the system tray.
// Atomic state lets the native close callback make its decision without
// waiting for the WebView or a filesystem read on the Windows event thread.
struct MinimizeToTrayState(AtomicU8);

#[derive(Default)]
struct OverlayNativeWindowState {
    visibility: HashMap<String, bool>,
    logical_sizes: HashMap<String, (u32, u32)>,
}

impl OverlayNativeWindowState {
    fn visibility_change_needed(&self, label: &str, visible: bool) -> bool {
        self.visibility.get(label).copied() != Some(visible)
    }

    fn remember_visibility(&mut self, label: &str, visible: bool) {
        self.visibility.insert(label.to_string(), visible);
    }

    fn size_change_needed(&self, label: &str, width: u32, height: u32) -> bool {
        self.logical_sizes
            .get(label)
            .is_none_or(|(previous_width, previous_height)| {
                previous_width.abs_diff(width) > 1 || previous_height.abs_diff(height) > 1
            })
    }

    fn remember_size(&mut self, label: &str, width: u32, height: u32) {
        self.logical_sizes
            .insert(label.to_string(), (width, height));
    }

}

fn native_window_operation_guard() -> Result<std::sync::MutexGuard<'static, ()>, String> {
    if DESKTOP_RUNTIME_STOP_STATE.load(Ordering::SeqCst) != 0 {
        return Err("桌面窗口正在关闭，已忽略新的原生窗口操作".to_string());
    }
    let guard = NATIVE_WINDOW_OPERATION_LOCK
        .get_or_init(|| Mutex::new(()))
        .try_lock()
        .map_err(|error| format!("原生窗口操作暂不可用（避免界面回调重入等待）：{error}"))?;
    // Shutdown may have started while this operation waited for the lock.
    if DESKTOP_RUNTIME_STOP_STATE.load(Ordering::SeqCst) != 0 {
        return Err("桌面窗口正在关闭，已忽略新的原生窗口操作".to_string());
    }
    Ok(guard)
}

fn overlay_native_window_state(
) -> Result<std::sync::MutexGuard<'static, OverlayNativeWindowState>, String> {
    OVERLAY_NATIVE_WINDOW_STATE
        .get_or_init(|| Mutex::new(OverlayNativeWindowState::default()))
        .lock()
        .map_err(|_| "悬浮窗原生状态锁已损坏".to_string())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum MainCloseDecision {
    HideToTray,
    BeginGracefulExit,
    WaitForGracefulExit,
    AllowExit,
}

fn main_close_decision(exit_state: u8, minimize_to_tray: bool) -> MainCloseDecision {
    if exit_state >= 2 {
        MainCloseDecision::AllowExit
    } else if exit_state == 1 {
        MainCloseDecision::WaitForGracefulExit
    } else if minimize_to_tray {
        MainCloseDecision::HideToTray
    } else {
        // Windows can clear focus before Tao delivers WM_CLOSE, especially
        // when the title-bar close button is clicked from another monitor or
        // while an always-on-top overlay owns the active window. Focus is
        // therefore not evidence that a close request is unexpected. Every
        // close request for the main window must enter the bounded save path.
        MainCloseDecision::BeginGracefulExit
    }
}

fn should_block_exit_request(exit_state: u8) -> bool {
    exit_state < 2
}

fn minimize_to_tray_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|root| root.join(MINIMIZE_TO_TRAY_FILE_NAME))
}

fn stored_minimize_to_tray(app: &tauri::AppHandle) -> bool {
    let Some(path) = minimize_to_tray_path(app) else {
        return true;
    };
    match fs::read_to_string(path) {
        Ok(value) => !matches!(value.trim(), "0" | "false" | "False" | "FALSE"),
        Err(_) => true,
    }
}

fn persist_minimize_to_tray(app: &tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let path = minimize_to_tray_path(app).ok_or_else(|| "无法取得窗口设置目录".to_string())?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("无法创建窗口设置目录：{error}"))?;
    }
    fs::write(path, if enabled { "1" } else { "0" })
        .map_err(|error| format!("无法保存最小化到托盘设置：{error}"))
}

#[tauri::command]
fn get_minimize_to_tray(state: tauri::State<'_, MinimizeToTrayState>) -> bool {
    state.0.load(Ordering::SeqCst) != 0
}

#[tauri::command]
fn set_minimize_to_tray(
    app: tauri::AppHandle,
    state: tauri::State<'_, MinimizeToTrayState>,
    enabled: bool,
) -> Result<(), String> {
    persist_minimize_to_tray(&app, enabled)?;
    state.0.store(u8::from(enabled), Ordering::SeqCst);
    append_app_diagnostic(
        "minimize-to-tray-setting",
        if enabled {
            "enabled=true"
        } else {
            "enabled=false"
        },
    );
    Ok(())
}

fn show_main_window(app: &tauri::AppHandle) -> Result<(), String> {
    with_registered_native_window_unlocked(app, "main", |window| {
        window.show().map_err(|error| error.to_string())?;
        if window.is_minimized().unwrap_or(false) {
            window.unminimize().map_err(|error| error.to_string())?;
        }
        window.set_focus().map_err(|error| error.to_string())
    })
}

fn hide_main_window(app: &tauri::AppHandle) -> Result<(), String> {
    with_registered_native_window_unlocked(app, "main", |window| {
        window.hide().map_err(|error| error.to_string())
    })
}

fn request_graceful_exit(app: &tauri::AppHandle, source: &str) {
    append_app_diagnostic("graceful-exit-requested", source);
    let exit_state = app.state::<ExitSaveState>().0.load(Ordering::SeqCst);
    if exit_state >= 2 {
        app.exit(0);
    } else if exit_state == 0 {
        begin_graceful_exit(app);
    }
}

#[derive(Default)]
struct HotkeyBindings {
    start_capture_id: Option<u32>,
    toggle_overlay_id: Option<u32>,
    disconnect_game_id: Option<u32>,
    start_capture: Option<Shortcut>,
    toggle_overlay: Option<Shortcut>,
    disconnect_game: Option<Shortcut>,
    stone_washing_id: Option<u32>,
    stone_washing: Option<Shortcut>,
}

struct GlobalHotkeyState(Mutex<HotkeyBindings>);

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct UtilityPluginStatus {
    game_running: bool,
    game_foreground: bool,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct UtilityPluginResult {
    action: String,
    success: bool,
    message: String,
}

fn dispatch_global_hotkey_action(app: &tauri::AppHandle, action: String) {
    match action.as_str() {
        "disconnect-game" => {
            let app = app.clone();
            std::thread::spawn(move || {
                let result = terminate_game_processes(true);
                emit_utility_result(&app, "disconnect-game", result);
            });
        }
        _ => {
            let _ = app.emit("dragon-nest-global-hotkey", action);
        }
    }
}

#[cfg(windows)]
fn shortcut_virtual_key(key: Code) -> Option<i32> {
    Some(match key {
        Code::KeyA => 0x41, Code::KeyB => 0x42, Code::KeyC => 0x43,
        Code::KeyD => 0x44, Code::KeyE => 0x45, Code::KeyF => 0x46,
        Code::KeyG => 0x47, Code::KeyH => 0x48, Code::KeyI => 0x49,
        Code::KeyJ => 0x4A, Code::KeyK => 0x4B, Code::KeyL => 0x4C,
        Code::KeyM => 0x4D, Code::KeyN => 0x4E, Code::KeyO => 0x4F,
        Code::KeyP => 0x50, Code::KeyQ => 0x51, Code::KeyR => 0x52,
        Code::KeyS => 0x53, Code::KeyT => 0x54, Code::KeyU => 0x55,
        Code::KeyV => 0x56, Code::KeyW => 0x57, Code::KeyX => 0x58,
        Code::KeyY => 0x59, Code::KeyZ => 0x5A,
        Code::Digit0 => 0x30, Code::Digit1 => 0x31, Code::Digit2 => 0x32,
        Code::Digit3 => 0x33, Code::Digit4 => 0x34, Code::Digit5 => 0x35,
        Code::Digit6 => 0x36, Code::Digit7 => 0x37, Code::Digit8 => 0x38,
        Code::Digit9 => 0x39,
        Code::F1 => 0x70, Code::F2 => 0x71, Code::F3 => 0x72,
        Code::F4 => 0x73, Code::F5 => 0x74, Code::F6 => 0x75,
        Code::F7 => 0x76, Code::F8 => 0x77, Code::F9 => 0x78,
        Code::F10 => 0x79, Code::F11 => 0x7A, Code::F12 => 0x7B,
        Code::F13 => 0x7C, Code::F14 => 0x7D, Code::F15 => 0x7E,
        Code::F16 => 0x7F, Code::F17 => 0x80, Code::F18 => 0x81,
        Code::F19 => 0x82, Code::F20 => 0x83, Code::F21 => 0x84,
        Code::F22 => 0x85, Code::F23 => 0x86, Code::F24 => 0x87,
        Code::Space => 0x20,
        Code::Enter | Code::NumpadEnter => 0x0D,
        Code::Tab => 0x09, Code::Escape => 0x1B, Code::Backspace => 0x08,
        Code::Insert => 0x2D, Code::Delete => 0x2E,
        Code::Home => 0x24, Code::End => 0x23,
        Code::PageUp => 0x21, Code::PageDown => 0x22,
        Code::ArrowLeft => 0x25, Code::ArrowUp => 0x26,
        Code::ArrowRight => 0x27, Code::ArrowDown => 0x28,
        Code::Numpad0 => 0x60, Code::Numpad1 => 0x61, Code::Numpad2 => 0x62,
        Code::Numpad3 => 0x63, Code::Numpad4 => 0x64, Code::Numpad5 => 0x65,
        Code::Numpad6 => 0x66, Code::Numpad7 => 0x67, Code::Numpad8 => 0x68,
        Code::Numpad9 => 0x69, Code::NumpadMultiply => 0x6A,
        Code::NumpadAdd => 0x6B, Code::NumpadSubtract => 0x6D,
        Code::NumpadDecimal => 0x6E, Code::NumpadDivide => 0x6F,
        _ => return None,
    })
}

#[cfg(windows)]
fn async_key_down(virtual_key: i32) -> bool {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState;
    unsafe { GetAsyncKeyState(virtual_key) < 0 }
}

#[cfg(windows)]
fn shortcut_pressed(shortcut: Shortcut) -> bool {
    let Some(key) = shortcut_virtual_key(shortcut.key) else { return false; };
    let control = async_key_down(0x11);
    let shift = async_key_down(0x10);
    let alt = async_key_down(0x12);
    let super_key = async_key_down(0x5B) || async_key_down(0x5C);
    control == shortcut.mods.contains(Modifiers::CONTROL)
        && shift == shortcut.mods.contains(Modifiers::SHIFT)
        && alt == shortcut.mods.contains(Modifiers::ALT)
        && super_key == shortcut.mods.contains(Modifiers::SUPER)
        && async_key_down(key)
}

#[cfg(windows)]
fn start_foreground_game_hotkey_poller(app: tauri::AppHandle) {
    let _ = std::thread::Builder::new()
        .name("dragon-nest-foreground-hotkeys".into())
        .spawn(move || {
            let mut game_process_ids = Vec::new();
            let mut next_process_refresh = Instant::now();
            let mut pressed = std::collections::HashSet::new();
            while DESKTOP_RUNTIME_STOP_STATE.load(Ordering::Relaxed) == 0 {
                let now = Instant::now();
                if now >= next_process_refresh {
                    game_process_ids = running_game_process_ids();
                    next_process_refresh = now + Duration::from_secs(2);
                }
                let game_foreground = foreground_process_id()
                    .is_some_and(|pid| game_process_ids.contains(&pid));
                if !game_foreground {
                    pressed.clear();
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
                let bindings = app.state::<GlobalHotkeyState>().0.lock().ok().map(|bindings| {
                    let mut rows = Vec::new();
                    if let Some(shortcut) = bindings.start_capture { rows.push(("start-capture".to_string(), shortcut)); }
                    if let Some(shortcut) = bindings.toggle_overlay { rows.push(("toggle-overlay".to_string(), shortcut)); }
                    if let Some(shortcut) = bindings.disconnect_game { rows.push(("disconnect-game".to_string(), shortcut)); }
                    if let Some(shortcut) = bindings.stone_washing { rows.push(("stone-washing-toggle".to_string(), shortcut)); }
                    rows
                }).unwrap_or_default();
                let valid_ids = bindings.iter().map(|(_, shortcut)| shortcut.id())
                    .collect::<std::collections::HashSet<_>>();
                pressed.retain(|id| valid_ids.contains(id));
                for (action, shortcut) in bindings {
                    if shortcut_pressed(shortcut) {
                        if pressed.insert(shortcut.id()) {
                            dispatch_global_hotkey_action(&app, action);
                        }
                    } else {
                        pressed.remove(&shortcut.id());
                    }
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        });
}

#[cfg(not(windows))]
fn start_foreground_game_hotkey_poller(_app: tauri::AppHandle) {}

const GAME_EXECUTABLE_NAMES: [&str; 2] = ["dragonnest.exe", "dragonnest_reborn.exe"];

#[cfg(windows)]
fn is_act_desktop_executable_name(value: &str) -> bool {
    // The installed desktop keeps this fixed executable name across releases.
    // Release installers are named DNACT4.xx.exe (and may omit "setup").
    // Matching the DNACT prefix here made the still-running NSIS installer
    // look like a second desktop when the finish page launched the app.
    // Modern desktop builds also hold the named mutex above; the process scan
    // only needs the legacy desktop binary that predates that mutex.
    value.eq_ignore_ascii_case("dncountdown.exe")
        || value.eq_ignore_ascii_case("dragon-nest-simulator-v420.exe")
}

#[cfg(windows)]
fn running_other_act_process_ids() -> Vec<u32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcessId;

    let current_process_id = unsafe { GetCurrentProcessId() };
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..unsafe { std::mem::zeroed() }
    };
    let mut process_ids = Vec::new();
    let mut has_entry = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
    while has_entry {
        let end = entry
            .szExeFile
            .iter()
            .position(|value| *value == 0)
            .unwrap_or(entry.szExeFile.len());
        let name = String::from_utf16_lossy(&entry.szExeFile[..end]);
        if entry.th32ProcessID != current_process_id
            && is_act_desktop_executable_name(&name)
        {
            process_ids.push(entry.th32ProcessID);
        }
        has_entry = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };
    process_ids
}

#[cfg(windows)]
fn show_duplicate_instance_warning(process_ids: &[u32]) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONINFORMATION, MB_OK, MB_TOPMOST,
    };

    let process_hint = if process_ids.is_empty() {
        String::new()
    } else {
        format!("\n\n已运行的进程 ID：{}", process_ids.iter().map(u32::to_string).collect::<Vec<_>>().join(", "))
    };
    let message = format!(
        "检测到另一个 DN倒计时工具 实例仍在运行，它可能已经最小化到系统托盘。\n\n\
         同时运行两个版本会让两套 TCP 监听同时解析同一批报文，造成悬浮窗重复刷新、计时跳动。\n\n\
         本次启动已取消。请从托盘打开已有实例，或先右键托盘图标退出，再启动当前版本。{process_hint}"
    );
    let title = "DN倒计时工具已经在运行";
    let message_wide = message.encode_utf16().chain(std::iter::once(0)).collect::<Vec<_>>();
    let title_wide = title.encode_utf16().chain(std::iter::once(0)).collect::<Vec<_>>();
    unsafe {
        let _ = MessageBoxW(
            std::ptr::null_mut(),
            message_wide.as_ptr(),
            title_wide.as_ptr(),
            MB_OK | MB_ICONINFORMATION | MB_TOPMOST,
        );
    }
}

#[cfg(windows)]
fn acquire_single_instance_guard() -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::CreateMutexW;

    // Deliberately does not contain the release version: all installed and
    // portable builds in this Windows session must share one capture/overlay
    // owner.  The process scan also catches v4.65/v4.66 builds created before
    // this mutex existed.
    let name = "Local\\DragonNestACT-Desktop-SingleInstance-v1"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
    let already_exists = !handle.is_null() && unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
    let other_process_ids = running_other_act_process_ids();
    if already_exists || !other_process_ids.is_empty() {
        if !handle.is_null() {
            unsafe { CloseHandle(handle) };
        }
        show_duplicate_instance_warning(&other_process_ids);
        return false;
    }
    if !handle.is_null() {
        // Keep the kernel object alive until process exit. Windows closes the
        // handle even after a crash, so unlike a lock file it cannot become a
        // stale false positive on the next launch.
        SINGLE_INSTANCE_MUTEX_HANDLE.store(handle as isize, Ordering::SeqCst);
    }
    true
}

#[cfg(not(windows))]
fn acquire_single_instance_guard() -> bool {
    true
}

fn is_game_executable_name(value: &str) -> bool {
    GAME_EXECUTABLE_NAMES
        .iter()
        .any(|name| value.eq_ignore_ascii_case(name))
}

#[cfg(test)]
fn is_game_executable_path(value: &str) -> bool {
    Path::new(value)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(is_game_executable_name)
}

#[cfg(windows)]
fn foreground_process_id() -> Option<u32> {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowThreadProcessId,
    };

    let window = unsafe { GetForegroundWindow() };
    if window.is_null() {
        return None;
    }
    let mut process_id = 0u32;
    unsafe { GetWindowThreadProcessId(window, &mut process_id) };
    (process_id != 0).then_some(process_id)
}

#[cfg(not(windows))]
fn foreground_process_id() -> Option<u32> {
    None
}

#[cfg(windows)]
fn foreground_game_process_id() -> Option<u32> {
    let process_id = foreground_process_id()?;
    // DragonNest may run elevated or behind its protection module. In that
    // state QueryFullProcessImageNameW can be denied to the simulator even
    // though Toolhelp still exposes the exact executable name. The old path
    // check therefore discarded a valid F4 press before termination was even
    // attempted. Match the foreground PID against the name-verified process
    // snapshot instead.
    running_game_process_ids()
        .contains(&process_id)
        .then_some(process_id)
}

#[cfg(not(windows))]
fn foreground_game_process_id() -> Option<u32> {
    None
}

#[cfg(windows)]
fn running_game_process_ids() -> Vec<u32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };

    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..unsafe { std::mem::zeroed() }
    };
    let mut process_ids = Vec::new();
    let mut has_entry = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
    while has_entry {
        let end = entry
            .szExeFile
            .iter()
            .position(|value| *value == 0)
            .unwrap_or(entry.szExeFile.len());
        let name = String::from_utf16_lossy(&entry.szExeFile[..end]);
        if is_game_executable_name(&name) {
            process_ids.push(entry.th32ProcessID);
        }
        has_entry = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };
    process_ids
}

#[cfg(windows)]
fn current_process_module_paths() -> Vec<String> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Module32FirstW, Module32NextW, MODULEENTRY32W, TH32CS_SNAPMODULE,
        TH32CS_SNAPMODULE32,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcessId;

    let snapshot = unsafe {
        CreateToolhelp32Snapshot(
            TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
            GetCurrentProcessId(),
        )
    };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }

    let mut entry = MODULEENTRY32W {
        dwSize: std::mem::size_of::<MODULEENTRY32W>() as u32,
        ..unsafe { std::mem::zeroed() }
    };
    let mut paths = Vec::new();
    let mut has_entry = unsafe { Module32FirstW(snapshot, &mut entry) } != 0;
    while has_entry {
        let end = entry
            .szExePath
            .iter()
            .position(|value| *value == 0)
            .unwrap_or(entry.szExePath.len());
        paths.push(String::from_utf16_lossy(&entry.szExePath[..end]));
        has_entry = unsafe { Module32NextW(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };
    paths
}

#[cfg(windows)]
fn known_risky_injected_module_paths() -> Vec<String> {
    current_process_module_paths()
        .into_iter()
        .filter(|path| {
            Path::new(path)
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.eq_ignore_ascii_case("GPP64.dll"))
        })
        .collect()
}

#[cfg(windows)]
fn show_injected_module_warning(paths: &[String]) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONWARNING, MB_OK, MB_TOPMOST,
    };

    if INJECTED_MODULE_WARNING_SHOWN.swap(1, Ordering::SeqCst) != 0 {
        return;
    }
    let path_list = paths.join("\n");
    let message = format!(
        "检测到 GamePP 的 GPP64.dll 已注入 DN倒计时工具。\n\n\
         已收到的崩溃现场表明，该模块注入后可能破坏桌面事件循环内存并造成随机闪退；平台无法安全卸载外部注入模块。\n\n\
         请退出平台，在 GamePP 中关闭对本程序的监控、悬浮窗或注入，或退出 GamePP，再重新启动平台。若 GamePP 支持进程排除，请排除 dragon-nest-simulator-v420.exe。\n\n\
         检测路径：\n{path_list}"
    );
    let title = "检测到可能导致闪退的第三方模块";
    let message_wide = message
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let title_wide = title
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    unsafe {
        let _ = MessageBoxW(
            std::ptr::null_mut(),
            message_wide.as_ptr(),
            title_wide.as_ptr(),
            MB_OK | MB_ICONWARNING | MB_TOPMOST,
        );
    }
}

#[cfg(windows)]
fn start_known_injected_module_monitor() {
    let _ = std::thread::Builder::new()
        .name("third-party-module-watch".into())
        .spawn(|| loop {
            let paths = known_risky_injected_module_paths();
            if !paths.is_empty() {
                append_app_diagnostic(
                    "known-risky-injected-module",
                    &format!(
                        "Detected GPP64.dll in the desktop process. This module is associated with native memory corruption in the supplied crash package.\n{}",
                        paths.join("\n")
                    ),
                );
                show_injected_module_warning(&paths);
                break;
            }
            std::thread::sleep(Duration::from_secs(5));
        });
}

#[cfg(not(windows))]
fn start_known_injected_module_monitor() {}

#[cfg(not(windows))]
fn running_game_process_ids() -> Vec<u32> {
    Vec::new()
}

#[cfg(windows)]
fn terminate_game_processes(require_foreground: bool) -> Result<String, String> {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, TerminateProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
    };

    if require_foreground && foreground_game_process_id().is_none() {
        return Err("龙之谷窗口不在前台，已忽略掉线快捷键".into());
    }
    let process_ids = running_game_process_ids();
    if process_ids.is_empty() {
        return Err("未找到正在运行的龙之谷进程".into());
    }
    let mut terminated = 0usize;
    let mut failures = Vec::new();
    for process_id in process_ids {
        let process = unsafe {
            OpenProcess(
                PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
                0,
                process_id,
            )
        };
        if process.is_null() {
            failures.push(process_id);
            continue;
        }
        // Revalidate by PID and exact executable name without querying the
        // protected image path. This retains the narrow DragonNest-only
        // safety boundary while working with elevated/protected clients.
        let still_game = running_game_process_ids().contains(&process_id);
        let ok = still_game && unsafe { TerminateProcess(process, 1) } != 0;
        unsafe { CloseHandle(process) };
        if ok {
            terminated += 1;
        } else {
            failures.push(process_id);
        }
    }
    if terminated == 0 {
        return Err(
            "无法结束龙之谷进程；如果游戏以管理员身份运行，请也以管理员身份启动模拟器".into(),
        );
    }
    if failures.is_empty() {
        Ok(format!("已结束 {terminated} 个龙之谷进程"))
    } else {
        Ok(format!(
            "已结束 {terminated} 个龙之谷进程，另有 {} 个结束失败",
            failures.len()
        ))
    }
}

#[cfg(not(windows))]
fn terminate_game_processes(_require_foreground: bool) -> Result<String, String> {
    Err("实用插件目前仅支持 Windows".into())
}

#[cfg(windows)]
fn show_utility_error(message: &str) {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        MessageBoxW, MB_ICONERROR, MB_OK, MB_TOPMOST,
    };

    let title: Vec<u16> = OsStr::new("DN倒计时工具 · 一键掉线")
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let body: Vec<u16> = OsStr::new(message)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            body.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR | MB_TOPMOST,
        );
    }
}

#[cfg(not(windows))]
fn show_utility_error(_message: &str) {}

fn emit_utility_result(app: &tauri::AppHandle, action: &str, result: Result<String, String>) {
    let (success, message) = match result {
        Ok(message) => (true, message),
        Err(message) => (false, message),
    };
    if !success {
        show_utility_error(&message);
    }
    let _ = app.emit(
        "dragon-nest-utility-result",
        UtilityPluginResult {
            action: action.into(),
            success,
            message,
        },
    );
}

#[tauri::command]
fn utility_plugin_status() -> UtilityPluginStatus {
    UtilityPluginStatus {
        game_running: !running_game_process_ids().is_empty(),
        game_foreground: foreground_game_process_id().is_some(),
    }
}

#[tauri::command]
fn disconnect_game(state: tauri::State<GlobalHotkeyState>) -> Result<String, String> {
    let enabled = state
        .0
        .lock()
        .map_err(|_| "快捷键状态不可用".to_string())?
        .disconnect_game
        .is_some();
    if !enabled {
        return Err("一键掉线尚未开启，请先在实用工具中开启并保存。".into());
    }
    terminate_game_processes(false)
}

#[cfg(windows)]
const STANDARD_OVERLAY_WINDOW_LABELS: [&str; 2] = [
    "buff-overlay",
    "skill-countdown-overlay",
];
const OVERLAY_WINDOW_LABELS: [&str; 2] = [
    "buff-overlay",
    "skill-countdown-overlay",
];
const NATIVE_WINDOW_LABELS: [&str; 3] = [
    "main",
    "buff-overlay",
    "skill-countdown-overlay",
];

#[derive(Clone, Copy)]
struct NativeWindowSpec {
    title: &'static str,
    view: &'static str,
    width: f64,
    height: f64,
}

fn native_window_spec(label: &str) -> Option<NativeWindowSpec> {
    Some(match label {
        "buff-overlay" => NativeWindowSpec {
            title: "增益 / 减益倒计时",
            view: "buff-overlay",
            width: 320.0,
            height: 46.0,
        },
        "skill-countdown-overlay" => NativeWindowSpec {
            title: "Boss 技能倒计时",
            view: "skill-countdown-overlay",
            width: 320.0,
            height: 160.0,
        },
        _ => return None,
    })
}

fn build_native_window(
    app: &tauri::AppHandle,
    label: &str,
) -> Result<tauri::WebviewWindow, String> {
    let spec = native_window_spec(label).ok_or_else(|| format!("原生窗口配置不存在：{label}"))?;
    let url = format!("index.html?view={}", spec.view);
    tauri::WebviewWindowBuilder::new(app, label, tauri::WebviewUrl::App(url.into()))
        .title(spec.title)
        .inner_size(spec.width, spec.height)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .skip_taskbar(true)
        .shadow(false)
        .visible(false)
        .focused(false)
        .build()
        .map_err(|error| format!("创建原生窗口 {label} 失败：{error}"))
}

fn ensure_native_window(
    app: &tauri::AppHandle,
    registry: &NativeWindowRegistry,
    label: &str,
) -> Result<tauri::WebviewWindow, String> {
    // Never create a WebView while the registry mutex is held. WebView2 can
    // synchronously re-enter window callbacks during construction; holding
    // this mutex there made a first-show operation freeze or hit the old
    // five-second timeout.
    if let Some(window) = registry
        .0
        .lock()
        .map_err(|_| "原生窗口注册表锁已损坏".to_string())?
        .get(label)
        .cloned()
    {
        return Ok(window);
    }
    let window = build_native_window(app, label)?;
    window
        .set_ignore_cursor_events(false)
        .map_err(|error| error.to_string())?;
    let mut windows = registry
        .0
        .lock()
        .map_err(|_| "原生窗口注册表锁已损坏".to_string())?;
    let registered = windows
        .entry(label.to_string())
        .or_insert_with(|| window.clone())
        .clone();
    append_app_diagnostic("overlay-webview-created", &format!("label={label}"));
    Ok(registered)
}

fn valid_overlay_label(label: &str) -> bool {
    OVERLAY_WINDOW_LABELS.contains(&label)
}

pub(crate) fn with_registered_native_window<T>(
    app: &tauri::AppHandle,
    label: &str,
    operation: impl FnOnce(&tauri::AppHandle, &str, &tauri::WebviewWindow) -> Result<T, String>
        + Send
        + 'static,
) -> Result<T, String>
where
    T: Send + 'static,
{
    let label = label.to_string();
    run_native_window_operation(app, move |app, registry| {
        let window = ensure_native_window(app, registry, &label)?;
        operation(app, &label, &window)
    })
}

fn run_native_window_operation<T>(
    app: &tauri::AppHandle,
    operation: impl FnOnce(&tauri::AppHandle, &NativeWindowRegistry) -> Result<T, String>
        + Send
        + 'static,
) -> Result<T, String>
where
    T: Send + 'static,
{
    let handle = app.clone();
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    app.run_on_main_thread(move || {
        let result = (|| {
            let _operation = native_window_operation_guard()?;
            let registry = handle
                .try_state::<NativeWindowRegistry>()
                .ok_or_else(|| "原生窗口注册表尚未初始化".to_string())?;
            operation(&handle, &registry)
        })();
        let _ = sender.send(result);
    })
    .map_err(|error| format!("无法提交原生窗口操作：{error}"))?;
    receiver
        .recv_timeout(Duration::from_secs(5))
        .map_err(|error| format!("原生窗口操作未及时完成：{error}"))?
}

fn with_registered_native_window_unlocked<T>(
    app: &tauri::AppHandle,
    label: &str,
    operation: impl FnOnce(&tauri::WebviewWindow) -> Result<T, String>,
) -> Result<T, String> {
    let registry = app
        .try_state::<NativeWindowRegistry>()
        .ok_or_else(|| "原生窗口注册表尚未初始化".to_string())?;
    // Native calls may synchronously deliver another window callback. Never
    // keep the registry locked across show/hide/focus/getter calls.
    let window = {
        let windows = registry
            .0
            .try_lock()
            .map_err(|error| format!("原生窗口注册表暂不可用：{error}"))?;
        windows
            .get(label)
            .cloned()
            .ok_or_else(|| format!("原生窗口不可用：{label}"))?
    };
    operation(&window)
}

fn overlay_position_path(app: &tauri::AppHandle, label: &str) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|root| root.join(format!("{label}-position.txt")))
}

fn stored_overlay_position(app: &tauri::AppHandle, label: &str) -> Option<PhysicalPosition<i32>> {
    let value = std::fs::read_to_string(overlay_position_path(app, label)?).ok()?;
    let (x, y) = value.trim().split_once(',')?;
    Some(PhysicalPosition::new(x.parse().ok()?, y.parse().ok()?))
}

fn save_overlay_position(app: &tauri::AppHandle, label: &str, position: PhysicalPosition<i32>) {
    let Some(path) = overlay_position_path(app, label) else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(path, format!("{},{}", position.x, position.y));
}

fn bundled_npcap_installer(app: &tauri::AppHandle) -> PathBuf {
    let resource_candidate = app.path().resource_dir().ok().map(|root| {
        root.join("third_party")
            .join("npcap")
            .join("npcap-1.88.exe")
    });
    let project_candidate = project_root()
        .join("third_party")
        .join("npcap")
        .join("npcap-1.88.exe");
    resource_candidate
        .filter(|path| path.is_file())
        .unwrap_or(project_candidate)
}

#[tauri::command]
fn install_npcap(app: tauri::AppHandle, installer_path: Option<String>) -> Result<(), String> {
    let path = installer_path
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| bundled_npcap_installer(&app));
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !path.is_file()
        || !name.starts_with("npcap")
        || path.extension().and_then(|value| value.to_str()) != Some("exe")
    {
        return Err("请选择有效的 npcap*.exe 安装程序".into());
    }
    Command::new(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn pick_npcap_path() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择 wpcap.dll")
        .add_filter("Npcap 库", &["dll"])
        .pick_file()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn pick_npcap_folder() -> Option<String> {
    let mut dialog = rfd::FileDialog::new().set_title("选择 Npcap 安装目录");
    if let Some(system_root) = std::env::var_os("SystemRoot") {
        let npcap = std::path::PathBuf::from(system_root).join("System32").join("Npcap");
        if npcap.is_dir() {
            dialog = dialog.set_directory(npcap);
        }
    }
    dialog
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn pick_record_folder(current: Option<String>) -> Option<String> {
    let mut dialog = rfd::FileDialog::new().set_title("选择调试记录保存目录");
    if let Some(seed) = current
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(std::path::PathBuf::from)
    {
        if seed.is_dir() {
            dialog = dialog.set_directory(seed);
        }
    }
    dialog
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
async fn set_buff_overlay_visible(
    app: tauri::AppHandle,
    visible: bool,
    overlay_labels: Option<Vec<String>>,
) -> Result<(), String> {
    let selected = overlay_labels.unwrap_or_else(|| {
        STANDARD_OVERLAY_WINDOW_LABELS
            .iter()
            .map(|label| (*label).to_string())
            .collect()
    });
    // Every overlay is a separate WebView2 window. Capture stop, automatic
    // stop and the overlay's own fallback used to issue overlapping hide calls
    // for the same HWND, while ResizeObserver callbacks could resize it at the
    // same time. Run every native mutation on Tao's GUI thread, serialize the
    // complete batch and suppress idempotent work.
    let selected_summary = selected.join(",");
    let transitions = run_native_window_operation(&app, move |app, registry| {
        let mut native_state = overlay_native_window_state()?;
        let mut transitions = Vec::new();
        if visible {
            for label in &selected {
                if valid_overlay_label(label) {
                    ensure_native_window(app, registry, label)?;
                }
            }
        }
        // Clone lightweight window handles, then release the registry lock
        // before show/hide/emit. Those calls may pump native messages.
        let windows = registry
            .0
            .lock()
            .map_err(|_| "原生窗口注册表锁已损坏".to_string())?
            .clone();
        for (index, label) in OVERLAY_WINDOW_LABELS.iter().enumerate() {
            let Some(window) = windows.get(*label) else {
                continue;
            };
            let selected_for_updates = selected.iter().any(|value| value.as_str() == *label);
            // Both remaining overlays are persistent: a visible window is shown
            // directly instead of waiting for an event alert to reveal it.
            let event_listener = false;
            let should_show = visible && selected_for_updates && !event_listener;
            if !should_show {
                // Dedicated event-alert webviews stay logically enabled while
                // hidden so they can observe the shared live payload and appear
                // only when their event occurs.
                let keep_event_listener_active = visible && selected_for_updates && event_listener;
                let _ = window.emit("buff-overlay-visible", keep_event_listener_active);
                if native_state.visibility_change_needed(label, false) {
                    window.hide().map_err(|error| error.to_string())?;
                    native_state.remember_visibility(label, false);
                    transitions.push(format!("{label}=hidden"));
                }
                continue;
            }
            if native_state.visibility_change_needed(label, true) {
                window
                    .set_ignore_cursor_events(false)
                    .map_err(|error| error.to_string())?;
                window
                    .set_always_on_top(true)
                    .map_err(|error| error.to_string())?;
                if let Some(position) = stored_overlay_position(app, label) {
                    let _ = window.set_position(position);
                } else if let Ok(Some(monitor)) = window.primary_monitor() {
                    let screen = monitor.size();
                    let origin = monitor.position();
                    let x = origin.x + screen.width as i32 - 450;
                    let y = match *label {
                        "skill-countdown-overlay" => origin.y + 120,
                        _ => origin.y + 28 + index as i32 * 220,
                    };
                    let _ = window.set_position(PhysicalPosition::new(x, y));
                }
                window.show().map_err(|error| error.to_string())?;
                native_state.remember_visibility(label, true);
                transitions.push(format!("{label}=visible"));
            }
            window
                .emit("buff-overlay-visible", true)
                .map_err(|error| error.to_string())?;
        }
        Ok(transitions)
    })?;
    if !transitions.is_empty() {
        append_app_diagnostic(
            "overlay-native-visibility",
            &format!(
                "requested_visible={visible}\nselected={}\ntransitions={}",
                selected_summary,
                transitions.join(","),
            ),
        );
    }
    Ok(())
}

#[tauri::command]
async fn set_buff_overlay_window_visible(
    app: tauri::AppHandle,
    window_label: String,
    visible: bool,
) -> Result<(), String> {
    let label = window_label.trim();
    if !valid_overlay_label(label) {
        return Err("Unknown overlay window".into());
    }
    with_registered_native_window(&app, label, move |app, label, window| {
        let mut native_state = overlay_native_window_state()?;
        if !native_state.visibility_change_needed(label, visible) {
            return Ok(());
        }
        if !visible {
            window.hide().map_err(|error| error.to_string())?;
            native_state.remember_visibility(label, false);
            return Ok(());
        }
        window
            .set_ignore_cursor_events(false)
            .map_err(|error| error.to_string())?;
        window
            .set_always_on_top(true)
            .map_err(|error| error.to_string())?;
        if let Some(position) = stored_overlay_position(&app, label) {
            let _ = window.set_position(position);
        } else if let Ok(Some(monitor)) = window.primary_monitor() {
            let screen = monitor.size();
            let origin = monitor.position();
            let x = origin.x + screen.width as i32 - 450;
            let y = origin.y + if label == "skill-countdown-overlay" { 120 } else { 328 };
            let _ = window.set_position(PhysicalPosition::new(x, y));
        }
        window.show().map_err(|error| error.to_string())?;
        native_state.remember_visibility(label, true);
        Ok(())
    })
}

#[tauri::command]
fn configure_global_hotkeys(
    app: tauri::AppHandle,
    state: tauri::State<GlobalHotkeyState>,
    start_capture_hotkey: String,
    toggle_overlay_hotkey: String,
    disconnect_game_hotkey: String,
    disconnect_game_enabled: bool,
    stone_washing_hotkey: String,
) -> Result<(), String> {
    let start_capture_hotkey = start_capture_hotkey.trim();
    let toggle_overlay_hotkey = toggle_overlay_hotkey.trim();
    let disconnect_game_hotkey = disconnect_game_hotkey.trim();
    let stone_washing_hotkey = stone_washing_hotkey.trim();
    if start_capture_hotkey.is_empty()
        || toggle_overlay_hotkey.is_empty()
        || stone_washing_hotkey.is_empty()
        || (disconnect_game_enabled && disconnect_game_hotkey.is_empty())
    {
        return Err("快捷键不能为空".into());
    }

    let start_shortcut: Shortcut = start_capture_hotkey
        .parse()
        .map_err(|error| format!("开始实时统计快捷键无效：{error}"))?;
    let overlay_shortcut: Shortcut = toggle_overlay_hotkey
        .parse()
        .map_err(|error| format!("悬浮窗快捷键无效：{error}"))?;
    let disconnect_shortcut = if disconnect_game_enabled {
        Some(
            disconnect_game_hotkey
                .parse::<Shortcut>()
                .map_err(|error| format!("一键掉线快捷键无效：{error}"))?,
        )
    } else {
        None
    };
    let stone_washing_shortcut: Shortcut = stone_washing_hotkey
        .parse()
        .map_err(|error| format!("石板洗练快捷键无效：{error}"))?;
    let mut shortcut_ids = vec![
        start_shortcut.id(),
        overlay_shortcut.id(),
        stone_washing_shortcut.id(),
    ];
    if let Some(shortcut) = disconnect_shortcut {
        shortcut_ids.push(shortcut.id());
    }
    let unique_ids = shortcut_ids
        .iter()
        .copied()
        .collect::<std::collections::HashSet<_>>();
    if unique_ids.len() != shortcut_ids.len() {
        return Err("实时统计、悬浮窗、掉线和石板洗练不能使用相同的快捷键".into());
    }

    let manager = app.global_shortcut();
    let previous = state
        .0
        .lock()
        .map_err(|_| "快捷键状态不可用".to_string())
        .map(|bindings| {
            let shortcuts = vec![
                bindings.start_capture,
                bindings.toggle_overlay,
                bindings.disconnect_game,
                bindings.stone_washing,
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
            shortcuts
        })?;
    manager
        .unregister_all()
        .map_err(|error| format!("释放旧快捷键失败：{error}"))?;
    let mut registrations = vec![
        (
            "开始实时统计".to_string(),
            start_capture_hotkey.to_string(),
            start_shortcut,
        ),
        (
            "悬浮窗".to_string(),
            toggle_overlay_hotkey.to_string(),
            overlay_shortcut,
        ),
        (
            "石板洗练".to_string(),
            stone_washing_hotkey.to_string(),
            stone_washing_shortcut,
        ),
    ];
    if let Some(shortcut) = disconnect_shortcut {
        registrations.push((
            "一键掉线".to_string(),
            disconnect_game_hotkey.to_string(),
            shortcut,
        ));
    }
    for (label, text, shortcut) in registrations {
        if let Err(error) = manager.register(shortcut) {
            let _ = manager.unregister_all();
            for old_shortcut in &previous {
                let _ = manager.register(*old_shortcut);
            }
            return Err(format!(
                "注册{label}快捷键 {text} 失败，可能已被其他程序占用：{error}"
            ));
        }
    }

    let mut bindings = state.0.lock().map_err(|_| "快捷键状态不可用".to_string())?;
    bindings.start_capture_id = Some(start_shortcut.id());
    bindings.toggle_overlay_id = Some(overlay_shortcut.id());
    bindings.disconnect_game_id = disconnect_shortcut.map(|shortcut| shortcut.id());
    bindings.stone_washing_id = Some(stone_washing_shortcut.id());
    bindings.start_capture = Some(start_shortcut);
    bindings.toggle_overlay = Some(overlay_shortcut);
    bindings.disconnect_game = disconnect_shortcut;
    bindings.stone_washing = Some(stone_washing_shortcut);
    Ok(())
}

#[tauri::command]
fn begin_buff_overlay_drag(
    app: tauri::AppHandle,
    window_label: Option<String>,
) -> Result<(), String> {
    let label = window_label
        .as_deref()
        .filter(|value| valid_overlay_label(value))
        .unwrap_or("buff-overlay");
    with_registered_native_window(&app, label, |app, label, window| {
        window.start_dragging().map_err(|error| error.to_string())?;
        if let Ok(position) = window.outer_position() {
            save_overlay_position(app, label, position);
        }
        Ok(())
    })
}

#[tauri::command]
fn set_buff_overlay_height(
    app: tauri::AppHandle,
    height: f64,
    window_label: Option<String>,
) -> Result<(), String> {
    let label = window_label
        .as_deref()
        .filter(|value| valid_overlay_label(value))
        .unwrap_or("buff-overlay");
    with_registered_native_window(&app, label, move |_, label, window| {
        let size = window.outer_size().map_err(|error| error.to_string())?;
        let height = height.clamp(38.0, 760.0).round() as u32;
        window
            .set_size(PhysicalSize::new(size.width, height))
            .map_err(|error| error.to_string())?;
        if let Ok(mut native_state) = overlay_native_window_state() {
            native_state.logical_sizes.remove(label);
        }
        Ok(())
    })
}

#[tauri::command]
fn set_buff_overlay_size(
    app: tauri::AppHandle,
    width: f64,
    height: f64,
    window_label: Option<String>,
) -> Result<(), String> {
    let label = window_label
        .as_deref()
        .filter(|value| valid_overlay_label(value))
        .unwrap_or("buff-overlay");
    // Frontend settings allow 260 CSS px and WebView zoom may be 0.5x.
    // Do not silently widen a configured overlay after DOM measurement.
    let width = width.clamp(120.0, 1280.0).round() as u32;
    let height = height.clamp(38.0, 760.0).round() as u32;
    with_registered_native_window(&app, label, move |_, label, window| {
        let mut native_state = overlay_native_window_state()?;
        if !native_state.size_change_needed(label, width, height) {
            return Ok(());
        }
        window
            .set_size(LogicalSize::new(width, height))
            .map_err(|error| error.to_string())?;
        native_state.remember_size(label, width, height);
        Ok(())
    })
}

fn project_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf()
}

#[tauri::command]
fn write_frontend_diagnostic(
    kind: String,
    message: String,
    stack: Option<String>,
    location: Option<String>,
) -> Result<(), String> {
    let body = format!(
        "message={}\nlocation={}\nstack={}",
        message,
        location.unwrap_or_default(),
        stack.unwrap_or_default(),
    );
    append_app_diagnostic(&format!("frontend-{kind}"), &body);
    Ok(())
}

#[tauri::command]
fn open_crash_diagnostics() -> Result<String, String> {
    let directory = prepare_crash_diagnostics_dir();
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let mut command = Command::new("explorer.exe");
    command
        .arg(&directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command.spawn().map_err(|error| error.to_string())?;
    Ok(directory.to_string_lossy().into_owned())
}

fn diagnostic_resource_root(app: &tauri::AppHandle) -> PathBuf {
    if cfg!(debug_assertions) {
        project_root()
    } else {
        app.path()
            .resource_dir()
            .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default())
    }
}

#[tauri::command]
fn update_frontend_diagnostic_state(state: serde_json::Value) -> Result<(), String> {
    crash_diagnostics::update_frontend_state(state)
}

#[tauri::command]
fn crash_diagnostic_status() -> crash_diagnostics::CrashDiagnosticStatus {
    crash_diagnostics::status()
}

#[tauri::command]
fn export_crash_diagnostic_package(app: tauri::AppHandle) -> Result<String, String> {
    let destination = crash_diagnostics::create_current_package(
        "manual-export",
        &diagnostic_resource_root(&app),
    )?;
    let mut command = Command::new("explorer.exe");
    command
        .arg(format!("/select,{}", destination.to_string_lossy()))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let _ = command.spawn();
    Ok(destination.to_string_lossy().into_owned())
}

fn python_command(root: &Path) -> Option<Command> {
    if let Ok(executable) = std::env::var("DRAGON_NEST_PYTHON") {
        let mut command = Command::new(executable);
        command.arg(root.join("server.py"));
        return Some(command);
    }

    let local_venv = root.join(".venv").join("Scripts").join("python.exe");
    if local_venv.exists() {
        let mut command = Command::new(local_venv);
        command.arg(root.join("server.py"));
        return Some(command);
    }

    let mut command = Command::new("py");
    command.arg("-3.12").arg(root.join("server.py"));
    Some(command)
}

#[cfg(all(windows, debug_assertions))]
fn stop_stale_dev_backend(root: &Path) {
    let pid_path = root.join(".server_pid");
    let script = "$current=$PID; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | \
        Where-Object { $_.ProcessId -ne $current -and $_.Name -match '^(python|py|DragonNestEngine|DragonNestEngine_V4\\.(?:21|22|23|24|25|30))(\\.exe)?$' -and $_.CommandLine -match 'server\\.py' } | \
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }";
    let mut command = Command::new("powershell.exe");
    command
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000);
    let _ = command.status();
    let _ = std::fs::remove_file(pid_path);
    std::thread::sleep(std::time::Duration::from_millis(180));
}

#[cfg(not(all(windows, debug_assertions)))]
fn stop_stale_dev_backend(_root: &Path) {}

#[cfg(windows)]
fn create_backend_kill_job(child: &Child) -> Option<isize> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            std::ptr::addr_of!(limits).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        let assigned =
            configured != 0 && AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) != 0;
        if !assigned {
            let _ = CloseHandle(job);
            return None;
        }
        Some(job as isize)
    }
}

/// A development launcher may own the Python backend itself (file watching,
/// log tee, restart).  Spawning it here would first kill that process through
/// `stop_stale_dev_backend` and then fight the launcher's watcher.
#[cfg(all(windows, debug_assertions))]
fn external_dev_backend_requested() -> bool {
    matches!(
        std::env::var("DRAGON_NEST_DEV_EXTERNAL_BACKEND").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE")
    )
}

#[cfg(not(all(windows, debug_assertions)))]
fn external_dev_backend_requested() -> bool {
    false
}

fn start_backend(root: &Path) -> Option<BackendChild> {
    if cfg!(debug_assertions) && external_dev_backend_requested() {
        append_app_diagnostic("dev-external-backend", "spawn_skipped=true");
        return None;
    }
    let bundled = if cfg!(debug_assertions) {
        root.join("runtime/DragonNestEngine/DragonNestEngine.exe")
    } else {
        match engine_runtime::resolve_engine(root, APP_VERSION) {
            Ok(path) => path,
            Err(error) => {
                append_app_diagnostic("engine-installation-invalid", &error);
                return None;
            }
        }
    };
    // Development must execute the current Python sources. Preferring a
    // previously frozen runtime here made backend edits appear only after a
    // full release rebuild. Packaged releases still use the bundled engine.
    if cfg!(debug_assertions) {
        stop_stale_dev_backend(root);
    }
    let bundled_dir = bundled.parent().unwrap_or(root).to_path_buf();
    let backend_working_dir = if !cfg!(debug_assertions) && bundled.exists() {
        bundled_dir
    } else {
        root.to_path_buf()
    };
    let mut command = if cfg!(debug_assertions) {
        python_command(root)?
    } else if bundled.exists() {
        Command::new(&bundled)
    } else {
        python_command(root)?
    };
    command
        // Do not inherit the caller's working directory. A desktop shortcut
        // can start the app from the Desktop or System32, which previously
        // made the frozen capture backend resolve resources differently.
        .current_dir(backend_working_dir)
        .env("DRAGON_NEST_APP_VERSION", app_display_version())
        .env("DRAGON_NEST_INSTALL_ROOT", root)
        .env("DN_SIMULATOR_NPCAP_INSTALLER", std::env::var_os("DN_SIMULATOR_NPCAP_INSTALLER")
            .map(PathBuf::from).unwrap_or_else(|| root.join("third_party/npcap/npcap-1.88.exe")))
        .env("DRAGON_NEST_DESKTOP_PID", std::process::id().to_string())
        .arg("--port")
        .arg("48710")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let child = command.spawn().ok()?;
    #[cfg(windows)]
    let kill_job = create_backend_kill_job(&child);
    Some(BackendChild {
        child,
        #[cfg(windows)]
        kill_job,
        stopped: false,
    })
}

#[tauri::command]
fn finish_exit_after_build_save(
    app: tauri::AppHandle,
    state: tauri::State<'_, ExitSaveState>,
) -> Result<(), String> {
    state.0.store(2, Ordering::SeqCst);
    append_app_diagnostic(
        "graceful-exit-acknowledged",
        "frontend_build_save_completed=true",
    );
    // The original close request has already been prevented while the
    // frontend saves. Closing the WebView again would generate a second
    // WM_CLOSE/CloseRequested cycle and could also wait behind the native
    // window-operation lock. Once exit_state is authorised, terminate the
    // application directly so normal and background closes share one path.
    app.exit(0);
    Ok(())
}

fn begin_graceful_exit(app: &tauri::AppHandle) {
    let exit_state = app.state::<ExitSaveState>();
    if exit_state
        .0
        .compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return;
    }

    append_app_diagnostic(
        "graceful-exit-begin",
        "waiting_for_frontend_build_save=true",
    );
    let _ = app.emit_to("main", "dragon-nest-save-builds-before-exit", ());

    // Never trap the user in a window that cannot close because the WebView
    // or local backend stopped replying. The frontend normally acknowledges
    // immediately; this is only the bounded final fallback.
    let fallback_handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(2));
        let fallback_state = fallback_handle.state::<ExitSaveState>();
        if fallback_state
            .0
            .compare_exchange(1, 2, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            append_app_diagnostic(
                "graceful-exit-timeout",
                "frontend_build_save_did_not_acknowledge_within_2_seconds",
            );
            // The WebView may be unresponsive, so do not try to close it and
            // re-enter the window callback. ExitRequested is now authorised
            // by state=2 and will perform the ordinary runtime teardown.
            fallback_handle.exit(0);
        }
    });
}

fn main_window_close_context(app: &tauri::AppHandle, exit_state: u8, uptime: Duration) -> String {
    // This callback runs on the Tao event-loop thread. Do not wait for the
    // command-operation mutex here: a worker holding it may itself be waiting
    // for a window getter to be served by this thread.
    let (focused, visible, minimized) =
        with_registered_native_window_unlocked(app, "main", |window| {
            Ok((
                window.is_focused().unwrap_or(false),
                window.is_visible().unwrap_or(false),
                window.is_minimized().unwrap_or(false),
            ))
        })
        .unwrap_or((false, false, false));
    format!(
        "exit_state={exit_state}\nfocused={focused}\nvisible={visible}\nminimized={minimized}\ngame_foreground={}\nuptime_seconds={:.3}",
        foreground_game_process_id().is_some(),
        uptime.as_secs_f64(),
    )
}

fn stop_desktop_runtime(app: &tauri::AppHandle) {
    // ExitRequested and Exit are both emitted during a normal Tauri shutdown.
    // Destroying the same WebView windows from both callbacks can race inside
    // WebView2, so the complete native teardown must run exactly once.
    if DESKTOP_RUNTIME_STOP_STATE.swap(1, Ordering::SeqCst) != 0 {
        return;
    }
    // This runs on the event-loop thread. A reentrant native callback must not
    // wait for an operation whose completion needs that same event loop.
    // Tauri still owns final window destruction when explicit teardown is busy.
    if let Ok(_operation) = NATIVE_WINDOW_OPERATION_LOCK
        .get_or_init(|| Mutex::new(()))
        .try_lock()
    {
        if let Some(registry) = app.try_state::<NativeWindowRegistry>() {
            let windows_to_destroy = registry.0.try_lock().ok().map(|windows| {
                OVERLAY_WINDOW_LABELS
                    .into_iter()
                    .filter_map(|label| windows.get(label).cloned())
                .collect::<Vec<_>>()
            });
            if let Some(windows) = windows_to_destroy {
                // Release the registry before WebView2 destruction callbacks.
                for window in windows {
                    let _ = window.destroy();
                }
            }
        }
        drop(_operation);
    } else {
        append_app_diagnostic("shutdown-native-operation-busy", "deferred_to_tauri_teardown=true");
    }
    let backend_state = app.state::<BackendProcess>();
    let child = match backend_state.0.try_lock() {
        Ok(mut process) => process.take(),
        Err(_) => {
            // An updater/restart worker owns it. Do not deadlock the UI while
            // waiting; the owned kill-on-close Job is released at process exit.
            append_app_diagnostic("shutdown-backend-busy", "owned_job_cleanup_on_process_exit=true");
            None
        }
    };
    if let Some(mut child) = child {
        child.stop();
    }
}

pub fn run() {
    if !acquire_single_instance_guard() {
        return;
    }
    prepare_crash_diagnostics_dir();
    install_panic_diagnostics();
    install_native_exception_diagnostics();
    append_app_diagnostic(
        "desktop-start",
        &format!(
            "version={APP_VERSION}\npid={}\nexecutable={}",
            std::process::id(),
            std::env::current_exe()
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_default(),
        ),
    );
    // Start the capture engine before the WebView is created. WebView2 needs a
    // couple of seconds to bring up its first window; the engine's own ~1.5 s
    // boot should overlap with that instead of starting afterwards.
    let prelaunched = if cfg!(debug_assertions) {
        None
    } else {
        std::env::current_exe()
            .ok()
            .and_then(|exe| exe.parent().map(|dir| dir.to_path_buf()))
            .and_then(|root| start_backend(&root))
    };
    match crash_diagnostics::initialize_session(APP_VERSION, app_diagnostic_path()) {
        Ok(true) => append_app_diagnostic(
            "previous-unclean-exit-detected",
            "The previous desktop session ended without a graceful close marker; a diagnostic package will be generated in the background.",
        ),
        Ok(false) => {}
        Err(error) => append_app_diagnostic("crash-session-initialize-error", &error),
    }
    start_known_injected_module_monitor();
    let app = tauri::Builder::default()
        // Register hotkey state before plugins, windows, and frontend commands
        // can observe the app. Managing it from setup() left a startup race while
        // the Python backend process was being launched.
        .manage(GlobalHotkeyState(Mutex::new(HotkeyBindings::default())))
        .manage(ExitSaveState(AtomicU8::new(0)))
        .manage(MinimizeToTrayState(AtomicU8::new(1)))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state != ShortcutState::Pressed {
                        return;
                    }
                    // Dragon Nest suppresses RegisterHotKey while it owns the
                    // foreground. The foreground poller is authoritative in
                    // that state; ignoring plugin callbacks also prevents a
                    // key from firing twice on systems where both paths work.
                    if foreground_game_process_id().is_some() {
                        return;
                    }
                    let action =
                        app.state::<GlobalHotkeyState>()
                            .0
                            .lock()
                            .ok()
                            .and_then(|bindings| {
                                if bindings.start_capture_id == Some(shortcut.id()) {
                                    Some("start-capture".to_string())
                                } else if bindings.toggle_overlay_id == Some(shortcut.id()) {
                                    Some("toggle-overlay".to_string())
                                } else if bindings.disconnect_game_id == Some(shortcut.id()) {
                                    Some("disconnect-game".to_string())
                                } else if bindings.stone_washing_id == Some(shortcut.id()) {
                                    Some("stone-washing-toggle".to_string())
                                } else {
                                    None
                                }
                            });
                    if let Some(action) = action {
                        dispatch_global_hotkey_action(app, action);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            install_npcap,
            pick_npcap_path,
            pick_npcap_folder,
            pick_record_folder,
            set_buff_overlay_visible,
            set_buff_overlay_window_visible,
            begin_buff_overlay_drag,
            set_buff_overlay_height,
            set_buff_overlay_size,
            configure_global_hotkeys,
            utility_plugin_status,
            disconnect_game,
            launch_bundled_utility,
            write_frontend_diagnostic,
            open_crash_diagnostics,
            update_frontend_diagnostic_state,
            crash_diagnostic_status,
            export_crash_diagnostic_package,
            get_minimize_to_tray,
            set_minimize_to_tray,
            finish_exit_after_build_save
        ])
        .setup(move |app| {
            // WebView2 and other native libraries may install their own exception
            // filter during startup. Re-register ours after window initialization.
            install_native_exception_diagnostics();
            // Launch the capture engine before any other setup work. The frozen
            // engine needs about 1.5 s to bind its port; starting it here lets
            // that overlap with WebView2, menu and tray initialisation instead
            // of pushing the window's first usable second out by several
            // seconds.
            let root = if cfg!(debug_assertions) {
                project_root()
            } else {
                app.path().resource_dir()?
            };
            app.manage(BackendProcess(Mutex::new(
                prelaunched.or_else(|| start_backend(&root)),
            )));
            start_foreground_game_hotkey_poller(app.handle().clone());
            let mut native_windows = HashMap::new();
            for label in NATIVE_WINDOW_LABELS {
                if let Some(window) = app.get_webview_window(label) {
                    if label == "main" {
                        window.set_title(&format!(
                            "DN倒计时工具 V{}",
                            app_display_version()
                        ))?;
                    } else {
                        window.set_ignore_cursor_events(false)?;
                    }
                    native_windows.insert(label.to_string(), window);
                }
            }
            app.manage(NativeWindowRegistry(Mutex::new(native_windows)));
            let minimize_to_tray = stored_minimize_to_tray(app.handle());
            app.state::<MinimizeToTrayState>()
                .0
                .store(u8::from(minimize_to_tray), Ordering::SeqCst);

            let show_item =
                MenuItem::with_id(app, TRAY_SHOW_MENU_ID, "显示主窗口", true, None::<&str>)?;
            let start_item = MenuItem::with_id(
                app,
                TRAY_START_MENU_ID,
                "一键开始（统计 + 悬浮窗）",
                true,
                None::<&str>,
            )?;
            let stop_item = MenuItem::with_id(
                app,
                TRAY_STOP_MENU_ID,
                "一键结束（停统计 + 关悬浮窗）",
                true,
                None::<&str>,
            )?;
            let exit_item = MenuItem::with_id(app, TRAY_EXIT_MENU_ID, "退出", true, None::<&str>)?;
            let tray_menu = Menu::with_items(
                app,
                &[&start_item, &stop_item, &show_item, &exit_item],
            )?;
            let mut tray_builder = TrayIconBuilder::with_id("dragon-nest-act-tray")
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .tooltip(format!(
                    "DN倒计时工具 V{}",
                    app_display_version()
                ))
                .on_menu_event(|app, event| {
                    if event.id() == TRAY_SHOW_MENU_ID {
                        if let Err(error) = show_main_window(app) {
                            append_app_diagnostic("tray-show-main-error", &error);
                        }
                    } else if event.id() == TRAY_START_MENU_ID {
                        dispatch_global_hotkey_action(app, "start-capture".to_string());
                    } else if event.id() == TRAY_STOP_MENU_ID {
                        // 前端把 toggle-overlay 固定解释为「一键结束」。
                        dispatch_global_hotkey_action(app, "toggle-overlay".to_string());
                    } else if event.id() == TRAY_EXIT_MENU_ID {
                        request_graceful_exit(app, "source=tray-menu");
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    let should_show = matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } | TrayIconEvent::DoubleClick {
                            button: MouseButton::Left,
                            ..
                        }
                    );
                    if should_show {
                        if let Err(error) = show_main_window(tray.app_handle()) {
                            append_app_diagnostic("tray-left-click-error", &error);
                        }
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }
            tray_builder.build(app)?;

            let diagnostic_root = root.clone();
            let diagnostic_handle = app.handle().clone();
            std::thread::spawn(move || {
                match crash_diagnostics::auto_package_previous(&diagnostic_root) {
                    Ok(Some(path)) => {
                        append_app_diagnostic(
                            "automatic-crash-package-ready",
                            &path.to_string_lossy(),
                        );
                        let _ = diagnostic_handle.emit(
                            "dragon-nest-crash-package-ready",
                            path.to_string_lossy().into_owned(),
                        );
                    }
                    Ok(None) => {}
                    Err(error) => append_app_diagnostic("automatic-crash-package-error", &error),
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the desktop app");

    let desktop_started_at = Instant::now();
    let mut last_backend_health_check = Instant::now() - Duration::from_secs(1);
    let mut backend_restart_history: Vec<Instant> = Vec::new();
    app.run(move |handle, event| {
        if matches!(event, RunEvent::MainEventsCleared)
            && last_backend_health_check.elapsed() >= Duration::from_secs(1)
        {
            last_backend_health_check = Instant::now();
            let mut unexpected_exit = None;
            let mut backend_exited = false;
            // A restart/update can own this mutex for seconds (runtime
            // verification, extraction, graceful shutdown). A health probe is
            // optional; it must never park the native event loop behind it.
            if let Ok(mut process) = handle.state::<BackendProcess>().0.try_lock() {
                if let Some(backend) = process.as_mut() {
                    match backend.child.try_wait() {
                        Ok(Some(status)) => {
                            backend_exited = true;
                            unexpected_exit = Some(format!(
                                "Python backend exited while the desktop app was running.\nstatus={status:?}\ncode={:?}",
                                status.code(),
                            ));
                        }
                        Ok(None) => {}
                        Err(error) => {
                            unexpected_exit = Some(format!(
                                "Unable to query Python backend state: {error}"
                            ));
                        }
                    }
                }
                if backend_exited {
                    process.take();
                }
            }
            if let Some(message) = unexpected_exit {
                append_app_diagnostic("backend-unexpected-exit", &message);
                let _ = handle.emit("dragon-nest-backend-unexpected-exit", message);
                let diagnostic_handle = handle.clone();
                let diagnostic_root = diagnostic_resource_root(handle);
                std::thread::spawn(move || {
                    match crash_diagnostics::create_current_package(
                        "backend-unexpected-exit",
                        &diagnostic_root,
                    ) {
                        Ok(path) => {
                            append_app_diagnostic(
                                "backend-crash-package-ready",
                                &path.to_string_lossy(),
                            );
                            let _ = diagnostic_handle.emit(
                                "dragon-nest-crash-package-ready",
                                path.to_string_lossy().into_owned(),
                            );
                        }
                        Err(error) => append_app_diagnostic(
                            "backend-crash-package-error",
                            &error,
                        ),
                    }
                });
                if backend_exited {
                    // A native Npcap fault used to leave the WebView alive
                    // but the HTTP engine gone. Stop then stayed on
                    // "正在结束" and every history query failed to fetch.
                    // Recover the engine, but cap retries to avoid a crash
                    // loop if a different persistent fault is present.
                    let now = Instant::now();
                    backend_restart_history.retain(|at| {
                        now.duration_since(*at) < Duration::from_secs(300)
                    });
                    if backend_restart_history.len() < 2
                        && DESKTOP_RUNTIME_STOP_STATE.load(Ordering::SeqCst) == 0
                        && handle.state::<ExitSaveState>().0.load(Ordering::SeqCst) == 0
                    {
                        backend_restart_history.push(now);
                        let restart_handle = handle.clone();
                        let restart_root = diagnostic_resource_root(handle);
                        std::thread::spawn(move || {
                            std::thread::sleep(Duration::from_millis(500));
                            if let Ok(mut process) = restart_handle
                                .state::<BackendProcess>()
                                .0
                                .lock()
                            {
                                if process.is_none()
                                    && DESKTOP_RUNTIME_STOP_STATE.load(Ordering::SeqCst) == 0
                                    && restart_handle
                                        .state::<ExitSaveState>()
                                        .0
                                        .load(Ordering::SeqCst) == 0
                                {
                                    *process = start_backend(&restart_root);
                                    if process.is_some() {
                                        append_app_diagnostic(
                                            "backend-auto-restarted",
                                            "Unexpected exit recovered; live capture must start a new session.",
                                        );
                                        let _ = restart_handle.emit(
                                            "dragon-nest-backend-restarted",
                                            (),
                                        );
                                    } else {
                                        append_app_diagnostic(
                                            "backend-auto-restart-failed",
                                            "Could not start the packaged backend.",
                                        );
                                    }
                                }
                            }
                        });
                    }
                }
            }
        }
        if let RunEvent::WindowEvent { label, event, .. } = &event {
            if valid_overlay_label(label) {
                if let WindowEvent::Moved(position) = event {
                    save_overlay_position(handle, label, *position);
                }
            }
            if label == "main" {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    let exit_state = handle.state::<ExitSaveState>();
                    let state_value = exit_state.0.load(Ordering::SeqCst);
                    let minimize_to_tray = handle
                        .state::<MinimizeToTrayState>()
                        .0
                        .load(Ordering::SeqCst)
                        != 0;
                    let context = main_window_close_context(
                        handle,
                        state_value,
                        desktop_started_at.elapsed(),
                    );
                    append_app_diagnostic(
                        "main-close-requested",
                        &format!("{context}\nminimize_to_tray={minimize_to_tray}"),
                    );
                    let decision = main_close_decision(state_value, minimize_to_tray);
                    match decision {
                        MainCloseDecision::HideToTray => {
                            api.prevent_close();
                            match hide_main_window(handle) {
                                Ok(()) => append_app_diagnostic(
                                    "main-hidden-to-tray",
                                    "close_button=true",
                                ),
                                Err(error) => {
                                    append_app_diagnostic("main-hide-to-tray-error", &error);
                                    request_graceful_exit(handle, "source=hide-to-tray-fallback");
                                }
                            }
                        }
                        MainCloseDecision::BeginGracefulExit => {
                            api.prevent_close();
                            request_graceful_exit(handle, "source=main-close-button");
                        }
                        MainCloseDecision::WaitForGracefulExit => api.prevent_close(),
                        MainCloseDecision::AllowExit => {
                            append_app_diagnostic("main-close-authorized", &context);
                            handle.exit(0);
                        }
                    }
                }
            }
        }
        match &event {
            RunEvent::ExitRequested { code, api, .. } => {
                let state_value = handle.state::<ExitSaveState>().0.load(Ordering::SeqCst);
                let message = format!(
                    "exit_state={state_value}\ncode={code:?}\nuptime_seconds={:.3}",
                    desktop_started_at.elapsed().as_secs_f64(),
                );
                if should_block_exit_request(state_value) {
                    api.prevent_exit();
                    append_app_diagnostic("desktop-exit-deferred", &message);
                    if state_value == 0 {
                        // Some Windows shutdown paths emit ExitRequested
                        // without first delivering a focused main-window close.
                        // Defer once for saving, then complete automatically.
                        begin_graceful_exit(handle);
                    }
                } else {
                    append_app_diagnostic("desktop-exit-requested", &message);
                    crash_diagnostics::mark_session_closing("desktop-exit-requested");
                    stop_desktop_runtime(handle);
                }
            }
            RunEvent::Exit => {
                append_app_diagnostic(
                    "desktop-exit-final",
                    &format!(
                        "uptime_seconds={:.3}",
                        desktop_started_at.elapsed().as_secs_f64(),
                    ),
                );
                crash_diagnostics::mark_session_closed("desktop-exit-final");
                stop_desktop_runtime(handle);
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod utility_plugin_tests {
    use super::*;

    #[test]
    fn native_operation_contention_returns_without_blocking_event_loop() {
        let held = NATIVE_WINDOW_OPERATION_LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .expect("operation mutex");
        let (tx, rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let rejected = native_window_operation_guard().is_err();
            let _ = tx.send(rejected);
        });
        let result = rx.recv_timeout(Duration::from_secs(1));
        // Release even on failure so a regression cannot hang the test runner.
        drop(held);
        worker.join().expect("probe thread");
        assert_eq!(result.expect("event loop must not wait on this mutex"), true);
        assert!(native_window_operation_guard().is_ok());
    }

    #[test]
    fn game_process_matching_is_exact_and_case_insensitive() {
        assert!(is_game_executable_name("DragonNest.exe"));
        assert!(is_game_executable_name("DRAGONNEST_REBORN.EXE"));
        assert!(!is_game_executable_name("dragonnest-helper.exe"));
        assert!(!is_game_executable_name("not-dragonnest.exe"));
    }

    #[cfg(windows)]
    #[test]
    fn act_desktop_executable_matching_excludes_release_installers() {
        assert!(is_act_desktop_executable_name(
            "dragon-nest-simulator-v420.exe"
        ));
        assert!(is_act_desktop_executable_name(
            "DRAGON-NEST-SIMULATOR-V420.EXE"
        ));
        assert!(!is_act_desktop_executable_name("DNACT4.68.exe"));
        assert!(!is_act_desktop_executable_name("DNACT4.68-setup.exe"));
        assert!(!is_act_desktop_executable_name(
            "DNACT4.68-自动抓包崩溃修复测试版.exe"
        ));
        assert!(!is_act_desktop_executable_name("DragonNest.exe"));
    }

    #[test]
    fn game_path_matching_only_accepts_the_two_client_names() {
        assert!(is_game_executable_path(
            r"C:\Games\DragonNest\dragonnest.exe"
        ));
        assert!(is_game_executable_path(
            r"D:\DragonNest\dragonnest_reborn.exe"
        ));
        assert!(!is_game_executable_path(
            r"C:\Tools\dragonnest_reborn.exe.backup"
        ));
    }

    #[test]
    fn main_close_is_routed_by_the_tray_preference_and_exit_state() {
        assert_eq!(main_close_decision(0, true), MainCloseDecision::HideToTray);
        assert_eq!(
            main_close_decision(0, false),
            MainCloseDecision::BeginGracefulExit
        );
        assert_eq!(
            main_close_decision(1, true),
            MainCloseDecision::WaitForGracefulExit
        );
        assert_eq!(
            main_close_decision(1, false),
            MainCloseDecision::WaitForGracefulExit
        );
        assert_eq!(main_close_decision(2, true), MainCloseDecision::AllowExit);
        assert_eq!(main_close_decision(2, false), MainCloseDecision::AllowExit);
    }

    #[test]
    fn raw_exit_request_is_blocked_until_frontend_save_finishes() {
        assert!(should_block_exit_request(0));
        assert!(should_block_exit_request(1));
        assert!(!should_block_exit_request(2));
    }

    #[test]
    fn every_native_overlay_has_a_hidden_predeclared_window() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json"))
            .expect("tauri.conf.json must be valid JSON");
        let windows = config["app"]["windows"]
            .as_array()
            .expect("Tauri windows must be configured");
        for label in NATIVE_WINDOW_LABELS
            .into_iter()
            .filter(|label| *label != "main")
        {
            let window = windows
                .iter()
                .find(|window| window["label"].as_str() == Some(label))
                .unwrap_or_else(|| panic!("native overlay {label} must be predeclared"));
            assert_eq!(
                window["visible"].as_bool(),
                Some(false),
                "native overlay {label} must start hidden"
            );
        }
    }

    #[test]
    fn overlay_native_state_suppresses_duplicate_visibility_requests() {
        let mut state = OverlayNativeWindowState::default();
        assert!(state.visibility_change_needed("buff-overlay", true));
        state.remember_visibility("buff-overlay", true);
        assert!(!state.visibility_change_needed("buff-overlay", true));
        assert!(state.visibility_change_needed("buff-overlay", false));
    }

    #[test]
    fn overlay_native_state_ignores_one_pixel_resize_feedback() {
        let mut state = OverlayNativeWindowState::default();
        assert!(state.size_change_needed("buff-overlay", 420, 180));
        state.remember_size("buff-overlay", 420, 180);
        assert!(!state.size_change_needed("buff-overlay", 421, 179));
        assert!(state.size_change_needed("buff-overlay", 422, 180));
    }

}
