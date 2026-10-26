use std::fs::{self, File};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use zip::write::FileOptions;
use zip::{CompressionMethod, ZipWriter};

const SESSION_SCHEMA: &str = "dragon-nest-crash-session-v1";
const PACKAGE_SCHEMA: &str = "dragon-nest-crash-package-v1";
const MAX_LOG_BYTES: usize = 2 * 1024 * 1024;
const MAX_DUMP_BYTES: u64 = 32 * 1024 * 1024;

static CURRENT_SESSION: OnceLock<Mutex<SessionRecord>> = OnceLock::new();
static PREVIOUS_UNCLEAN_SESSION: OnceLock<Mutex<Option<SessionRecord>>> = OnceLock::new();
static PREVIOUS_UNCLEAN_DETECTED: OnceLock<bool> = OnceLock::new();
static PENDING_PACKAGE: OnceLock<Mutex<Option<PathBuf>>> = OnceLock::new();

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SessionRecord {
    schema: String,
    session_id: String,
    version: String,
    state: String,
    pid: u32,
    started_epoch_ms: u128,
    ended_epoch_ms: Option<u128>,
    close_reason: String,
    executable: String,
    desktop_log: String,
    frontend_state: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CrashDiagnosticStatus {
    pub previous_unclean_exit_detected: bool,
    pub latest_package: String,
    pub diagnostics_directory: String,
}

fn epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

pub fn diagnostics_dir() -> PathBuf {
    let base = std::env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    base.join("Documents")
        .join("DragonNestDpsCapture")
        .join("crash_diagnostics")
}

fn active_session_path() -> PathBuf {
    diagnostics_dir().join("active-session.json")
}

fn sessions_dir() -> PathBuf {
    diagnostics_dir().join("sessions")
}

fn packages_dir() -> PathBuf {
    diagnostics_dir().join("packages")
}

#[cfg(windows)]
fn replace_file_atomic(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let source_wide = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let destination_wide = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let ok = unsafe {
        MoveFileExW(
            source_wide.as_ptr(),
            destination_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_file_atomic(source: &Path, destination: &Path) -> std::io::Result<()> {
    fs::rename(source, destination)
}

fn write_json_atomic(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "诊断文件路径无父目录".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = path.with_extension("tmp");
    let payload = serde_json::to_vec_pretty(value).map_err(|error| error.to_string())?;
    fs::write(&temporary, payload).map_err(|error| error.to_string())?;
    replace_file_atomic(&temporary, path).map_err(|error| error.to_string())
}

fn read_session(path: &Path) -> Option<SessionRecord> {
    let payload = fs::read(path).ok()?;
    let session: SessionRecord = serde_json::from_slice(&payload).ok()?;
    (session.schema == SESSION_SCHEMA).then_some(session)
}

#[cfg(windows)]
fn process_is_running(pid: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    if pid == 0 {
        return false;
    }
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return false;
    }
    let mut exit_code = 0u32;
    let ok = unsafe { GetExitCodeProcess(handle, &mut exit_code) } != 0;
    unsafe { CloseHandle(handle) };
    ok && exit_code == STILL_ACTIVE as u32
}

#[cfg(not(windows))]
fn process_is_running(_pid: u32) -> bool {
    false
}

pub fn initialize_session(version: &str, desktop_log: &Path) -> Result<bool, String> {
    fs::create_dir_all(diagnostics_dir()).map_err(|error| error.to_string())?;
    fs::create_dir_all(sessions_dir()).map_err(|error| error.to_string())?;
    fs::create_dir_all(packages_dir()).map_err(|error| error.to_string())?;

    let detected_at = epoch_ms();
    let previous = read_session(&active_session_path())
        .filter(|session| {
            session.state != "closed"
                && session.pid != std::process::id()
                && !process_is_running(session.pid)
        })
        .map(|mut session| {
            session.state = "unclean".to_string();
            session.ended_epoch_ms = Some(detected_at);
            session.close_reason = "missing-graceful-close-marker".to_string();
            session
        });
    let previous_detected = previous.is_some();
    let _ = PREVIOUS_UNCLEAN_DETECTED.set(previous_detected);
    let _ = PREVIOUS_UNCLEAN_SESSION.set(Mutex::new(previous));

    let started = epoch_ms();
    let session_id = format!("{started}-{}", std::process::id());
    let frontend_state = sessions_dir().join(format!("frontend-{session_id}.json"));
    let session = SessionRecord {
        schema: SESSION_SCHEMA.to_string(),
        session_id,
        version: version.to_string(),
        state: "running".to_string(),
        pid: std::process::id(),
        started_epoch_ms: started,
        ended_epoch_ms: None,
        close_reason: String::new(),
        executable: std::env::current_exe()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_default(),
        desktop_log: desktop_log.to_string_lossy().into_owned(),
        frontend_state: frontend_state.to_string_lossy().into_owned(),
    };
    write_json_atomic(&active_session_path(), &session)?;
    write_json_atomic(
        &sessions_dir().join(format!("session-{}.json", session.session_id)),
        &session,
    )?;
    let _ = CURRENT_SESSION.set(Mutex::new(session));
    let _ = PENDING_PACKAGE.set(Mutex::new(None));
    Ok(previous_detected)
}

pub fn mark_session_closed(reason: &str) {
    let Some(lock) = CURRENT_SESSION.get() else {
        return;
    };
    let Ok(mut session) = lock.lock() else {
        return;
    };
    if session.state == "closed" {
        return;
    }
    session.state = "closed".to_string();
    session.ended_epoch_ms = Some(epoch_ms());
    session.close_reason = reason.chars().take(256).collect();
    let _ = write_json_atomic(&active_session_path(), &*session);
    let _ = write_json_atomic(
        &sessions_dir().join(format!("session-{}.json", session.session_id)),
        &*session,
    );
}

pub fn mark_session_closing(reason: &str) {
    let Some(lock) = CURRENT_SESSION.get() else {
        return;
    };
    let Ok(mut session) = lock.lock() else {
        return;
    };
    if session.state == "closed" {
        return;
    }
    session.state = "closing".to_string();
    session.close_reason = reason.chars().take(256).collect();
    let _ = write_json_atomic(&active_session_path(), &*session);
    let _ = write_json_atomic(
        &sessions_dir().join(format!("session-{}.json", session.session_id)),
        &*session,
    );
}

pub fn update_frontend_state(mut state: Value) -> Result<(), String> {
    let lock = CURRENT_SESSION
        .get()
        .ok_or_else(|| "诊断会话尚未初始化".to_string())?;
    let session = lock.lock().map_err(|_| "诊断会话锁已损坏".to_string())?;
    if let Some(object) = state.as_object_mut() {
        object.insert("schema".into(), json!("dragon-nest-frontend-state-v1"));
        object.insert("sessionId".into(), json!(session.session_id));
        object.insert("desktopPid".into(), json!(session.pid));
        object.insert("recordedEpochMs".into(), json!(epoch_ms().to_string()));
    }
    write_json_atomic(Path::new(&session.frontend_state), &state)
}

fn sanitize_text(input: &str) -> String {
    let profile = std::env::var("USERPROFILE").unwrap_or_default();
    let escaped_profile = profile.replace('\\', "\\\\");
    let mut output = String::with_capacity(input.len());
    for raw_line in input.lines() {
        let upper = raw_line.to_ascii_uppercase();
        let sensitive = [
            "TEAM_SCORE_UPLOAD_TOKEN",
            "CLOUDBASE_APIKEY",
            "UPLOADTOKEN",
            "AUTHORIZATION:",
            "PASSWORD",
            "SECRET",
        ]
        .iter()
        .any(|needle| upper.contains(needle));
        if sensitive {
            let key = raw_line.split(['=', ':']).next().unwrap_or("sensitive");
            output.push_str(key);
            output.push_str("=[REDACTED]\n");
            continue;
        }
        let mut line = raw_line.to_string();
        if !profile.is_empty() {
            line = line.replace(&profile, "%USERPROFILE%");
            line = line.replace(&escaped_profile, "%USERPROFILE%");
        }
        output.push_str(&line);
        output.push('\n');
    }
    output
}

fn read_text_tail(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let length = file.metadata().ok()?.len();
    if length > MAX_LOG_BYTES as u64 {
        use std::io::{Seek, SeekFrom};
        file.seek(SeekFrom::End(-(MAX_LOG_BYTES as i64))).ok()?;
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).ok()?;
    Some(sanitize_text(&String::from_utf8_lossy(&bytes)))
}

fn file_modified_ms(path: &Path) -> u128 {
    path.metadata()
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_millis())
        .unwrap_or_default()
}

fn related_diagnostic_files(session: &SessionRecord) -> Vec<PathBuf> {
    let start = session.started_epoch_ms.saturating_sub(120_000);
    let end = session.ended_epoch_ms.unwrap_or_else(epoch_ms) + 120_000;
    let mut candidates = fs::read_dir(diagnostics_dir())
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_file())
        .filter(|path| {
            let extension = path.extension().and_then(|value| value.to_str());
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("");
            matches!(extension, Some("log") | Some("dmp"))
                || (extension == Some("json") && name.starts_with("backend-state-"))
        })
        .filter(|path| {
            let modified = file_modified_ms(path);
            modified >= start && modified <= end
        })
        .collect::<Vec<_>>();
    let desktop_log = PathBuf::from(&session.desktop_log);
    if desktop_log.is_file() && !candidates.contains(&desktop_log) {
        candidates.push(desktop_log);
    }
    candidates.sort_by_key(|path| file_modified_ms(path));
    if candidates.len() > 32 {
        candidates = candidates.split_off(candidates.len() - 32);
    }
    candidates
}

#[cfg(windows)]
fn run_hidden(command: &mut Command) -> std::io::Result<std::process::Output> {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000).output()
}

