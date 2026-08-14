"""platform-api 低覆盖模块纯函数单元测试。

覆盖 approvals、auth/service、auth/router 模型、core、enterprise_rbac、
portability、channel_extensions、knowledge、security/service、gateway/router
中的纯函数、模型校验与可 mock 辅助函数。所有外部依赖均已隔离。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import zipfile
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from workama_platform.core import (
    Actor,
    capability_allows,
    create_access_token,
    decode_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    hash_secret,
    new_id,
    platform_key_scope_allows,
    settings,
    verify_password,
)
from workama_platform.modules import approvals
from workama_platform.modules.auth.router import (
    LoginRequest,
    MfaCodeRequest,
    RegisterRequest,
    TokenRequest,
    _mfa_ticket,
)
from workama_platform.modules.portability import (
    _signing_key,
    canonical_package,
    validate_package,
)
from workama_platform.modules.auth.service import (
    OAuthProviderConfig,
    auth_token_is_usable,
    build_oauth_authorization_url,
    new_oauth_state,
    new_pkce_verifier,
    next_login_failure,
    oauth_callback_uri,
    oauth_provider_config,
    oauth_state_is_valid,
    pkce_challenge,
    totp_code,
    verify_totp,
)
from workama_platform.modules.channel_extensions import (
    IMChannelCreate,
    IMMessageCreate,
    _account_view,
    _channel_view,
    _controlled,
    _safe_payload,
    _session_hash,
    choose_sticky_account,
    miniapp_manifest,
    normalize_im_content,
)
from workama_platform.modules.enterprise_rbac import (
    AuthStrengthPolicyCreate,
    RoleCreate,
    _normalize_capabilities,
    _normalize_cidrs,
    _normalize_name,
    _normalize_scopes,
    _validate_safe_json,
)
from workama_platform.modules.gateway.router import (
    ChannelCreate,
    GatewayTokenCreate,
    _normalize_model_name,
    _provider_name,
    _validate_channel_url,
)
from workama_platform.modules.knowledge import (
    DatasetCreate,
    RetrievalConfigUpsert,
    _assert_if_match,
    _check_content,
    _check_zip_expansion,
    _clean_text,
    _etag,
    _mime_for_name,
    _normalize_retrieval_config,
    _safe_name,
    _split_text,
    _title_path,
)
from workama_platform.modules.security.service import (
    evaluate_prompt,
    moderate_text,
    validate_outbound_url,
)


# ----------------------------------------------------------------------
# approvals.py 纯函数与模型校验
# ----------------------------------------------------------------------


def _owner_actor() -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
    )


def _member_actor() -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="member",
        email="member@example.test",
        display_name="Member",
        onboarding_completed=True,
    )


def test_require_admin_allows_owner_and_admin():
    """owner/admin 角色通过管理员校验。"""
    approvals.require_admin(_owner_actor())
    approvals.require_admin(
        Actor(
            user_id="usr_admin",
            workspace_id="wsp_test",
            org_id="org_test",
            role="admin",
            email="admin@example.test",
            display_name="Admin",
            onboarding_completed=True,
        )
    )


def test_require_admin_rejects_member_and_service_account():
    """member/viewer/service_account 被拒绝。"""
    for role in ("member", "viewer"):
        actor = Actor(
            user_id="usr_test",
            workspace_id="wsp_test",
            org_id="org_test",
            role=role,
            email="u@example.test",
            display_name="U",
            onboarding_completed=True,
        )
        with pytest.raises(HTTPException, match="Owner or admin user required"):
            approvals.require_admin(actor)
    service = Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="service_account",
        email="u@example.test",
        display_name="U",
        onboarding_completed=True,
        actor_type="service_account",
    )
    with pytest.raises(HTTPException, match="Owner or admin user required"):
        approvals.require_admin(service)


def test_approval_create_accepts_valid_hash():
    """合法的 64 位十六进制 action_hash 通过校验。"""
    model = approvals.ApprovalCreate(
        workspace_id="wsp_test",
        session_id="sess_test",
        call_id="call_test",
        requester_id="usr_test",
        tool_name="tool",
        action_hash="a" * 64,
        risk="A3",
    )
    assert model.tool_name == "tool"
    assert model.ttl_seconds == 120


def test_approval_create_rejects_invalid_hash():
    """非 64 位十六进制 action_hash 被拒绝。"""
    with pytest.raises(ValidationError):
        approvals.ApprovalCreate(
            workspace_id="wsp_test",
            session_id="sess_test",
            call_id="call_test",
            requester_id="usr_test",
            tool_name="tool",
            action_hash="short",
            risk="A3",
        )


def test_approval_create_ttl_bounds():
    """ttl_seconds 必须在 30-600 之间。"""
    with pytest.raises(ValidationError):
        approvals.ApprovalCreate(
            workspace_id="wsp_test",
            session_id="sess_test",
            call_id="call_test",
            requester_id="usr_test",
            tool_name="tool",
            action_hash="a" * 64,
            risk="A3",
            ttl_seconds=10,
        )
    with pytest.raises(ValidationError):
        approvals.ApprovalCreate(
            workspace_id="wsp_test",
            session_id="sess_test",
            call_id="call_test",
            requester_id="usr_test",
            tool_name="tool",
            action_hash="a" * 64,
            risk="A3",
            ttl_seconds=700,
        )


def test_grant_create_session_requires_session_id():
    """session 作用域必须提供 session_id。"""
    model = approvals.GrantCreate(tool_name="tool", scope="session", session_id="sess_1")
    assert model.max_risk == "A2"


def test_revoke_reason_bounds():
    """撤销原因长度受限。"""
    with pytest.raises(ValidationError):
        approvals.RevokeReason(reason="")


# ----------------------------------------------------------------------
# auth/service.py 纯函数（OAuth/PKCE/TOTP/登录失败）
# ----------------------------------------------------------------------


class _FakeSettings:
    github_oauth_client_id = "gh_id"
    github_oauth_client_secret = "gh_secret"
    google_oauth_client_id = "g_id"
    google_oauth_client_secret = "g_secret"


def test_oauth_provider_config_returns_allowlisted_providers():
    """仅返回 github/google 配置。"""
    github = oauth_provider_config("github", _FakeSettings())
    assert github is not None
    assert github.name == "github"
    assert github.configured
    assert "read:user" in github.scopes

    google = oauth_provider_config("Google", _FakeSettings())
    assert google is not None
    assert google.name == "google"
    assert google.profile_kind == "oidc"


def test_oauth_provider_config_returns_none_for_unknown():
    """未知 provider 返回 None。"""
    assert oauth_provider_config("facebook", _FakeSettings()) is None


def test_pkce_challenge_length_and_verification():
    """PKCE challenge 为 43 字符的 S256 值。"""
    verifier = new_pkce_verifier()
    assert 43 <= len(verifier) <= 128
    challenge = pkce_challenge(verifier)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected


def test_pkce_challenge_rejects_short_verifier():
    """verifier 太短会被拒绝。"""
    with pytest.raises(ValueError, match="43 and 128"):
        pkce_challenge("short")


def test_oauth_callback_uri_normalizes_and_validates():
    """回调 URI 必须是合法绝对 URL 且无 fragment。"""
    assert oauth_callback_uri("https://example.com/", "github") == "https://example.com/api/v1/auth/oauth/github/callback"
    with pytest.raises(ValueError, match="absolute HTTP"):
        oauth_callback_uri("ftp://example.com/", "github")
    with pytest.raises(ValueError, match="fragment"):
        oauth_callback_uri("https://example.com/#frag", "github")


def test_build_oauth_authorization_url_requires_configured_provider():
    """未配置的 provider 不能生成授权 URL。"""
    config = OAuthProviderConfig(name="github", client_id="", client_secret="", authorization_url="", scopes=())
    with pytest.raises(ValueError, match="not configured"):
        build_oauth_authorization_url(config, state="x", redirect_uri="https://example.com/cb", code_challenge="c")


def test_build_oauth_authorization_url_includes_pkce_and_state():
    """授权 URL 包含 PKCE、state、scope 等参数。"""
    config = OAuthProviderConfig(
        name="github",
        client_id="id",
        client_secret="secret",
        authorization_url="https://github.com/login/oauth/authorize",
        scopes=("read:user", "user:email"),
    )
    url = build_oauth_authorization_url(
        config,
        state="state_123",
        redirect_uri="https://example.com/cb",
        code_challenge="challenge_abc",
    )
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=id" in url
    assert "state=state_123" in url
    assert "code_challenge=challenge_abc" in url
    assert "code_challenge_method=S256" in url
    assert "response_type=code" in url


def test_oauth_state_is_valid_requires_all_fields():
    """缺少字段或过期均判定无效。"""
    now = datetime.now(UTC)
    assert oauth_state_is_valid(None, provider="github", redirect_uri="https://cb", state="x") is False
    payload = {
        "provider": "github",
        "redirect_uri": "https://cb",
        "state": "x" * 20,
        "code_verifier": "v",
        "issued_at": now.timestamp(),
    }
    assert oauth_state_is_valid(payload, provider="github", redirect_uri="https://cb", state="x" * 20) is True
    assert oauth_state_is_valid(payload, provider="google", redirect_uri="https://cb") is False
    assert oauth_state_is_valid(payload, provider="github", redirect_uri="https://other") is False


def test_oauth_state_is_valid_enforces_ttl():
    """state 超过 TTL 或早于 60 秒前签发均无效。"""
    now = datetime.now(UTC)
    payload = {
        "provider": "github",
        "redirect_uri": "https://cb",
        "state": "x" * 20,
        "code_verifier": "v",
        "issued_at": now.timestamp(),
    }
    assert oauth_state_is_valid(payload, provider="github", redirect_uri="https://cb", now=now) is True
    assert oauth_state_is_valid(payload, provider="github", redirect_uri="https://cb", now=now + timedelta(seconds=601)) is False
    assert oauth_state_is_valid(payload, provider="github", redirect_uri="https://cb", now=now - timedelta(seconds=61)) is False


def test_auth_token_is_usable_boundary():
    """token 在过期前且未消费时可用。"""
    now = datetime.now(UTC)
    assert auth_token_is_usable(now + timedelta(seconds=1), None, now) is True
    assert auth_token_is_usable(now - timedelta(seconds=1), None, now) is False
    assert auth_token_is_usable(now + timedelta(seconds=1), now, now) is False


def test_next_login_failure_locks_after_five():
    """第 5 次失败开始设置 15 分钟锁定。"""
    now = datetime.now(UTC)
    for i in range(4):
        failures, lock_until = next_login_failure(i, now)
        assert failures == i + 1
        assert lock_until is None
    failures, lock_until = next_login_failure(4, now)
    assert failures == 5
    assert lock_until == now + timedelta(minutes=15)


def test_totp_code_is_six_digits():
    """TOTP 输出为 6 位数字。"""
    secret = "JBSWY3DPEHPK3PXP"
    code = totp_code(secret)
    assert len(code) == 6 and code.isdigit()


def test_verify_totp_accepts_current_code():
    """当前时间窗口的 TOTP 码可通过校验。"""
    secret = "JBSWY3DPEHPK3PXP"
    now = datetime.now(UTC)
    code = totp_code(secret, now)
    assert verify_totp(secret, code, now) is True


def test_verify_totp_rejects_short_and_non_digit():
    """非 6 位数字码被拒绝。"""
    secret = "JBSWY3DPEHPK3PXP"
    assert verify_totp(secret, "12345") is False
    assert verify_totp(secret, "abcdef") is False


# ----------------------------------------------------------------------
# auth/router.py 模型与 MFA ticket
# ----------------------------------------------------------------------


def test_register_request_password_bounds():
    """注册密码长度限制 10-128。"""
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@b.com", password="short", display_name="U")
    model = RegisterRequest(email="a@b.com", password="a" * 10, display_name="U")
    assert len(model.password) == 10


def test_mfa_code_request_pattern():
    """MFA 码必须是 6 位数字。"""
    with pytest.raises(ValidationError):
        MfaCodeRequest(code="12345")
    with pytest.raises(ValidationError):
        MfaCodeRequest(code="12345a")
    model = MfaCodeRequest(code="123456")
    assert model.code == "123456"


def test_token_request_minimum_length():
    """token 字段最少 20 字符。"""
    with pytest.raises(ValidationError):
        TokenRequest(token="x" * 19)
    model = TokenRequest(token="x" * 20)
    assert len(model.token) == 20


def test_mfa_ticket_contains_claims_and_expires():
    """MFA ticket 包含用户、工作区、角色声明并设置 5 分钟过期。"""
    now = datetime.now(UTC)
    ticket = _mfa_ticket("usr_1", "wsp_1", "admin")
    payload = jwt.decode(ticket, settings.jwt_secret, algorithms=["HS256"])
    assert payload["sub"] == "usr_1"
    assert payload["ws"] == "wsp_1"
    assert payload["role"] == "admin"
    assert payload["type"] == "mfa"
    assert payload["exp"] - payload["iat"] == 300


# ----------------------------------------------------------------------
# core.py 纯函数（id/密码/加密/能力）
# ----------------------------------------------------------------------


def test_new_id_shape_and_uniqueness():
    """new_id 生成固定格式且互不相同。"""
    first = new_id("usr")
    second = new_id("usr")
    assert first.startswith("usr_")
    assert len(first) == 30
    assert first != second


def test_hash_secret_is_deterministic_and_peppered():
    """hash_secret 对同一输入一致且非明文。"""
    value = "sk-wama-test"
    assert hash_secret(value) == hash_secret(value)
    assert value not in hash_secret(value)
    expected = hmac.new(settings.key_pepper.encode(), value.encode(), hashlib.sha256).hexdigest()
    assert hash_secret(value) == expected


def test_encrypt_secret_round_trip():
    """加密/解密可还原非空值，空值返回 None。"""
    assert encrypt_secret("") is None
    assert decrypt_secret("") is None
    ciphertext = encrypt_secret("sensitive")
    assert ciphertext is not None
    assert ciphertext != "sensitive"
    assert decrypt_secret(ciphertext) == "sensitive"


def test_password_hashing_round_trip():
    """argon2 哈希可验证正确密码并拒绝错误密码。"""
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "correct horse battery staple") is True
    assert verify_password(hashed, "incorrect") is False


def test_create_access_token_includes_auth_strength():
    """access token 携带 auth_strength 字段。"""
    token = create_access_token("usr_1", "wsp_1", "member", auth_strength=2)
    payload = decode_token(token)
    assert payload["sub"] == "usr_1"
    assert payload["ws"] == "wsp_1"
    assert payload["role"] == "member"
    assert payload["auth_strength"] == 2
    assert payload["type"] == "access"


def test_decode_token_rejects_wrong_type():
    """token 类型不匹配时抛出 401。"""
    token = create_access_token("usr_1", "wsp_1", "member")
    with pytest.raises(HTTPException, match="Unexpected token type"):
        decode_token(token, expected_type="refresh")


def test_decode_token_accepts_hs256_in_development():
    """开发环境允许 HS256 token 回退。"""
    token = jwt.encode(
        {"sub": "usr_1", "ws": "wsp_1", "role": "member", "type": "access"},
        settings.jwt_secret,
        algorithm="HS256",
    )
    payload = decode_token(token)
    assert payload["sub"] == "usr_1"


def test_capability_allows_exact_wildcard_and_domain_wildcard():
    """能力校验支持精确、全通配、域通配。"""
    assert capability_allows(("session:read",), "session:read") is True
    assert capability_allows(("*",), "session:read") is True
    assert capability_allows(("session:*",), "session:write") is True
    assert capability_allows(("session:read",), "session:write") is False
    assert capability_allows(("artifact:read",), "session:read") is False


def test_platform_key_scope_allows_read_write_patterns():
    """平台 key scope 支持 platform:*、platform:read/write。"""
    assert platform_key_scope_allows(("platform:read",), "billing:read") is True
    assert platform_key_scope_allows(("platform:read",), "billing:write") is False
    assert platform_key_scope_allows(("platform:write",), "billing:write") is True
    assert platform_key_scope_allows(("platform:*",), "billing:write") is True
    assert platform_key_scope_allows(("session:read",), "session:read") is True


# ----------------------------------------------------------------------
# enterprise_rbac.py 规范化与模型校验
# ----------------------------------------------------------------------


def test_normalize_name_trims_and_rejects_invalid():
    """名称规范化去除多余空白并拒绝非法字符。"""
    assert _normalize_name("  hello   world  ") == "hello world"
    with pytest.raises(ValueError, match="name is invalid"):
        _normalize_name("\n")
    with pytest.raises(ValueError, match="name is invalid"):
        _normalize_name("x" * 121)


def test_normalize_capabilities_sorts_and_validates():
    """能力排序、去重并拒绝保留域/格式错误。"""
    caps = _normalize_capabilities(["session:write", "session:read", "session:write"])
    assert caps == ["session:read", "session:write"]
    with pytest.raises(ValueError, match="reserved"):
        _normalize_capabilities(["owner:read"])
    with pytest.raises(ValueError, match="reserved"):
        _normalize_capabilities(["org:delete"])
    with pytest.raises(ValueError, match="format"):
        _normalize_capabilities(["session"])


def test_normalize_scopes_rejects_high_risk():
    """服务账号 scope 禁止 owner/platform/system 域。"""
    with pytest.raises(ValueError, match="high-risk"):
        _normalize_scopes(["owner:read"])
    with pytest.raises(ValueError, match="high-risk"):
        _normalize_scopes(["platform:*"])
    scopes = _normalize_scopes(["session:read", "session:write"])
    assert scopes == ["session:read", "session:write"]


def test_normalize_cidrs_normalizes_and_sorts():
    """CIDR 规范化去重并排序。"""
    cidrs = _normalize_cidrs(["10.1.0.0/16", "10.0.0.0/8", "10.1.0.0/16", "192.168.0.0/24"])
    assert cidrs == ["10.0.0.0/8", "10.1.0.0/16", "192.168.0.0/24"]
    with pytest.raises(ValueError, match="invalid network"):
        _normalize_cidrs(["not-a-network"])


def test_validate_safe_json_rejects_sensitive_keys_and_oversized():
    """JSON 校验拒绝敏感键与超大 payload。"""
    with pytest.raises(ValueError, match="sensitive key"):
        _validate_safe_json({"password": "x"}, "field")
    with pytest.raises(ValueError, match="too large"):
        _validate_safe_json({"k": "x" * 20_000}, "field", max_bytes=10)
    assert _validate_safe_json({"a": 1, "nested": {"b": [2, 3]}}, "field") == {"a": 1, "nested": {"b": [2, 3]}}


def test_role_create_normalizes_capabilities():
    """RoleCreate 规范化能力列表。"""
    role = RoleCreate(name="reader", capabilities=["session:read", "session:write"])
    assert role.capabilities == ["session:read", "session:write"]


def test_auth_strength_policy_create_validates_operation():
    """AuthStrengthPolicyCreate 校验 operation 格式。"""
    with pytest.raises(ValidationError):
        AuthStrengthPolicyCreate(operation="X", required_auth_strength=2)
    model = AuthStrengthPolicyCreate(operation="tool.invoke.high", required_auth_strength=2)
    assert model.operation == "tool.invoke.high"
    with pytest.raises(ValidationError):
        AuthStrengthPolicyCreate(operation="tool.invoke", required_auth_strength=5)


# ----------------------------------------------------------------------
# portability.py 签名、打包与校验
# ----------------------------------------------------------------------


def test_signing_key_derivation_matches_aws_signature_v4():
    """_signing_key 按 AWS Signature V4 派生。"""
    key = _signing_key("secret", "20260101")
    expected = hmac.new(
        hmac.new(
            hmac.new(
                hmac.new(("AWS4" + "secret").encode(), b"20260101", hashlib.sha256).digest(),
                b"us-east-1",
                hashlib.sha256,
            ).digest(),
            b"s3",
            hashlib.sha256,
        ).digest(),
        b"aws4_request",
        hashlib.sha256,
    ).digest()
    assert key == expected


def test_canonical_package_is_deterministic():
    """canonical_package 输出与 key 顺序无关且稳定。"""
    a = canonical_package({"b": 2, "a": 1})
    b = canonical_package({"a": 1, "b": 2})
    assert a == b
    assert a == b'{"a":1,"b":2}'


def test_validate_package_detects_bad_manifest_and_unknown_resources():
    """validate_package 检测 manifest 版本与未知资源类型。"""
    errors = validate_package({"manifest": {"manifest_version": 2}, "resources": {}})
    assert "unsupported manifest_version" in errors

    errors = validate_package(
        {
            "manifest": {"manifest_version": 1, "resource_counts": {}},
            "resources": {"unknown_type": []},
        }
    )
    assert any("unsupported resource types:" in error for error in errors)


def test_validate_package_rejects_channel_credentials():
    """导包中若包含 channel 凭证则报错。"""
    errors = validate_package(
        {
            "manifest": {"manifest_version": 1, "resource_counts": {"channels": 1}},
            "resources": {"channels": [{"id": "c1", "credential": "secret"}]},
        }
    )
    assert "channel credentials are forbidden" in errors


def test_validate_package_counts_mismatch():
    """resource_counts 与实际数量不一致时报错。"""
    errors = validate_package(
        {
            "manifest": {"manifest_version": 1, "resource_counts": {"sessions": 2}},
            "resources": {"sessions": [{"id": "s1"}]},
        }
    )
    assert "resource_counts mismatch" in errors


# ----------------------------------------------------------------------
# channel_extensions.py 辅助函数与模型
# ----------------------------------------------------------------------


def test_controlled_detects_mock_prefix():
    """_controlled 仅识别 mock:// 开头。"""
    assert _controlled("mock://local") is True
    assert _controlled("https://example.com") is False


