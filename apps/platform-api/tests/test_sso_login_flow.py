"""M9 SSO/SCIM 增强：SAML/OIDC 登录流程 + SCIM 同步执行 测试。

覆盖：
- OIDC authorize：生成授权 URL/重定向 302/state 存 session/不存在 provider 404 (5)
- SAML authorize：返回 HTML 表单自动提交/不存在 provider 404 (2)
- OIDC callback：成功换 token+获取 userinfo/创建新用户/更新已有用户/签发平台 token/
  code 无效/provider 404/IdP 返回错误/missing code/missing state/invalid state/wrong provider type (11)
- SAML ACS：成功解析 SAML Response/提取 NameID+Attributes/创建新用户/更新已有用户/
  签发平台 token/SAML Response 无效/provider 404/wrong provider type (8)
- 连接测试：OIDC 成功/OIDC 不可达/OIDC 无 issuer/SAML 成功/SAML 证书无效/SAML 无证书/
  不存在 provider 404 (7)
- SCIM 同步：成功拉取用户/创建缺失用户/更新变更用户/禁用 IdP 不存在用户/
  同步报告记录/同步历史查询/不存在 provider 404/IdP 返回错误 (8)
- 辅助函数：_parse_saml_response_basic 成功/空/无效 base64/无效 XML/缺少 Assertion/缺少 NameID,
  _fetch_oidc_userinfo 成功/HTTP 错误, _exchange_oidc_code_basic 成功,
  _create_or_update_user_from_sso 创建新/更新已有, _sync_run_view 格式化 (12)

所有测试使用 fake pool/connection mock 模式，对 urllib.request 调用用 monkeypatch mock。
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import identity_federation as idf
from workama_platform.modules.identity_federation import (
    SAML_ASSERTION_NS,
    SAML_PROTOCOL_NS,
    SCHEMA_STATEMENTS,
    _create_or_update_user_from_sso,
    _exchange_oidc_code_basic,
    _fetch_oidc_userinfo,
    _get_provider_config,
    _http_get_json,
    _http_get_raw,
    _http_post_json,
    _parse_saml_response_basic,
    _sync_run_view,
    _sync_scim_users,
    _test_oidc_connection,
    _test_saml_connection,
    scim_sync,
    scim_sync_history,
    sso_acs,
    sso_authorize,
    sso_oidc_callback,
    sso_test_connection,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return False


class _SeqConnection:
    """按调用顺序返回预置结果的连接 mock。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    def transaction(self):
        return _Transaction(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(*, capabilities=("*",), workspace_id="wsp_test", user_id="usr_test") -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _oidc_config_row(**overrides) -> dict:
    base = {
        "id": "sso_oidc_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "provider": "oidc",
        "name": "OIDC IdP",
        "issuer": "https://idp.example.com",
        "metadata_url": "https://idp.example.com/.well-known/openid-configuration",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": None,
        "client_id": "workama-client",
        "client_secret_hash": None,
        "client_secret_enc": None,
        "client_secret_ref": None,
        "client_secret_last4": None,
        "certificate_hash": None,
        "certificate_enc": None,
        "certificate_ref": None,
        "certificate_last4": None,
        "redirect_allowlist": ["https://console.example.com/oidc/callback"],
        "mapping": {},
        "status": "active",
        "pending_reason": None,
        "version": 1,
    }
    base.update(overrides)
    return base


def _saml_config_row(**overrides) -> dict:
    base = {
        "id": "sso_saml_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "provider": "saml",
        "name": "SAML IdP",
        "issuer": "urn:example:corporate-idp",
        "metadata_url": "https://idp.example.com/saml/metadata",
        "authorization_endpoint": None,
        "token_endpoint": None,
        "jwks_uri": None,
        "client_id": None,
        "client_secret_hash": None,
        "client_secret_enc": None,
        "client_secret_ref": None,
        "client_secret_last4": None,
        "certificate_hash": None,
        "certificate_enc": None,
        "certificate_ref": None,
        "certificate_last4": None,
        "redirect_allowlist": ["https://console.example.com/saml/acs"],
        "mapping": {"email_attribute": "email", "sso_url": "https://idp.example.com/saml/sso"},
        "status": "active",
        "pending_reason": None,
        "version": 1,
    }
    base.update(overrides)
    return base


def _user_row(**overrides) -> dict:
    base = {
        "id": "usr_existing",
        "email": "user@example.com",
        "display_name": "Existing User",
        "onboarding_completed": True,
        "workspace_id": "wsp_test",
        "org_id": "org_test",
        "role": "member",
    }
    base.update(overrides)
    return base


def _sync_run_row(**overrides) -> dict:
    base = {
        "id": "sync_1",
        "workspace_id": "wsp_test",
        "provider_id": "sso_oidc_1",
        "status": "completed",
        "created_users": 3,
        "updated_users": 2,
        "deactivated_users": 1,
        "total_users": 6,
        "error_message": None,
        "started_at": datetime.now(UTC),
        "completed_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _make_saml_response(
    email: str = "user@example.com",
    name_id: str = "provider-user-1",
    display_name: str = "Test User",
) -> str:
    """生成 base64 编码的 SAML Response XML（用于测试 _parse_saml_response_basic）。"""
    response = ET.Element(f"{{{SAML_PROTOCOL_NS}}}Response", ID="_response-1", Version="2.0")
    assertion = ET.SubElement(response, f"{{{SAML_ASSERTION_NS}}}Assertion", ID="_assertion-1")
    ET.SubElement(assertion, f"{{{SAML_ASSERTION_NS}}}Issuer").text = "urn:example:corporate-idp"
    subject = ET.SubElement(assertion, f"{{{SAML_ASSERTION_NS}}}Subject")
    ET.SubElement(subject, f"{{{SAML_ASSERTION_NS}}}NameID").text = name_id
    statement = ET.SubElement(assertion, f"{{{SAML_ASSERTION_NS}}}AttributeStatement")
    attr_email = ET.SubElement(statement, f"{{{SAML_ASSERTION_NS}}}Attribute", Name="email")
    ET.SubElement(attr_email, f"{{{SAML_ASSERTION_NS}}}AttributeValue").text = email
    attr_name = ET.SubElement(statement, f"{{{SAML_ASSERTION_NS}}}Attribute", Name="displayName")
    ET.SubElement(attr_name, f"{{{SAML_ASSERTION_NS}}}AttributeValue").text = display_name
    return base64.b64encode(ET.tostring(response)).decode()


def _claims_from_saml(
    email: str = "user@example.com",
    name_id: str = "provider-user-1",
    display_name: str = "Test User",
) -> dict:
    """构造 ``_validate_saml_response`` 的返回值（模拟验签通过后的 claims）。

    v7.172 起 ``sso_acs`` 改为调用 ``_validate_saml_response`` 强制校验 XML 签名，
    单元测试中通过 monkeypatch 替换该函数以聚焦于 sso_acs 业务逻辑。
    """
    return {
        "response_id": "_response-1",
        "sub": name_id,
        "iss": "urn:example:corporate-idp",
        "email": email,
        "email_verified": True,
        "attributes": {"email": email, "displayName": display_name},
    }


def _patch_validate_saml_response(monkeypatch, email, name_id, display_name):
    """monkeypatch ``_validate_saml_response`` 返回模拟 claims，跳过签名验证。"""
    monkeypatch.setattr(
        idf,
        "_validate_saml_response",
        lambda payload, *, config, now=None: _claims_from_saml(email, name_id, display_name),
    )


def _mock_http_get_json(payload):
    """返回一个替换 _http_get_json 的 mock 函数。"""
    def _mock(url, headers=None):
        return payload
    return _mock


# ============================================================================
# 1. 辅助函数：_parse_saml_response_basic
# ============================================================================


def test_parse_saml_response_basic_success():
    """成功解析 SAML Response，提取 NameID 和 Attributes。"""
    b64 = _make_saml_response(email="alice@example.com", name_id="alice-id", display_name="Alice")
    result = _parse_saml_response_basic(b64)
    assert result["name_id"] == "alice-id"
    assert result["response_id"] == "_response-1"
    assert result["attributes"]["email"] == "alice@example.com"
    assert result["attributes"]["displayName"] == "Alice"


def test_parse_saml_response_basic_empty():
    """空字符串应抛出 ValueError。"""
    with pytest.raises(ValueError, match="empty"):
        _parse_saml_response_basic("")


def test_parse_saml_response_basic_invalid_base64():
    """无效 base64 应抛出 ValueError。"""
    with pytest.raises(ValueError, match="base64"):
        _parse_saml_response_basic("!!!not-base64!!!")


def test_parse_saml_response_basic_invalid_xml():
    """无效 XML 应抛出 ValueError。"""
    bad_xml = base64.b64encode(b"<not-saml>").decode()
    with pytest.raises(ValueError, match="XML is invalid"):
        _parse_saml_response_basic(bad_xml)


def test_parse_saml_response_basic_missing_assertion():
    """缺少 Assertion 应抛出 ValueError。"""
    response = ET.Element(f"{{{SAML_PROTOCOL_NS}}}Response", ID="_r1", Version="2.0")
    b64 = base64.b64encode(ET.tostring(response)).decode()
    with pytest.raises(ValueError, match="missing Assertion"):
        _parse_saml_response_basic(b64)


def test_parse_saml_response_basic_missing_nameid():
    """缺少 NameID 应抛出 ValueError。"""
    response = ET.Element(f"{{{SAML_PROTOCOL_NS}}}Response", ID="_r1", Version="2.0")
    assertion = ET.SubElement(response, f"{{{SAML_ASSERTION_NS}}}Assertion", ID="_a1")
    ET.SubElement(assertion, f"{{{SAML_ASSERTION_NS}}}Issuer").text = "urn:test"
    statement = ET.SubElement(assertion, f"{{{SAML_ASSERTION_NS}}}AttributeStatement")
    attr = ET.SubElement(statement, f"{{{SAML_ASSERTION_NS}}}Attribute", Name="email")
    ET.SubElement(attr, f"{{{SAML_ASSERTION_NS}}}AttributeValue").text = "test@example.com"
    b64 = base64.b64encode(ET.tostring(response)).decode()
    with pytest.raises(ValueError, match="missing NameID"):
        _parse_saml_response_basic(b64)


# ============================================================================
# 2. 辅助函数：_fetch_oidc_userinfo / _exchange_oidc_code_basic
# ============================================================================


def test_fetch_oidc_userinfo_success(monkeypatch):
    """_fetch_oidc_userinfo 成功获取 userinfo。"""
    expected = {"sub": "user-1", "email": "alice@example.com", "name": "Alice"}
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(expected))
    result = _fetch_oidc_userinfo("access-token", "https://idp.example.com/userinfo")
    assert result["sub"] == "user-1"
    assert result["email"] == "alice@example.com"