#[cfg(not(windows))]
fn run_hidden(command: &mut Command) -> std::io::Result<std::process::Output> {
    command.output()
}

fn powershell_report(script: &str) -> String {
    let mut command = Command::new("powershell.exe");
    command
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .stdout(Stdio::piped());
    match run_hidden(&mut command) {
        Ok(output) => {
            let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
            if !output.stderr.is_empty() {
                text.push_str("\n[stderr]\n");
                text.push_str(&String::from_utf8_lossy(&output.stderr));
            }
            sanitize_text(&text)
        }
        Err(error) => format!("无法读取系统诊断信息：{error}"),
    }
}

fn system_report() -> String {
    powershell_report(
        "$ErrorActionPreference='SilentlyContinue'; \
         '=== OS ==='; Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,LastBootUpTime,FreePhysicalMemory,TotalVisibleMemorySize | Format-List; \
         '=== Computer ==='; Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory | Format-List; \
         '=== CPU ==='; Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | Format-List; \
         '=== GPU ==='; Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM | Format-Table -AutoSize; \
         '=== Network adapters (no address) ==='; Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed | Format-Table -AutoSize; \
         '=== Npcap ==='; Get-Service npcap,npf -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType | Format-Table -AutoSize; \
         '=== Relevant processes ==='; Get-Process | Where-Object {$_.ProcessName -match 'dragon|python|msedgewebview2'} | Select-Object ProcessName,Id,CPU,WorkingSet64,StartTime | Format-Table -AutoSize",
    )
}

