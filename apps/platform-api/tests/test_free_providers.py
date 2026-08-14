"""免费 / 公益大模型供应商集成的端到端单元测试。

覆盖：
- ``FREE_PROVIDER_PRESETS`` 预设目录完整性（15 新增 + 9 第二批 + 5 第三批 + 7 现有 = 82 条）
- ``GET /api/v1/gateway/free-providers`` 返回完整预设
- ``POST /api/v1/gateway/free-providers/{provider_key}/enable`` 一键启用
- 无效 provider_key 返回 404
- 缺少 api_key 返回 422
- 非管理员不能启用（403）
- 重复启用幂等性

所有外部依赖（pool / redis）均通过 fake 类隔离，不依赖真实 DB/Redis/网络。
测试风格参考 test_setup.py 与 test_enterprise.py。
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
    PROVIDER_CATALOG,
    FreeProviderEnableRequest,
    enable_free_provider,
    list_free_providers,
)


# ----------------------------------------------------------------------
# 测试常量
# ----------------------------------------------------------------------


# 15 个新增免费 / 公益供应商
NEW_FREE_PROVIDER_KEYS = (
    "siliconflow",
    "openrouter",
    "groq",
    "together",
    "cerebras",
    "cloudflare",
    "huggingface",
    "modelscope",
    "iflytek",
    "dmxapi",
    "n1n",
    "github",
    "perplexity",
    "cohere",
    "mistral_free",
)

# 第二批免费 / 公益供应商（开源模型推理 / 聚合 / 官方免费层）
NEW_FREE_PROVIDER_KEYS_BATCH_2 = (
    "deepinfra",
    "fireworks",
    "novita",
    "lepton",
    "replicate",
    "stepfun",
    "lingyi",
    "gemini_free",
    "openai_free",
)

# 第三批免费 / 公益供应商（开源模型推理 / 国内免费层）
NEW_FREE_PROVIDER_KEYS_BATCH_3 = (
    "sambanova",
    "chutes",
    "nebius",
    "openbayes",
    "siliconflow_cn",
)

# 第四批免费 / 公益供应商（46 个新增：国际聚合 / 自部署 / 国内中转）
NEW_FREE_PROVIDER_KEYS_BATCH_4 = (
    "aimlapi",
    "monsterapi",
    "predibase",
    "baseten",
    "runpod",
    "anyscale",
    "modal",
    "featherless",
    "inference_net",
    "lambda",
    "fal",
    "bentocloud",
    "xai",
    "nvidia",
    "kluster",
    "hyperbolic",
    "ai21",
    "reka",
    "watsonx",
    "lightning",
    "duckduckgo",
    "gpt4free",
    "aihubmix",
    "api2d",
    "openai_hk",
    "closeai",
    "zhizengzeng",
    "ohmygpt",
    "chatanywhere",
    "ai_ls",
    "v3api",
    "gptgod",
    "minimax",
    "baichuan",
    "metaso",
    "ppio",
    "ollama",
    "oneapi",
    "newapi",
    "llamacpp",
    "vllm",
    "xinference",
    "localai",
    "lmdeploy",
    "lmstudio",
    "gpt_link",
)

# 7 个现有供应商（也提供免费试用额度）
EXISTING_FREE_PROVIDER_KEYS = (
    "deepseek",
    "qwen",
    "zhipu",
    "kimi",
    "doubao",
    "qianfan",
    "hunyuan",
)


# ----------------------------------------------------------------------
# 测试辅助：mock psycopg 连接池
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

    - SELECT (existing check) 返回 self._existing_row
    - INSERT 返回空结果
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


def _member_actor() -> Actor:
    return Actor(
        user_id="usr_member",
        workspace_id="wsp_test",
        org_id="org_test",
        role="member",
        email="member@example.com",
        display_name="Member",
        onboarding_completed=True,
    )


def _app_with_actor(actor: Actor | None, *, patch_pool=None):
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
# 1. FREE_PROVIDER_PRESETS 预设目录完整性
# ----------------------------------------------------------------------


def test_free_provider_presets_includes_all_15_new_providers():
    """15 个新增免费供应商全部存在预设。"""
    for key in NEW_FREE_PROVIDER_KEYS:
        assert key in FREE_PROVIDER_PRESETS, f"missing preset for new free provider: {key}"


def test_free_provider_presets_includes_batch_2_providers():
    """第二批 9 个免费供应商全部存在预设。"""
    for key in NEW_FREE_PROVIDER_KEYS_BATCH_2:
        assert key in FREE_PROVIDER_PRESETS, f"missing preset for batch 2 free provider: {key}"


def test_free_provider_presets_includes_batch_3_providers():
    """第三批 5 个免费供应商全部存在预设。"""
    for key in NEW_FREE_PROVIDER_KEYS_BATCH_3:
        assert key in FREE_PROVIDER_PRESETS, f"missing preset for batch 3 free provider: {key}"


def test_free_provider_presets_includes_batch_4_providers():
    """batch 4: 46 new free providers present。"""
    for key in NEW_FREE_PROVIDER_KEYS_BATCH_4:
        assert key in FREE_PROVIDER_PRESETS, f"missing preset for batch 4 free provider: {key}"


def test_free_provider_presets_includes_all_7_existing_free_providers():
    """7 个现有供应商也提供免费预设。"""
    for key in EXISTING_FREE_PROVIDER_KEYS:
        assert key in FREE_PROVIDER_PRESETS, f"missing preset for existing free provider: {key}"


def test_free_provider_presets_total_count_is_100():
    """预设目录总数 = 100（v7.134 起第五批 +18，达 100 条）。

    历史：v7.131=82 -> v7.134=100。本测试硬编码为 100，
    与 ``test_free_providers_comprehensive.py`` 的 ``>=100`` 门槛互为校验。
    """
    assert len(FREE_PROVIDER_PRESETS) == 100


def test_each_free_preset_has_required_fields():
    """每个预设必须包含完整字段：name、base_url、protocol、signup_url、free_quota、
    free_models、capabilities、regions、retention_mode、notes。"""
    required_fields = {
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
    for key, preset in FREE_PROVIDER_PRESETS.items():
        missing = required_fields - set(preset.keys())
        assert not missing, f"preset {key!r} missing fields: {missing}"
        # provider 字段要么等于预设 key，要么指向 PROVIDER_CATALOG 中已有的
        # 标准 provider（如 gemini_free -> gemini、openai_free -> openai）
        assert preset["provider"] == key or preset["provider"] in PROVIDER_CATALOG, (
            f"preset {key!r} provider must equal key or be a known catalog provider"
        )
        assert preset["protocol"] in ("openai", "gemini", "anthropic"), (
            f"preset {key!r} protocol must be openai/gemini/anthropic"
        )
        assert preset["free_models"], f"preset {key!r} has empty free_models"
        assert preset["capabilities"], f"preset {key!r} has empty capabilities"
        assert preset["regions"], f"preset {key!r} has empty regions"
        assert preset["retention_mode"] == "provider_retained"


def test_get_free_preset_returns_none_for_unknown_key():
    """未知 key 返回 None。"""
    assert get_free_preset("nonexistent-provider") is None


def test_get_free_preset_returns_preset_for_known_key():
    """已知 key 返回对应预设。"""
    preset = get_free_preset("siliconflow")
    assert preset is not None
    assert preset["provider"] == "siliconflow"
    assert "DeepSeek-V3" in " ".join(preset["free_models"])


# ----------------------------------------------------------------------
# 2. FreeProviderEnableRequest Pydantic 模型校验
# ----------------------------------------------------------------------


def test_enable_request_requires_api_key():
    """api_key 必填，缺失会触发 ValidationError（FastAPI 会转为 422）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FreeProviderEnableRequest()  # type: ignore[call-arg]


