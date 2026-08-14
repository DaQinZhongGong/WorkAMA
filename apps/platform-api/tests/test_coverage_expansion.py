"""扩展测试覆盖 - 针对低覆盖模块的深度测试。

本文件为 notifications、subscriptions、skills、connectors、automation、
moderation、design 等模块提供额外的边界情况、错误处理和业务逻辑测试。
"""
from __future__ import annotations

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from workama_platform.core import Actor
from workama_platform.modules.notification.service import (
    should_notify_low_balance,
    low_balance_dedupe_key,
    is_forced_in_app,
    preference_change_allowed,
    retry_delay_seconds,
    RETRY_DELAYS_SECONDS,
    NOTIFICATION_CHANNELS,
    FORCED_IN_APP_PREFIXES,
)
from workama_platform.modules.notification.delivery import (
    send_email,
    send_webhook_mock,
    classify_delivery_error,
)
from workama_platform.modules.subscriptions import (
    PLAN_CATALOG,
    CheckoutRequest,
    ConfirmPaymentRequest,
    OrderCreateRequest,
    OrderCancelRequest,
    PaymentMethodSetupRequest,
    DeletePaymentMethodRequest,
    SubscriptionChangeRequest,
    SubscriptionChangeConfirm,
    InvoiceRequestCreate,
    RefundCreateRequest,
    _plan,
    _owner,
    _require_step_up,
    _order_summary,
    _subscription_summary,
    _mock_signature,
)
from workama_platform.modules.skills import (
    SkillManifest,
    validate_manifest as validate_skill_manifest,
    validate_package_path,
    validate_artifact_reference,
    compute_risk_level,
    redact_sensitive,
    redact_sensitive_text,
    canonical_hash,
    skill_content_hash,
)
from workama_platform.modules.connectors import (
    validate_endpoint,
    validate_manifest,
    validate_controlled_reference,
    default_endpoint,
    normalize_credentials,
    credential_hash,
    canonical_hash as connector_canonical_hash,
    AccessControl,
    SourceItem,
    ConnectorManifest,
    ConnectorCreate,
)
from workama_platform.modules.automation import (
    parse_cron_expression,
    normalize_cron_expression,
    normalize_timezone,
    next_cron_at,
    normalize_automation_target_id,
    ScheduleCreate,
    SchedulePatch,
    _parse_field,
    _cron_matches,
    _redact_payload,
    schedule_view,
    run_view,
)
from workama_platform.modules.moderation import (
    ModerationRule,
    ModerationPolicyCreate,
    ModerationPolicyUpdate,
    evaluate_rules,
    moderate_text,
    _normalize_rule,
    _rule_applies,
)
from workama_platform.modules.design import (
    validate_controlled_ref,
    validate_design_artifact_ref,
    manifest_hash,
    ProjectCreate,
    ProjectPatch,
    DesignJobCreate,
    _content_type,
    _base64url_encode,
    _base64url_decode,
    design_public_key_fingerprint,
    content_credential_signature_payload,
)


# ----------------------------------------------------------------------
# notification/service.py 深度测试
# ----------------------------------------------------------------------


class TestNotificationService:
    """通知服务纯函数测试。"""

    def test_should_notify_low_balance_boundary_values(self):
        """余额阈值边界测试。"""
        assert should_notify_low_balance(Decimal("999.99"), Decimal("1000")) is True
        assert should_notify_low_balance(Decimal("1000"), Decimal("1000")) is False
        assert should_notify_low_balance(Decimal("1000.01"), Decimal("1000")) is False
        assert should_notify_low_balance(Decimal("0"), Decimal("1000")) is True

    def test_low_balance_dedupe_key_format_and_timezone(self):
        """去重键格式和时区处理。"""
        dt_utc = datetime(2026, 7, 25, 10, 30, 0, tzinfo=UTC)
        key = low_balance_dedupe_key("wsp_test", dt_utc)
        assert key == "billing.low_balance:wsp_test:2026-07-25"
        
        # 不同时区应该转换为UTC日期
        dt_shanghai = datetime(2026, 7, 25, 23, 30, 0, tzinfo=UTC)
        key2 = low_balance_dedupe_key("wsp_test", dt_shanghai)
        assert "2026-07-25" in key2

    @pytest.mark.parametrize("event_type,channel,expected", [
        ("security.alert", "in_app", True),
        ("auth.login", "in_app", True),
        ("billing.low_balance", "in_app", True),
        ("automation.run.succeeded", "in_app", False),
        ("security.alert", "email", False),
        ("custom.event", "in_app", False),
    ])
    def test_is_forced_in_app_combinations(self, event_type, channel, expected):
        """强制应用内通知的组合测试。"""
        assert is_forced_in_app(event_type, channel) is expected

    @pytest.mark.parametrize("event_type,channel,enabled,expected", [
        ("security.alert", "in_app", False, False),  # 强制事件不允许禁用
        ("security.alert", "in_app", True, True),
        ("custom.event", "in_app", False, True),  # 非强制事件允许禁用
        ("custom.event", "email", False, True),
    ])
    def test_preference_change_allowed_combinations(
        self, event_type, channel, enabled, expected
    ):
        """偏好设置变更允许性测试。"""
        assert preference_change_allowed(event_type, channel, enabled) is expected

    def test_retry_delay_seconds_boundary_and_clamping(self):
        """重试延迟边界和限制。"""
        assert retry_delay_seconds(1) == 60
        assert retry_delay_seconds(2) == 300
        assert retry_delay_seconds(3) == 1800
        assert retry_delay_seconds(4) == 7200
        assert retry_delay_seconds(5) == 43200
        # 超出范围应该被限制
        assert retry_delay_seconds(0) == 60
        assert retry_delay_seconds(6) == 43200
        assert retry_delay_seconds(100) == 43200

    def test_notification_channels_constant(self):
        """通知渠道常量验证。"""
        assert "in_app" in NOTIFICATION_CHANNELS
        assert "email" in NOTIFICATION_CHANNELS
        assert "webhook" in NOTIFICATION_CHANNELS
        assert len(NOTIFICATION_CHANNELS) == 3

    def test_forced_in_app_prefixes_coverage(self):
        """强制应用内前缀覆盖。"""
        assert "security." in FORCED_IN_APP_PREFIXES
        assert "auth." in FORCED_IN_APP_PREFIXES
        assert "billing." in FORCED_IN_APP_PREFIXES


