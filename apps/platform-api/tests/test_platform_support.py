import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import platform_support
from workama_platform.modules.platform_support import (
    LifecyclePolicyUpsert,
    LifecycleRunRequest,
    TemplateTest,
    TemplateUpsert,
    _require_admin,
    render_template,
    router,
    validate_template,
)


SCHEMA = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}


def _actor(role: str) -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role=role,
        email="test@example.test",
        display_name="Test",
        onboarding_completed=True,
    )


# --- validate_template -----------------------------------------------------


def test_validate_template_accepts_schema_bound_placeholders():
    assert validate_template("Hello {{name}}", "Done", SCHEMA) == []


def test_validate_template_flags_unknown_variables():
    errors = validate_template("Hello {{unknown}}", "Done", SCHEMA)
    assert any("unknown" in err for err in errors)


def test_validate_template_flags_required_variables_not_rendered():
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}, "city": {"type": "string"}},
    }
    errors = validate_template("Hello {{city}}", "Done", schema)
    assert any("required" in err for err in errors)


def test_validate_template_rejects_triple_brace_unsafe_syntax():
    errors = validate_template("Hello {{{name}}}", "Done", SCHEMA)
    assert any("unsafe" in err for err in errors)


def test_validate_template_rejects_safe_filter_unsafe_syntax():
    errors = validate_template("Hello {{name}}|safe", "Done", SCHEMA)
    assert any("unsafe" in err for err in errors)


def test_validate_template_rejects_non_object_schema():
    errors = validate_template(
        "Hello {{name}}", "Done", {"type": "string", "properties": {"name": {}}}
    )
    assert any("type must be object" in err for err in errors)


# --- render_template -------------------------------------------------------


def test_render_template_substitutes_placeholders():
    assert render_template("Hello {{name}}", {"name": "WorkAMA"}, SCHEMA) == "Hello WorkAMA"


def test_render_template_raises_for_missing_required_variable():
    with pytest.raises(ValueError, match="missing"):
        render_template("Hello {{name}}", {}, SCHEMA)


def test_render_template_raises_for_unknown_variable():
    with pytest.raises(ValueError, match="unknown"):
        render_template("Hello {{name}}", {"name": "A", "secret": "x"}, SCHEMA)


# --- pydantic models -------------------------------------------------------


def test_template_upsert_defaults_and_channel_literals():
    payload = TemplateUpsert(
        channel="email",
        subject_template="Welcome {{name}}",
        body_template="Hi {{name}}",
        variables_schema=SCHEMA,
    )
    assert payload.locale == "zh-CN"
    assert payload.sensitive_level == "C2"
    assert payload.status == "draft"
    with pytest.raises(ValueError):
        TemplateUpsert(
            channel="sms",
            subject_template="Welcome",
            body_template="Hi",
            variables_schema=SCHEMA,
        )


def test_template_upsert_enforces_length_bounds():
    with pytest.raises(ValueError):
        TemplateUpsert(
            channel="email",
            subject_template="",
            body_template="Hi {{name}}",
            variables_schema=SCHEMA,
        )
    with pytest.raises(ValueError):
        TemplateUpsert(
            channel="email",
            subject_template="x" * 501,
            body_template="Hi {{name}}",
            variables_schema=SCHEMA,
        )


def test_lifecycle_policy_upsert_enforces_bounds_and_defaults():
    policy = LifecyclePolicyUpsert(retention_days=30, runbook="Purge expired notifications")
    assert policy.batch_size == 100
    assert policy.status == "enabled"
    with pytest.raises(ValueError):
        LifecyclePolicyUpsert(retention_days=0, runbook="Purge")
    with pytest.raises(ValueError):
        LifecyclePolicyUpsert(retention_days=4000, runbook="Purge")
    with pytest.raises(ValueError):
        LifecyclePolicyUpsert(retention_days=30, batch_size=0, runbook="Purge")
    with pytest.raises(ValueError):
        LifecyclePolicyUpsert(retention_days=30, runbook="x")


def test_lifecycle_run_request_defaults_dry_run_and_resource_types():
    request = LifecycleRunRequest(resource_type="notification")
    assert request.dry_run is True
    with pytest.raises(ValueError):
        LifecycleRunRequest(resource_type="unknown")


# --- router contract -------------------------------------------------------