def test_enable_request_accepts_minimal_body_with_api_key_only():
    """只提供 api_key 也能通过校验，其他字段使用默认值。"""
    body = FreeProviderEnableRequest(api_key="sk-test-123")
    assert body.api_key == "sk-test-123"
    assert body.name is None
    assert body.models is None
    assert body.base_url is None
    assert body.enabled is True


def test_enable_request_rejects_empty_api_key():
    """空 api_key 被拒绝（min_length=1）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FreeProviderEnableRequest(api_key="")


# ----------------------------------------------------------------------
# 3. GET /api/v1/gateway/free-providers 端点
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_free_providers_returns_full_catalog():
    """GET /free-providers 返回 100 条预设，结构完整（v7.134 起 = 100）。"""
    result = await list_free_providers()
    assert result["total"] == 100
    items = result["items"]
    assert len(items) == 100

    # 每条 item 必须含 provider 字段（=预设 key，即可用于 enable URL 的 key）
    # 且 catalog_provider 字段指向该预设底层使用的 PROVIDER_CATALOG 适配器
    for item in items:
        assert "provider" in item
        assert item["provider"] in FREE_PROVIDER_PRESETS
        preset = FREE_PROVIDER_PRESETS[item["provider"]]
        assert item["catalog_provider"] == preset["provider"]
        assert item["catalog_provider"] == item["provider"] or item["catalog_provider"] in PROVIDER_CATALOG


@pytest.mark.asyncio
async def test_list_free_providers_endpoint_is_public(monkeypatch):
    """GET /free-providers 不需要认证（不依赖 get_actor）。"""
    # 不设置 dependency_overrides[get_actor]，模拟未认证请求
    app = FastAPI()
    app.include_router(gateway_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://free-providers.test"
    ) as client:
        response = await client.get("/api/v1/gateway/free-providers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 100
    assert len(payload["items"]) == 100
    provider_ids = {item["provider"] for item in payload["items"]}
    assert "siliconflow" in provider_ids
    assert "openrouter" in provider_ids
    assert "deepseek" in provider_ids


@pytest.mark.asyncio
async def test_list_free_providers_endpoint_returns_preset_fields(monkeypatch):
    """返回的 item 包含所有预设字段。"""
    app = FastAPI()
    app.include_router(gateway_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://free-providers.test"
    ) as client:
        response = await client.get("/api/v1/gateway/free-providers")
    items = response.json()["items"]
    siliconflow = next(item for item in items if item["provider"] == "siliconflow")
    for field in (
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
    ):
        assert field in siliconflow, f"siliconflow preset missing field: {field}"
    assert siliconflow["base_url"] == "https://api.siliconflow.cn/v1"


# ----------------------------------------------------------------------
# 4. POST /api/v1/gateway/free-providers/{key}/enable 一键启用
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_free_provider_creates_channel(monkeypatch):
    """管理员启用合法 provider 时，应插入 gw_channel 并返回 201。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    body = FreeProviderEnableRequest(api_key="sk-siliconflow-test")
    result = await enable_free_provider("siliconflow", body, _admin_actor())

    assert result["idempotent"] is False
    assert result["provider"] == "siliconflow"
    assert result["base_url"] == "https://api.siliconflow.cn/v1"
    assert result["status"] == "enabled"
    assert "DeepSeek-V3" in " ".join(result["models"])

    # 应该有 INSERT INTO gw_channel
    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO gw_channel" in joined


