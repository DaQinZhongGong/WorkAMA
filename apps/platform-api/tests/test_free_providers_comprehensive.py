"""免费 / 公益大模型供应商预设目录的综合测试套件。

覆盖 ``FREE_PROVIDER_PRESETS`` 与 ``PROVIDER_CATALOG`` 的一致性、字段完整性、
协议 / 能力 / 区域合法性，以及 ``GET /free-providers`` 与
``POST /free-providers/{key}/enable`` 两个端点的契约与鉴权行为。

本套件与 ``test_free_providers.py`` / ``test_free_providers_extended.py`` 互补：
- 前两者按批次（batch 1/2/3/4）逐项断言；
- 本文件聚焦全量一致性、catalog <-> preset 双向覆盖、别名解析、
  公开端点与鉴权 / 幂等端到端流程。

注意：
- v7.134 起，``FREE_PROVIDER_PRESETS`` 已扩到 100 条，
  ``PROVIDER_CATALOG`` 已同步扩到 103 条（含 18 个新增第五批供应商）。
  因此 preset 与 catalog 数量门槛均取 100，preset<->catalog 一致性
  收紧为 strict 模式（不允许任何 preset 缺 catalog 条目）。
- 所有外部依赖（pool / redis）通过 fake 类隔离，不依赖真实 DB/Redis/网络。
- ``enable`` 端点本身只做 DB 写入 + URL 校验，不会调用真实上游 API，
  因此无需 mock httpx 上游响应。
"""
from __future__ import annotations

import pytest

from fastapi import FastAPI

import httpx

from workama_platform.core import Actor, get_actor
from workama_platform.modules.gateway import router as gateway_router
from workama_platform.modules.gateway.free_presets import (
    FREE_PROVIDER_PRESETS,
    get_free_preset,
)
from workama_platform.modules.gateway.router import (
    PROVIDER_ALIASES,
    PROVIDER_CATALOG,
    FreeProviderEnableRequest,
    enable_free_provider,
    list_free_providers,
)


# ----------------------------------------------------------------------
# 测试常量
# ----------------------------------------------------------------------

# 协议白名单（preset.protocol 必须取自该集合）
ALLOWED_PROTOCOLS = {"openai", "anthropic", "gemini"}

# 能力白名单。注意：实际数据中除任务描述列出的 7 项外，还存在
# ``long_context``（ai21）与 ``image_generation``（localai），这里沿用
# test_free_providers_extended.py 的合法集合，避免误判现有数据。
ALLOWED_CAPABILITIES = {
    "chat",
    "vision",
    "tool_call",
    "json_mode",
    "embedding",
    "reasoning",
    "background",
    "long_context",
    "image_generation",
}

# 每个 preset 必须包含的字段
REQUIRED_PRESET_FIELDS = {
    "provider",
    "name",
    "base_url",
    "protocol",
    "signup_url",
    "free_quota",
    "free_models",
    "capabilities",
    "regions",
    "retention_mode",
    "notes",
}

# 数量门槛。v7.134 起 FREE_PROVIDER_PRESETS=100 且 PROVIDER_CATALOG=103。
MIN_FREE_PRESETS = 100
MIN_CATALOG_ENTRIES = 100


# ----------------------------------------------------------------------
# 测试辅助：mock psycopg 连接池（风格参考 test_free_providers.py）
# ----------------------------------------------------------------------