def test_router_exposes_template_lifecycle_and_admin_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in router.routes}
    for expected in (
        ("/api/v1/admin/notification-templates", ("GET",)),
        ("/api/v1/admin/notification-templates/{template_id}", ("GET",)),
        ("/api/v1/admin/notification-templates/{template_id}", ("PUT",)),
        ("/api/v1/admin/notification-template-validations", ("POST",)),
        ("/api/v1/admin/notification-templates/{template_id}/tests", ("POST",)),
        ("/api/v1/admin/lifecycle-policies", ("GET",)),
        ("/api/v1/admin/lifecycle-policies/{resource_type}", ("PUT",)),
        ("/api/v1/admin/lifecycle-runs", ("POST",)),
        ("/api/v1/admin/lifecycle-runs", ("GET",)),
    ):
        assert expected in paths


def test_router_is_scoped_under_admin_prefix():
    for route in router.routes:
        assert route.path.startswith("/api/v1/admin/")


# --- access control --------------------------------------------------------


def test_require_admin_allows_owner_and_admin():
    _require_admin(_actor("owner"))
    _require_admin(_actor("admin"))


def test_require_admin_rejects_non_admin_roles():
    for role in ("member", "viewer", "guest"):
        with pytest.raises(HTTPException) as error:
            _require_admin(_actor(role))
        assert error.value.status_code == 403


# --- database-bound endpoints (mock pool / transaction / object storage) ---
# 参考 test_agent_tools.py / test_search.py / test_setup.py 的内联 mock 模式：
# _Result / _Connection / _Pool / _Transaction 模拟 psycopg 连接池与事务，
# 通过 monkeypatch.setattr 替换模块级 pool / submit_operation / delete_object，
# 避免触碰真实 DB 与外部对象存储。


class _Result:
    """模拟 psycopg Cursor 结果，支持 fetchone / fetchall。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    """模拟 psycopg 事务上下文管理器。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Pool:
    """最小化连接池上下文管理器，返回预设的 connection。"""

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


def _stub_submit_operation(captured=None, return_value=None):
    """构造替换 submit_operation 的协程桩，记录入参。"""
    return_value = return_value or {"id": "op_test", "status": "queued"}

    async def _stub(conn, **kwargs):
        if captured is not None:
            captured.update(kwargs)
        return return_value

    return _stub


# --- list_templates ------------------------------------------------------


class _ListTemplatesConnection:
    """记录 execute 调用，SELECT 返回预设的模板行列表。"""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Result(rows=self._rows)


@pytest.mark.asyncio
async def test_list_templates_requires_admin_and_returns_workspace_scoped_items(monkeypatch):
    # Arrange: ops_notification_template 返回 2 行（按 version DESC 排序）
    rows = [
        {"template_id": "welcome", "version": 2, "locale": "zh-CN", "channel": "email"},
        {"template_id": "welcome", "version": 1, "locale": "zh-CN", "channel": "email"},
    ]
    conn = _ListTemplatesConnection(rows)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn))

    # Act
    result = await platform_support.list_templates(_actor("admin"))

    # Assert: 返回 items 字段（ListResponse 信封），查询按 workspace_id 过滤
    assert result["items"] == rows
    query, params = conn.calls[0]
    assert "ops_notification_template" in query
    assert "workspace_id=%s" in query
    assert params == ("wsp_test",)


# --- update_template -----------------------------------------------------


class _UpdateTemplateConnection:
    """区分版本号查询与 INSERT...RETURNING，返回配置结果。"""

    def __init__(self, *, version, returning_row):
        self._version = version
        self._returning_row = returning_row
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "COALESCE(max(version)" in query:
            return _Result(row={"version": self._version})
        if "INSERT INTO ops_notification_template" in query:
            return _Result(row=self._returning_row)
        return _Result()

    def transaction(self):
        return _Transaction()


@pytest.mark.asyncio
async def test_update_template_persists_new_version_with_content_hash(monkeypatch):
    # Arrange: 当前最大版本为 1，新版本应为 2
    returning_row = {
        "id": "tpl_new", "template_id": "welcome", "version": 2, "locale": "zh-CN",
        "channel": "email", "subject_template": "Welcome {{name}}",
        "body_template": "Hi {{name}}", "status": "published", "content_hash": "a" * 64,
    }
    conn = _UpdateTemplateConnection(version=2, returning_row=returning_row)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn))

    body = TemplateUpsert(
        channel="email",
        subject_template="Welcome {{name}}",
        body_template="Hi {{name}}",
        variables_schema=SCHEMA,
        status="published",
    )

    # Act
    result = await platform_support.update_template("welcome", body, _actor("admin"))

    # Assert: 返回新版本行
    assert result == returning_row
    # 版本号查询被执行
    version_calls = [c for c in conn.calls if "COALESCE(max(version)" in c[0]]
    assert len(version_calls) == 1
    # INSERT 携带新版本号与 content_hash
    insert_calls = [c for c in conn.calls if "INSERT INTO ops_notification_template" in c[0]]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert params[3] == 2  # version
    assert params[10] == "published"  # status
    assert len(params[11]) == 64  # content_hash (sha256 hex)


