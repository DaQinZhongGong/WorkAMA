from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from workama_platform.modules.moderation_model import (
    DeterministicMockProvider,
    HttpModerationProvider,
    ModerationModelConfig,
    ModerationModelService,
    ProviderAssessment,
    model_version_hash,
    validate_provider_endpoint,
)
from workama_platform.modules.security.service import UrlValidationResult


@pytest.mark.asyncio
async def test_unconfigured_provider_fails_closed_without_original_text():
    service = ModerationModelService(
        ModerationModelConfig(provider="none", model="reviewer", model_version="unset")
    )
    decision = await service.moderate("private user content", "input")

    assert decision.action == "block"
    assert decision.text is None
    assert decision.reason == "provider_not_configured"
    assert decision.failed_closed
    assert decision.model_version_hash == model_version_hash("none", "reviewer", "unset")
    assert "private user content" not in repr(decision)


@pytest.mark.asyncio
async def test_deterministic_mock_provider_supports_allow_block_and_mask():
    config = ModerationModelConfig(
        provider="mock",
        model="mock-reviewer",
        model_version="2026-07-15",
    )
    service = ModerationModelService(
        config,
        provider=DeterministicMockProvider(
            blocked_terms=("blocked",),
            masked_terms=("alice@example.com",),
            model="mock-reviewer",
            model_version="2026-07-15",
        ),
    )

    allowed = await service.moderate("ordinary message", "input")
    blocked = await service.moderate("this is BLOCKED", "output")
    masked = await service.moderate("contact alice@example.com", "output")

    assert allowed.action == "allow"
    assert allowed.text == "ordinary message"
    assert blocked.action == "block"
    assert blocked.text is None
    assert masked.action == "mask"
    assert masked.text == "contact ***"
    assert masked.model_version_hash == model_version_hash("mock", "mock-reviewer", "2026-07-15")


@pytest.mark.asyncio
async def test_http_provider_sends_content_only_to_validated_endpoint_and_parses_mask():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "action": "mask",
                "text": "hello ***",
                "categories": ["pii"],
                "model": "remote-reviewer",
                "model_version": "2026.07",
            },
        )

    async def allow_endpoint(*args, **kwargs) -> UrlValidationResult:
        return UrlValidationResult(True)

    config = ModerationModelConfig(
        provider="http",
        endpoint="https://moderation.example.com/review",
        allowed_hosts=("moderation.example.com",),
        api_key="secret-provider-key",
        timeout_seconds=1,
    )
    provider = HttpModerationProvider(
        config,
        transport=httpx.MockTransport(handler),
        endpoint_validator=allow_endpoint,
    )
    decision = await ModerationModelService(config, provider=provider).moderate(
        "hello alice@example.com", "output"
    )

    assert decision.action == "mask"
    assert decision.text == "hello ***"
    assert decision.categories == ("pii",)
    assert seen["url"] == "https://moderation.example.com/review"
    assert seen["payload"] == {
        "model": "workama-moderation",
        "direction": "output",
        "input": "hello alice@example.com",
    }
    assert decision.model_version_hash == model_version_hash("http", "remote-reviewer", "2026.07")


@pytest.mark.asyncio
async def test_http_provider_ignores_provider_text_for_allow_and_blocks_ssrf():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"action": "allow", "text": "provider altered content"})

    async def allow_endpoint(*args, **kwargs) -> UrlValidationResult:
        return UrlValidationResult(True)

    safe_config = ModerationModelConfig(
        provider="http", endpoint="https://moderation.example.com/review", timeout_seconds=1
    )
    safe_provider = HttpModerationProvider(
        safe_config,
        transport=httpx.MockTransport(handler),
        endpoint_validator=allow_endpoint,
    )
    allowed = await ModerationModelService(safe_config, provider=safe_provider).moderate(
        "original content", "input"
    )
    assert allowed.action == "allow"
    assert allowed.text == "original content"

    ssrf_config = ModerationModelConfig(
        provider="http", endpoint="http://127.0.0.1:8000/review", timeout_seconds=1
    )
    ssrf_provider = HttpModerationProvider(ssrf_config, transport=httpx.MockTransport(handler))
    rejected = await ModerationModelService(ssrf_config, provider=ssrf_provider).moderate(
        "do not send this", "input"
    )
    assert rejected.action == "block"
    assert rejected.reason == "ssrf_rejected"
    assert rejected.failed_closed
    assert rejected.text is None


@pytest.mark.asyncio
async def test_provider_timeout_and_oversized_response_fail_closed():
    class SlowProvider:
        name = "slow"

        async def moderate(self, text: str, direction: str) -> ProviderAssessment:
            del text, direction
            await asyncio.sleep(0.2)
            return ProviderAssessment(action="allow")

    timeout_config = ModerationModelConfig(
        provider="mock", timeout_seconds=0.05, connect_timeout_seconds=0.05
    )
    timed_out = await ModerationModelService(timeout_config, provider=SlowProvider()).moderate(
        "sensitive text", "input"
    )
    assert timed_out.action == "block"
    assert timed_out.reason == "provider_timeout"
    assert timed_out.failed_closed

    async def allow_endpoint(*args, **kwargs) -> UrlValidationResult:
        return UrlValidationResult(True)

    oversized_config = ModerationModelConfig(
        provider="http",
        endpoint="https://moderation.example.com/review",
        max_response_bytes=1024,
        timeout_seconds=1,
    )
    oversized_provider = HttpModerationProvider(
        oversized_config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"{" + b"a" * 2048 + b"}")
        ),
        endpoint_validator=allow_endpoint,
    )
    oversized = await ModerationModelService(
        oversized_config, provider=oversized_provider
    ).moderate("sensitive text", "output")
    assert oversized.action == "block"
    assert oversized.reason == "provider_response_too_large"


@pytest.mark.asyncio
async def test_endpoint_validation_rejects_private_hosts_and_enforces_allowlist():
    for endpoint in (
        "http://127.0.0.1:8000/review",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/review",
        "ftp://moderation.example.com/review",
    ):
        result = await validate_provider_endpoint(endpoint)
        assert not result.allowed, endpoint

    result = await validate_provider_endpoint(
        "https://other.example.com/review",
        allowed_hosts=("moderation.example.com",),
        timeout_seconds=0.1,
    )
    assert not result.allowed
    assert result.reason == "provider host is outside the allowlist"


def test_config_from_env_is_explicit_and_does_not_accept_unbounded_timeout(monkeypatch):
    monkeypatch.setenv("WORKAMA_MODERATION_MODEL_PROVIDER", "http")
    monkeypatch.setenv("WORKAMA_MODERATION_MODEL_ENDPOINT", "https://moderation.example.com/review")
    monkeypatch.setenv("WORKAMA_MODERATION_MODEL_ALLOWED_HOSTS", "moderation.example.com")
    monkeypatch.setenv("WORKAMA_MODERATION_MODEL_TIMEOUT_SECONDS", "2")
    config = ModerationModelConfig.from_env()
    assert config.provider == "http"
    assert config.allowed_hosts == ("moderation.example.com",)
    assert config.timeout_seconds == 2

    with pytest.raises(ValueError, match="between 0.05 and 30"):
        ModerationModelConfig(provider="http", timeout_seconds=31)