@pytest.mark.asyncio
async def test_enable_free_provider_via_http_returns_201(monkeypatch):
    """通过 HTTP 调用启用端点返回 201。"""
    conn = _RecordingConnection(existing_row=None)
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
    assert payload["provider"] == "siliconflow"
    assert payload["idempotent"] is False
    assert payload["status"] == "enabled"


@pytest.mark.asyncio
async def test_enable_free_provider_unknown_key_returns_404(monkeypatch):
    """无效 provider_key 返回 404。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    app = _app_with_actor(_admin_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        response = await client.post(
            "/api/v1/gateway/free-providers/nonexistent-provider/enable",
            json={"api_key": "sk-test"},
        )
    assert response.status_code == 404
    assert "Unknown free provider" in response.json()["detail"]
    # 不应有任何 DB 调用
    assert conn.calls == []


@pytest.mark.asyncio
async def test_enable_free_provider_missing_api_key_returns_422(monkeypatch):
    """请求体缺少 api_key 时返回 422（FastAPI 默认行为）。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    app = _app_with_actor(_admin_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        # 故意不传 api_key
        response = await client.post(
            "/api/v1/gateway/free-providers/siliconflow/enable",
            json={},
        )
    assert response.status_code == 422
    # 422 在 Pydantic 校验阶段触发，不应触达 DB
    assert conn.calls == []


@pytest.mark.asyncio
async def test_enable_free_provider_rejects_non_admin_with_403(monkeypatch):
    """非管理员（member）调用启用端点返回 403。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    app = _app_with_actor(_member_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://enable.test"
    ) as client:
        response = await client.post(
            "/api/v1/gateway/free-providers/siliconflow/enable",
            json={"api_key": "sk-test"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin role required"
    # 403 在权限校验阶段触发，不应触达 DB
    assert conn.calls == []


@pytest.mark.asyncio
async def test_enable_free_provider_is_idempotent_on_duplicate(monkeypatch):
    """同一 workspace + 同名 + 同 provider + 同 base_url 的渠道已存在时，
    返回已存在的渠道信息（idempotent=True），不重复 INSERT。"""
    existing_row = {
        "id": "chn_existing_001",
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

    body = FreeProviderEnableRequest(api_key="sk-siliconflow-test")
    result = await enable_free_provider("siliconflow", body, _admin_actor())

    # 应返回已存在渠道，标记 idempotent=True
    assert result["idempotent"] is True
    assert result["id"] == "chn_existing_001"
    assert result["provider"] == "siliconflow"

    # 不应有 INSERT INTO gw_channel
    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO gw_channel" not in joined
    # 应该有 SELECT 用于幂等检查
    assert "SELECT" in joined


@pytest.mark.asyncio
async def test_enable_free_provider_idempotent_via_http(monkeypatch):
    """通过 HTTP 调用，重复启用同一 provider 也应返回幂等结果。"""
    existing_row = {
        "id": "chn_existing_002",
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
    # 注意：即使返回已存在渠道，HTTP 状态码仍是 201（端点声明的 status_code）
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["idempotent"] is True
    assert payload["id"] == "chn_existing_002"


# ----------------------------------------------------------------------
# 5. 启用端点支持自定义参数（base_url / models / name / enabled）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_free_provider_with_custom_base_url_for_cloudflare(monkeypatch):
    """Cloudflare 预设 base_url 含 {account_id} 占位符，启用时支持自定义替换。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    custom_url = "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    body = FreeProviderEnableRequest(api_key="sk-cf-test", base_url=custom_url)
    result = await enable_free_provider("cloudflare", body, _admin_actor())

    assert result["base_url"] == custom_url
    assert result["provider"] == "cloudflare"

    # 验证 INSERT 时使用的是自定义 base_url
    insert_call = next(
        (q, p) for q, p in conn.calls if "INSERT INTO gw_channel" in q
    )
    _, params = insert_call
    # params 顺序: (id, workspace_id, name, provider, base_url, credential_enc, models, weight, status)
    assert params[4] == custom_url


@pytest.mark.asyncio
async def test_enable_free_provider_with_custom_name_and_models(monkeypatch):
    """支持自定义渠道名称和模型列表。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    body = FreeProviderEnableRequest(
        api_key="sk-groq-test",
        name="我的 Groq 渠道",
        models=["llama-3.3-70b-versatile"],
        enabled=False,
    )
    result = await enable_free_provider("groq", body, _admin_actor())

    assert result["name"] == "我的 Groq 渠道"
    assert result["models"] == ["llama-3.3-70b-versatile"]
    assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_enable_free_provider_for_existing_free_provider_deepseek(monkeypatch):
    """启用现有供应商 deepseek 的免费预设（验证 7 个现有供应商也可一键启用）。"""
    conn = _RecordingConnection(existing_row=None)
    monkeypatch.setattr(gateway_router, "pool", _Pool(conn))

    body = FreeProviderEnableRequest(api_key="sk-deepseek-test")
    result = await enable_free_provider("deepseek", body, _admin_actor())

    assert result["provider"] == "deepseek"
    assert result["base_url"] == "https://api.deepseek.com/v1"
    assert "deepseek-chat" in result["models"]
    assert result["idempotent"] is False


# ----------------------------------------------------------------------
# 6. 路由契约
# ----------------------------------------------------------------------


def test_gateway_router_exposes_free_provider_endpoints():
    """router 注册了 GET /free-providers 与 POST /free-providers/{key}/enable。"""
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in gateway_router.router.routes
    }
    assert ("/api/v1/gateway/free-providers", ("GET",)) in paths
    assert (
        "/api/v1/gateway/free-providers/{provider_key}/enable",
        ("POST",),
    ) in paths
