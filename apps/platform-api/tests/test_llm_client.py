"""统一 LLM 客户端 (gateway.llm_client) 单元测试。

v7.159：12 个测试覆盖 ``call_llm`` 的所有路径：
- 成功路径：mock httpx 返回 200 + 合法 JSON，验证返回 method=llm
- 容错路径：无 API Key / 401 / 超时 / 连接错误 / 5xx / 非法 JSON / 空 content
  全部回退到 method=mock，不抛错
- 行为路径：disabled 强制 mock / 支持 system 消息 / 记录 tokens_used / 自定义 model
- 格式路径：返回 dict 含 content/tokens_used/model/method 四个字段

所有测试通过 mock ``httpx.AsyncClient.post`` 模拟 gateway 响应，不真实调用 gateway。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from workama_platform.modules.gateway import llm_client
from workama_platform.modules.gateway.llm_client import call_llm


# ============================================================================
# 测试辅助
# ============================================================================


class _LLMResponse:
    """模拟 httpx gateway 响应。"""

    def __init__(self, status_code=200, content_text="hello", *, with_usage=True):
        self.status_code = status_code
        body: dict = {}
        if status_code < 400:
            body["choices"] = [{"message": {"content": content_text}}]
            if with_usage:
                body["usage"] = {"total_tokens": 42}
        else:
            body = {"error": "bad request"}
        self._body = body

    def json(self):
        return self._body


class _NonJsonResponse:
    """模拟 httpx 返回非 JSON body。"""

    status_code = 200

    def json(self):
        raise ValueError("not json")


def _patch_gateway_post(monkeypatch, *, response=None, exc=None):
    """拦截 gateway ``/v1/chat/completions`` 调用，不影响 ASGITransport 请求。

    - response: 返回的响应对象（exc 为 None 时生效）
    - exc: 抛出的异常（优先于 response）
    """
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):
        if "/v1/chat/completions" in str(url):
            if exc is not None:
                raise exc
            return response
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _actor() -> SimpleNamespace:
    """构造最小 actor，仅含 user_id 字段供 llm_client 使用。"""
    return SimpleNamespace(user_id="usr_test")


# ============================================================================
# 1. 成功路径
# ============================================================================


class TestCallLlmSuccess:
    """call_llm 成功路径测试。"""

    @pytest.mark.asyncio
    async def test_call_llm_success_returns_llm_method(self, monkeypatch):
        """mock httpx 返回 200 + 合法 JSON，验证返回 method='llm'。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "Hello from LLM")
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "llm"
        assert result["content"] == "Hello from LLM"
        assert result["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_call_llm_records_tokens_used_from_usage(self, monkeypatch):
        """tokens_used 来自 gateway 返回的 usage.total_tokens。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "abc", with_usage=True)
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "llm"
        # _LLMResponse 默认 usage.total_tokens = 42
        assert result["tokens_used"] == 42

    @pytest.mark.asyncio
    async def test_call_llm_records_tokens_used_when_no_usage(self, monkeypatch):
        """gateway 不返回 usage 时，tokens_used 回退到 len(content)//4。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        # content = "abcdefgh" 长度 8，//4 = 2
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "abcdefgh", with_usage=False)
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "llm"
        assert result["tokens_used"] == 2  # len("abcdefgh") // 4 = 2


# ============================================================================
# 2. 容错路径
# ============================================================================


