//! 全局快捷键与自动更新集成测试。
//!
//! 验证快捷键配置和更新状态的公共 API 可从 crate 外部访问，
//! 且序列化/反序列化行为符合预期（camelCase、默认值正确）。

use workama_desktop_lib::shortcut::{ShortcutConfig, DEFAULT_SHORTCUT};
use workama_desktop_lib::updater::UpdateStatus;

#[test]
fn shortcut_default_config_is_enabled() {
    let config = ShortcutConfig::default();
    assert!(config.enabled, "默认应启用全局快捷键");
    assert_eq!(config.accelerator, DEFAULT_SHORTCUT);
    assert_eq!(config.accelerator, "Ctrl+Shift+W");
}

#[test]
fn shortcut_config_uses_camel_case_in_json() {
    let config = ShortcutConfig::default();
    let json = serde_json::to_string(&config).expect("序列化失败");
    assert!(json.contains("enabled"), "JSON 应包含 enabled 字段");
    assert!(json.contains("accelerator"), "JSON 应包含 accelerator 字段");
}

#[test]
fn shortcut_config_round_trips_through_json() {
    let config = ShortcutConfig::default();
    let json = serde_json::to_string(&config).expect("序列化失败");
    let decoded: ShortcutConfig = serde_json::from_str(&json).expect("反序列化失败");
    assert_eq!(decoded.enabled, config.enabled);
    assert_eq!(decoded.accelerator, config.accelerator);
}

#[test]
fn shortcut_config_can_be_disabled_via_json() {
    let json = r#"{"enabled":false,"accelerator":"Alt+Space"}"#;
    let config: ShortcutConfig = serde_json::from_str(json).expect("反序列化失败");
    assert!(!config.enabled, "应可通过 JSON 禁用快捷键");
    assert_eq!(config.accelerator, "Alt+Space");
}
#[test]
fn update_status_serializes_to_camel_case() {
    let status = UpdateStatus {
        update_available: true,
        current_version: "0.1.0".to_string(),
        latest_version: Some("0.2.0".to_string()),
        release_notes: Some("修复若干问题".to_string()),
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    assert!(json.contains("updateAvailable"), "应包含 camelCase 字段 updateAvailable");
    assert!(json.contains("currentVersion"));
    assert!(json.contains("latestVersion"));
    assert!(json.contains("releaseNotes"));
}

#[test]
fn update_status_no_update_round_trips() {
    let status = UpdateStatus {
        update_available: false,
        current_version: "0.1.0".to_string(),
        latest_version: None,
        release_notes: None,
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    let decoded: UpdateStatus = serde_json::from_str(&json).expect("反序列化失败");
    assert!(!decoded.update_available);
    assert_eq!(decoded.current_version, "0.1.0");
    assert!(decoded.latest_version.is_none());
    assert!(decoded.release_notes.is_none());
}

#[test]
fn update_status_with_update_round_trips() {
    let status = UpdateStatus {
        update_available: true,
        current_version: "0.1.0".to_string(),
        latest_version: Some("0.2.0".to_string()),
        release_notes: Some("新功能上线".to_string()),
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    let decoded: UpdateStatus = serde_json::from_str(&json).expect("反序列化失败");
    assert!(decoded.update_available);
    assert_eq!(decoded.latest_version, Some("0.2.0".to_string()));
    assert_eq!(decoded.release_notes, Some("新功能上线".to_string()));
}