fn windows_event_report(session: &SessionRecord) -> String {
    let start_seconds = (session.started_epoch_ms / 1000).saturating_sub(300);
    let script = format!(
        "$ErrorActionPreference='SilentlyContinue'; \
         $start=[DateTimeOffset]::FromUnixTimeSeconds({start_seconds}).LocalDateTime; \
         Get-WinEvent -FilterHashtable @{{LogName='Application';StartTime=$start;Id=1000,1001,1026}} -MaxEvents 120 | \
         Where-Object {{$_.Message -match 'dragon|DragonNestEngine|dragon-nest-simulator|WebView2|msedgewebview2|python'}} | \
         Select-Object TimeCreated,Id,ProviderName,LevelDisplayName,Message | Format-List"
    );
    powershell_report(&script)
}

fn local_backend_get(path: &str) -> String {
    let address = SocketAddr::from(([127, 0, 0, 1], 48710));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) else {
        return "backend_unavailable".to_string();
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(700)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(300)));
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:48710\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return "backend_request_failed".to_string();
    }
    let mut bytes = Vec::new();
    let _ = stream.take(2 * 1024 * 1024).read_to_end(&mut bytes);
    sanitize_text(&String::from_utf8_lossy(&bytes))
}

fn zip_add_text(writer: &mut ZipWriter<File>, name: &str, text: &str) -> Result<(), String> {
    let options: FileOptions<'_, ()> = FileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .unix_permissions(0o600);
    writer
        .start_file(name.replace('\\', "/"), options)
        .map_err(|error| error.to_string())?;
    writer
        .write_all(text.as_bytes())
        .map_err(|error| error.to_string())
}

