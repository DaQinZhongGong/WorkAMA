"""Tests for LLM status/test endpoints and internal_channel auto-select (v7.160).

Covers:
- ``auto_select_best_free_provider`` returns a known provider with models
- ``get_internal_llm_status`` returns correct structure under various env configs
- ``GET /api/v1/gateway/llm-status`` returns status dict
- ``POST /api/v1/gateway/llm-test`` returns mock response with timing when no API key
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from workama_platform.modules.gateway.internal_channel import (
    AUTO_SELECT_PROVIDER,
    DEFAULT_INTERNAL_PROVIDER,
    auto_select_best_free_provider,
    get_internal_llm_status,
)
from workama_platform.modules.gateway.free_presets import FREE_PROVIDER_PRESETS
from workama_platform.modules.gateway.llm_client import call_llm


# ============================================================================
# 1. auto_select_best_free_provider
# ============================================================================


class TestAutoSelectBestFreeProvider:
    """auto_select_best_free_provider 单元测试。"""

    def test_returns_a_known_provider_key(self):
        """auto_select 应返回 FREE_PROVIDER_PRESETS 中的有效 key。"""
        result = auto_select_best_free_provider()
        assert result is not None
        assert result in FREE_PROVIDER_PRESETS

    def test_returned_provider_has_free_models(self):
        """auto_select 返回的供应商应有至少一个 free_model。"""
        result = auto_select_best_free_provider()
        assert result is not None
        preset = FREE_PROVIDER_PRESETS[result]
        assert len(preset.get("free_models", [])) > 0

    def test_returned_provider_has_non_localhost_base_url(self):
        """auto_select 返回的供应商 base_url 不应包含 localhost。"""
        result = auto_select_best_free_provider()
        assert result is not None
        preset = FREE_PROVIDER_PRESETS[result]
        assert "localhost" not in (preset.get("base_url") or "")

    def test_prefers_siliconflow_if_available(self):
        """siliconflow 应被优先选择（它在偏好列表首位）。"""
        result = auto_select_best_free_provider()
        # siliconflow has non-localhost base_url and free_models,
        # so it should be the first choice
        assert result == "siliconflow"


# ============================================================================
# 2. get_internal_llm_status
# ============================================================================


class TestGetInternalLlmStatus:
    """get_internal_llm_status 单元测试。"""

    def test_returns_mock_when_no_api_key(self, monkeypatch):
        """未配置 API Key 时 method 应为 mock。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["method"] == "mock"
        assert status["api_key_configured"] is False

    def test_returns_llm_when_api_key_set(self, monkeypatch):
        """配置了 API Key 时 method 应为 llm。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-key")
        status = get_internal_llm_status()
        assert status["method"] == "llm"
        assert status["api_key_configured"] is True

    def test_auto_selected_when_provider_is_auto(self, monkeypatch):
        """provider 设为 auto 时 auto_selected 应为 True。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_PROVIDER", "auto")
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["auto_selected"] is True
        assert status["provider"] is not None

    def test_auto_selected_when_provider_is_empty(self, monkeypatch):
        """provider 为空时 auto_selected 应为 True。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["auto_selected"] is True

    def test_not_auto_selected_when_provider_explicit(self, monkeypatch):
        """provider 显式指定时 auto_selected 应为 False。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_PROVIDER", "groq")
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["auto_selected"] is False
        assert status["provider"] == "groq"

    def test_returns_all_required_fields(self, monkeypatch):
        """返回 dict 应包含所有必需字段。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_PROVIDER", raising=False)
        status = get_internal_llm_status()
        required_fields = {
            "provider",
            "method",
            "auto_selected",
            "gateway_url",
            "api_key_configured",
            "available_channels",
            "total_free_presets",
        }
        assert required_fields <= set(status.keys())

    def test_available_channels_positive(self, monkeypatch):
        """available_channels 应大于 0（我们有 100+ 个免费预设）。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["available_channels"] > 0

    def test_total_free_presets_matches_constant(self, monkeypatch):
        """total_free_presets 应等于 FREE_PROVIDER_PRESETS 的长度。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["total_free_presets"] == len(FREE_PROVIDER_PRESETS)

    def test_gateway_url_from_env(self, monkeypatch):
        """gateway_url 应来自 WORKAMA_GATEWAY_URL 环境变量。"""
        monkeypatch.setenv("WORKAMA_GATEWAY_URL", "http://custom-gateway:9090")
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        status = get_internal_llm_status()
        assert status["gateway_url"] == "http://custom-gateway:9090"


# ============================================================================
# 3. LLM test endpoint integration (via call_llm)
# ============================================================================


class _LLMResponse:
    """Simulated httpx gateway response."""

    def __init__(self, status_code=200, content_text="hello"):
        self.status_code = status_code
        body: dict = {}
        if status_code < 400:
            body["choices"] = [{"message": {"content": content_text}}]
            body["usage"] = {"total_tokens": 42}
        else:
            body = {"error": "bad request"}
        self._body = body

    def json(self):
        return self._body


def _patch_gateway_post(monkeypatch, *, response=None, exc=None):
    """Patch httpx.AsyncClient.post for gateway calls."""
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):
        if "/v1/chat/completions" in str(url):
            if exc is not None:
                raise exc
            return response
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _actor() -> SimpleNamespace:
    return SimpleNamespace(user_id="usr_test")


class TestLlmTestEndpoint:
    """Test the llm-test endpoint behavior via call_llm."""

    @pytest.mark.asyncio
    async def test_llm_test_returns_mock_without_api_key(self, monkeypatch):
        """llm-test 在无 API Key 时应返回 mock 响应。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        result = await call_llm(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "mock"
        assert "hello" in result["content"]

    @pytest.mark.asyncio
    async def test_llm_test_returns_llm_with_api_key(self, monkeypatch):
        """llm-test 在有 API Key 时应调用 LLM 并返回结果。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "Hello from LLM")
        )
        result = await call_llm(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert result["method"] == "llm"
        assert result["content"] == "Hello from LLM"

    @pytest.mark.asyncio
    async def test_llm_test_result_has_required_fields(self, monkeypatch):
        """llm-test 结果应包含 content/tokens_used/model/method 字段。"""
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)
        result = await call_llm(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-4o-mini",
            workspace_id="wsp_test",
            actor=_actor(),
        )
        assert "content" in result
        assert "tokens_used" in result
        assert "model" in result
        assert "method" in result