def test_fetch_oidc_userinfo_http_error(monkeypatch):
    """_fetch_oidc_userinfo HTTP 错误时应抛出异常。"""
    def _raise(url, headers=None):
        raise ConnectionError("network unreachable")
    monkeypatch.setattr(idf, "_http_get_json", _raise)
    with pytest.raises(ConnectionError):
        _fetch_oidc_userinfo("access-token", "https://idp.example.com/userinfo")


def test_exchange_oidc_code_basic_success(monkeypatch):
    """_exchange_oidc_code_basic 成功用授权码换 access_token。"""
    token_response = {"access_token": "opaque-token", "token_type": "bearer", "id_token": "jwt-token"}

    def _mock_post(url, data, headers=None):
        assert "grant_type=authorization_code" in url or data.get("grant_type") == "authorization_code"
        assert data["code"] == "the-code"
        assert data["client_id"] == "workama-client"
        return token_response

    monkeypatch.setattr(idf, "_http_post_json", _mock_post)
    config = {
        "issuer": "https://idp.example.com",
        "token_endpoint": "https://idp.example.com/token",
        "client_id": "workama-client",
        "client_secret": "secret",
        "redirect_uri": "https://console.example.com/callback",
    }
    result = _exchange_oidc_code_basic("the-code", config)
    assert result["access_token"] == "opaque-token"