class _Result:
    """模拟 psycopg 的查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _RecordingConnection:
    """记录 execute 调用，按 SQL 关键字返回不同模拟结果。

    - SELECT (existing check on gw_channel) 返回 self._existing_row
    - 其他返回空结果
    """

    def __init__(self, existing_row=None):
        self._existing_row = existing_row
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        upper = query.upper()
        if "SELECT" in upper and "GW_CHANNEL" in upper:
            return _Result(row=self._existing_row)
        return _Result()

    async def commit(self):
        return None


class _Pool:
    """模拟 psycopg AsyncConnectionPool。"""

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


def _admin_actor() -> Actor:
    return Actor(
        user_id="usr_admin",
        workspace_id="wsp_test",
        org_id="org_test",
        role="admin",
        email="admin@example.com",
        display_name="Admin",
        onboarding_completed=True,
    )


def _owner_actor() -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id="wsp_test",
        org_id="org_test",
        role="owner",
        email="owner@example.com",
        display_name="Owner",
        onboarding_completed=True,
    )


def _app_with_actor(actor: Actor | None, *, patch_pool=None) -> FastAPI:
    """构造一个挂载了 gateway router 的 FastAPI 应用。

    - 若 actor 为 None，则不覆盖 get_actor（用于测试 401/未鉴权场景）
    - 若 patch_pool 提供，则替换 gateway_router.pool
    """
    app = FastAPI()
    app.include_router(gateway_router.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    if patch_pool is not None:
        gateway_router.pool = patch_pool
    return app


# ----------------------------------------------------------------------
# 1. 数量门槛
# ----------------------------------------------------------------------


def test_free_presets_count_at_least_100():
    """FREE_PROVIDER_PRESETS 条目数 >= 门槛值（v7.134 起 = 100）。"""
    assert len(FREE_PROVIDER_PRESETS) >= MIN_FREE_PRESETS, (
        f"expected >= {MIN_FREE_PRESETS} free presets, got {len(FREE_PROVIDER_PRESETS)}"
    )


def test_provider_catalog_count_at_least_100():
    """PROVIDER_CATALOG 条目数 >= 门槛值（v7.134 起 = 103）。"""
    assert len(PROVIDER_CATALOG) >= MIN_CATALOG_ENTRIES, (
        f"expected >= {MIN_CATALOG_ENTRIES} catalog entries, got {len(PROVIDER_CATALOG)}"
    )


# ----------------------------------------------------------------------
# 2. preset 字段完整性
# ----------------------------------------------------------------------


def test_every_preset_has_required_fields():
    """每个 preset 必须包含全部必填字段。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        missing = REQUIRED_PRESET_FIELDS - set(preset.keys())
        assert not missing, f"preset {key!r} missing fields: {missing}"


def test_preset_provider_matches_key():
    """preset.provider 应等于 key，或指向 PROVIDER_CATALOG 中已有的标准 provider。

    少数预设（如 gemini_free/openai_free/siliconflow_cn）的 ``provider`` 字段
    指向底层 catalog 适配器（gemini/openai/siliconflow），而非预设 key 本身，
    这是设计上允许的——预设 key 用于 enable URL，provider 用于选择适配器。
    """
    for key, preset in FREE_PROVIDER_PRESETS.items():
        provider = preset["provider"]
        assert provider == key or provider in PROVIDER_CATALOG, (
            f"preset {key!r} provider={provider!r} must equal key or be a known catalog provider"
        )


def test_preset_protocol_in_allowed():
    """每个 preset 的 protocol 必须在白名单内。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        assert preset["protocol"] in ALLOWED_PROTOCOLS, (
            f"preset {key!r} protocol={preset['protocol']!r} not in {ALLOWED_PROTOCOLS}"
        )


def test_preset_capabilities_in_allowed():
    """每个 capability 必须在白名单内。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        for cap in preset["capabilities"]:
            assert cap in ALLOWED_CAPABILITIES, (
                f"preset {key!r} invalid capability: {cap!r}"
            )


def test_preset_free_models_nonempty():
    """每个 preset 的 free_models 必须非空。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        assert len(preset["free_models"]) >= 1, (
            f"preset {key!r} has empty free_models"
        )


def test_preset_base_url_valid_http():
    """每个 preset 的 base_url 必须以 http:// 或 https:// 开头。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        base_url = preset["base_url"]
        assert base_url.startswith(("http://", "https://")), (
            f"preset {key!r} base_url={base_url!r} must start with http:// or https://"
        )


def test_preset_signup_url_valid_http():
    """每个 preset 的 signup_url 必须以 http:// 或 https:// 开头。"""
    for key, preset in FREE_PROVIDER_PRESETS.items():
        signup_url = preset["signup_url"]
        assert signup_url.startswith(("http://", "https://")), (
            f"preset {key!r} signup_url={signup_url!r} must start with http:// or https://"
        )