fn zip_add_binary(writer: &mut ZipWriter<File>, name: &str, path: &Path) -> Result<(), String> {
    let metadata = path.metadata().map_err(|error| error.to_string())?;
    if metadata.len() > MAX_DUMP_BYTES {
        return Ok(());
    }
    let options: FileOptions<'_, ()> = FileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .unix_permissions(0o600);
    writer
        .start_file(name.replace('\\', "/"), options)
        .map_err(|error| error.to_string())?;
    let mut source = File::open(path).map_err(|error| error.to_string())?;
    std::io::copy(&mut source, writer).map_err(|error| error.to_string())?;
    Ok(())
}

fn include_install_metadata(
    writer: &mut ZipWriter<File>,
    resource_dir: &Path,
) -> Result<(), String> {
    let candidates = [
        resource_dir.join("runtime").join("active-engine.json"),
        resource_dir.join("runtime").join("previous-engine.json"),
        resource_dir.join("runtime").join("install-manifest.json"),
        resource_dir
            .join("runtime")
            .join("install-verification.json"),
        resource_dir.join("install-manifest.json"),
        resource_dir.join("install-verification.json"),
    ];
    for path in candidates {
        if !path.is_file() {
            continue;
        }
        if let Some(text) = read_text_tail(&path) {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("install.json");
            zip_add_text(writer, &format!("installation/{name}"), &text)?;
        }
    }
    Ok(())
}

fn create_package_for(
    session: SessionRecord,
    trigger: &str,
    resource_dir: &Path,
) -> Result<PathBuf, String> {
    fs::create_dir_all(packages_dir()).map_err(|error| error.to_string())?;
    let timestamp = epoch_ms();
    let safe_version = session.version.replace(
        |value: char| !value.is_ascii_alphanumeric() && value != '.',
        "-",
    );
    let destination = packages_dir().join(format!(
        "dragon-nest-crash-{safe_version}-{timestamp}-{}.zip",
        session.pid
    ));
    let temporary = destination.with_extension("tmp");
    let file = File::create(&temporary).map_err(|error| error.to_string())?;
    let mut writer = ZipWriter::new(file);
    let report = json!({
        "schema": PACKAGE_SCHEMA,
        "created_epoch_ms": timestamp.to_string(),
        "trigger": trigger,
        "session": session,
        "privacy": {
            "capture_payload_included": false,
            "build_payload_included": false,
            "tokens_redacted": true,
            "minidump_note": "A diagnostic native dump may contain small stack and indirectly referenced memory fragments; share privately."
        }
    });
    let report_text = serde_json::to_string_pretty(&report).map_err(|error| error.to_string())?;
    zip_add_text(&mut writer, "report.json", &sanitize_text(&report_text))?;
    zip_add_text(
        &mut writer,
        "请先阅读.txt",
        "这是龙之谷ACT（高级战斗追踪器）工具及模拟工具自动生成的闪退诊断包。\r\n包含程序日志、诊断原生转储、系统摘要、相关 Windows 应用程序事件和最后页面状态。\r\n不包含 dpscap 抓包正文、完整配装、上传令牌或 API Key。\r\n异常退出生成的诊断包会在下次启动后自动上传到 CloudBase 私有云存储；上传失败时保留本地文件并自动重试。\r\n诊断转储可能包含少量线程栈及间接引用内存，仅用于开发者定位闪退。\r\n",
    )?;
    zip_add_text(&mut writer, "system/system.txt", &system_report())?;
    zip_add_text(
        &mut writer,
        "system/windows-application-events.txt",
        &windows_event_report(&session),
    )?;
    if session.pid == std::process::id() {
        zip_add_text(
            &mut writer,
            "runtime/backend-health.http.txt",
            &local_backend_get("/api/health"),
        )?;
        zip_add_text(
            &mut writer,
            "runtime/capture-status.http.txt",
            &local_backend_get("/api/capture/status?includeSnapshot=0"),
        )?;
    } else {
        zip_add_text(
            &mut writer,
            "runtime/backend-health.http.txt",
            "未查询：这是上一次异常退出会话，避免把本次新进程的状态误写入旧诊断包。\r\n",
        )?;
    }

    for path in related_diagnostic_files(&session) {
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("diagnostic.bin");
        if path.extension().and_then(|value| value.to_str()) == Some("dmp") {
            zip_add_binary(&mut writer, &format!("dumps/{name}"), &path)?;
        } else if let Some(text) = read_text_tail(&path) {
            let directory = if path.extension().and_then(|value| value.to_str()) == Some("json") {
                "runtime"
            } else {
                "logs"
            };
            zip_add_text(&mut writer, &format!("{directory}/{name}"), &text)?;
        }
    }
    let frontend_state = PathBuf::from(&session.frontend_state);
    if let Some(text) = read_text_tail(&frontend_state) {
        zip_add_text(&mut writer, "runtime/frontend-state.json", &text)?;
    }
    include_install_metadata(&mut writer, resource_dir)?;
    writer.finish().map_err(|error| error.to_string())?;
    fs::rename(&temporary, &destination).map_err(|error| error.to_string())?;
    if let Some(lock) = PENDING_PACKAGE.get() {
        if let Ok(mut pending) = lock.lock() {
            *pending = Some(destination.clone());
        }
    }
    prune_packages();
    Ok(destination)
}

