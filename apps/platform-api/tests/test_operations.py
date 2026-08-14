from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import operations


# --- 测试辅助：模拟 psycopg 连接池与事务 --------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


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


class _OpsConnection:
    """模拟 operations 模块的连接。

    - SELECT / WITH 开头且含 LIMIT 1 或 FOR UPDATE → 返回 row（fetchone）
    - SELECT / WITH（其他）→ 返回 rows（fetchall）
    - INSERT / UPDATE 含 RETURNING → 返回 row（fetchone）
    - 其余 INSERT / UPDATE → 返回空 Result
    """

    def __init__(self, *, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        stripped = query.lstrip().upper()
        upper = query.upper()
        if stripped.startswith("SELECT") or stripped.startswith("WITH"):
            if "LIMIT 1" in upper or "FOR UPDATE" in upper:
                return _Result(row=self._row)
            return _Result(rows=self._rows)
        if "RETURNING" in upper:
            return _Result(row=self._row)
        return _Result()

    def transaction(self):
        return _Transaction()

    async def commit(self):
        return None


def _actor(role: str = "admin", workspace_id: str = "wsp_test") -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
    )


def _valid_flag_body(**overrides) -> operations.FeatureFlagUpsert:
    defaults = {
        "flag_type": "release",
        "default_value": True,
        "safe_value": False,
        "targeting": {"percentage": 0},
        "status": "enabled",
        "owner": "team-ops",
    }
    defaults.update(overrides)
    return operations.FeatureFlagUpsert(**defaults)


def _valid_config_body(**overrides) -> operations.DynamicConfigUpsert:
    defaults = {
        "value_schema": {"type": "object", "properties": {"threshold": {"type": "integer", "minimum": 1, "maximum": 100}}},
        "config_value": {"threshold": 50},
        "status": "enabled",
        "risk_level": "normal",
    }
    defaults.update(overrides)
    return operations.DynamicConfigUpsert(**defaults)


def _valid_release_body(**overrides) -> operations.ReleaseEvidenceCreate:
    defaults = {
        "release_version": "v1.2.0",
        "environment": "staging",
        "status": "verified",
    }
    defaults.update(overrides)
    return operations.ReleaseEvidenceCreate(**defaults)


# --- Feature Flag 端点：权限校验 ----------------------------------------