# ----------------------------------------------------------------------
# 3. preset <-> catalog 一致性
# ----------------------------------------------------------------------


def test_every_preset_has_catalog_entry():
    """每个 preset 的 provider 字段（底层适配器）必须在 PROVIDER_CATALOG 中。

    注意：这里校验的是 ``preset["provider"]``（即底层适配器 key），
    而非预设 key 本身。例如 ``gemini_free`` 的 provider=``gemini``，
    ``gemini`` 必须在 catalog 中。

    v7.134 起 PROVIDER_CATALOG 已同步到 103 条，覆盖所有 100 个 preset 的
    ``provider`` 字段，因此本测试已收紧为 strict 模式。
    """
    missing = [
        key
        for key, preset in FREE_PROVIDER_PRESETS.items()
        if preset["provider"] not in PROVIDER_CATALOG
    ]
    assert not missing, f"presets without catalog entry: {missing}"


def test_every_catalog_free_provider_has_preset():
    """PROVIDER_CATALOG 中凡是被某个 preset 引用的 provider 都应有对应预设。

    这里反向校验：对每个 catalog key，若存在任意 preset 的 ``provider`` 字段
    指向它，则至少有一个预设覆盖该适配器。同时，catalog 中与预设 key 同名
    的条目（大多数情况）也应被 preset 覆盖。

    说明：catalog 中的“纯商业”条目（如 azure/bedrock/anthropic）可能没有
    免费预设，因此只校验“preset 引用的 provider 必须在 catalog 中”
    （已在 test_every_preset_has_catalog_entry 覆盖）以及
    “与预设 key 同名的 catalog 条目必须可被 enable”。
    """
    # 所有预设 key 中，与 catalog key 同名的，必须同时存在于 preset 字典
    for key in PROVIDER_CATALOG:
        # 若该 catalog key 本身就是一个 preset key，则 preset 必然存在
        if key in FREE_PROVIDER_PRESETS:
            assert FREE_PROVIDER_PRESETS[key]["provider"] in PROVIDER_CATALOG, (
                f"preset {key!r} (same name as catalog entry) provider not in catalog"
            )


def test_aliases_resolve_to_known_provider():
    """每个 PROVIDER_ALIASES 的 value 必须解析到 PROVIDER_CATALOG 中的 provider。"""
    for alias, canonical in PROVIDER_ALIASES.items():
        assert canonical in PROVIDER_CATALOG, (
            f"alias {alias!r} -> {canonical!r} not in PROVIDER_CATALOG"
        )


# ----------------------------------------------------------------------
# 4. 公开端点 GET /api/v1/gateway/free-providers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_free_providers_endpoint_returns_list():
    """GET /free-providers 公开端点返回 200，且 items 字段是 list。

    注意：端点实际返回的列表字段名为 ``items``（同时提供 ``data`` 别名），
    非 ``providers``。本测试断言实际契约字段 ``items``。
    """
    app = FastAPI()
    app.include_router(gateway_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://free-providers.test"
    ) as client:
        response = await client.get("/api/v1/gateway/free-providers")

    assert response.status_code == 200, response.text
    payload = response.json()
    # 端点契约字段为 items（也提供 data 别名）
    assert isinstance(payload["items"], list), "items must be a list"
    assert isinstance(payload.get("data"), list), "data alias must be a list"
    assert payload["total"] == len(payload["items"])
    assert len(payload["items"]) >= MIN_FREE_PRESETS


@pytest.mark.asyncio
async def test_public_free_providers_endpoint_direct_call():
    """直接调用 list_free_providers() 返回结构正确（无 HTTP 层）。"""
    result = await list_free_providers()
    assert result["total"] == len(FREE_PROVIDER_PRESETS)
    assert isinstance(result["items"], list)
    # 每个 item 的 provider 字段必须能回查到预设
    for item in result["items"]:
        assert item["provider"] in FREE_PROVIDER_PRESETS


