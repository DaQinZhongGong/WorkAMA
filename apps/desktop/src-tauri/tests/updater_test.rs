//! updater 模块集成测试。
//!
//! 验证 `UpdateStatus` 结构的序列化/反序列化、字段行为和边界情况。
//!
//! 注意：`check_for_updates` / `download_and_install` / `setup` 依赖 Tauri 运行时
//! （`AppHandle` + updater 插件上下文），无法在纯单元测试中覆盖。
//! `UpdateStatus` 作为 `#[derive(Serialize, Deserialize)]` 的纯数据结构可完整测试。

use workama_desktop_lib::updater::UpdateStatus;

#[test]
fn update_status_serializes_to_camel_case() {
    let status = UpdateStatus {
        update_available: true,
        current_version: "1.0.0".to_string(),
        latest_version: Some("1.1.0".to_string()),
        release_notes: Some("Bug fixes".to_string()),
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    assert!(json.contains("\"updateAvailable\""), "应包含 camelCase 字段 updateAvailable");
    assert!(json.contains("\"currentVersion\""));
    assert!(json.contains("\"latestVersion\""));
    assert!(json.contains("\"releaseNotes\""));
    assert!(!json.contains("update_available"));
    assert!(!json.contains("current_version"));
    assert!(!json.contains("latest_version"));
    assert!(!json.contains("release_notes"));
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

#[test]
fn update_status_deserializes_from_raw_camel_case_json() {
    let json = r#"{
        "updateAvailable": true,
        "currentVersion": "2.0.0",
        "latestVersion": "2.1.0",
        "releaseNotes": "Major update with breaking changes"
    }"#;
    let status: UpdateStatus = serde_json::from_str(json).expect("反序列化失败");
    assert!(status.update_available);
    assert_eq!(status.current_version, "2.0.0");
    assert_eq!(status.latest_version, Some("2.1.0".to_string()));
    assert_eq!(status.release_notes, Some("Major update with breaking changes".to_string()));
}

#[test]
fn update_status_none_fields_serialize_as_null() {
    let status = UpdateStatus {
        update_available: false,
        current_version: "0.1.0".to_string(),
        latest_version: None,
        release_notes: None,
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    assert!(json.contains("\"latestVersion\":null"), "None 字段应序列化为 null");
    assert!(json.contains("\"releaseNotes\":null"));
}

#[test]
fn update_status_with_empty_release_notes_round_trips() {
    let status = UpdateStatus {
        update_available: true,
        current_version: "1.0.0".to_string(),
        latest_version: Some("1.0.1".to_string()),
        release_notes: Some("".to_string()),
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    let decoded: UpdateStatus = serde_json::from_str(&json).expect("反序列化失败");
    assert_eq!(decoded.release_notes, Some("".to_string()), "空字符串应保留");
}

#[test]
fn update_status_clone_produces_equal_value() {
    let status = UpdateStatus {
        update_available: true,
        current_version: "1.0.0".to_string(),
        latest_version: Some("1.1.0".to_string()),
        release_notes: Some("notes".to_string()),
    };
    let cloned = status.clone();
    assert_eq!(cloned.update_available, status.update_available);
    assert_eq!(cloned.current_version, status.current_version);
    assert_eq!(cloned.latest_version, status.latest_version);
    assert_eq!(cloned.release_notes, status.release_notes);
}

#[test]
fn update_status_debug_format_is_informative() {
    let status = UpdateStatus {
        update_available: false,
        current_version: "0.1.0".to_string(),
        latest_version: None,
        release_notes: None,
    };
    let debug = format!("{:?}", status);
    assert!(!debug.is_empty(), "Debug 输出不应为空");
    assert!(debug.contains("UpdateStatus"), "Debug 输出应包含类型名");
    assert!(debug.contains("0.1.0"), "Debug 输出应包含版本号");
}

#[test]
fn update_status_with_unicode_release_notes_round_trips() {
    let notes = "修复了中文输入法下的崩溃问题";
    let status = UpdateStatus {
        update_available: true,
        current_version: "1.0.0".to_string(),
        latest_version: Some("1.0.1".to_string()),
        release_notes: Some(notes.to_string()),
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    let decoded: UpdateStatus = serde_json::from_str(&json).expect("反序列化失败");
    assert_eq!(decoded.release_notes, Some(notes.to_string()), "Unicode 内容应正确往返");
}