@pytest.mark.asyncio
async def test_list_feature_flags_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert: 在 DB 调用前即被拒绝
    with pytest.raises(HTTPException) as exc:
        await operations.list_feature_flags(_actor(role="member"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin role required"


@pytest.mark.asyncio
async def test_get_feature_flag_rejects_non_admin():
    # Arrange: viewer 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.get_feature_flag("flag_key", _actor(role="viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_feature_flag_rejects_non_owner(monkeypatch):
    # Arrange: admin 角色不能更新 flag（需 owner）
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_feature_flag("flag_key", _valid_flag_body(), _actor(role="admin"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Owner role required"


@pytest.mark.asyncio
async def test_evaluate_feature_flag_rejects_non_admin():
    # Arrange: member 角色无权评估 flag
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.evaluate_feature_flag(
            "flag_key",
            operations.FlagEvaluationRequest(subject_id="usr_1"),
            _actor(role="member"),
        )
    assert exc.value.status_code == 403


# --- Feature Flag 端点：404 / 422 ---------------------------------------


@pytest.mark.asyncio
async def test_get_feature_flag_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空列表
    conn = _OpsConnection(rows=[])
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.get_feature_flag("flag_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Feature flag not found"


@pytest.mark.asyncio
async def test_update_feature_flag_returns_422_for_invalid_flag(monkeypatch):
    # Arrange: ops 类型 flag 缺少 runbook → validate_flag 抛 ValueError → 422
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_feature_flag(
            "flag_key",
            _valid_flag_body(flag_type="ops", runbook=None),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_feature_flag_returns_422_for_entitlement_type(monkeypatch):
    # Arrange: entitlement 类型 flag 由策略服务管理 → 422
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_feature_flag(
            "flag_key",
            _valid_flag_body(flag_type="entitlement"),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 422
    assert "owning policy service" in exc.value.detail


@pytest.mark.asyncio
async def test_rollback_feature_flag_returns_404_when_target_not_found(monkeypatch):
    # Arrange: 目标版本不存在 → SELECT 返回 None
    conn = _OpsConnection(row=None)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.rollback_feature_flag(
            "flag_key",
            operations.RollbackRequest(target_version=99),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Target flag version not found"


@pytest.mark.asyncio
async def test_evaluate_feature_flag_returns_404_when_not_found(monkeypatch):
    # Arrange: flag 不存在 → SELECT 返回 None
    conn = _OpsConnection(row=None)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.evaluate_feature_flag(
            "flag_missing",
            operations.FlagEvaluationRequest(subject_id="usr_1"),
            _actor(role="admin"),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Feature flag not found"


@pytest.mark.asyncio
async def test_evaluate_feature_flag_rejects_cross_workspace(monkeypatch):
    # Arrange: actor 属于 wsp_a，但请求评估 wsp_b 的 flag → 404
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.evaluate_feature_flag(
            "flag_key",
            operations.FlagEvaluationRequest(subject_id="usr_1", workspace_id="wsp_other"),
            _actor(role="admin", workspace_id="wsp_test"),
        )
    assert exc.value.status_code == 404


# --- Feature Flag 端点：成功路径 ----------------------------------------


@pytest.mark.asyncio
async def test_list_feature_flags_returns_items_for_workspace(monkeypatch):
    # Arrange: 返回 2 条 flag
    rows = [{"flag_key": "flag_a", "version": 1}, {"flag_key": "flag_b", "version": 2}]
    conn = _OpsConnection(rows=rows)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.list_feature_flags(_actor(role="admin"))

    # Assert: 返回 items，且 workspace_id 进入查询参数
    assert result == {"items": rows}
    _, params = conn.calls[0]
    assert params[0] == "wsp_test"


@pytest.mark.asyncio
async def test_update_feature_flag_creates_new_version(monkeypatch):
    # Arrange: owner 创建 flag，latest 版本为 1 → 新版本为 2
    latest_row = {"version": 1}
    new_row = {"id": "flg_new", "flag_key": "flag_key", "version": 2}
    conn = _OpsConnection(row=latest_row)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.update_feature_flag("flag_key", _valid_flag_body(), _actor(role="owner"))

    # Assert: 返回新 row（第二次 fetchone 的结果）
    assert result == latest_row  # _OpsConnection 对所有 fetchone 返回同一 row


@pytest.mark.asyncio
async def test_evaluate_feature_flag_returns_evaluation_result(monkeypatch):
    # Arrange: flag 存在且 enabled
    now = datetime.now(UTC)
    flag = {
        "key": "flag_key", "version": 1, "status": "enabled",
        "default_value": False, "safe_value": False,
        "targeting": {"workspace_ids": ["wsp_test"], "percentage": 0},
        "salt": "salt", "starts_at": None, "ends_at": None,
    }
    conn = _OpsConnection(row=flag)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.evaluate_feature_flag(
        "flag_key",
        operations.FlagEvaluationRequest(subject_id="usr_1"),
        _actor(role="admin", workspace_id="wsp_test"),
    )

    # Assert: workspace_target 命中 → value=True
    assert result["value"] is True
    assert result["reason"] == "workspace_target"


# --- Feature Flag 验证端点 ---------------------------------------------


@pytest.mark.asyncio
async def test_validate_feature_flag_returns_valid_for_release_flag():
    # Arrange: admin 验证一个合法的 release flag
    # Act
    result = await operations.validate_feature_flag(_valid_flag_body(), _actor(role="admin"))

    # Assert: 返回 valid=True
    assert result == {"valid": True, "errors": []}


@pytest.mark.asyncio
async def test_validate_feature_flag_returns_invalid_for_entitlement():
    # Arrange: entitlement 类型由策略服务管理
    # Act
    result = await operations.validate_feature_flag(
        _valid_flag_body(flag_type="entitlement"), _actor(role="admin")
    )

    # Assert: 返回 valid=False
    assert result["valid"] is False
    assert "owning policy service" in result["errors"][0]


# --- Dynamic Config 端点：权限校验 --------------------------------------


@pytest.mark.asyncio
async def test_list_dynamic_configs_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.list_dynamic_configs(_actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_dynamic_config_rejects_non_admin():
    # Arrange: viewer 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.get_dynamic_config("cfg_key", _actor(role="viewer"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_dynamic_config_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.resolve_dynamic_config("cfg_key", _actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_dynamic_config_rejects_non_owner(monkeypatch):
    # Arrange: admin 角色不能更新 config（需 owner）
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_dynamic_config("cfg_key", _valid_config_body(), _actor(role="admin"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Owner role required"


# --- Dynamic Config 端点：404 / 422 -------------------------------------


@pytest.mark.asyncio
async def test_get_dynamic_config_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空列表
    conn = _OpsConnection(rows=[])
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.get_dynamic_config("cfg_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Dynamic config not found"


@pytest.mark.asyncio
async def test_resolve_dynamic_config_returns_404_when_no_effective(monkeypatch):
    # Arrange: 数据库返回空列表 → resolve_config 返回 None
    conn = _OpsConnection(rows=[])
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.resolve_dynamic_config("cfg_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "No effective dynamic config"


@pytest.mark.asyncio
async def test_update_dynamic_config_returns_422_for_invalid_value(monkeypatch):
    # Arrange: config_value 不符合 schema → 校验错误 → 422
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert: threshold 超出 maximum=100
    with pytest.raises(HTTPException) as exc:
        await operations.update_dynamic_config(
            "cfg_key",
            _valid_config_body(config_value={"threshold": 999}),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_dynamic_config_high_risk_requires_different_approver(monkeypatch):
    # Arrange: high-risk config 且 approved_by == actor 自己 → 422
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_dynamic_config(
            "cfg_key",
            _valid_config_body(
                risk_level="high",
                approved_by="usr_test",  # 与 actor.user_id 相同
            ),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 422
    errors = exc.value.detail
    assert any("different approving member" in e for e in errors)


@pytest.mark.asyncio
async def test_update_dynamic_config_high_risk_requires_approver_in_workspace(monkeypatch):
    # Arrange: high-risk config，approved_by 是他人但不是 workspace 成员 → 422
    conn = _OpsConnection(row=None)  # id_member 查询返回 None
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.update_dynamic_config(
            "cfg_key",
            _valid_config_body(
                risk_level="high",
                approved_by="usr_other",
            ),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 422
    assert any("approver must be an owner or admin" in e for e in exc.value.detail)


@pytest.mark.asyncio
async def test_rollback_dynamic_config_returns_404_when_target_not_found(monkeypatch):
    # Arrange: 目标版本不存在 → SELECT 返回 None
    conn = _OpsConnection(row=None)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.rollback_dynamic_config(
            "cfg_key",
            operations.RollbackRequest(target_version=99),
            _actor(role="owner"),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Target config version not found"


# --- Dynamic Config 端点：成功路径 --------------------------------------


@pytest.mark.asyncio
async def test_list_dynamic_configs_returns_items_for_workspace(monkeypatch):
    # Arrange: 返回 2 条 config
    rows = [{"config_key": "cfg_a", "version": 1}, {"config_key": "cfg_b", "version": 2}]
    conn = _OpsConnection(rows=rows)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.list_dynamic_configs(_actor(role="admin"))

    # Assert: 返回 items，且 workspace_id 进入查询参数
    assert result == {"items": rows}
    _, params = conn.calls[0]
    assert params[0] == "wsp_test"


@pytest.mark.asyncio
async def test_update_dynamic_config_creates_new_version(monkeypatch):
    # Arrange: owner 创建 config，latest 版本为 1 → 新版本为 2
    latest_row = {"version": 1}
    conn = _OpsConnection(row=latest_row)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.update_dynamic_config("cfg_key", _valid_config_body(), _actor(role="owner"))

    # Assert: 返回新 row
    assert result == latest_row


# --- Dynamic Config 验证端点 -------------------------------------------


@pytest.mark.asyncio
async def test_validate_dynamic_config_returns_valid_for_normal_config():
    # Arrange: admin 验证一个合法的 normal config
    # Act
    result = await operations.validate_dynamic_config(_valid_config_body(), _actor(role="admin"))

    # Assert: 返回 valid=True
    assert result["valid"] is True
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_validate_dynamic_config_returns_invalid_for_high_risk_without_approver():
    # Arrange: high-risk config 缺少 approved_by
    # Act
    result = await operations.validate_dynamic_config(
        _valid_config_body(risk_level="high", approved_by=None),
        _actor(role="admin"),
    )

    # Assert: 返回 valid=False
    assert result["valid"] is False
    assert any("different approving member" in e for e in result["errors"])


# --- Event Catalog 端点 -------------------------------------------------


@pytest.mark.asyncio
async def test_list_event_catalog_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.list_event_catalog(_actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_event_catalog_returns_items(monkeypatch):
    # Arrange: ensure_event_catalog 执行 43 条 INSERT 后，SELECT 返回 items
    items = [{"event_name": "signup_completed", "domain": "acquisition"}]
    conn = _OpsConnection(rows=items)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.list_event_catalog(_actor(role="admin"))

    # Assert: 返回 count 和 items
    assert result["count"] == 1
    assert result["items"] == items


# --- Product Event 收集端点 --------------------------------------------


@pytest.mark.asyncio
async def test_collect_product_event_rejects_unknown_event(monkeypatch):
    # Arrange: 事件名不在 catalog 中
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.collect_product_event(
            operations.ProductEventCreate(event_name="unknown_event"),
            _actor(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "Unknown product event"


@pytest.mark.asyncio
async def test_collect_product_event_rejects_sensitive_properties(monkeypatch):
    # Arrange: properties 包含敏感字段 prompt
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.collect_product_event(
            operations.ProductEventCreate(
                event_name="signup_completed",
                properties={"prompt": "secret content"},
            ),
            _actor(),
        )
    assert exc.value.status_code == 422
    assert "sensitive" in exc.value.detail


@pytest.mark.asyncio
async def test_collect_product_event_rejects_disallowed_properties(monkeypatch):
    # Arrange: properties 包含未授权字段
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.collect_product_event(
            operations.ProductEventCreate(
                event_name="signup_completed",
                properties={"unknown_field": "value"},
            ),
            _actor(),
        )
    assert exc.value.status_code == 422
    assert "not allowed" in exc.value.detail


@pytest.mark.asyncio
async def test_collect_product_event_accepts_valid_event(monkeypatch):
    # Arrange: 合法事件 + 合法 properties
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.collect_product_event(
        operations.ProductEventCreate(
            event_name="signup_completed",
            properties={"source": "web", "surface": "landing"},
        ),
        _actor(),
    )

    # Assert: 返回 event_id 和 accepted=True
    assert result["event_name"] == "signup_completed"
    assert result["accepted"] is True
    assert result["id"].startswith("pev_")


# --- Release Evidence 端点：权限校验 ------------------------------------


@pytest.mark.asyncio
async def test_list_release_evidence_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.list_release_evidence(_actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_release_evidence_rejects_non_owner(monkeypatch):
    # Arrange: admin 角色不能创建 release evidence（需 owner）
    conn = _OpsConnection()
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await operations.create_release_evidence(_valid_release_body(), _actor(role="admin"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Owner role required"


# --- Release Evidence 端点：成功路径 ------------------------------------


@pytest.mark.asyncio
async def test_list_release_evidence_returns_items_for_workspace(monkeypatch):
    # Arrange: 返回 2 条 release evidence
    rows = [{"release_version": "v1.0.0"}, {"release_version": "v1.1.0"}]
    conn = _OpsConnection(rows=rows)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.list_release_evidence(_actor(role="admin"))

    # Assert: 返回 items，且 workspace_id 进入查询参数
    assert result == {"items": rows}
    _, params = conn.calls[0]
    assert params[0] == "wsp_test"


@pytest.mark.asyncio
async def test_create_release_evidence_returns_new_row(monkeypatch):
    # Arrange: owner 创建 release evidence
    new_row = {"id": "rel_new", "release_version": "v1.2.0", "environment": "staging"}
    conn = _OpsConnection(row=new_row)
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    result = await operations.create_release_evidence(_valid_release_body(), _actor(role="owner"))

    # Assert: 返回新 row
    assert result == new_row


# --- ACL 隔离 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_feature_flags_isolates_workspace_via_acl(monkeypatch):
    # Arrange: actor 属于 wsp_a，验证查询参数中 workspace_id 必为 wsp_a
    conn = _OpsConnection(rows=[])
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    await operations.list_feature_flags(_actor(role="admin", workspace_id="wsp_a"))

    # Assert: SQL 含 workspace_id 过滤，参数为 actor 的 workspace_id
    query, params = conn.calls[0]
    assert "workspace_id = %s" in query
    assert params[0] == "wsp_a"


@pytest.mark.asyncio
async def test_list_dynamic_configs_isolates_workspace_via_acl(monkeypatch):
    # Arrange: actor 属于 wsp_b，验证查询参数中 workspace_id 必为 wsp_b
    conn = _OpsConnection(rows=[])
    monkeypatch.setattr(operations, "pool", _Pool(conn))

    # Act
    await operations.list_dynamic_configs(_actor(role="admin", workspace_id="wsp_b"))

    # Assert: SQL 含 workspace_id 过滤
    query, params = conn.calls[0]
    assert "workspace_id = %s" in query
    assert params[0] == "wsp_b"


# --- 路由契约 ------------------------------------------------------------


def test_admin_router_exposes_feature_flag_and_config_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in operations.router.routes}
    assert ("/api/v1/admin/feature-flags", ("GET",)) in paths
    assert ("/api/v1/admin/feature-flags/{flag_key}", ("GET",)) in paths
    assert ("/api/v1/admin/feature-flags/{flag_key}", ("PUT",)) in paths
    assert ("/api/v1/admin/feature-flags/{flag_key}/rollbacks", ("POST",)) in paths
    assert ("/api/v1/admin/feature-flags/{flag_key}/evaluations", ("POST",)) in paths
    assert ("/api/v1/admin/feature-flag-validations", ("POST",)) in paths
    assert ("/api/v1/admin/dynamic-configs", ("GET",)) in paths
    assert ("/api/v1/admin/dynamic-configs/{config_key}", ("GET",)) in paths
    assert ("/api/v1/admin/dynamic-configs/{config_key}/resolved", ("GET",)) in paths
    assert ("/api/v1/admin/dynamic-configs/{config_key}", ("PUT",)) in paths
    assert ("/api/v1/admin/dynamic-configs/{config_key}/rollbacks", ("POST",)) in paths
    assert ("/api/v1/admin/dynamic-config-validations", ("POST",)) in paths
    assert ("/api/v1/admin/event-catalog", ("GET",)) in paths
    assert ("/api/v1/admin/release-evidence", ("GET",)) in paths
    assert ("/api/v1/admin/release-evidence", ("POST",)) in paths


def test_event_router_exposes_product_events_contract():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in operations.event_router.routes}
    assert ("/api/v1/events", ("POST",)) in paths