fn prune_packages() {
    let mut entries = fs::read_dir(packages_dir())
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension().and_then(|value| value.to_str()) == Some("zip"))
        .collect::<Vec<_>>();
    entries.sort_by_key(|entry| file_modified_ms(&entry.path()));
    let remove_count = entries.len().saturating_sub(12);
    for entry in entries.into_iter().take(remove_count) {
        let _ = fs::remove_file(entry.path());
    }
}

pub fn auto_package_previous(resource_dir: &Path) -> Result<Option<PathBuf>, String> {
    let Some(lock) = PREVIOUS_UNCLEAN_SESSION.get() else {
        return Ok(None);
    };
    let previous = lock
        .lock()
        .map_err(|_| "上次会话锁已损坏".to_string())?
        .take();
    match previous {
        Some(session) => {
            create_package_for(session, "previous-unclean-exit", resource_dir).map(Some)
        }
        None => Ok(None),
    }
}

pub fn create_current_package(trigger: &str, resource_dir: &Path) -> Result<PathBuf, String> {
    let lock = CURRENT_SESSION
        .get()
        .ok_or_else(|| "诊断会话尚未初始化".to_string())?;
    let session = lock
        .lock()
        .map_err(|_| "诊断会话锁已损坏".to_string())?
        .clone();
    create_package_for(session, trigger, resource_dir)
}

pub fn status() -> CrashDiagnosticStatus {
    let previous_unclean_exit_detected = PREVIOUS_UNCLEAN_DETECTED.get().copied().unwrap_or(false);
    let latest_package = PENDING_PACKAGE
        .get()
        .and_then(|lock| lock.lock().ok())
        .and_then(|value| value.clone())
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_default();
    CrashDiagnosticStatus {
        previous_unclean_exit_detected,
        latest_package,
        diagnostics_directory: diagnostics_dir().to_string_lossy().into_owned(),
    }
}

#[cfg(windows)]
pub unsafe fn write_minidump(
    exception_info: *const windows_sys::Win32::System::Diagnostics::Debug::EXCEPTION_POINTERS,
    component: &str,
) -> Option<PathBuf> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::Diagnostics::Debug::{
        MiniDumpNormal, MiniDumpWithDataSegs, MiniDumpWithFullMemoryInfo,
        MiniDumpWithIndirectlyReferencedMemory, MiniDumpWithThreadInfo, MiniDumpWriteDump,
        MINIDUMP_EXCEPTION_INFORMATION,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcess, GetCurrentProcessId, GetCurrentThreadId,
    };

    let path = diagnostics_dir().join(format!(
        "{}_{}_{}.dmp",
        component.replace(
            |value: char| !value.is_ascii_alphanumeric() && value != '-',
            "-"
        ),
        epoch_ms(),
        std::process::id(),
    ));
    let file = File::create(&path).ok()?;
    let mut exception = MINIDUMP_EXCEPTION_INFORMATION {
        ThreadId: unsafe { GetCurrentThreadId() },
        ExceptionPointers: exception_info.cast_mut(),
        ClientPointers: 0,
    };
    let exception_ptr = if exception_info.is_null() {
        std::ptr::null()
    } else {
        &mut exception as *mut MINIDUMP_EXCEPTION_INFORMATION as *const _
    };
    // Keep the package substantially smaller than a full-memory dump while
    // preserving heap objects referenced by the crashing thread. This is
    // enough to inspect corrupted Arc/closure state in native Tauri crashes.
    let dump_type = MiniDumpNormal
        | MiniDumpWithThreadInfo
        | MiniDumpWithDataSegs
        | MiniDumpWithFullMemoryInfo
        | MiniDumpWithIndirectlyReferencedMemory;
    let ok = unsafe {
        MiniDumpWriteDump(
            GetCurrentProcess(),
            GetCurrentProcessId(),
            file.as_raw_handle() as HANDLE,
            dump_type,
            exception_ptr,
            std::ptr::null(),
            std::ptr::null(),
        )
    } != 0;
    ok.then_some(path)
}

