use std::{collections::BTreeSet, time::Instant};

use url::Url;

pub const ALLOWED_SCOPES: [&str; 2] = ["mcp:read", "mcp:tools"];

pub fn validate_endpoint(endpoint: &str) -> Result<String, String> {
    let parsed = Url::parse(endpoint).map_err(|_| "endpoint must be a valid URL".to_string())?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("local MCP endpoint must use http or https".to_string());
    }
    if parsed.username() != "" || parsed.password().is_some() || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("endpoint cannot contain credentials, query parameters, or fragments".to_string());
    }
    let host = parsed.host_str().ok_or_else(|| "endpoint host is required".to_string())?;
    if !matches!(host, "localhost" | "127.0.0.1" | "::1" | "[::1]") {
        return Err("endpoint must resolve to localhost, 127.0.0.1, or ::1".to_string());
    }
    Ok(parsed.to_string())
}

pub fn authorization_matches(
    item: &StoredAuthorization,
    authorization_id: &str,
    endpoint: &str,
    scope: &str,
    now: Instant,
) -> bool {
    item.id == authorization_id
        && item.endpoint == endpoint
        && item.expires_at > now
        && item.scopes.contains(scope)
}

pub struct StoredAuthorization {
    pub id: String,
    pub endpoint: String,
    pub scopes: BTreeSet<String>,
    pub expires_at: Instant,
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeSet,
        time::{Duration, Instant},
    };

    use super::{authorization_matches, validate_endpoint, StoredAuthorization};

    #[test]
    fn endpoint_policy_accepts_loopback_without_credentials() {
        assert_eq!(
            validate_endpoint("http://127.0.0.1:8787/mcp").unwrap(),
            "http://127.0.0.1:8787/mcp"
        );
    }

    #[test]
    fn endpoint_policy_rejects_remote_or_credential_bearing_urls() {
        assert!(validate_endpoint("https://example.com/mcp").is_err());
        assert!(validate_endpoint("http://user:pass@localhost/mcp").is_err());
        assert!(validate_endpoint("http://localhost/mcp?token=secret").is_err());
    }

    #[test]
    fn authorization_matches_endpoint_scope_and_expiry() {
        let now = Instant::now();
        let item = StoredAuthorization {
            id: "auth-1".into(),
            endpoint: "http://127.0.0.1:8787/mcp".into(),
            scopes: BTreeSet::from(["mcp:read".into()]),
            expires_at: now + Duration::from_secs(30),
        };
        assert!(authorization_matches(
            &item,
            "auth-1",
            "http://127.0.0.1:8787/mcp",
            "mcp:read",
            now
        ));
        assert!(!authorization_matches(
            &item,
            "auth-1",
            "http://127.0.0.1:8787/mcp",
            "mcp:tools",
            now
        ));
        assert!(!authorization_matches(
            &item,
            "auth-1",
            "http://127.0.0.1:8787/other",
            "mcp:read",
            now
        ));
    }

    #[test]
    fn expired_authorization_does_not_match() {
        let now = Instant::now();
        let item = StoredAuthorization {
            id: "auth-2".into(),
            endpoint: "http://localhost:9000/mcp".into(),
            scopes: BTreeSet::from(["mcp:read".into()]),
            expires_at: now - Duration::from_secs(1),
        };
        assert!(!authorization_matches(
            &item,
            "auth-2",
            "http://localhost:9000/mcp",
            "mcp:read",
            now
        ));
    }

    #[test]
    fn wrong_id_does_not_match() {
        let now = Instant::now();
        let item = StoredAuthorization {
            id: "auth-3".into(),
            endpoint: "http://localhost:9000/mcp".into(),
            scopes: BTreeSet::from(["mcp:read".into()]),
            expires_at: now + Duration::from_secs(60),
        };
        assert!(!authorization_matches(
            &item,
            "wrong-id",
            "http://localhost:9000/mcp",
            "mcp:read",
            now
        ));
    }

    #[test]
    fn https_localhost_accepted() {
        assert!(validate_endpoint("https://localhost:8443/mcp").is_ok());
    }

    #[test]
    fn ipv6_loopback_accepted() {
        assert!(validate_endpoint("http://[::1]:8787/mcp").is_ok());
    }

    #[test]
    fn fragment_rejected() {
        assert!(validate_endpoint("http://localhost:8787/mcp#section").is_err());
    }

    #[test]
    fn non_http_scheme_rejected() {
        assert!(validate_endpoint("ftp://localhost/mcp").is_err());
        assert!(validate_endpoint("file:///mcp").is_err());
    }
}