# ----------------------------------------------------------------------
# notification/delivery.py 深度测试
# ----------------------------------------------------------------------


class TestNotificationDelivery:
    """通知交付纯函数测试。"""

    def test_send_email_mock_deterministic(self):
        """模拟邮件发送的确定性。"""
        result1 = send_email("test@example.com", "Title", "Summary", mock=True)
        result2 = send_email("test@example.com", "Title", "Summary", mock=True)
        assert result1 == result2
        assert result1.startswith("mock-email:")
        assert len(result1) > 20

    def test_send_email_mock_different_inputs(self):
        """不同输入产生不同结果。"""
        result1 = send_email("test1@example.com", "Title", "Summary", mock=True)
        result2 = send_email("test2@example.com", "Title", "Summary", mock=True)
        assert result1 != result2

    def test_send_webhook_mock_signature_and_structure(self):
        """Webhook模拟签名和结构。"""
        result = send_webhook_mock(
            "mock://webhook/test",
            "secret_key",
            {"event": "test"},
            "idempotency_123",
            occurred_at=datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC),
        )
        assert result["status_code"] == 202
        assert result["provider_id"].startswith("mock-webhook:")
        assert "signature" in result
        assert result["signature"].startswith("t=")
        assert "v1=" in result["signature"]
        assert result["body"] == '{"event":"test"}'

    def test_send_webhook_mock_rejects_non_mock_url(self):
        """Webhook模拟拒绝非mock URL。"""
        with pytest.raises(ValueError, match="mock_webhook_url_required"):
            send_webhook_mock("https://example.com", "secret", {}, "key")

    @pytest.mark.parametrize("exception,expected", [
        (TimeoutError("timeout"), "transient_provider_error"),
        (asyncio.TimeoutError("timeout"), "transient_provider_error"),
        (OSError("network"), "transient_provider_error"),
        (smtplib.SMTPException("smtp"), "transient_provider_error"),
        (ValueError("custom error"), "custom error"),
        (RuntimeError(""), "RuntimeError"),
    ])
    def test_classify_delivery_error_types(self, exception, expected):
        """交付错误分类测试。"""
        result = classify_delivery_error(exception)
        assert result == expected


# ----------------------------------------------------------------------
# subscriptions.py 深度测试
# ----------------------------------------------------------------------