# ============================================================================
# 3. 辅助函数：_test_oidc_connection / _test_saml_connection
# ============================================================================


def test_test_oidc_connection_success(monkeypatch):
    """OIDC 连接测试成功：GET .well-known/openid-configuration 可达。"""
    metadata = {
        "issuer": "https://idp.example.com",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
        "jwks_uri": "https://idp.example.com/jwks",
    }
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(metadata))
    result = _test_oidc_connection(_oidc_config_row())
    assert result["status"] == "ok"
    assert result["provider"] == "oidc"
    assert result["authorization_endpoint"] == "https://idp.example.com/authorize"


def test_test_oidc_connection_no_issuer():
    """OIDC 连接测试：未配置 issuer 时返回 failed。"""
    result = _test_oidc_connection(_oidc_config_row(issuer=None))
    assert result["status"] == "failed"
    assert "issuer" in result["error"]


def test_test_oidc_connection_unreachable(monkeypatch):
    """OIDC 连接测试：IdP 不可达时返回 failed。"""
    def _raise(url, headers=None):
        raise ConnectionError("timeout")
    monkeypatch.setattr(idf, "_http_get_json", _raise)
    result = _test_oidc_connection(_oidc_config_row())
    assert result["status"] == "failed"
    assert "timeout" in result["error"]


def test_test_saml_connection_success(monkeypatch):
    """SAML 连接测试成功：证书格式正确 + endpoint 可达。"""
    from workama_platform.core import encrypt_secret
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test IdP")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    monkeypatch.setattr(idf, "_http_get_raw", lambda url, headers=None: b"<EntityDescriptor/>")
    config = _saml_config_row(certificate_enc=encrypt_secret(cert_pem))
    result = _test_saml_connection(config)
    assert result["status"] == "ok"
    assert result["provider"] == "saml"
    assert result["certificate_configured"] is True
    assert result["endpoint_reachable"] is True


def test_test_saml_connection_no_certificate():
    """SAML 连接测试：未配置证书时返回 failed。"""
    result = _test_saml_connection(_saml_config_row())
    assert result["status"] == "failed"
    assert result["certificate_configured"] is False


def test_test_saml_connection_invalid_certificate(monkeypatch):
    """SAML 连接测试：证书无效时返回 failed。"""
    from workama_platform.core import encrypt_secret
    monkeypatch.setattr(idf, "decrypt_secret", lambda x: "not-a-cert")
    config = _saml_config_row(certificate_enc=encrypt_secret("dummy"))
    result = _test_saml_connection(config)
    assert result["status"] == "failed"
    assert "certificate" in result["error"].lower()


# ============================================================================
# 4. 辅助函数：_create_or_update_user_from_sso
# ============================================================================


@pytest.mark.asyncio
async def test_create_or_update_user_from_sso_creates_new():
    """SSO 用户不存在时创建新用户。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    user, created = await _create_or_update_user_from_sso(
        conn, "new@example.com", "New User", "sso_1", "wsp_test", "org_test",
    )
    assert created is True
    assert user["email"] == "new@example.com"
    assert user["display_name"] == "New User"
    assert user["workspace_id"] == "wsp_test"
    assert user["role"] == "member"
    # 验证执行了 SELECT + INSERT id_user + INSERT id_member
    assert len(conn.calls) == 3


@pytest.mark.asyncio
async def test_create_or_update_user_from_sso_updates_existing():
    """SSO 用户已存在时更新 display_name。"""
    existing = _user_row(display_name="Old Name")
    conn = _SeqConnection(results=[_Result(row=existing)])
    user, created = await _create_or_update_user_from_sso(
        conn, "user@example.com", "New Name", "sso_1", "wsp_test", "org_test",
    )
    assert created is False
    assert user["email"] == "user@example.com"
    # 验证执行了 SELECT + UPDATE
    assert len(conn.calls) == 2
    assert "UPDATE" in conn.calls[1][0]


@pytest.mark.asyncio
async def test_create_or_update_user_from_sso_no_update_when_same_name():
    """SSO 用户已存在且 display_name 未变时不执行 UPDATE。"""
    existing = _user_row(display_name="Same Name")
    conn = _SeqConnection(results=[_Result(row=existing)])
    user, created = await _create_or_update_user_from_sso(
        conn, "user@example.com", "Same Name", "sso_1", "wsp_test", "org_test",
    )
    assert created is False
    # 只执行了 SELECT，没有 UPDATE
    assert len(conn.calls) == 1


# ============================================================================
# 5. 辅助函数：_sync_run_view
# ============================================================================


def test_sync_run_view_formats_correctly():
    """_sync_run_view 正确格式化同步记录。"""
    row = _sync_run_row()
    view = _sync_run_view(row)
    assert view["id"] == "sync_1"
    assert view["status"] == "completed"
    assert view["created_users"] == 3
    assert view["updated_users"] == 2
    assert view["deactivated_users"] == 1
    assert view["total_users"] == 6
    assert view["error_message"] is None
    assert view["started_at"] is not None
    assert view["completed_at"] is not None


# ============================================================================
# 6. OIDC authorize 端点
# ============================================================================


@pytest.mark.asyncio
async def test_oidc_authorize_generates_url_and_redirects(monkeypatch):
    """OIDC authorize 生成授权 URL 并 302 重定向。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    resp = await sso_authorize("sso_oidc_1", redirect_uri="https://console.example.com/oidc/callback")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "response_type=code" in location
    assert "client_id=workama-client" in location
    assert "scope=openid+profile+email" in location
    assert "state=" in location


