//! lib.rs 核心库集成测试。
//!
//! 验证 `lib.rs` 中定义的公共数据类型：
//! - `BridgeState`：默认状态、可创建性
//! - `StoredAuthorizationEntry`：字段可访问性
//! - `LocalMcpAuthorizationRequest`：反序列化（含默认 scopes）
//! - `LocalMcpStatus`：序列化（camelCase）
//! - `LocalMcpAuthorization`：序列化（camelCase）
//!
//! 注意：`commands` 模块和 `run()` 标记为 `#[cfg(not(test))]`，在 `cargo test --lib`
//! 时不可用；但集成测试（`tests/`）以非 test cfg 编译 lib crate，因此可访问。
//! 不过 `run()` 会启动 Tauri 应用，`#[tauri::command]` 需要 Tauri 运行时 State，
//! 均无法在纯单元测试中调用。这里只测试纯数据结构。

use std::collections::BTreeSet;
use std::time::Instant;

use workama_desktop_lib::{
    BridgeState, LocalMcpAuthorization, LocalMcpAuthorizationRequest, LocalMcpStatus,
    StoredAuthorizationEntry,
};

#[test]
fn bridge_state_default_can_be_constructed() {
    // BridgeState 实现 Default，可创建空状态
    let _state = BridgeState::default();
}

#[test]
fn stored_authorization_entry_fields_are_public() {
    let now = Instant::now();
    let entry = StoredAuthorizationEntry {
        id: "auth-1".to_string(),
        endpoint: "http://127.0.0.1:8787/mcp".to_string(),
        scopes: BTreeSet::from(["mcp:read".to_string()]),
        expires_at: now,
    };
    assert_eq!(entry.id, "auth-1");
    assert_eq!(entry.endpoint, "http://127.0.0.1:8787/mcp");
    assert!(entry.scopes.contains("mcp:read"));
    assert_eq!(entry.expires_at, now);
}

#[test]
fn local_mcp_authorization_request_deserializes_with_scopes() {
    let json = r#"{"endpoint":"http://localhost:8787/mcp","scopes":["mcp:read","mcp:tools"]}"#;
    let req: LocalMcpAuthorizationRequest = serde_json::from_str(json).expect("反序列化失败");
    assert_eq!(req.endpoint, "http://localhost:8787/mcp");
    assert_eq!(req.scopes.len(), 2);
    assert!(req.scopes.contains(&"mcp:read".to_string()));
    assert!(req.scopes.contains(&"mcp:tools".to_string()));
}

#[test]
fn local_mcp_authorization_request_scopes_default_to_empty() {
    // scopes 字段标记了 #[serde(default)]，省略时应为空 Vec
    let json = r#"{"endpoint":"http://localhost:8787/mcp"}"#;
    let req: LocalMcpAuthorizationRequest = serde_json::from_str(json).expect("反序列化失败");
    assert_eq!(req.endpoint, "http://localhost:8787/mcp");
    assert!(req.scopes.is_empty(), "省略 scopes 时应为空 Vec");
}

#[test]
fn local_mcp_status_serializes_to_camel_case() {
    let status = LocalMcpStatus {
        enabled: true,
        native_transport: false,
        credential_persistence: false,
        active_authorizations: 3,
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    assert!(json.contains("\"enabled\""));
    assert!(json.contains("\"nativeTransport\""), "应包含 camelCase nativeTransport");
    assert!(json.contains("\"credentialPersistence\""));
    assert!(json.contains("\"activeAuthorizations\""));
    // 不应包含 snake_case
    assert!(!json.contains("native_transport"));
    assert!(!json.contains("credential_persistence"));
    assert!(!json.contains("active_authorizations"));
}

#[test]
fn local_mcp_authorization_serializes_to_camel_case() {
    let auth = LocalMcpAuthorization {
        authorization_id: "local-mcp-123".to_string(),
        endpoint: "http://127.0.0.1:8787/mcp".to_string(),
        scopes: vec!["mcp:read".to_string(), "mcp:tools".to_string()],
        expires_in_seconds: 300,
    };
    let json = serde_json::to_string(&auth).expect("序列化失败");
    assert!(json.contains("\"authorizationId\""), "应包含 camelCase authorizationId");
    assert!(json.contains("\"endpoint\""));
    assert!(json.contains("\"scopes\""));
    assert!(json.contains("\"expiresInSeconds\""));
    // 不应包含 snake_case
    assert!(!json.contains("authorization_id"));
    assert!(!json.contains("expires_in_seconds"));
}

#[test]
fn local_mcp_status_round_trips_through_json() {
    let status = LocalMcpStatus {
        enabled: false,
        native_transport: true,
        credential_persistence: false,
        active_authorizations: 0,
    };
    let json = serde_json::to_string(&status).expect("序列化失败");
    // LocalMcpStatus 只 derive Serialize，不可反序列化；验证 JSON 内容即可
    assert!(json.contains("\"enabled\":false"));
    assert!(json.contains("\"nativeTransport\":true"));
    assert!(json.contains("\"activeAuthorizations\":0"));
}

#[test]
fn local_mcp_authorization_request_with_empty_scopes_array() {
    let json = r#"{"endpoint":"http://localhost/mcp","scopes":[]}"#;
    let req: LocalMcpAuthorizationRequest = serde_json::from_str(json).expect("反序列化失败");
    assert_eq!(req.endpoint, "http://localhost/mcp");
    assert!(req.scopes.is_empty());
}