class TestSubscriptions:
    """订阅模块纯函数和模型测试。"""

    def test_plan_catalog_structure_and_quotas(self):
        """计划目录结构和配额验证。"""
        assert "free" in PLAN_CATALOG
        assert "pro" in PLAN_CATALOG
        assert "team" in PLAN_CATALOG
        assert "enterprise" in PLAN_CATALOG
        
        free_plan = PLAN_CATALOG["free"]
        assert free_plan["monthly_price"] == 0
        assert free_plan["quotas"]["members"] == 1
        assert free_plan["quotas"]["workspaces"] == 1
        
        enterprise_plan = PLAN_CATALOG["enterprise"]
        assert enterprise_plan["quotas"]["members"] is None

    def test_plan_helper_validates_and_returns(self):
        """_plan辅助函数验证和返回。"""
        plan = _plan("pro")
        assert plan["name"] == "Pro"
        assert plan["monthly_price"] == 99
        
        with pytest.raises(HTTPException, match="Unknown subscription plan"):
            _plan("invalid_plan")

    def test_owner_helper_enforces_role(self):
        """_owner辅助函数强制角色检查。"""
        owner_actor = Actor(
            user_id="usr_1",
            workspace_id="wsp_1",
            org_id="org_1",
            role="owner",
            email="owner@test.com",
            display_name="Owner",
            onboarding_completed=True,
        )
        _owner(owner_actor)  # 应该通过
        
        member_actor = Actor(
            user_id="usr_2",
            workspace_id="wsp_1",
            org_id="org_1",
            role="member",
            email="member@test.com",
            display_name="Member",
            onboarding_completed=True,
        )
        with pytest.raises(HTTPException, match="Organization owner required"):
            _owner(member_actor)

    def test_require_step_up_enforces_auth_strength(self):
        """_require_step_up强制认证强度。"""
        weak_actor = Actor(
            user_id="usr_1",
            workspace_id="wsp_1",
            org_id="org_1",
            role="owner",
            email="owner@test.com",
            display_name="Owner",
            onboarding_completed=True,
            auth_strength=1,
        )
        with pytest.raises(HTTPException, match="step-up authentication"):
            _require_step_up(weak_actor, "Test operation")
        
        strong_actor = Actor(
            user_id="usr_1",
            workspace_id="wsp_1",
            org_id="org_1",
            role="owner",
            email="owner@test.com",
            display_name="Owner",
            onboarding_completed=True,
            auth_strength=2,
        )
        _require_step_up(strong_actor, "Test operation")  # 应该通过

    def test_order_summary_extracts_fields(self):
        """_order_summary提取字段。"""
        row = {
            "id": "ord_123",
            "order_no": "WMA-123",
            "workspace_id": "wsp_1",
            "order_type": "subscription",
            "plan_code": "pro",
            "amount": Decimal("99.00"),
            "currency": "CNY",
            "credits": Decimal("0"),
            "price_snapshot": {},
            "tax_snapshot": {},
            "discount_snapshot": {},
            "status": "pending",
            "expires_at": datetime.now(UTC),
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        summary = _order_summary(row)
        assert summary["id"] == "ord_123"
        assert summary["order_no"] == "WMA-123"
        assert summary["amount"] == Decimal("99.00")

    def test_subscription_summary_extracts_fields(self):
        """_subscription_summary提取字段。"""
        row = {
            "id": "sub_123",
            "workspace_id": "wsp_1",
            "plan_code": "pro",
            "plan_name": "Pro",
            "status": "active",
            "current_period_start": datetime.now(UTC),
            "current_period_end": datetime.now(UTC) + timedelta(days=30),
            "cancel_at_period_end": False,
            "pending_plan_code": None,
            "monthly_price": 99,
            "currency": "CNY",
            "quotas": {},
        }
        summary = _subscription_summary(row)
        assert summary["id"] == "sub_123"
        assert summary["plan_code"] == "pro"
        assert summary["status"] == "active"

    def test_checkout_request_validates_fields(self):
        """CheckoutRequest字段验证。"""
        valid = CheckoutRequest(
            plan_code="pro",
            provider="mock",
            idempotency_key="checkout_12345678",
        )
        assert valid.plan_code == "pro"
        
        with pytest.raises(ValidationError):
            CheckoutRequest(
                plan_code="",  # 太短
                provider="mock",
                idempotency_key="checkout_12345678",
            )
        
        with pytest.raises(ValidationError):
            CheckoutRequest(
                plan_code="pro",
                provider="mock",
                idempotency_key="short",  # 太短
            )

    def test_order_create_request_validates_order_type(self):
        """OrderCreateRequest验证订单类型。"""
        # 订阅订单需要plan_code
        with pytest.raises(ValueError, match="plan_code is required"):
            OrderCreateRequest(
                order_type="subscription",
                idempotency_key="order_12345678",
            )
        
        # 积分订单需要amount或credits
        with pytest.raises(ValueError, match="amount or credits is required"):
            OrderCreateRequest(
                order_type="credits",
                idempotency_key="order_12345678",
            )
        
        # 订阅订单不能有amount
        with pytest.raises(ValueError, match="subscription orders use the selected plan price"):
            OrderCreateRequest(
                order_type="subscription",
                plan_code="pro",
                amount=Decimal("100"),
                idempotency_key="order_12345678",
            )

    def test_refund_create_request_validates_target(self):
        """RefundCreateRequest验证目标。"""
        with pytest.raises(ValueError, match="payment_id or order_id is required"):
            RefundCreateRequest(
                reason="duplicate charge",
                idempotency_key="refund_12345678",
            )
        
        valid = RefundCreateRequest(
            payment_id="pay_123",
            reason="duplicate charge",
            idempotency_key="refund_12345678",
        )
        assert valid.payment_id == "pay_123"

    def test_mock_signature_is_deterministic(self):
        """模拟签名是确定性的。"""
        payload = b'{"test":"data"}'
        sig1 = _mock_signature(payload)
        sig2 = _mock_signature(payload)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA256十六进制长度


# ----------------------------------------------------------------------
# skills.py 深度测试
# ----------------------------------------------------------------------


class TestSkills:
    """技能模块纯函数和模型测试。"""

    def test_validate_package_path_accepts_safe_paths(self):
        """validate_package_path接受安全路径。"""
        assert validate_package_path("skill.yaml") == "skill.yaml"
        assert validate_package_path("prompt.md") == "prompt.md"
        assert validate_package_path("scripts/test.py") == "scripts/test.py"
        assert validate_package_path("resources/data.json") == "resources/data.json"

    @pytest.mark.parametrize("path", [
        "../escape.py",
        "/absolute/path.py",
        "scripts/../escape.py",
        "scripts//double.py",
        "scripts/./current.py",
        "invalid/file.py",  # 不在scripts/或resources/下
        "scripts/test.py\x00",  # 控制字符
        "scripts\\backslash.py",  # 反斜杠
    ])
    def test_validate_package_path_rejects_unsafe_paths(self, path):
        """validate_package_path拒绝不安全路径。"""
        with pytest.raises(ValueError):
            validate_package_path(path)

    def test_validate_artifact_reference_mock_skill(self):
        """validate_artifact_reference处理mock://skill引用。"""
        ref = validate_artifact_reference("mock://skill/publisher/name/1.0.0")
        assert ref.kind == "mock"
        assert ref.publisher == "publisher"
        assert ref.name == "name"
        assert ref.version == "1.0.0"

    def test_validate_artifact_reference_local_artifact(self):
        """validate_artifact_reference处理local://artifact引用。"""
        ref = validate_artifact_reference("local://artifact/art_123")
        assert ref.kind == "local"
        assert ref.artifact_id == "art_123"

    @pytest.mark.parametrize("ref", [
        "https://evil.com/skill",
        "mock://skill/publisher/name/1.0.0?token=secret",
        "mock://skill/publisher/name/1.0.0#fragment",
        "mock://skill/../escape",
        "invalid://scheme",
    ])
    def test_validate_artifact_reference_rejects_unsafe(self, ref):
        """validate_artifact_reference拒绝不安全引用。"""
        with pytest.raises(ValueError):
            validate_artifact_reference(ref)

    def test_validate_manifest_normalizes_and_validates(self):
        """validate_manifest规范化和验证。"""
        manifest = {
            "schema_version": 1,
            "name": "test-skill",
            "version": "1.0.0",
            "publisher": "test",
            "required_tools": ["web.search", "web.search"],  # 重复
            "permissions": [],
            "files": ["skill.yaml", "prompt.md"],
            "entrypoint": "prompt.md",
        }
        result = validate_skill_manifest(manifest)
        assert result["required_tools"] == ["web.search"]  # 去重
        assert result["files"] == sorted(result["files"])

    def test_validate_manifest_rejects_sensitive_fields(self):
        """validate_manifest拒绝敏感字段。"""
        manifest = {
            "schema_version": 1,
            "name": "test-skill",
            "version": "1.0.0",
            "publisher": "test",
            "api_key": "secret123",  # 敏感字段
            "files": ["skill.yaml", "prompt.md"],
            "entrypoint": "prompt.md",
        }
        with pytest.raises(ValueError, match="secret-like field"):
            validate_skill_manifest(manifest)

    def test_compute_risk_level_from_permissions(self):
        """compute_risk_level从权限计算风险。"""
        manifest = {
            "schema_version": 1,
            "name": "test-skill",
            "version": "1.0.0",
            "publisher": "test",
            "required_tools": [],
            "permissions": ["secret:access"],  # critical风险
            "files": ["skill.yaml", "prompt.md"],
            "entrypoint": "prompt.md",
        }
        risk = compute_risk_level(manifest)
        assert risk == "critical"
        
        manifest["permissions"] = ["network:request"]  # high风险
        risk = compute_risk_level(manifest)
        assert risk == "high"
        
        manifest["permissions"] = ["data:write"]  # medium风险
        risk = compute_risk_level(manifest)
        assert risk == "medium"

    def test_redact_sensitive_removes_secrets(self):
        """redact_sensitive移除秘密。"""
        data = {
            "api_key": "secret123",
            "normal_field": "visible",
            "nested": {
                "password": "secret456",
                "other": "visible",
            },
        }
        result = redact_sensitive(data)
        assert result["api_key"] == "<redacted>"
        assert result["normal_field"] == "visible"
        assert result["nested"]["password"] == "<redacted>"
        assert result["nested"]["other"] == "visible"

    def test_redact_sensitive_text_masks_patterns(self):
        """redact_sensitive_text掩码模式。"""
        text = "Bearer token123 and api_key=value456"
        result = redact_sensitive_text(text)
        assert "token123" not in result
        assert "value456" not in result
        assert "<redacted>" in result

    def test_canonical_hash_is_deterministic(self):
        """canonical_hash是确定性的。"""
        data = {"b": 2, "a": 1}
        hash1 = canonical_hash(data)
        hash2 = canonical_hash(data)
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256十六进制

    def test_skill_content_hash_combines_ref_and_manifest(self):
        """skill_content_hash组合引用和清单。"""
        manifest = {
            "schema_version": 1,
            "name": "test-skill",
            "version": "1.0.0",
            "publisher": "test",
            "required_tools": [],
            "permissions": [],
            "files": ["skill.yaml", "prompt.md"],
            "entrypoint": "prompt.md",
        }
        hash1 = skill_content_hash("mock://skill/test/name/1.0.0", manifest)
        hash2 = skill_content_hash("mock://skill/test/name/1.0.0", manifest)
        assert hash1 == hash2


# ----------------------------------------------------------------------
# connectors.py 深度测试
# ----------------------------------------------------------------------


class TestConnectors:
    """连接器模块纯函数和模型测试。"""

    def test_validate_endpoint_mock_provider(self):
        """validate_endpoint处理mock提供者。"""
        endpoint = validate_endpoint("mock://connector/wiki", "mock")
        assert endpoint == "mock://connector/wiki"

    @pytest.mark.parametrize("endpoint,provider", [
        ("https://evil.com", "mock"),
        ("mock://connector/wiki?token=secret", "mock"),
        ("mock://connector/../escape", "mock"),
        ("file:///etc/passwd", "local"),
        ("local://artifact/art_1#fragment", "local"),
    ])
    def test_validate_endpoint_rejects_unsafe(self, endpoint, provider):
        """validate_endpoint拒绝不安全端点。"""
        with pytest.raises(ValueError):
            validate_endpoint(endpoint, provider)

    def test_validate_controlled_reference_accepts_safe(self):
        """validate_controlled_reference接受安全引用。"""
        ref = validate_controlled_reference("local://artifact/art_123")
        assert ref == "local://artifact/art_123"

    @pytest.mark.parametrize("ref", [
        "local://artifact/../escape",
        "local://artifact/art_1?query=1",
        "local://artifact/art_1#fragment",
        "https://evil.com",
        "local://artifact/C:\\secret",
    ])
    def test_validate_controlled_reference_rejects_unsafe(self, ref):
        """validate_controlled_reference拒绝不安全引用。"""
        with pytest.raises(ValueError):
            validate_controlled_reference(ref)

    def test_default_endpoint_generates_safe_urls(self):
        """default_endpoint生成安全URL。"""
        endpoint = default_endpoint("mock", "Test Connector")
        assert endpoint.startswith("mock://connector/")
        assert "test-connector" in endpoint
        
        endpoint = default_endpoint("local", "Test Connector")
        assert endpoint.startswith("local://artifact/")

    def test_normalize_credentials_validates_keys(self):
        """normalize_credentials验证键。"""
        creds = normalize_credentials({"client_id": "id123"})
        assert creds == {"client_id": "id123"}
        
        with pytest.raises(ValueError, match="unsupported credential field"):
            normalize_credentials({"invalid_key": "value"})

    def test_normalize_credentials_rejects_auth_mode_none(self):
        """normalize_credentials拒绝auth_mode=none时的凭证。"""
        with pytest.raises(ValueError, match="not allowed when auth_mode is none"):
            normalize_credentials({"client_id": "id"}, auth_mode="none")

    def test_credential_hash_is_deterministic(self):
        """credential_hash是确定性的。"""
        creds = {"client_id": "id123"}
        hash1 = credential_hash(creds)
        hash2 = credential_hash(creds)
        assert hash1 == hash2
        assert hash1 is not None

    def test_access_control_normalizes_principals(self):
        """AccessControl规范化主体。"""
        acl = AccessControl(
            users=["user1", "user1"],  # 重复
            groups=["group1"],
            roles=["owner"],
        )
        normalized = acl.normalized()
        assert normalized["allow_users"] == ["user1"]  # 去重
        assert normalized["allow_groups"] == ["group1"]
        assert normalized["allow_roles"] == ["owner"]

    def test_source_item_validates_payload(self):
        """SourceItem验证有效载荷。"""
        item = SourceItem(
            source_id="test:1",
            source_version="1",
            title="Test",
            content="Content",
        )
        assert item.source_id == "test:1"
        
        # 活跃源项需要content或content_ref
        with pytest.raises(ValueError, match="requires content or content_ref"):
            SourceItem(
                source_id="test:1",
                source_version="1",
                title="Test",
            )

    def test_connector_manifest_validates_documents(self):
        """ConnectorManifest验证文档。"""
        manifest = ConnectorManifest(
            schema_version=1,
            description="Test",
            documents=[
                SourceItem(
                    source_id="test:1",
                    source_version="1",
                    title="Test",
                    content="Content",
                )
            ],
        )
        assert len(manifest.documents) == 1

    def test_connector_create_validates_endpoint(self):
        """ConnectorCreate验证端点。"""
        connector = ConnectorCreate(
            name="Test Connector",
            provider="mock",
            auth_mode="none",
        )
        assert connector.name == "Test Connector"
        assert connector.enabled is True


# ----------------------------------------------------------------------
# automation.py 深度测试
# ----------------------------------------------------------------------


class TestAutomation:
    """自动化模块纯函数和模型测试。"""

    def test_parse_field_supports_ranges_and_steps(self):
        """_parse_field支持范围和步长。"""
        field = _parse_field("*/15", 0, 59)
        assert 0 in field
        assert 15 in field
        assert 30 in field
        assert 45 in field
        
        field = _parse_field("1-5", 0, 59)
        assert field == frozenset({1, 2, 3, 4, 5})
        
        field = _parse_field("1-10/2", 0, 59)
        assert field == frozenset({1, 3, 5, 7, 9})

    @pytest.mark.parametrize("expression", [
        "* *",  # 字段太少
        "* * * * * *",  # 字段太多
        "61 * * * *",  # 超出范围
        "*/0 * * * *",  # 步长为0
        "a * * * *",  # 非数字
    ])
    def test_parse_cron_expression_rejects_invalid(self, expression):
        """parse_cron_expression拒绝无效表达式。"""
        with pytest.raises(ValueError):
            parse_cron_expression(expression)

    def test_normalize_cron_expression_trims_whitespace(self):
        """normalize_cron_expression修剪空白。"""
        normalized = normalize_cron_expression("  */15   9-17   *   *   1-5  ")
        assert normalized == "*/15 9-17 * * 1-5"

    def test_normalize_timezone_validates_zones(self):
        """normalize_timezone验证时区。"""
        assert normalize_timezone("UTC") == "UTC"
        assert normalize_timezone("Asia/Shanghai") == "Asia/Shanghai"
        
        with pytest.raises(ValueError, match="not supported"):
            normalize_timezone("Invalid/Zone")
        
        with pytest.raises(ValueError, match="required"):
            normalize_timezone("")

    def test_next_cron_at_is_timezone_aware(self):
        """next_cron_at是时区感知的。"""
        after = datetime(2026, 7, 15, 0, 0, 0, tzinfo=UTC)
        result = next_cron_at("0 9 * * *", after, "Asia/Shanghai")
        assert result.tzinfo == UTC
        assert result > after

    def test_cron_matches_day_or_semantics(self):
        """_cron_matches支持day-or语义。"""
        fields = parse_cron_expression("0 0 15 * 1")  # 15号或周一
        # 15号应该匹配
        dt_15th = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
        assert _cron_matches(dt_15th, fields) is True
        
        # 周一应该匹配
        dt_monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)  # 2026-07-13是周一
        assert _cron_matches(dt_monday, fields) is True
        
        # 其他日期不匹配
        dt_other = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)  # 周二
        assert _cron_matches(dt_other, fields) is False

    def test_normalize_automation_target_id_rejects_external(self):
        """normalize_automation_target_id拒绝外部URL。"""
        assert normalize_automation_target_id("wfl_123") == "wfl_123"
        
        with pytest.raises(ValueError):
            normalize_automation_target_id("https://example.com/workflow")
        
        with pytest.raises(ValueError):
            normalize_automation_target_id("mock://workflow")

    def test_redact_payload_removes_sensitive_keys(self):
        """_redact_payload移除敏感键。"""
        payload = {
            "authorization": "Bearer token",
            "api_key": "secret",
            "normal": "visible",
        }
        redacted = _redact_payload(payload)
        assert redacted["authorization"] == "<redacted>"
        assert redacted["api_key"] == "<redacted>"
        assert redacted["normal"] == "visible"

    def test_schedule_view_never_returns_webhook_secret(self):
        """schedule_view不返回webhook_secret除非明确提供。"""
        row = {
            "id": "sch_123",
            "workspace_id": "wsp_1",
            "name": "Test Schedule",
            "trigger_type": "webhook",
            "cron_expression": None,
            "timezone": "UTC",
            "target_type": "workflow",
            "target_id": "wfl_123",
            "payload": {"api_key": "secret"},
            "status": "active",
            "enabled": True,
        }
        view = schedule_view(row)
        assert "webhook_secret" not in view
        assert view["payload"]["api_key"] == "<redacted>"
        
        # 明确提供时应该包含
        view_with_secret = schedule_view(row, webhook_secret="secret123")
        assert view_with_secret["webhook_secret"] == "secret123"

    def test_run_view_redacts_payload(self):
        """run_view规范化有效载荷。"""
        row = {
            "id": "run_123",
            "schedule_id": "sch_123",
            "workspace_id": "wsp_1",
            "trigger_source": "cron",
            "idempotency_key": "key_123",
            "status": "succeeded",
            "payload": {"password": "secret"},
        }
        view = run_view(row)
        assert view["payload"]["password"] == "<redacted>"

    def test_schedule_create_validates_trigger(self):
        """ScheduleCreate验证触发器。"""
        # cron触发器需要cron_expression
        with pytest.raises(ValueError, match="cron_expression is required"):
            ScheduleCreate(
                name="Test",
                trigger_type="cron",
                target_id="wfl_123",
            )
        
        # webhook触发器不能有cron_expression
        with pytest.raises(ValueError, match="only valid for cron"):
            ScheduleCreate(
                name="Test",
                trigger_type="webhook",
                cron_expression="* * * * *",
                target_id="wfl_123",
            )

    def test_schedule_patch_validates_fields(self):
        """SchedulePatch验证字段。"""
        # cron_expression不能清空
        with pytest.raises(ValueError, match="cannot be cleared"):
            SchedulePatch(cron_expression=None)