@pytest.mark.asyncio
async def test_oidc_authorize_creates_login_session(monkeypatch):
    """OIDC authorize 在 DB 中创建 sso_login_session 记录。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    await sso_authorize("sso_oidc_1", redirect_uri="https://console.example.com/oidc/callback")
    # 第二个 execute 应该是 INSERT INTO sso_login_session
    insert_calls = [c for c in conn.calls if "INSERT" in c[0] and "sso_login_session" in c[0]]
    assert len(insert_calls) == 1


@pytest.mark.asyncio
async def test_oidc_authorize_provider_not_found_404(monkeypatch):
    """OIDC authorize 不存在的 provider 返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_authorize("sso_missing", redirect_uri="https://console.example.com/cb")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_saml_authorize_returns_html_form(monkeypatch):
    """SAML authorize 返回 HTML 表单（含 SAMLRequest 和 RelayState）。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    resp = await sso_authorize("sso_saml_1", redirect_uri="https://console.example.com/saml/acs")
    assert resp.media_type == "text/html"
    body = resp.body.decode()
    assert "SAMLRequest" in body
    assert "RelayState" in body
    assert "https://idp.example.com/saml/sso" in body


@pytest.mark.asyncio
async def test_saml_authorize_provider_not_found_404(monkeypatch):
    """SAML authorize 不存在的 provider 返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_authorize("sso_missing", redirect_uri="https://console.example.com/saml/acs")
    assert exc.value.status_code == 404


# ============================================================================
# 7. OIDC callback 端点
# ============================================================================


@pytest.mark.asyncio
async def test_oidc_callback_success_creates_new_user(monkeypatch):
    """OIDC callback 成功换 token + 获取 userinfo + 创建新用户 + 签发平台 token。"""
    config = _oidc_config_row()
    session_row = {"id": "ssos_1", "token": "state-123", "provider_id": "sso_oidc_1"}
    # _get_provider_config SELECT → config; verify session SELECT → session; _create_or_update SELECT → None
    conn = _SeqConnection(results=[
        _Result(row=config),       # _get_provider_config
        _Result(row=session_row),  # verify session
        _Result(row=None),         # _create_or_update_user_from_sso SELECT
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    monkeypatch.setattr(idf, "_exchange_oidc_code_basic", lambda code, cfg: {"access_token": "opaque-token"})
    monkeypatch.setattr(idf, "_fetch_oidc_userinfo", lambda token, ep: {
        "sub": "provider-user-1", "email": "new@example.com", "name": "New User",
    })

    result = await sso_oidc_callback("sso_oidc_1", code="the-code", state="state-123", error=None)
    assert result["access_token"]
    assert result["token_type"] == "bearer"
    assert result["user"]["email"] == "new@example.com"
    assert result["sso"]["provider"] == "oidc"
    assert result["sso"]["created"] is True


@pytest.mark.asyncio
async def test_oidc_callback_updates_existing_user(monkeypatch):
    """OIDC callback 更新已有用户。"""
    config = _oidc_config_row()
    session_row = {"id": "ssos_1", "token": "state-123", "provider_id": "sso_oidc_1"}
    existing_user = _user_row()
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row=session_row),
        _Result(row=existing_user),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    monkeypatch.setattr(idf, "_exchange_oidc_code_basic", lambda code, cfg: {"access_token": "opaque-token"})
    monkeypatch.setattr(idf, "_fetch_oidc_userinfo", lambda token, ep: {
        "sub": "provider-user-1", "email": "user@example.com", "name": "Updated Name",
    })

    result = await sso_oidc_callback("sso_oidc_1", code="the-code", state="state-123", error=None)
    assert result["sso"]["created"] is False
    assert result["user"]["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_oidc_callback_issues_platform_token(monkeypatch):
    """OIDC callback 签发平台 access_token。"""
    config = _oidc_config_row()
    session_row = {"id": "ssos_1", "token": "state-123", "provider_id": "sso_oidc_1"}
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row=session_row),
        _Result(row=None),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    monkeypatch.setattr(idf, "_exchange_oidc_code_basic", lambda code, cfg: {"access_token": "opaque"})
    monkeypatch.setattr(idf, "_fetch_oidc_userinfo", lambda token, ep: {
        "sub": "u1", "email": "new@example.com", "name": "New",
    })

    result = await sso_oidc_callback("sso_oidc_1", code="code", state="state-123", error=None)
    assert isinstance(result["access_token"], str)
    assert len(result["access_token"]) > 0


@pytest.mark.asyncio
async def test_oidc_callback_invalid_code(monkeypatch):
    """OIDC callback code 无效（token exchange 失败）返回 502。"""
    config = _oidc_config_row()
    session_row = {"id": "ssos_1", "token": "state-123", "provider_id": "sso_oidc_1"}
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row=session_row),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    def _raise(code, cfg):
        raise ConnectionError("token endpoint down")
    monkeypatch.setattr(idf, "_exchange_oidc_code_basic", _raise)

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_oidc_1", code="bad-code", state="state-123", error=None)
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_oidc_callback_provider_not_found_404(monkeypatch):
    """OIDC callback provider 不存在返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_missing", code="code", state="state")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_oidc_callback_idp_returns_error(monkeypatch):
    """OIDC callback IdP 返回 error 参数时返回 400。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_oidc_1", code=None, state="state", error="access_denied")
    assert exc.value.status_code == 400
    assert "access_denied" in exc.value.detail