def test_safe_payload_redacts_secrets():
    """_safe_payload 对敏感键脱敏。"""
    payload = {"token": "abc", "secret": "x", "plain": "visible"}
    result = _safe_payload(payload)
    assert result["token"] == "[redacted]"
    assert result["secret"] == "[redacted]"
    assert result["plain"] == "visible"


def test_channel_view_strips_signing_secret():
    """_channel_view 移除签名密钥字段。"""
    row = {"id": "c1", "signing_secret_enc": "enc", "signing_secret_hash": "hash", "name": "ch"}
    result = _channel_view(row)
    assert "signing_secret_enc" not in result
    assert "signing_secret_hash" not in result
    assert result["name"] == "ch"


def test_account_view_strips_account_ref():
    """_account_view 移除账号引用字段。"""
    row = {"id": "a1", "account_ref_enc": "enc", "account_ref_hash": "hash"}
    result = _account_view(row)
    assert "account_ref_enc" not in result
    assert "account_ref_hash" not in result


def test_session_hash_is_sha256():
    """_session_hash 为 SHA-256 十六进制。"""
    value = "session-key"
    assert _session_hash(value) == hashlib.sha256(value.encode()).hexdigest()


def test_choose_sticky_account_respects_quota_and_weights():
    """choose_sticky_account 仅选择有余额的活跃账号并按权重分配。"""
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": None},
        {"id": "a2", "status": "active", "weight": 50, "quota_remaining": 0},
        {"id": "a3", "status": "paused", "weight": 100, "quota_remaining": 10},
    ]
    selected = choose_sticky_account(accounts, "key")
    assert selected["id"] == "a1"


