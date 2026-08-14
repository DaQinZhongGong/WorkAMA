//! 配置解析集成测试。
//!
//! 验证 `tauri.conf.json` 和 `Cargo.toml` 的结构正确性：
//! - updater 插件配置存在且字段完整
//! - bundle.createUpdaterArtifacts 已启用
//! - Cargo.toml 包含必要的插件依赖

/// 读取并解析 tauri.conf.json
fn load_tauri_config() -> serde_json::Value {
    let content = std::fs::read_to_string("tauri.conf.json")
        .expect("应能读取 tauri.conf.json");
    serde_json::from_str(&content).expect("tauri.conf.json 应为有效 JSON")
}

#[test]
fn tauri_config_has_updater_plugin_section() {
    let config = load_tauri_config();
    let plugins = config.get("plugins").expect("应包含 plugins 节");
    let updater = plugins.get("updater").expect("plugins.updater 应存在");
    let endpoints = updater.get("endpoints")
        .and_then(|v| v.as_array())
        .expect("updater.endpoints 应为数组");
    assert!(!endpoints.is_empty(), "endpoints 不应为空");
    let first = endpoints[0].as_str().expect("endpoint 应为字符串");
    assert!(first.contains("{{target}}"), "endpoint 应包含 {{target}}");
    assert!(first.contains("{{arch}}"), "endpoint 应包含 {{arch}}");
    assert!(first.contains("{{current_version}}"), "endpoint 应包含 {{current_version}}");
}

#[test]
fn tauri_config_bundle_creates_updater_artifacts() {
    let config = load_tauri_config();
    let bundle = config.get("bundle").expect("应包含 bundle 节");
    let create = bundle.get("createUpdaterArtifacts")
        .and_then(|v| v.as_bool())
        .expect("bundle.createUpdaterArtifacts 应为布尔值");
    assert!(create, "createUpdaterArtifacts 应为 true");
}

#[test]
fn tauri_config_has_main_window() {
    let config = load_tauri_config();
    let windows = config.get("app")
        .and_then(|a| a.get("windows"))
        .and_then(|w| w.as_array())
        .expect("app.windows 应为数组");
    assert!(!windows.is_empty(), "应至少有一个窗口");
    assert_eq!(
        windows[0].get("label").and_then(|v| v.as_str()),
        Some("main"),
        "第一个窗口 label 应为 main"
    );
}
#[test]
fn cargo_toml_contains_required_plugin_dependencies() {
    let content = std::fs::read_to_string("Cargo.toml").expect("应能读取 Cargo.toml");
    assert!(content.contains("tauri-plugin-global-shortcut"),
        "Cargo.toml 应包含 tauri-plugin-global-shortcut 依赖");
    assert!(content.contains("tauri-plugin-notification"),
        "Cargo.toml 应包含 tauri-plugin-notification 依赖");
    assert!(content.contains("tauri-plugin-updater"),
        "Cargo.toml 应包含 tauri-plugin-updater 依赖");
    assert!(content.contains("tray-icon"),
        "Cargo.toml 应为 tauri 启用 tray-icon 特性");
}

#[test]
fn cargo_toml_preserves_local_mcp_bridge_feature() {
    let content = std::fs::read_to_string("Cargo.toml").expect("应能读取 Cargo.toml");
    assert!(content.contains("local-mcp-bridge"),
        "Cargo.toml 应保留 local-mcp-bridge feature 以维持向后兼容");
    assert!(content.contains("default = [\"custom-protocol\"]"),
        "default feature 应仍为 custom-protocol");
}

#[test]
fn capabilities_default_json_includes_plugin_permissions() {
    let content = std::fs::read_to_string("capabilities/default.json")
        .expect("应能读取 capabilities/default.json");
    let json: serde_json::Value =
        serde_json::from_str(&content).expect("capabilities/default.json 应为有效 JSON");
    let permissions = json.get("permissions")
        .and_then(|v| v.as_array())
        .expect("permissions 应为数组");
    let perm_set: std::collections::HashSet<&str> = permissions
        .iter().filter_map(|v| v.as_str()).collect();
    assert!(perm_set.contains("notification:default"), "应包含 notification:default");
    assert!(perm_set.contains("global-shortcut:default"), "应包含 global-shortcut:default");
    assert!(perm_set.contains("updater:default"), "应包含 updater:default");
    assert!(perm_set.contains("core:default"), "应保留 core:default");
}