#[cfg(not(windows))]
pub unsafe fn write_minidump(
    _exception_info: *const std::ffi::c_void,
    _component: &str,
) -> Option<PathBuf> {
    None
}

#[cfg(test)]
mod tests {
    use super::{
        create_package_for, epoch_ms, sanitize_text, write_json_atomic, SessionRecord,
        SESSION_SCHEMA,
    };

    #[test]
    fn redacts_credentials_and_keeps_safe_context() {
        let text = "route=/live\nTEAM_SCORE_UPLOAD_TOKEN=do-not-share\nCLOUDBASE_APIKEY:secret\nstate=running\n";
        let sanitized = sanitize_text(text);
        assert!(sanitized.contains("route=/live"));
        assert!(sanitized.contains("state=running"));
        assert!(!sanitized.contains("do-not-share"));
        assert!(!sanitized.contains("secret"));
        assert!(sanitized.contains("[REDACTED]"));
    }

    #[test]
    fn atomic_json_state_can_be_overwritten_on_windows() {
        let directory = std::env::temp_dir().join(format!(
            "dragon-nest-crash-state-test-{}-{}",
            std::process::id(),
            epoch_ms(),
        ));
        let path = directory.join("state.json");
        write_json_atomic(&path, &serde_json::json!({"value": 1})).unwrap();
        write_json_atomic(&path, &serde_json::json!({"value": 2})).unwrap();
        let payload: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(payload["value"], 2);
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_dir(directory);
    }

    #[test]
    #[ignore = "collects a real Windows system report and validates the complete ZIP"]
    fn complete_package_contains_required_sections() {
        let original_profile = std::env::var_os("USERPROFILE");
        let root = std::env::temp_dir().join(format!(
            "dragon-nest-crash-package-test-{}-{}",
            std::process::id(),
            epoch_ms(),
        ));
        std::fs::create_dir_all(&root).unwrap();
        std::env::set_var("USERPROFILE", &root);
        let diagnostics = root
            .join("Documents")
            .join("DragonNestDpsCapture")
            .join("crash_diagnostics");
        std::fs::create_dir_all(&diagnostics).unwrap();
        let desktop_log = diagnostics.join("desktop-test.log");
        let frontend_state = diagnostics.join("frontend-test.json");
        std::fs::write(&desktop_log, "state=running\nCLOUDBASE_APIKEY=secret\n").unwrap();
        std::fs::write(&frontend_state, r#"{"route":"live"}"#).unwrap();
        let session = SessionRecord {
            schema: SESSION_SCHEMA.to_string(),
            session_id: "package-test".to_string(),
            version: "test".to_string(),
            state: "running".to_string(),
            pid: std::process::id(),
            started_epoch_ms: epoch_ms(),
            ended_epoch_ms: None,
            close_reason: String::new(),
            executable: String::new(),
            desktop_log: desktop_log.to_string_lossy().into_owned(),
            frontend_state: frontend_state.to_string_lossy().into_owned(),
        };
        let package = create_package_for(session, "test", &root).unwrap();
        let mut archive = zip::ZipArchive::new(std::fs::File::open(&package).unwrap()).unwrap();
        for required in [
            "report.json",
            "请先阅读.txt",
            "system/system.txt",
            "system/windows-application-events.txt",
            "runtime/backend-health.http.txt",
            "runtime/capture-status.http.txt",
            "runtime/frontend-state.json",
            "logs/desktop-test.log",
        ] {
            assert!(archive.by_name(required).is_ok(), "missing {required}");
        }
        let mut log = String::new();
        use std::io::Read as _;
        archive
            .by_name("logs/desktop-test.log")
            .unwrap()
            .read_to_string(&mut log)
            .unwrap();
        assert!(!log.contains("secret"));
        let mut report = String::new();
        archive
            .by_name("report.json")
            .unwrap()
            .read_to_string(&mut report)
            .unwrap();
        assert!(serde_json::from_str::<serde_json::Value>(&report).is_ok());
        assert!(report.contains("%USERPROFILE%"));
        assert!(!report.contains(&root.to_string_lossy().into_owned()));
        drop(archive);
        match original_profile {
            Some(value) => std::env::set_var("USERPROFILE", value),
            None => std::env::remove_var("USERPROFILE"),
        }
        let _ = std::fs::remove_dir_all(root);
    }
}