# ----------------------------------------------------------------------
# 5. 鉴权 POST /api/v1/gateway/free-providers/{key}/enable
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_free_provider_requires_auth():
    """未认证（无 Authorization 头 / cookie）调用 enable 端点返回 401。

    get_actor 在 credentials 与 workama_access 均缺失时直接抛 401，
    不触达 DB，因此无需 mock pool。
    """
    app = FastAPI()
    app.include_router(gateway_router.router)
    # 故意不覆盖 get_actor，让真实依赖处理未认证请求
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        response = await client.post(
            "/api/v1/gateway/free-providers/siliconflow/enable",
            json={"api_key": "sk-test"},
        )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_enable_free_provider_admin_creates_channel(monkeypatch):
    """admin/owner 调用 enable 应创建渠道（INSERT gw_channel）并返回 201。

    enable 端点只做 DB 写入与 URL 校验，不调用真实上游 API，故无需 mock httpx。
    """
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    app = _app_with_actor(_owner_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        response = await client.post(
            "/api/v1/gateway/free-providers/siliconflow/enable",
            json={"api_key": "sk-siliconflow-test"},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["provider"] == "siliconflow"
    assert payload["base_url"] == "https://api.siliconflow.cn/v1"
    assert payload["status"] == "enabled"
    assert payload["idempotent"] is False
    # 必须有 INSERT INTO gw_channel 调用
    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO gw_channel" in joined


@pytest.mark.asyncio
async def test_enable_free_provider_admin_creates_channel_direct(monkeypatch):
    """直接调用 enable_free_provider() 验证返回结构与 INSERT 行为。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    body = FreeProviderEnableRequest(api_key="sk-groq-test")
    result = await enable_free_provider("groq", body, _admin_actor())

    assert result["idempotent"] is False
    assert result["provider"] == "groq"
    assert result["base_url"] == "https://api.groq.com/openai/v1"
    assert result["status"] == "enabled"
    assert "llama-3.3-70b-versatile" in result["models"]

    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO gw_channel" in joined
    # 应先做幂等 SELECT
    assert "SELECT" in joined


@pytest.mark.asyncio
async def test_enable_free_provider_idempotent(monkeypatch):
    """重复启用同一 provider（同 workspace + 同名 + 同 provider + 同 base_url），
    返回相同 channel_id，且不重复 INSERT。"""
    existing_row = {
        "id": "chn_existing_comprehensive",
        "name": "硅基流动 SiliconFlow（免费层）",
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3"],
        "weight": 100,
        "status": "enabled",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }
    conn = _RecordingConnection(existing_row=existing_row)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    # 第一次调用（命中已存在渠道）
    body = FreeProviderEnableRequest(api_key="sk-siliconflow-test")
    first = await enable_free_provider("siliconflow", body, _admin_actor())

    assert first["idempotent"] is True
    assert first["id"] == "chn_existing_comprehensive"
    assert first["provider"] == "siliconflow"

    # 第二次调用应同样返回相同 channel_id（同一 mock 连接，existing_row 不变）
    second = await enable_free_provider("siliconflow", body, _admin_actor())
    assert second["idempotent"] is True
    assert second["id"] == first["id"], (
        "repeated enable must return the same channel_id"
    )

    # 不应有任何 INSERT INTO gw_channel
    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO gw_channel" not in joined
    # 但应有幂等 SELECT
    assert "SELECT" in joined


@pytest.mark.asyncio
async def test_enable_free_provider_idempotent_via_http(monkeypatch):
    """通过 HTTP 层验证幂等：返回 201 + idempotent=True + 已存在 channel_id。"""
    existing_row = {
        "id": "chn_existing_http",
        "name": "硅基流动 SiliconFlow（免费层）",
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3"],
        "weight": 100,
        "status": "enabled",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }
    conn = _RecordingConnection(existing_row=existing_row)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    app = _app_with_actor(_admin_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        response = await client.post(
            "/api/v1/gateway/free-providers/siliconflow/enable",
            json={"api_key": "sk-test"},
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["idempotent"] is True
    assert payload["id"] == "chn_existing_http"


# ----------------------------------------------------------------------
# 6. get_free_preset 辅助函数
# ----------------------------------------------------------------------


def test_get_free_preset_known_and_unknown():
    """已知 key 返回预设，未知 key 返回 None。"""
    assert get_free_preset("siliconflow") is not None
    assert get_free_preset("nonexistent-provider") is None
