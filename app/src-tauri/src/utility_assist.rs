use std::path::PathBuf;
use std::process::{Command, Stdio};

use tauri::Manager;


struct BundledUtility {
    relative_path: &'static str,
    display_name: &'static str,
}

fn bundled_utility(tool: &str) -> Option<BundledUtility> {
    match tool {
        "skill-hit-tool" => Some(BundledUtility {
            relative_path: "third_party/skill_hit_tool/技能时间轴与实战Hit对照器.exe",
            display_name: "技能时间轴与实战Hit对照器",
        }),
        _ => None,
    }
}

fn bundled_utility_candidates(app: &tauri::AppHandle, relative_path: &str) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join(relative_path));
    }
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(executable_dir) = current_exe.parent() {
            candidates.push(executable_dir.join(relative_path));
        }
    }
    // Source-tree fallback keeps `tauri dev` and the development launcher
    // useful without coupling installed builds to the developer machine.
    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(relative_path),
    );
    candidates
}

#[tauri::command]
pub(crate) fn launch_bundled_utility(
    app: tauri::AppHandle,
    tool: String,
) -> Result<String, String> {
    let utility = bundled_utility(tool.trim()).ok_or_else(|| "未知的实用工具".to_string())?;
    let candidates = bundled_utility_candidates(&app, utility.relative_path);
    let executable = candidates
        .iter()
        .find(|path| path.is_file())
        .cloned()
        .ok_or_else(|| {
            format!(
                "未找到{}，请重新安装或修复 V{}：{}",
                utility.display_name,
                env!("CARGO_PKG_VERSION").trim_end_matches(".0"),
                candidates
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join("；")
            )
        })?;
    let working_directory = executable
        .parent()
        .ok_or_else(|| format!("{}所在目录无效", utility.display_name))?;
    Command::new(&executable)
        .current_dir(working_directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("启动{}失败：{error}", utility.display_name))?;
    Ok(format!("已启动{}", utility.display_name))
}
