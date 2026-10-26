fn main() {
    // Development runs the Python sources and must not require a frozen ZIP
    // to have been generated first. Release builds still validate/copy every
    // declared installer resource and fail if the package is missing.
    if std::env::var("PROFILE").as_deref() == Ok("debug") {
        let mut config: serde_json::Value = serde_json::from_str(
            &std::env::var("TAURI_CONFIG").unwrap_or_else(|_| "{}".into()),
        ).expect("invalid TAURI_CONFIG");
        if !config["bundle"].is_object() { config["bundle"] = serde_json::json!({}); }
        config["bundle"]["resources"] = serde_json::json!([]);
        let config = config.to_string();
        std::env::set_var("TAURI_CONFIG", &config);
        println!("cargo:rustc-env=TAURI_CONFIG={config}");
        // tauri dev launches the child with ordinary CreateProcess. Embedding
        // requireAdministrator in that child would fail with Windows error
        // 740, so development builds deliberately keep Tauri's default
        // asInvoker manifest.
        tauri_build::build();
        return;
    }
    let windows = tauri_build::WindowsAttributes::new()
        .app_manifest(include_str!("app-release-manifest.xml"));
    let attributes = tauri_build::Attributes::new().windows_attributes(windows);
    tauri_build::try_build(attributes).expect("failed to run Tauri release build script")
}