class TestCallLlmFallback:
    """call_llm 失败回退测试（全部回退到 mock，不抛错）。"""

    @pytest.mark.asyncio
    async def test_call_llm_no_api_key_returns_mock(self, monkeypatch):
        """未配置 WORKAMA_INTERNAL_LLM_API_KEY 时返回 method='mock'。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        # 即使 mock 了 httpx，也不应被调用（因为没 API Key）
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "should not be reached")
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"
        assert "[mock-llm]" in result["content"]
        assert result["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_call_llm_401_returns_mock(self, monkeypatch):
        """gateway 返回 401 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, response=_LLMResponse(401, ""))

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_timeout_returns_mock(self, monkeypatch):
        """httpx 抛 TimeoutException 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, exc=httpx.TimeoutException("simulated timeout")
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_connect_error_returns_mock(self, monkeypatch):
        """httpx 抛 ConnectError 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, exc=httpx.ConnectError("simulated connect error"))

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_5xx_returns_mock(self, monkeypatch):
        """gateway 返回 5xx 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, response=_LLMResponse(503, ""))

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_non_json_body_returns_mock(self, monkeypatch):
        """gateway 返回非 JSON body 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, response=_NonJsonResponse())

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_empty_content_returns_mock(self, monkeypatch):
        """gateway 返回空 content 时回退到 mock。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, response=_LLMResponse(200, ""))

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_disabled_kwarg_returns_mock(self, monkeypatch):
        """disabled=True 时直接走 mock，即使配置了 API Key。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "should not be reached")
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
            disabled=True,
        )
        assert result["method"] == "mock"


# ============================================================================
# 3. 行为路径
# ============================================================================


class TestCallLlmBehavior:
    """call_llm 行为测试：system 消息 / 返回格式 / 自定义 model。"""

    @pytest.mark.asyncio
    async def test_call_llm_supports_system_message(self, monkeypatch):
        """messages 含 system 消息时，gateway payload 应包含 system role。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        captured: dict = {}
        real_post = httpx.AsyncClient.post

        async def fake_post(self, url, *args, **kwargs):
            if "/v1/chat/completions" in str(url):
                captured["payload"] = kwargs.get("json")
                return _LLMResponse(200, "ok")
            return await real_post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        result = await call_llm(
            messages=messages,
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "llm"
        assert captured["payload"]["messages"] == messages
        assert captured["payload"]["messages"][0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_call_llm_returns_correct_format(self, monkeypatch):
        """返回 dict 含 content/tokens_used/model/method 四个字段。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "hello world")
        )

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert set(result.keys()) == {"content", "tokens_used", "model", "method"}
        assert isinstance(result["content"], str)
        assert isinstance(result["tokens_used"], int)
        assert isinstance(result["model"], str)
        assert result["method"] in ("llm", "mock")

    @pytest.mark.asyncio
    async def test_call_llm_custom_model_propagated(self, monkeypatch):
        """自定义 model 名应透传到 gateway payload 与返回值。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        captured: dict = {}
        real_post = httpx.AsyncClient.post

        async def fake_post(self, url, *args, **kwargs):
            if "/v1/chat/completions" in str(url):
                captured["payload"] = kwargs.get("json")
                return _LLMResponse(200, "ok")
            return await real_post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        result = await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="deepseek-chat",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["model"] == "deepseek-chat"
        assert captured["payload"]["model"] == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_call_llm_mock_response_deterministic(self, monkeypatch):
        """同一输入的 mock 响应应是确定性的（重复调用相同）。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)

        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ]
        result1 = await call_llm(
            messages=messages,
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        result2 = await call_llm(
            messages=messages,
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result1 == result2
        assert result1["method"] == "mock"

    @pytest.mark.asyncio
    async def test_call_llm_actor_user_id_in_header(self, monkeypatch):
        """actor.user_id 应放入 X-Actor-Id 请求头。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        captured: dict = {}
        real_post = httpx.AsyncClient.post

        async def fake_post(self, url, *args, **kwargs):
            if "/v1/chat/completions" in str(url):
                captured["headers"] = kwargs.get("headers")
                return _LLMResponse(200, "ok")
            return await real_post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        actor = SimpleNamespace(user_id="usr_custom_42")
        await call_llm(
            messages=[{"role": "user", "content": "Hi"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=actor,
        )
        assert captured["headers"]["X-Actor-Id"] == "usr_custom_42"
        assert captured["headers"]["X-Workspace-Id"] == "wsp_test"
        assert captured["headers"]["Authorization"] == "Bearer sk-test-internal-key"