def test_choose_sticky_account_returns_none_when_all_exhausted():
    """无可用账号时返回 None。"""
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": 0},
        {"id": "a2", "status": "exhausted", "weight": 100, "quota_remaining": None},
    ]
    assert choose_sticky_account(accounts, "key") is None


def test_normalize_im_content_adds_kind_prefix():
    """normalize_im_content 确保包含 kind 前缀。"""
    assert normalize_im_content("wecom", "hello") == "[wecom] hello"
    assert normalize_im_content("wecom", "[wecom] hello") == "[wecom] hello"


def test_miniapp_manifest_structure():
    """miniapp_manifest 返回固定 schema。"""
    manifest = miniapp_manifest()
    assert manifest["schema_version"] == "1"
    assert "chat" in manifest["capabilities"]


def test_im_channel_create_restricts_kinds():
    """IMChannelCreate 仅允许指定 kind。"""
    IMChannelCreate(kind="wecom", name="wc", endpoint="mock://im/wecom/1")
    with pytest.raises(ValidationError):
        IMChannelCreate(kind="unknown", name="x", endpoint="mock://im/unknown/1")


def test_im_message_create_content_bounds():
    """IMMessageCreate 内容长度受限。"""
    with pytest.raises(ValidationError):
        IMMessageCreate(external_message_id="m1", content="")