@pytest.mark.asyncio
async def test_oidc_callback_missing_code(monkeypatch):
    """OIDC callback 缺少 code 参数返回 400。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_oidc_1", code=None, state="state-123", error=None)
    assert exc.value.status_code == 400
    assert "code" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_oidc_callback_missing_state(monkeypatch):
    """OIDC callback 缺少 state 参数返回 400。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_oidc_1", code="the-code", state=None, error=None)
    assert exc.value.status_code == 400
    assert "state" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_oidc_callback_invalid_state(monkeypatch):
    """OIDC callback state 无效（session 不存在）返回 400。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row=None),  # session not found
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_oidc_1", code="code", state="invalid-state", error=None)
    assert exc.value.status_code == 400
    assert "state" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_oidc_callback_wrong_provider_type(monkeypatch):
    """OIDC callback provider 类型不是 OIDC 返回 409。"""
    config = _saml_config_row(id="sso_saml_1")
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_oidc_callback("sso_saml_1", code="code", state="state")
    assert exc.value.status_code == 409


# ============================================================================
# 8. SAML ACS 端点
# ============================================================================


@pytest.mark.asyncio
async def test_saml_acs_success_parses_response(monkeypatch):
    """SAML ACS 成功解析 SAML Response 并创建用户。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),  # _get_provider_config
        _Result(row={"id": "sreplay_1"}),  # v7.171 replay INSERT RETURNING (防重放)
        _Result(row=None),    # _create_or_update_user_from_sso SELECT → None (new user)
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "new@example.com", "new-user-id", "New User")

    saml_b64 = _make_saml_response(email="new@example.com", name_id="new-user-id", display_name="New User")
    result = await sso_acs("sso_saml_1", saml_response=saml_b64, relay_state="state-123")
    assert result["access_token"]
    assert result["user"]["email"] == "new@example.com"
    assert result["sso"]["provider"] == "saml"
    assert result["sso"]["subject"] == "new-user-id"
    assert result["sso"]["created"] is True


@pytest.mark.asyncio
async def test_saml_acs_extracts_nameid_and_attributes(monkeypatch):
    """SAML ACS 正确提取 NameID 和 Attributes。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row={"id": "sreplay_1"}),  # v7.171 replay INSERT RETURNING
        _Result(row=None),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "alice@example.com", "alice-id", "Alice")

    saml_b64 = _make_saml_response(email="alice@example.com", name_id="alice-id", display_name="Alice")
    result = await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert result["sso"]["subject"] == "alice-id"
    assert result["user"]["display_name"] == "Alice"


@pytest.mark.asyncio
async def test_saml_acs_creates_new_user(monkeypatch):
    """SAML ACS 用户不存在时创建新用户。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row={"id": "sreplay_1"}),  # v7.171 replay INSERT RETURNING
        _Result(row=None),  # no existing user
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "create@example.com", "provider-user-1", "Test User")

    saml_b64 = _make_saml_response(email="create@example.com")
    result = await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert result["sso"]["created"] is True


@pytest.mark.asyncio
async def test_saml_acs_updates_existing_user(monkeypatch):
    """SAML ACS 用户已存在时更新。"""
    config = _saml_config_row()
    existing = _user_row(email="user@example.com", display_name="Old")
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row={"id": "sreplay_1"}),  # v7.171 replay INSERT RETURNING
        _Result(row=existing),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "user@example.com", "provider-user-1", "Updated Name")

    saml_b64 = _make_saml_response(email="user@example.com", display_name="Updated Name")
    result = await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert result["sso"]["created"] is False