# ----------------------------------------------------------------------
# moderation.py 深度测试
# ----------------------------------------------------------------------


class TestModeration:
    """内容审查模块纯函数和模型测试。"""

    def test_moderation_rule_validates_pattern(self):
        """ModerationRule验证模式。"""
        rule = ModerationRule(
            kind="sensitive_word",
            pattern="badword",
            action="block",
        )
        assert rule.pattern == "badword"
        
        # sensitive_word需要pattern
        with pytest.raises(ValueError, match="pattern is required"):
            ModerationRule(kind="sensitive_word", action="block")
        
        # regex需要有效正则
        with pytest.raises(ValueError, match="invalid regex"):
            ModerationRule(kind="regex", pattern="[invalid", action="block")

    def test_moderation_rule_validates_length(self):
        """ModerationRule验证长度规则。"""
        rule = ModerationRule(
            kind="length",
            max_length=1000,
            action="block",
        )
        assert rule.max_length == 1000
        
        # length规则需要max_length
        with pytest.raises(ValueError, match="max_length is required"):
            ModerationRule(kind="length", action="block")
        
        # 非length规则不能有max_length
        with pytest.raises(ValueError, match="only valid for length"):
            ModerationRule(kind="sensitive_word", pattern="test", max_length=100)

    def test_normalize_rule_normalizes_kind(self):
        """_normalize_rule规范化kind。"""
        rule = {"kind": "word", "pattern": "test"}
        normalized = _normalize_rule(rule, 0)
        assert normalized["kind"] == "sensitive_word"
        
        rule = {"kind": "keyword", "pattern": "test"}
        normalized = _normalize_rule(rule, 0)
        assert normalized["kind"] == "sensitive_word"

    def test_normalize_rule_rejects_unsupported(self):
        """_normalize_rule拒绝不支持的类型。"""
        with pytest.raises(ValueError, match="unsupported"):
            _normalize_rule({"kind": "invalid"}, 0)

    def test_rule_applies_checks_direction_and_enabled(self):
        """_rule_applies检查方向和启用状态。"""
        rule = {"direction": "input", "enabled": True}
        assert _rule_applies(rule, "input") is True
        assert _rule_applies(rule, "output") is False
        
        rule = {"direction": "both", "enabled": True}
        assert _rule_applies(rule, "input") is True
        assert _rule_applies(rule, "output") is True
        
        rule = {"direction": "input", "enabled": False}
        assert _rule_applies(rule, "input") is False

    def test_evaluate_rules_blocks_sensitive_words(self):
        """evaluate_rules阻止敏感词。"""
        rules = [
            ModerationRule(kind="sensitive_word", pattern="badword", action="block")
        ]
        decision = evaluate_rules("this contains badword here", rules, "input")
        assert decision.action == "block"
        assert decision.blocked is True
        assert decision.text is None
        assert "badword" in decision.matches

    def test_evaluate_rules_masks_sensitive_words(self):
        """evaluate_rules掩码敏感词。"""
        rules = [
            ModerationRule(
                kind="sensitive_word",
                pattern="badword",
                action="mask",
                replacement="***",
            )
        ]
        decision = evaluate_rules("this contains badword here", rules, "input")
        assert decision.action == "mask"
        assert decision.masked is True
        assert "badword" not in decision.text
        assert "***" in decision.text

    def test_evaluate_rules_handles_length(self):
        """evaluate_rules处理长度规则。"""
        rules = [
            ModerationRule(kind="length", max_length=10, action="block")
        ]
        decision = evaluate_rules("short", rules, "input")
        assert decision.action == "allow"
        
        decision = evaluate_rules("this is a very long text", rules, "input")
        assert decision.action == "block"

    def test_evaluate_rules_respects_priority(self):
        """evaluate_rules尊重优先级。"""
        rules = [
            ModerationRule(
                kind="sensitive_word",
                pattern="test",
                action="log",
                priority=100,
            ),
            ModerationRule(
                kind="sensitive_word",
                pattern="test",
                action="block",
                priority=200,
            ),
        ]
        decision = evaluate_rules("test", rules, "input")
        assert decision.action == "block"  # 高优先级胜出

    def test_moderation_policy_create_validates_rules(self):
        """ModerationPolicyCreate验证规则。"""
        policy = ModerationPolicyCreate(
            name="Test Policy",
            rules=[
                ModerationRule(kind="sensitive_word", pattern="test", action="block")
            ],
        )
        assert len(policy.rules) == 1

    def test_moderation_policy_update_requires_change(self):
        """ModerationPolicyUpdate要求变更。"""
        with pytest.raises(ValueError, match="At least one policy field"):
            ModerationPolicyUpdate()
        
        update = ModerationPolicyUpdate(name="New Name")
        assert update.name == "New Name"