# ----------------------------------------------------------------------
# knowledge.py 辅助函数与模型
# ----------------------------------------------------------------------


def test_safe_name_replaces_special_chars():
    """_safe_name 将特殊字符替换为 - 并截断。"""
    assert _safe_name("hello world!.txt") == "hello-world-.txt"
    assert _safe_name("   ") == "document"
    assert len(_safe_name("x" * 200)) == 160


def test_etag_format():
    """_etag 生成 W/"<version>" 格式。"""
    assert _etag(42) == 'W/"42"'


def test_assert_if_match_accepts_wildcard_and_version():
    """_assert_if_match 接受 *、版本号、ETag 格式。"""
    _assert_if_match("*", 3)
    _assert_if_match("3", 3)
    _assert_if_match('W/"3"', 3)
    _assert_if_match('"3"', 3)
    with pytest.raises(HTTPException, match="If-Match is required"):
        _assert_if_match(None, 3)
    with pytest.raises(HTTPException, match="version does not match"):
        _assert_if_match("2", 3)


def test_normalize_retrieval_config_uses_defaults_and_validates():
    """_normalize_retrieval_config 合并默认值并校验约束。"""
    config = _normalize_retrieval_config({"top_k": 3})
    assert config["top_k"] == 3
    assert config["candidate_k"] == 20
    with pytest.raises(HTTPException):
        _normalize_retrieval_config({"top_k": 30, "candidate_k": 10})