# --- test_template -------------------------------------------------------


class _TestTemplateConnection:
    """SELECT 返回已发布模板，INSERT 被忽略，记录 commit。"""

    def __init__(self, template):
        self._template = template
        self.calls = []
        self.committed = False

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if query.lstrip().upper().startswith("SELECT"):
            return _Result(row=self._template)
        return _Result()

    async def commit(self):
        self.committed = True
        return None


@pytest.mark.asyncio
async def test_test_template_renders_published_template_and_creates_notification(monkeypatch):
    # Arrange: 已发布模板，channel=email 触发投递记录插入
    template = {
        "template_id": "welcome", "version": 3, "channel": "email",
        "subject_template": "Welcome {{name}}",
        "body_template": "Hello {{name}}, your account is ready.",
        "variables_schema": SCHEMA,
    }
    conn = _TestTemplateConnection(template)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn))

    # Act
    result = await platform_support.test_template(
        "welcome", TemplateTest(variables={"name": "Alice"}), _actor("admin")
    )

    # Assert: title/summary 已渲染，channel 透传，notification_id 已生成
    assert result["title"] == "Welcome Alice"
    assert result["summary"] == "Hello Alice, your account is ready."
    assert result["channel"] == "email"
    assert result["notification_id"].startswith("ntf_")
    # INSERT 通知 + INSERT 投递（email channel）均被执行
    assert any("INSERT INTO id_notification(" in q for q, _ in conn.calls)
    assert any("INSERT INTO id_notification_delivery(" in q for q, _ in conn.calls)
    # commit 被调用
    assert conn.committed is True


# --- upsert_lifecycle_policy --------------------------------------------


class _UpsertPolicyConnection:
    """返回 upsert 后的行，记录 commit。"""

    def __init__(self, returning_row):
        self._returning_row = returning_row
        self.calls = []
        self.committed = False

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Result(row=self._returning_row)

    async def commit(self):
        self.committed = True
        return None


@pytest.mark.asyncio
async def test_upsert_lifecycle_policy_is_idempotent_per_resource_type(monkeypatch):
    # Arrange: upsert 返回固定行
    returning_row = {
        "id": "lcp_1", "workspace_id": "wsp_test", "resource_type": "notification",
        "retention_days": 30, "batch_size": 100, "status": "enabled",
        "runbook": "Purge expired notifications", "updated_by": "usr_test",
    }
    conn = _UpsertPolicyConnection(returning_row)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn))

    body = LifecyclePolicyUpsert(retention_days=30, runbook="Purge expired notifications")

    # Act
    result = await platform_support.upsert_lifecycle_policy("notification", body, _actor("admin"))

    # Assert: 返回 upsert 后的行
    assert result == returning_row
    # SQL 含 ON CONFLICT upsert 子句，参数含 workspace_id 与 resource_type
    query, params = conn.calls[0]
    assert "ON CONFLICT(workspace_id,resource_type)" in query
    assert "DO UPDATE" in query
    assert params[1] == "wsp_test"  # workspace_id
    assert params[2] == "notification"  # resource_type
    assert params[3] == 30  # retention_days
    # commit 被调用
    assert conn.committed is True


# --- create_lifecycle_run ------------------------------------------------


class _LifecycleRunConnection:
    """按 enabled 标志返回 policy 存在性查询结果。"""

    def __init__(self, *, enabled):
        self._enabled = enabled
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "ops_lifecycle_policy" in query and "status='enabled'" in query:
            return _Result(row={"1": 1} if self._enabled else None)
        return _Result()

    def transaction(self):
        return _Transaction()