# ----------------------------------------------------------------------
# design.py 深度测试
# ----------------------------------------------------------------------


class TestDesign:
    """设计模块纯函数和模型测试。"""

    def test_validate_controlled_ref_accepts_safe(self):
        """validate_controlled_ref接受安全引用。"""
        ref = validate_controlled_ref("mock://prompt/reference-1")
        assert ref == "mock://prompt/reference-1"
        
        ref = validate_controlled_ref("local://artifact/source_1")
        assert ref == "local://artifact/source_1"

    @pytest.mark.parametrize("ref", [
        "https://evil.com/image.png",
        "file:///etc/passwd",
        "local://artifact/../escape",
        "local://artifact/C:\\secret",
    ])
    def test_validate_controlled_ref_rejects_unsafe(self, ref):
        """validate_controlled_ref拒绝不安全引用。"""
        with pytest.raises(ValueError):
            validate_controlled_ref(ref)

    def test_validate_design_artifact_ref_accepts_safe(self):
        """validate_design_artifact_ref接受安全引用。"""
        ref = validate_design_artifact_ref("design://artifact/dsgasset_ABC-123")
        assert ref == "design://artifact/dsgasset_ABC-123"

    @pytest.mark.parametrize("ref", [
        "https://evil.example/design.png",
        "design://artifact/../escape",
        "design://artifact/dsgasset_ABC/other",
        "design://artifact/dsgasset_ABC\\secret",
    ])
    def test_validate_design_artifact_ref_rejects_unsafe(self, ref):
        """validate_design_artifact_ref拒绝不安全引用。"""
        with pytest.raises(ValueError):
            validate_design_artifact_ref(ref)

    def test_manifest_hash_is_deterministic(self):
        """manifest_hash是确定性的。"""
        manifest = {"version": 1, "generator": "test"}
        hash1 = manifest_hash(manifest)
        hash2 = manifest_hash(manifest)
        assert hash1 == hash2
        assert len(hash1) == 64

    def test_content_type_maps_formats(self):
        """_content_type映射格式。"""
        assert _content_type("json") == "application/json"
        assert _content_type("svg") == "image/svg+xml"
        assert _content_type("png") == "image/png"
        assert _content_type("jpeg") == "image/jpeg"

    def test_base64url_encode_decode_roundtrip(self):
        """base64url编码/解码往返。"""
        data = b"test data"
        encoded = _base64url_encode(data)
        decoded = _base64url_decode(encoded)
        assert decoded == data

    def test_base64url_decode_rejects_invalid(self):
        """base64url_decode拒绝无效值。"""
        with pytest.raises(ValueError, match="not valid base64url"):
            _base64url_decode("invalid!!!")

    def test_design_public_key_fingerprint_validates_length(self):
        """design_public_key_fingerprint验证长度。"""
        key = b"x" * 32
        fingerprint = design_public_key_fingerprint(key)
        assert len(fingerprint) == 64
        
        with pytest.raises(ValueError, match="32 bytes"):
            design_public_key_fingerprint(b"short")

    def test_content_credential_signature_payload_is_deterministic(self):
        """content_credential_signature_payload是确定性的。"""
        payload1 = content_credential_signature_payload(
            workspace_id="wsp_1",
            asset_id="asset_1",
            content_sha256="abc123",
            claim_hash="def456",
            parents=[],
            operation="generate",
        )
        payload2 = content_credential_signature_payload(
            workspace_id="wsp_1",
            asset_id="asset_1",
            content_sha256="abc123",
            claim_hash="def456",
            parents=[],
            operation="generate",
        )
        assert payload1 == payload2

    def test_project_create_validates_slug(self):
        """ProjectCreate验证slug。"""
        project = ProjectCreate(name="Test Project", slug="test-project")
        assert project.slug == "test-project"
        
        with pytest.raises(ValueError, match="slug is invalid"):
            ProjectCreate(name="Test Project", slug="Invalid Slug!")

    def test_project_create_normalizes_name(self):
        """ProjectCreate规范化名称。"""
        project = ProjectCreate(name="  Test   Project  ")
        assert project.name == "Test Project"

    def test_design_job_create_validates_operation(self):
        """DesignJobCreate验证操作。"""
        # edit操作需要source_refs或parent_asset_ids
        with pytest.raises(ValueError, match="edit requires"):
            DesignJobCreate(operation="edit", prompt="edit this")
        
        # generate操作不需要
        job = DesignJobCreate(operation="generate", prompt="generate this")
        assert job.operation == "generate"

    def test_design_job_create_validates_sources(self):
        """DesignJobCreate验证源。"""
        job = DesignJobCreate(
            operation="generate",
            prompt="test",
            source_refs=["mock://source/a", "mock://source/a"],  # 重复
        )
        assert job.source_refs == ["mock://source/a"]  # 去重
        
        with pytest.raises(ValueError):
            DesignJobCreate(
                operation="generate",
                prompt="test",
                source_refs=["https://evil.com"],
            )

    def test_design_job_create_normalizes_prompt(self):
        """DesignJobCreate规范化提示词。"""
        job = DesignJobCreate(operation="generate", prompt="  test prompt  ")
        assert job.prompt == "test prompt"
        
        with pytest.raises(ValueError, match="control character"):
            DesignJobCreate(operation="generate", prompt="test\x00prompt")
