//! policy 模块集成测试。
//!
//! 验证本地 MCP 授权策略的公共 API：
//! - `validate_endpoint`：endpoint 校验（协议、host、端口、路径、credential、query、fragment）
//! - `authorization_matches`：授权匹配逻辑（id、endpoint、scope、过期时间）
//! - `ALLOWED_SCOPES`：scope 白名单内容

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use workama_desktop_lib::policy::{
    authorization_matches, validate_endpoint, StoredAuthorization, ALLOWED_SCOPES,
};

// ============================== ALLOWED_SCOPES ==============================

#[test]
fn allowed_scopes_contains_exactly_read_and_tools() {
    assert_eq!(ALLOWED_SCOPES, ["mcp:read", "mcp:tools"]);
}

#[test]
fn allowed_scopes_has_two_entries() {
    assert_eq!(ALLOWED_SCOPES.len(), 2, "ALLOWED_SCOPES 应恰好包含 2 个 scope");
}

// ====================== validate_endpoint：接受用例 ==========================

#[test]
fn validate_endpoint_accepts_localhost_without_port() {
    let result = validate_endpoint("http://localhost/mcp");
    assert!(result.is_ok(), "应接受无端口的 localhost");
    assert_eq!(result.unwrap(), "http://localhost/mcp");
}

#[test]
fn validate_endpoint_accepts_127_0_0_1_with_port_and_path() {
    let url = "http://127.0.0.1:8787/mcp";
    let result = validate_endpoint(url);
    assert!(result.is_ok());
    assert_eq!(result.unwrap(), url);
}

#[test]
fn validate_endpoint_accepts_https_localhost() {
    assert!(validate_endpoint("https://localhost:8443/mcp").is_ok());
}

#[test]
fn validate_endpoint_accepts_ipv6_loopback_with_port() {
    assert!(validate_endpoint("http://[::1]:8787/mcp").is_ok());
}

#[test]
fn validate_endpoint_preserves_port_and_nested_path() {
    let url = "http://127.0.0.1:9000/api/v1/mcp";
    assert_eq!(validate_endpoint(url).unwrap(), url);
}

// ====================== validate_endpoint：拒绝用例 ==========================

#[test]
fn validate_endpoint_rejects_empty_string() {
    assert!(validate_endpoint("").is_err());
}

#[test]
fn validate_endpoint_rejects_remote_hostname() {
    assert!(validate_endpoint("http://example.com/mcp").is_err());
    assert!(validate_endpoint("https://api.workama.com/mcp").is_err());
}

#[test]
fn validate_endpoint_rejects_non_loopback_ipv4() {
    assert!(validate_endpoint("http://192.168.1.1/mcp").is_err());
    assert!(validate_endpoint("http://10.0.0.1/mcp").is_err());
    assert!(validate_endpoint("http://172.16.0.1/mcp").is_err());
}

#[test]
fn validate_endpoint_rejects_non_http_s_schemes() {
    assert!(validate_endpoint("ftp://localhost/mcp").is_err());
    assert!(validate_endpoint("file:///mcp").is_err());
    assert!(validate_endpoint("ws://localhost/mcp").is_err());
    assert!(validate_endpoint("data:text/plain,hello").is_err());
}

#[test]
fn validate_endpoint_rejects_credentials_in_url() {
    assert!(validate_endpoint("http://user:pass@localhost/mcp").is_err());
    assert!(validate_endpoint("http://user@localhost/mcp").is_err());
    assert!(validate_endpoint("http://:pass@localhost/mcp").is_err());
}

#[test]
fn validate_endpoint_rejects_query_parameters() {
    assert!(validate_endpoint("http://localhost/mcp?token=secret").is_err());
    assert!(validate_endpoint("http://127.0.0.1:8787/mcp?key=value&foo=bar").is_err());
}

#[test]
fn validate_endpoint_rejects_fragment() {
    assert!(validate_endpoint("http://localhost/mcp#section").is_err());
    assert!(validate_endpoint("http://127.0.0.1:8787/mcp#top").is_err());
}

#[test]
fn validate_endpoint_rejects_non_loopback_ipv6() {
    assert!(validate_endpoint("http://[fe80::1]/mcp").is_err());
    assert!(validate_endpoint("http://[2001:db8::1]/mcp").is_err());
}

// ========================= authorization_matches ===========================

#[test]
fn authorization_matches_with_correct_id_endpoint_scope() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-1".into(),
        endpoint: "http://127.0.0.1:8787/mcp".into(),
        scopes: BTreeSet::from(["mcp:read".into(), "mcp:tools".into()]),
        expires_at: now + Duration::from_secs(60),
    };
    assert!(authorization_matches(
        &item,
        "auth-1",
        "http://127.0.0.1:8787/mcp",
        "mcp:read",
        now
    ));
    assert!(authorization_matches(
        &item,
        "auth-1",
        "http://127.0.0.1:8787/mcp",
        "mcp:tools",
        now
    ));
}

#[test]
fn authorization_does_not_match_wrong_scope() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-2".into(),
        endpoint: "http://localhost/mcp".into(),
        scopes: BTreeSet::from(["mcp:read".into()]),
        expires_at: now + Duration::from_secs(60),
    };
    assert!(!authorization_matches(
        &item,
        "auth-2",
        "http://localhost/mcp",
        "mcp:tools",
        now
    ));
}

#[test]
fn authorization_does_not_match_wrong_endpoint() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-3".into(),
        endpoint: "http://localhost:9000/mcp".into(),
        scopes: BTreeSet::from(["mcp:read".into()]),
        expires_at: now + Duration::from_secs(60),
    };
    assert!(!authorization_matches(
        &item,
        "auth-3",
        "http://localhost:8000/mcp",
        "mcp:read",
        now
    ));
}

#[test]
fn authorization_does_not_match_expired() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-4".into(),
        endpoint: "http://localhost/mcp".into(),
        scopes: BTreeSet::from(["mcp:read".into()]),
        expires_at: now - Duration::from_secs(1),
    };
    assert!(!authorization_matches(
        &item,
        "auth-4",
        "http://localhost/mcp",
        "mcp:read",
        now
    ));
}

#[test]
fn authorization_does_not_match_wrong_id() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-5".into(),
        endpoint: "http://localhost/mcp".into(),
        scopes: BTreeSet::from(["mcp:read".into()]),
        expires_at: now + Duration::from_secs(60),
    };
    assert!(!authorization_matches(
        &item,
        "wrong-id",
        "http://localhost/mcp",
        "mcp:read",
        now
    ));
}

#[test]
fn authorization_with_empty_scopes_never_matches() {
    let now = Instant::now();
    let item = StoredAuthorization {
        id: "auth-6".into(),
        endpoint: "http://localhost/mcp".into(),
        scopes: BTreeSet::new(),
        expires_at: now + Duration::from_secs(60),
    };
    assert!(!authorization_matches(
        &item,
        "auth-6",
        "http://localhost/mcp",
        "mcp:read",
        now
    ));
    assert!(!authorization_matches(
        &item,
        "auth-6",
        "http://localhost/mcp",
        "mcp:tools",
        now
    ));
}