@pytest.mark.asyncio
async def test_create_lifecycle_run_requires_enabled_policy_and_queues_operation(monkeypatch):
    # 场景 1: policy 未启用 → 409 拒绝，且不应调用 submit_operation
    conn_disabled = _LifecycleRunConnection(enabled=False)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn_disabled))

    async def _noop_submit(conn, **kwargs):
        raise AssertionError("submit_operation 不应在 policy 未启用时被调用")

    monkeypatch.setattr(platform_support, "submit_operation", _noop_submit)

    with pytest.raises(HTTPException) as exc:
        await platform_support.create_lifecycle_run(
            LifecycleRunRequest(resource_type="notification"), _actor("admin")
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Enabled lifecycle policy required"

    # 场景 2: policy 已启用 → 排队 operation
    captured = {}
    conn_enabled = _LifecycleRunConnection(enabled=True)
    monkeypatch.setattr(platform_support, "pool", _Pool(conn_enabled))
    monkeypatch.setattr(
        platform_support, "submit_operation",
        _stub_submit_operation(captured, {"id": "op_test", "status": "queued"}),
    )

    # Act
    result = await platform_support.create_lifecycle_run(
        LifecycleRunRequest(resource_type="notification"), _actor("admin")
    )

    # Assert: 返回 202 契约，operation 已排队
    assert result["status"] == "queued"
    assert result["operation_id"] == "op_test"
    assert result["id"].startswith("lcr_")
    assert captured["operation_type"] == "lifecycle.run"
    assert captured["workspace_id"] == "wsp_test"
    assert captured["job_type"] == "lifecycle.run"
    # ops_lifecycle_run INSERT 被执行
    insert_calls = [c for c in conn_enabled.calls if "INSERT INTO ops_lifecycle_run" in c[0]]
    assert len(insert_calls) == 1


# --- execute_lifecycle_run ----------------------------------------------


class _ExecuteLifecycleConnection:
    """按查询内容区分 run / legal_hold / artifact 查询与 DELETE。"""

    def __init__(self, *, run, hold_count, items):
        self._run = run
        self._hold_count = hold_count
        self._items = items
        self.calls = []
        self.deleted_ids = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        upper = query.lstrip().upper()
        if upper.startswith("SELECT") and "ops_lifecycle_run r JOIN" in query:
            return _Result(row=self._run)
        if upper.startswith("SELECT") and "sec_legal_hold" in query:
            return _Result(row={"count": self._hold_count})
        if upper.startswith("SELECT") and "ag_artifact" in query:
            return _Result(rows=self._items)
        if upper.startswith("DELETE") and "ag_artifact" in query:
            self.deleted_ids.append(params[0])
            return _Result()
        return _Result()


@pytest.mark.asyncio
async def test_execute_lifecycle_run_deletes_expired_resources_respecting_legal_hold(monkeypatch):
    # Arrange: artifact 路径，dry_run=False，2 个待清理 artifact
    run = {
        "resource_type": "artifact", "retention_days": 30,
        "batch_size": 100, "dry_run": False,
    }
    items = [
        {"id": "art_1", "s3_key": "artifacts/k1"},
        {"id": "art_2", "s3_key": "artifacts/k2"},
    ]
    deleted_buckets_keys = []

    async def _fake_delete_object(bucket, key):
        deleted_buckets_keys.append((bucket, key))

    monkeypatch.setattr(platform_support, "delete_object", _fake_delete_object)

    # 场景 1: 无 legal hold → 删除对象并删除 DB 行
    conn = _ExecuteLifecycleConnection(run=run, hold_count=0, items=items)

    # Act
    result = await platform_support.execute_lifecycle_run(conn, "lcr_test", "wsp_test")

    # Assert: eligible=2, processed=2, skipped_hold=0
    assert result["status"] == "succeeded"
    assert result["eligible"] == 2
    assert result["processed"] == 2
    assert result["skipped_hold"] == 0
    assert deleted_buckets_keys == [
        ("workama-artifacts", "artifacts/k1"),
        ("workama-artifacts", "artifacts/k2"),
    ]
    assert conn.deleted_ids == ["art_1", "art_2"]
    # 最终 UPDATE 标记 completed
    assert any("UPDATE ops_lifecycle_run SET status='completed'" in q for q, _ in conn.calls)

    # 场景 2: 存在 legal hold → 不删除，全部计入 skipped_hold
    conn_hold = _ExecuteLifecycleConnection(run=run, hold_count=1, items=items)
    deleted_buckets_keys.clear()

    # Act
    result_hold = await platform_support.execute_lifecycle_run(conn_hold, "lcr_test", "wsp_test")

    # Assert: processed=0, skipped_hold=2，对象存储与 DB 行均未被删除
    assert result_hold["processed"] == 0
    assert result_hold["skipped_hold"] == 2
    assert deleted_buckets_keys == []
    assert conn_hold.deleted_ids == []
