use std::fs;
use std::path::{Path, PathBuf};

fn read_selection(path: &Path) -> Result<serde_json::Value, String> {
    let size = fs::metadata(path).map_err(|e| format!("{}: {e}", path.display()))?.len();
    if size > 16 * 1024 {
        return Err("Engine selection metadata is oversized".into());
    }
    let text = fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|e| e.to_string())
}

/// An installed archive is never itself a runnable engine. Only the verified,
/// atomically published selection is eligible; invalid updates cannot silently
/// fall back to an unrelated legacy runtime or a system Python installation.
pub fn resolve_engine(root: &Path, expected_version: &str) -> Result<PathBuf, String> {
    let runtime = root.join("runtime");
    let pointer = runtime.join("active-engine.json");
    if pointer.exists() {
        let selection = read_selection(&pointer)?;
        if selection["schemaVersion"].as_u64() != Some(1)
            || selection["verified"].as_bool() != Some(true)
            || selection["appVersion"].as_str() != Some(expected_version)
        {
            return Err("Engine selection is unverified or does not match the desktop version".into());
        }
        let directory = selection["directory"].as_str().unwrap_or("");
        let id = directory.strip_prefix("engines/").unwrap_or("");
        if id.len() != 12 || !id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err("Invalid engine release directory".into());
        }
        let release = runtime.join("engines").join(id);
        let marker = read_selection(&release.join("engine-release.json"))?;
        if marker != selection {
            return Err("Engine release marker does not match the active selection".into());
        }
        let canonical_root = root.canonicalize().map_err(|e| e.to_string())?;
        let executable = release.join("DragonNestEngine.exe").canonicalize().map_err(|e| e.to_string())?;
        // Comparing to the installation root (not a separately canonicalized
        // engines link) also rejects a release redirected outside installation.
        if !executable.starts_with(&canonical_root.join("runtime").join("engines")) || !executable.is_file() {
            return Err("Engine executable escapes its installation or is missing".into());
        }
        return Ok(executable);
    }
    if runtime.join("engine-package.zip").exists() {
        return Err("Engine package has not been verified and activated; complete the installation".into());
    }
    let legacy = runtime.join("DragonNestEngine/DragonNestEngine.exe");
    if legacy.is_file() { return Ok(legacy); }
    Err("No installed engine was found".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let id = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
            let root = std::env::temp_dir().join(format!("dn-engine-test-{}-{id}", std::process::id()));
            fs::create_dir_all(root.join("runtime/DragonNestEngine")).unwrap();
            fs::write(root.join("runtime/DragonNestEngine/DragonNestEngine.exe"), "old fixture").unwrap();
            Self(root)
        }
        fn publish(&self) -> serde_json::Value {
            let directory = self.0.join("runtime/engines/abcdef012345");
            fs::create_dir_all(&directory).unwrap();
            fs::write(directory.join("DragonNestEngine.exe"), "new fixture").unwrap();
            let value = json!({"schemaVersion": 1, "verified": true, "appVersion": "4.523.0", "directory": "engines/abcdef012345"});
            fs::write(directory.join("engine-release.json"), value.to_string()).unwrap();
            fs::write(self.0.join("runtime/active-engine.json"), value.to_string()).unwrap();
            value
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let target = self.0.canonicalize().unwrap();
            let temp = std::env::temp_dir().canonicalize().unwrap();
            assert_eq!(target.parent(), Some(temp.as_path()));
            assert!(target.file_name().unwrap().to_string_lossy().starts_with("dn-engine-test-"));
            let _ = fs::remove_dir_all(target);
        }
    }
    #[test]
    fn verified_release_takes_precedence_over_legacy() {
        let f = Fixture::new(); f.publish();
        assert!(resolve_engine(&f.0, "4.523.0").unwrap().to_string_lossy().contains("abcdef012345"));
    }
    #[test]
    fn legacy_layout_remains_supported_without_a_new_package() {
        let f = Fixture::new();
        assert!(resolve_engine(&f.0, "4.523.0").unwrap().to_string_lossy().contains("DragonNestEngine"));
    }
    #[test]
    fn incomplete_install_must_not_start_legacy_engine() {
        let f = Fixture::new();
        fs::write(f.0.join("runtime/engine-package.zip"), "fixture").unwrap();
        assert!(resolve_engine(&f.0, "4.523.0").is_err());
    }
    #[test]
    fn mismatched_desktop_version_is_rejected() {
        let f = Fixture::new(); f.publish();
        assert!(resolve_engine(&f.0, "4.524.0").is_err());
    }
    #[test]
    fn path_traversal_and_incomplete_markers_are_rejected() {
        let f = Fixture::new(); let mut selection = f.publish();
        for directory in ["../DragonNestEngine", "engines/../abcdef012345", "engines/abcdef012345/extra", "C:/outside"] {
            selection["directory"] = json!(directory);
            fs::write(f.0.join("runtime/active-engine.json"), selection.to_string()).unwrap();
            assert!(resolve_engine(&f.0, "4.523.0").is_err());
        }
        f.publish();
        fs::remove_file(f.0.join("runtime/engines/abcdef012345/engine-release.json")).unwrap();
        assert!(resolve_engine(&f.0, "4.523.0").is_err());
    }
}