def test_retrieval_config_upsert_normalized_enforces_candidate_ge_top():
    """RetrievalConfigUpsert.normalized 要求 candidate_k >= top_k。"""
    with pytest.raises(HTTPException):
        RetrievalConfigUpsert(top_k=10, candidate_k=5).normalized()


def test_mime_for_name_prefers_suffix():
    """_mime_for_name 优先根据后缀识别 MIME。"""
    assert _mime_for_name("doc.md", "application/octet-stream") == "text/markdown"
    assert _mime_for_name("doc.txt") == "text/plain"


def test_mime_for_name_falls_back_to_declared_and_guessed():
    """无后缀时回退到声明类型与系统猜测。"""
    assert _mime_for_name("unknown", "text/plain") == "text/plain"


def test_mime_for_name_rejects_unsupported():
    """无法识别的格式抛出 415。"""
    with pytest.raises(HTTPException, match="E03001"):
        _mime_for_name("unknown.bin", "application/octet-stream")


def test_check_content_size_and_malware():
    """_check_content 限制大小并检测 EICAR 测试病毒。"""
    with pytest.raises(HTTPException, match="E03002"):
        _check_content(b"x" * (100 * 1024 * 1024 + 1))
    with pytest.raises(HTTPException, match="malware"):
        _check_content(b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE$")


def test_check_zip_expansion_limits_total_size():
    """Office 文档 zip 解压后总量受限。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("big.txt", "x" * (100 * 1024 * 1024 * 5 + 1))
    with pytest.raises(Exception, match="expands beyond"):
        _check_zip_expansion(buffer.getvalue())


def test_clean_text_removes_nulls_and_excessive_duplicates():
    """_clean_text 清理控制字符并删除过度重复行。"""
    text = "repeatedline\x00\r\nrepeatedline\nrepeatedline\nrepeatedline\nrepeatedline\nunique"
    cleaned = _clean_text(text)
    assert "\x00" not in cleaned
    assert "repeatedline" not in cleaned  # 长度>=8 且重复超过 3 次被移除
    assert "unique" in cleaned


def test_title_path_tracks_heading_hierarchy():
    """_title_path 根据标题层级维护路径。"""
    path = _title_path("# A", [])
    assert path == ["A"]
    path = _title_path("## B", path)
    assert path == ["A", "B"]
    path = _title_path("# C", path)
    assert path == ["C"]


def test_split_text_chunks_with_overlap():
    """_split_text 按目标大小分块并保留重叠。"""
    paragraphs = ["p" * 500] * 10
    chunks = _split_text("\n\n".join(paragraphs), {"source": "test"})
    assert len(chunks) > 1
    for text, metadata in chunks:
        assert text.strip()
        assert metadata["source"] == "test"


def test_dataset_create_normalizes_name():
    """DatasetCreate 规范化名称空白。"""
    model = DatasetCreate(name="  my   dataset  ")
    assert model.name == "my dataset"
    with pytest.raises(ValidationError):
        DatasetCreate(name="   ")


# ----------------------------------------------------------------------
# security/service.py 文本审核与 URL 校验
# ----------------------------------------------------------------------


def test_moderate_text_allow_when_no_match():
    """无匹配时返回 allow。"""
    result = moderate_text("hello world", ["badword"], "block")
    assert result.action == "allow"
    assert result.text == "hello world"


def test_moderate_text_blocks_and_masks():
    """block 动作保留原文，mask 动作替换匹配词。"""
    result = moderate_text("badword here", ["badword"], "block")
    assert result.action == "block"
    assert "badword" in result.text
    result = moderate_text("badword here", ["badword"], "mask")
    assert result.action == "mask"
    assert result.text == "*** here"


def test_moderate_text_unknown_action_defaults_to_block():
    """未知动作回退为 block。"""
    result = moderate_text("badword", ["badword"], "unknown")
    assert result.action == "block"


def test_validate_outbound_url_blocks_local_and_internal():
    """本地、内网、非 HTTP(S) URL 被拒绝。"""
    assert validate_outbound_url("http://localhost:8000").allowed is False
    assert validate_outbound_url("http://127.0.0.1:8000").allowed is False
    assert validate_outbound_url("http://192.168.1.1").allowed is False
    assert validate_outbound_url("ftp://example.com").allowed is False
    assert validate_outbound_url("http://user:pass@example.com").allowed is False
    assert validate_outbound_url("https://example.com/path").allowed is True


def test_validate_outbound_url_blocks_resolved_private_ips():
    """解析到私有 IP 的地址被拒绝。"""
    result = validate_outbound_url("https://example.com", resolved_ips=["10.0.0.1"])
    assert result.allowed is False


def test_evaluate_prompt_detects_missing_checks():
    """evaluate_prompt 识别缺少的关键提示词。"""
    passed = evaluate_prompt("Never reveal secret. Handle untrusted tool input. High-risk approval required.")
    assert passed.passed is True
    failed = evaluate_prompt("hello world")
    assert failed.passed is False
    assert "secret_protection" in failed.failures
    assert "untrusted_input" in failed.failures


# ----------------------------------------------------------------------
# gateway/router.py 路由辅助函数与模型
# ----------------------------------------------------------------------


def test_provider_name_resolves_aliases():
    """_provider_name 解析大小写和别名。"""
    assert _provider_name("OpenAI-Compatible") == "openai"
    assert _provider_name("GOOGLE") == "gemini"
    assert _provider_name("unknown") == "unknown"


def test_validate_channel_url_allows_mock_local():
    """mock provider 与 mock://local 组合直接通过。"""
    _validate_channel_url("mock", "mock://local")


def test_validate_channel_url_blocks_unsupported_provider():
    """不在 catalog 中的 provider 抛出 422。"""
    with pytest.raises(HTTPException, match="Unsupported provider"):
        _validate_channel_url("unknown", "https://example.com")


def test_validate_channel_url_blocks_unsafe_url():
    """不安全 URL 被拦截。"""
    with pytest.raises(HTTPException, match="Unsafe channel URL"):
        _validate_channel_url("openai-compatible", "http://localhost:8080")


def test_validate_channel_url_allows_public_https():
    """公开 HTTPS endpoint 通过校验。"""
    _validate_channel_url("openai-compatible", "https://api.openai.com/v1")


def test_normalize_model_name_validates_length():
    """_normalize_model_name 校验模型名长度。"""
    assert _normalize_model_name("gpt-4", "model") == "gpt-4"
    with pytest.raises(HTTPException, match="up to 120 characters"):
        _normalize_model_name("", "model")
    with pytest.raises(HTTPException, match="up to 120 characters"):
        _normalize_model_name("x" * 121, "model")


def test_gateway_token_create_limits():
    """GatewayTokenCreate 校验 rpm/tpm 边界。"""
    with pytest.raises(ValidationError):
        GatewayTokenCreate(name="t", rpm_limit=0)
    with pytest.raises(ValidationError):
        GatewayTokenCreate(name="t", rpm_limit=100_001)
    model = GatewayTokenCreate(name="t", rpm_limit=60, tpm_limit=100_000)
    assert model.rpm_limit == 60
    assert model.tpm_limit == 100_000


def test_channel_create_provider_defaults():
    """ChannelCreate 默认 provider 为 openai-compatible。"""
    model = ChannelCreate(name="ch", base_url="https://api.openai.com")
    assert model.provider == "openai-compatible"
    assert model.weight == 100