@pytest.mark.asyncio
async def test_saml_acs_issues_platform_token(monkeypatch):
    """SAML ACS 签发平台 access_token。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),
        _Result(row={"id": "sreplay_1"}),  # v7.171 replay INSERT RETURNING
        _Result(row=None),
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "token@example.com", "provider-user-1", "Test User")

    saml_b64 = _make_saml_response(email="token@example.com")
    result = await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert isinstance(result["access_token"], str)
    assert len(result["access_token"]) > 0


@pytest.mark.asyncio
async def test_saml_acs_invalid_response(monkeypatch):
    """SAML ACS 无效 SAML Response 返回 401（v7.172 验签后由 SamlValidationError 触发）。

    v7.172 修复：原先调用 _parse_saml_response_basic 不验签，无效 base64 返回 400；
    现调用 _validate_saml_response + _decode_saml_response_form，无效 base64 抛
    SamlValidationError（继承自 ValueError），sso_acs 统一返回 401。
    """
    config = _saml_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_acs("sso_saml_1", saml_response="!!!invalid-base64!!!")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_saml_acs_unsigned_response_rejected_401(monkeypatch):
    """v7.172 新增：未签名 SAML Response 必须被拒绝（验证 _validate_saml_response 真实验签路径）。

    不 monkeypatch _validate_saml_response，让其真实调用。_saml_config_row 没有配置
    certificate_enc，_validate_saml_response 会抛 SamlValidationError("SAML signing
    certificate is not configured")，sso_acs 返回 401。
    """
    config = _saml_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    saml_b64 = _make_saml_response(email="unsigned@example.com")
    with pytest.raises(HTTPException) as exc:
        await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert exc.value.status_code == 401
    assert "SAML response validation failed" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_saml_acs_provider_not_found_404(monkeypatch):
    """SAML ACS provider 不存在返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_acs("sso_missing", saml_response=_make_saml_response())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_saml_acs_wrong_provider_type(monkeypatch):
    """SAML ACS provider 类型不是 SAML 返回 409。"""
    config = _oidc_config_row(id="sso_oidc_1")
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_acs("sso_oidc_1", saml_response=_make_saml_response())
    assert exc.value.status_code == 409


# ============================================================================
# 9. SSO 连接测试端点
# ============================================================================


@pytest.mark.asyncio
async def test_sso_test_connection_oidc_success(monkeypatch):
    """OIDC 连接测试端点成功。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    metadata = {"issuer": "https://idp.example.com", "authorization_endpoint": "https://idp.example.com/authorize"}
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(metadata))

    result = await sso_test_connection("sso_oidc_1", actor=_actor())
    assert result["status"] == "ok"
    assert result["provider"] == "oidc"


@pytest.mark.asyncio
async def test_sso_test_connection_saml_success(monkeypatch):
    """SAML 连接测试端点成功。"""
    from workama_platform.core import encrypt_secret
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    config = _saml_config_row(certificate_enc=encrypt_secret(cert_pem))
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    monkeypatch.setattr(idf, "_http_get_raw", lambda url, headers=None: b"<EntityDescriptor/>")

    result = await sso_test_connection("sso_saml_1", actor=_actor())
    assert result["status"] == "ok"
    assert result["provider"] == "saml"


@pytest.mark.asyncio
async def test_sso_test_connection_provider_not_found_404(monkeypatch):
    """连接测试端点 provider 不存在返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_test_connection("sso_missing", actor=_actor())
    assert exc.value.status_code == 404


# ============================================================================
# 10. SCIM 同步端点
# ============================================================================


@pytest.mark.asyncio
async def test_scim_sync_success_pulls_users(monkeypatch):
    """SCIM 同步成功从 IdP 拉取用户并创建。"""
    config = _oidc_config_row(mapping={"scim_endpoint": "https://idp.example.com/scim", "scim_bearer": "token"})
    conn = _SeqConnection(results=[
        _Result(row=config),  # _get_sso SELECT
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    remote_users = {
        "Resources": [
            {"externalId": "ext-1", "userName": "alice@example.com", "displayName": "Alice", "active": True},
            {"externalId": "ext-2", "userName": "bob@example.com", "displayName": "Bob", "active": True},
        ]
    }
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(remote_users))

    # mock _sync_scim_users 的内部 pool 调用
    sync_conn = _SeqConnection(results=[])  # all defaults (None for fetchone, [] for fetchall)
    original_pool = idf.pool

    def _switch_pool(*args, **kwargs):
        idf.pool = _Pool(sync_conn)
        return _Pool(sync_conn)

    # 由于 _sync_scim_users 内部使用 pool，我们需要让 pool 在调用时返回 sync_conn
    # 简化方案：直接 mock _sync_scim_users
    async def _mock_sync(config, ws, org):
        return {"created": 2, "updated": 0, "deactivated": 0, "total": 2}
    monkeypatch.setattr(idf, "_sync_scim_users", _mock_sync)

    result = await scim_sync("sso_oidc_1", actor=_actor())
    assert result["status"] == "completed"
    assert result["created"] == 2
    assert result["total"] == 2
    assert result["run_id"]


@pytest.mark.asyncio
async def test_scim_sync_records_sync_run(monkeypatch):
    """SCIM 同步在 DB 中记录 sync_run（INSERT + UPDATE completed）。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    async def _mock_sync(config, ws, org):
        return {"created": 1, "updated": 1, "deactivated": 0, "total": 2}
    monkeypatch.setattr(idf, "_sync_scim_users", _mock_sync)

    await scim_sync("sso_oidc_1", actor=_actor())
    # 验证 INSERT 和 UPDATE sync_run 被调用
    insert_calls = [c for c in conn.calls if "INSERT" in c[0] and "scim_sync_run" in c[0]]
    update_calls = [c for c in conn.calls if "UPDATE" in c[0] and "scim_sync_run" in c[0] and "completed" in c[0]]
    assert len(insert_calls) == 1
    assert len(update_calls) == 1


@pytest.mark.asyncio
async def test_scim_sync_provider_not_found_404(monkeypatch):
    """SCIM 同步 provider 不存在返回 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await scim_sync("sso_missing", actor=_actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_scim_sync_idp_returns_error(monkeypatch):
    """SCIM 同步 IdP 返回错误时记录 failed 并返回 502。"""
    config = _oidc_config_row()
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    async def _raise(config, ws, org):
        raise ConnectionError("IdP unreachable")
    monkeypatch.setattr(idf, "_sync_scim_users", _raise)

    with pytest.raises(HTTPException) as exc:
        await scim_sync("sso_oidc_1", actor=_actor())
    assert exc.value.status_code == 502
    # 验证 UPDATE sync_run SET status='failed' 被调用
    failed_calls = [c for c in conn.calls if "UPDATE" in c[0] and "scim_sync_run" in c[0] and "failed" in c[0]]
    assert len(failed_calls) == 1


@pytest.mark.asyncio
async def test_scim_sync_history_query(monkeypatch):
    """SCIM 同步历史分页查询。"""
    workspace_row = {"id": "wsp_test", "org_id": "org_test", "name": "Test WS"}
    rows = [
        _sync_run_row(id="sync_1", status="completed"),
        _sync_run_row(id="sync_2", status="failed", error_message="timeout"),
    ]
    conn = _SeqConnection(results=[
        _Result(row=workspace_row),  # _workspace_for_actor SELECT
        _Result(rows=rows),          # SELECT sync_run history
        _Result(row={"total": 2}),   # SELECT count
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    result = await scim_sync_history("sso_oidc_1", actor=_actor(), limit=20, offset=0)
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["id"] == "sync_1"
    assert result["items"][1]["id"] == "sync_2"
    assert result["limit"] == 20
    assert result["offset"] == 0


# ============================================================================
# 11. _sync_scim_users 直接测试
# ============================================================================


class _SyncConnection:
    """根据查询内容返回结果的同步连接 mock。"""

    def __init__(self, local_users=None):
        self._local_users = local_users or {}
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Transaction(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "external_id=%s" in query and "FOR UPDATE" in query:
            # SELECT * FROM id_federation_scim_user WHERE workspace_id=%s AND external_id=%s FOR UPDATE
            external_id = params[1] if len(params) > 1 else None
            return _Result(row=self._local_users.get(external_id) if external_id else None)
        if "active=TRUE" in query:
            # SELECT id,external_id FROM id_federation_scim_user WHERE workspace_id=%s AND active=TRUE
            return _Result(rows=list(self._local_users.values()))
        return _Result()

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_sync_scim_users_creates_missing_users(monkeypatch):
    """_sync_scim_users 创建 IdP 中有但本地没有的用户。"""
    config = _oidc_config_row(mapping={"scim_endpoint": "https://idp.example.com/scim", "scim_bearer": "token"})
    remote_users = {
        "Resources": [
            {"externalId": "ext-1", "userName": "alice@example.com", "displayName": "Alice", "active": True},
            {"externalId": "ext-2", "userName": "bob@example.com", "displayName": "Bob", "active": True},
        ]
    }
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(remote_users))
    sync_conn = _SyncConnection(local_users={})
    monkeypatch.setattr(idf, "pool", _Pool(sync_conn))

    report = await _sync_scim_users(config, "wsp_test", "org_test")
    assert report["created"] == 2
    assert report["updated"] == 0
    assert report["deactivated"] == 0
    assert report["total"] == 2
    # 验证 INSERT 被调用（每个新用户 3 个 INSERT: id_user, id_member, scim_user）
    insert_calls = [c for c in sync_conn.calls if "INSERT" in c[0]]
    assert len(insert_calls) == 6


@pytest.mark.asyncio
async def test_sync_scim_users_updates_changed_users(monkeypatch):
    """_sync_scim_users 更新本地已有但属性变更的用户。"""
    config = _oidc_config_row(mapping={"scim_endpoint": "https://idp.example.com/scim", "scim_bearer": "token"})
    local_user_1 = {
        "id": "scu_1", "external_id": "ext-1", "user_id": "usr_1",
        "user_name": "alice@example.com", "display_name": "Old Alice", "active": True,
    }
    remote_users = {
        "Resources": [
            {"externalId": "ext-1", "userName": "alice@example.com", "displayName": "New Alice", "active": True},
        ]
    }
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(remote_users))
    sync_conn = _SyncConnection(local_users={"ext-1": local_user_1})
    monkeypatch.setattr(idf, "pool", _Pool(sync_conn))

    report = await _sync_scim_users(config, "wsp_test", "org_test")
    assert report["created"] == 0
    assert report["updated"] == 1
    assert report["deactivated"] == 0
    # 验证 UPDATE scim_user 被调用（排除 FOR UPDATE 的 SELECT）
    update_calls = [c for c in sync_conn.calls if "UPDATE id_federation_scim_user SET" in c[0]]
    assert len(update_calls) == 1


@pytest.mark.asyncio
async def test_sync_scim_users_deactivates_removed_users(monkeypatch):
    """_sync_scim_users 禁用 IdP 中不存在的本地用户。"""
    config = _oidc_config_row(mapping={"scim_endpoint": "https://idp.example.com/scim", "scim_bearer": "token"})
    local_user = {
        "id": "scu_1", "external_id": "ext-gone", "user_id": "usr_1",
        "user_name": "gone@example.com", "display_name": "Gone", "active": True,
    }
    remote_users = {"Resources": []}  # IdP 中没有用户
    monkeypatch.setattr(idf, "_http_get_json", _mock_http_get_json(remote_users))
    sync_conn = _SyncConnection(local_users={"ext-gone": local_user})
    monkeypatch.setattr(idf, "pool", _Pool(sync_conn))

    report = await _sync_scim_users(config, "wsp_test", "org_test")
    assert report["created"] == 0
    assert report["updated"] == 0
    assert report["deactivated"] == 1
    assert report["total"] == 0
    # 验证 UPDATE SET active=FALSE 被调用
    deactivate_calls = [c for c in sync_conn.calls if "UPDATE" in c[0] and "active=FALSE" in c[0]]
    assert len(deactivate_calls) == 1


@pytest.mark.asyncio
async def test_sync_scim_users_idp_returns_error(monkeypatch):
    """_sync_scim_users IdP 不可达时抛出异常。"""
    config = _oidc_config_row(mapping={"scim_endpoint": "https://idp.example.com/scim", "scim_bearer": "token"})

    def _raise(url, headers=None):
        raise ConnectionError("IdP unreachable")
    monkeypatch.setattr(idf, "_http_get_json", _raise)
    monkeypatch.setattr(idf, "pool", _Pool(_SyncConnection()))

    with pytest.raises(ConnectionError):
        await _sync_scim_users(config, "wsp_test", "org_test")


# ============================================================================
# 12. Schema / 路由注册验证
# ============================================================================


def test_new_tables_in_schema_statements():
    """验证 sso_login_session 和 scim_sync_run 表在 SCHEMA_STATEMENTS 中。"""
    joined = "\n".join(SCHEMA_STATEMENTS)
    assert "CREATE TABLE IF NOT EXISTS id_federation_sso_login_session" in joined
    assert "CREATE TABLE IF NOT EXISTS id_federation_scim_sync_run" in joined
    assert "idx_id_federation_sso_login_session_provider_token" in joined
    assert "idx_id_federation_scim_sync_run_workspace_provider" in joined


def test_new_provider_endpoints_registered_in_router():
    """验证 6 个新端点路径注册在 router 中。"""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(idf.router)
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/identity-federation/providers/{provider_id}/authorize" in paths
    assert "/api/v1/identity-federation/providers/{provider_id}/acs" in paths
    assert "/api/v1/identity-federation/providers/{provider_id}/callback" in paths
    assert "/api/v1/identity-federation/providers/{provider_id}/test" in paths
    assert "/api/v1/identity-federation/providers/{provider_id}/sync" in paths
    assert "/api/v1/identity-federation/providers/{provider_id}/sync-history" in paths


def test_pydantic_models_accept_valid_data():
    """验证新 Pydantic 模型可正常实例化。"""
    from workama_platform.modules.identity_federation import (
        ScimSyncReport,
        ScimSyncHistoryItem,
        SsoLoginResult,
        SsoTestResult,
    )
    login = SsoLoginResult(access_token="tok", user={"id": "u1"}, sso={"provider": "oidc"})
    assert login.token_type == "bearer"
    test_result = SsoTestResult(status="ok", provider="oidc")
    assert test_result.error is None
    report = ScimSyncReport(run_id="r1", status="completed", created=1, updated=0, deactivated=0, total=1)
    assert report.created == 1
    item = ScimSyncHistoryItem(id="r1", workspace_id="w", provider_id="p", status="completed")
    assert item.created_users == 0


# ============================================================================
# 13. SSRF 防护：_http_get_json / _http_post_json / _http_get_raw 出站校验
# ============================================================================


class _MockUrlResponse:
    """模拟 urllib urlopen 返回的上下文管理器响应。"""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _guard_urlopen(*_args, **_kwargs):
    """SSRF 拒绝测试的安全网：若 urlopen 被调用则测试失败。"""
    raise AssertionError("urlopen should not be reached when SSRF validation rejects the URL")


def test_http_get_json_rejects_loopback_url(monkeypatch):
    """_http_get_json 对 loopback 地址 127.0.0.1 抛 ValueError 含 'not allowed'。"""
    monkeypatch.setattr(idf.urllib.request, "urlopen", _guard_urlopen)
    with pytest.raises(ValueError, match="not allowed"):
        _http_get_json("http://127.0.0.1:8080/", None)


def test_http_get_json_rejects_private_ip(monkeypatch):
    """_http_get_json 对私有 IP 段 10.0.0.1 抛 ValueError。"""
    monkeypatch.setattr(idf.urllib.request, "urlopen", _guard_urlopen)
    with pytest.raises(ValueError, match="not allowed"):
        _http_get_json("http://10.0.0.1/", None)


def test_http_get_json_rejects_metadata_endpoint(monkeypatch):
    """_http_get_json 对云元数据端点 169.254.169.254 抛 ValueError。"""
    monkeypatch.setattr(idf.urllib.request, "urlopen", _guard_urlopen)
    with pytest.raises(ValueError, match="not allowed"):
        _http_get_json("http://169.254.169.254/", None)


def test_http_post_json_rejects_unsafe_url(monkeypatch):
    """_http_post_json 对 localhost 抛 ValueError。"""
    monkeypatch.setattr(idf.urllib.request, "urlopen", _guard_urlopen)
    with pytest.raises(ValueError, match="not allowed"):
        _http_post_json("http://localhost/", {}, None)


def test_http_get_raw_rejects_unsafe_url(monkeypatch):
    """_http_get_raw 对 IPv6 loopback ::1 抛 ValueError。"""
    monkeypatch.setattr(idf.urllib.request, "urlopen", _guard_urlopen)
    with pytest.raises(ValueError, match="not allowed"):
        _http_get_raw("http://[::1]/", None)


def test_http_get_json_allows_public_https(monkeypatch):
    """_http_get_json 对公网 https 通过 SSRF 校验并返回解析后的 JSON。"""
    response_body = b'{"sub": "user-1", "email": "alice@example.com"}'
    monkeypatch.setattr(
        idf.urllib.request,
        "urlopen",
        lambda req, timeout=10: _MockUrlResponse(response_body),
    )
    result = _http_get_json("https://example.com/", None)
    assert result["sub"] == "user-1"
    assert result["email"] == "alice@example.com"


# ============================================================================
# 14. v7.171 安全修复：开放重定向 / HTML 转义 / SAML 防重放
# ============================================================================


@pytest.mark.asyncio
async def test_authorize_rejects_redirect_uri_not_in_allowlist(monkeypatch):
    """v7.171：authorize 端点 redirect_uri 不在 redirect_allowlist 时返回 400。"""
    config = _oidc_config_row(redirect_allowlist=["https://console.example.com/oidc/callback"])
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await sso_authorize("sso_oidc_1", redirect_uri="https://evil.example.com/callback")
    assert exc.value.status_code == 400
    assert "redirect_uri not allowed" in exc.value.detail


@pytest.mark.asyncio
async def test_authorize_allows_redirect_uri_with_allowlist_prefix(monkeypatch):
    """v7.171：redirect_uri 以 allowlist 任一项为前缀时通过校验（前缀匹配语义）。"""
    config = _oidc_config_row(redirect_allowlist=["https://console.example.com/"])
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    resp = await sso_authorize("sso_oidc_1", redirect_uri="https://console.example.com/oidc/callback")
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_saml_authorize_escapes_html_values(monkeypatch):
    """v7.171：SAML authorize 生成的 HTML/XML 对 sso_url/entity_id 做转义，防止 XSS。"""
    config = _saml_config_row(
        issuer='urn:test<script>alert(1)</script>',
        mapping={"email_attribute": "email", "sso_url": 'https://idp.example.com/saml?sso="x'},
        redirect_allowlist=["https://console.example.com/saml/acs"],
    )
    conn = _SeqConnection(results=[_Result(row=config)])
    monkeypatch.setattr(idf, "pool", _Pool(conn))

    resp = await sso_authorize("sso_saml_1", redirect_uri="https://console.example.com/saml/acs")
    assert resp.media_type == "text/html"
    body = resp.body.decode()
    # 未转义的恶意 <script> 不应原样出现在 HTML 中
    assert "<script>alert(1)</script>" not in body
    # entity_id 位于 base64 编码的 SAMLRequest 内；解码后应是 XML 转义后的值
    import re
    match = re.search(r'name="SAMLRequest" value="([^"]+)"', body)
    assert match is not None
    decoded_saml_request = base64.b64decode(match.group(1)).decode()
    assert "<script>alert(1)</script>" not in decoded_saml_request
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in decoded_saml_request
    # sso_url 中的双引号被转义为 &quot;，不会跳出 HTML action 属性
    assert 'action="https://idp.example.com/saml?sso=&quot;x"' in body


@pytest.mark.asyncio
async def test_saml_acs_replay_detected_returns_409(monkeypatch):
    """v7.171：ACS 重复 response_id（replay INSERT ON CONFLICT 返回空行）抛 409。"""
    config = _saml_config_row()
    conn = _SeqConnection(results=[
        _Result(row=config),  # _get_provider_config
        _Result(row=None),    # replay INSERT RETURNING → 空（已存在，冲突）
    ])
    monkeypatch.setattr(idf, "pool", _Pool(conn))
    _patch_validate_saml_response(monkeypatch, "replay@example.com", "replay-user", "Replay")

    saml_b64 = _make_saml_response(email="replay@example.com")
    with pytest.raises(HTTPException) as exc:
        await sso_acs("sso_saml_1", saml_response=saml_b64)
    assert exc.value.status_code == 409
    assert "replay" in exc.value.detail.lower()
