from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence
from urllib.parse import urlsplit

import httpx

from workama_platform.modules.security.service import (
    UrlValidationResult,
    validate_outbound_url,
    validate_resolved_outbound_url,
)


ModelAction = Literal["allow", "block", "mask"]
ModerationDirection = Literal["input", "output"]
ProviderName = Literal["none", "mock", "http"]

_ACTIONS = frozenset({"allow", "block", "mask"})
_PROVIDERS = frozenset({"none", "mock", "http"})
_DEFAULT_MODEL = "workama-moderation"
_DEFAULT_VERSION = "unconfigured"


class ModerationModelError(RuntimeError):
    """Provider failure without the content being included in the exception."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ModerationProvider(Protocol):
    name: str

    async def moderate(self, text: str, direction: ModerationDirection) -> "ProviderAssessment":
        ...


@dataclass(frozen=True)
class ProviderAssessment:
    action: ModelAction
    masked_text: str | None = None
    categories: tuple[str, ...] = ()
    model: str = _DEFAULT_MODEL
    model_version: str = _DEFAULT_VERSION


@dataclass(frozen=True)
class ModerationModelDecision:
    """The model-layer result. Block decisions never carry the submitted text."""

    action: ModelAction
    text: str | None
    provider: str
    model: str
    model_version_hash: str
    reason: str | None = None
    categories: tuple[str, ...] = ()
    failed_closed: bool = False

    @property
    def blocked(self) -> bool:
        return self.action == "block"

    @property
    def masked(self) -> bool:
        return self.action == "mask"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "model_version_hash": self.model_version_hash,
            "reason": self.reason,
            "categories": list(self.categories),
            "failed_closed": self.failed_closed,
        }


@dataclass(frozen=True)
class ModerationModelConfig:
    """Runtime-only provider settings; this object never stores submitted content."""

    provider: ProviderName = "none"
    endpoint: str | None = None
    api_key: str | None = None
    model: str = _DEFAULT_MODEL
    model_version: str = _DEFAULT_VERSION
    timeout_seconds: float = 3.0
    connect_timeout_seconds: float = 0.75
    max_input_chars: int = 1_000_000
    max_response_bytes: int = 64 * 1024
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider = str(self.provider).strip().lower()
        if provider not in _PROVIDERS:
            raise ValueError(f"Unsupported moderation model provider: {provider}")
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError("Moderation model name must be 1-200 characters")
        if not self.model_version.strip() or len(self.model_version) > 200:
            raise ValueError("Moderation model version must be 1-200 characters")
        if not 0.05 <= self.timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0.05 and 30")
        if not 0.01 <= self.connect_timeout_seconds <= self.timeout_seconds:
            raise ValueError("connect_timeout_seconds must be positive and no greater than timeout_seconds")
        if not 1 <= self.max_input_chars <= 10_000_000:
            raise ValueError("max_input_chars is out of range")
        if not 1024 <= self.max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError("max_response_bytes is out of range")
        normalized_hosts = tuple(sorted({host.strip().lower().rstrip(".") for host in self.allowed_hosts if host.strip()}))
        if any("/" in host or ":" in host for host in normalized_hosts):
            raise ValueError("allowed_hosts must contain host names only")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "model_version", self.model_version.strip())
        object.__setattr__(self, "allowed_hosts", normalized_hosts)

    @classmethod
    def from_env(cls, prefix: str = "WORKAMA_MODERATION_MODEL_") -> "ModerationModelConfig":
        """Load provider configuration without reading any content-bearing setting."""

        def _float(name: str, default: float) -> float:
            raw = os.getenv(prefix + name)
            return default if raw is None or not raw.strip() else float(raw)

        def _int(name: str, default: int) -> int:
            raw = os.getenv(prefix + name)
            return default if raw is None or not raw.strip() else int(raw)

        hosts = tuple(
            host.strip()
            for host in os.getenv(prefix + "ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        return cls(
            provider=os.getenv(prefix + "PROVIDER", "none").strip().lower(),
            endpoint=os.getenv(prefix + "ENDPOINT") or None,
            api_key=os.getenv(prefix + "API_KEY") or None,
            model=os.getenv(prefix + "MODEL", _DEFAULT_MODEL),
            model_version=os.getenv(prefix + "VERSION", _DEFAULT_VERSION),
            timeout_seconds=_float("TIMEOUT_SECONDS", 3.0),
            connect_timeout_seconds=_float("CONNECT_TIMEOUT_SECONDS", 0.75),
            max_input_chars=_int("MAX_INPUT_CHARS", 1_000_000),
            max_response_bytes=_int("MAX_RESPONSE_BYTES", 64 * 1024),
            allowed_hosts=hosts,
        )


def model_version_hash(provider: str, model: str, version: str) -> str:
    """Hash only provider metadata, never the submitted text or response."""

    canonical = "|".join((provider.strip().lower(), model.strip(), version.strip()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decision(
    *,
    action: ModelAction,
    text: str | None,
    provider: str,
    model: str,
    version: str,
    reason: str | None = None,
    categories: Sequence[str] = (),
    failed_closed: bool = False,
) -> ModerationModelDecision:
    return ModerationModelDecision(
        action=action,
        text=text if action != "block" else None,
        provider=provider,
        model=model,
        model_version_hash=model_version_hash(provider, model, version),
        reason=reason,
        categories=tuple(str(category) for category in categories)[:50],
        failed_closed=failed_closed,
    )


def _validate_assessment(assessment: ProviderAssessment) -> ProviderAssessment:
    if assessment.action not in _ACTIONS:
        raise ModerationModelError("invalid_provider_action")
    if len(assessment.model) > 200 or not assessment.model.strip():
        raise ModerationModelError("invalid_provider_model")
    if len(assessment.model_version) > 200 or not assessment.model_version.strip():
        raise ModerationModelError("invalid_provider_version")
    if assessment.action == "mask":
        if not isinstance(assessment.masked_text, str) or not assessment.masked_text:
            raise ModerationModelError("invalid_masked_response")
    return assessment


class DeterministicMockProvider:
    """Small deterministic provider for local tests and development only."""

    name = "mock"

    def __init__(
        self,
        *,
        blocked_terms: Sequence[str] = ("forbidden", "secret"),
        masked_terms: Sequence[str] = ("email@example.com",),
        model: str = "workama-moderation-mock",
        model_version: str = "v1",
    ) -> None:
        self.blocked_terms = tuple(term.strip() for term in blocked_terms if term.strip())
        self.masked_terms = tuple(term.strip() for term in masked_terms if term.strip())
        self.model = model
        self.model_version = model_version

    async def moderate(self, text: str, direction: ModerationDirection) -> ProviderAssessment:
        del direction
        for term in self.blocked_terms:
            if re.search(re.escape(term), text, flags=re.IGNORECASE):
                return ProviderAssessment(
                    action="block",
                    categories=("mock_block",),
                    model=self.model,
                    model_version=self.model_version,
                )
        masked = text
        matched = False
        for term in self.masked_terms:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            if pattern.search(masked):
                matched = True
                masked = pattern.sub("***", masked)
        if matched:
            return ProviderAssessment(
                action="mask",
                masked_text=masked,
                categories=("mock_mask",),
                model=self.model,
                model_version=self.model_version,
            )
        return ProviderAssessment(
            action="allow",
            model=self.model,
            model_version=self.model_version,
        )


def _host_is_allowed(hostname: str, allowed_hosts: Sequence[str]) -> bool:
    if not allowed_hosts:
        return True
    normalized = hostname.rstrip(".").lower()
    return normalized in {host.rstrip(".").lower() for host in allowed_hosts}


async def validate_provider_endpoint(
    endpoint: str | None,
    *,
    allowed_hosts: Sequence[str] = (),
    timeout_seconds: float = 0.75,
) -> UrlValidationResult:
    """Validate scheme, host, DNS resolution, and an optional exact host allowlist."""

    if not endpoint:
        return UrlValidationResult(False, "provider endpoint is not configured")
    initial = validate_outbound_url(endpoint)
    if not initial.allowed:
        return initial
    hostname = urlsplit(endpoint).hostname
    if not hostname:
        return UrlValidationResult(False, "provider host is missing")
    if not _host_is_allowed(hostname, allowed_hosts):
        return UrlValidationResult(False, "provider host is outside the allowlist")
    try:
        resolved = await asyncio.wait_for(
            validate_resolved_outbound_url(endpoint), timeout=max(0.05, timeout_seconds)
        )
    except asyncio.TimeoutError:
        return UrlValidationResult(False, "provider DNS validation timed out")
    return resolved


class HttpModerationProvider:
    """HTTP JSON provider with SSRF checks, no redirects, bounded response, and timeouts."""

    name = "http"

    def __init__(
        self,
        config: ModerationModelConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        endpoint_validator: Callable[..., Awaitable[UrlValidationResult]] = validate_provider_endpoint,
    ) -> None:
        if config.provider != "http":
            raise ValueError("HttpModerationProvider requires provider='http'")
        self.config = config
        self.transport = transport
        self.endpoint_validator = endpoint_validator

    async def moderate(self, text: str, direction: ModerationDirection) -> ProviderAssessment:
        validation = await self.endpoint_validator(
            self.config.endpoint,
            allowed_hosts=self.config.allowed_hosts,
            timeout_seconds=self.config.connect_timeout_seconds,
        )
        if not validation.allowed:
            reason = "ssrf_rejected" if validation.reason != "provider endpoint is not configured" else "endpoint_not_configured"
            raise ModerationModelError(reason)

        timeout = httpx.Timeout(
            self.config.timeout_seconds,
            connect=self.config.connect_timeout_seconds,
        )
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        payload = {"model": self.config.model, "direction": direction, "input": text}

        async def _request() -> ProviderAssessment:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers=headers,
                transport=self.transport,
            ) as client:
                async with client.stream("POST", self.config.endpoint or "", json=payload) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ModerationModelError("provider_http_error")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.config.max_response_bytes:
                            raise ModerationModelError("provider_response_too_large")
                        chunks.append(chunk)
                    try:
                        body = json.loads(b"".join(chunks))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ModerationModelError("invalid_provider_response") from exc
            if not isinstance(body, dict):
                raise ModerationModelError("invalid_provider_response")
            action = body.get("action")
            if action not in _ACTIONS:
                raise ModerationModelError("invalid_provider_action")
            masked_text = body.get("text")
            if masked_text is not None and not isinstance(masked_text, str):
                raise ModerationModelError("invalid_provider_response")
            categories = body.get("categories", ())
            if not isinstance(categories, list | tuple) or any(not isinstance(item, str) for item in categories):
                raise ModerationModelError("invalid_provider_categories")
            return _validate_assessment(
                ProviderAssessment(
                    action=action,
                    masked_text=masked_text,
                    categories=tuple(categories),
                    model=str(body.get("model") or self.config.model),
                    model_version=str(body.get("model_version") or self.config.model_version),
                )
            )

        try:
            return await asyncio.wait_for(_request(), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ModerationModelError("provider_timeout") from exc
        except httpx.TimeoutException as exc:
            raise ModerationModelError("provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise ModerationModelError("provider_network_error") from exc


class ModerationModelService:
    """Model review boundary with explicit fail-closed behavior."""

    def __init__(
        self,
        config: ModerationModelConfig | None = None,
        *,
        provider: ModerationProvider | None = None,
    ) -> None:
        self.config = config or ModerationModelConfig.from_env()
        self.provider = provider if provider is not None else self._build_provider()

    def _build_provider(self) -> ModerationProvider | None:
        if self.config.provider == "mock":
            return DeterministicMockProvider(
                model=self.config.model,
                model_version=self.config.model_version,
            )
        if self.config.provider == "http":
            return HttpModerationProvider(self.config)
        return None

    async def moderate(self, text: str, direction: ModerationDirection) -> ModerationModelDecision:
        if not isinstance(text, str):
            return self._fail_closed("invalid_input")
        if direction not in {"input", "output"}:
            return self._fail_closed("invalid_direction")
        if len(text) > self.config.max_input_chars:
            return self._fail_closed("input_too_large")
        if self.provider is None:
            # No configured model is an unavailable safety control, never an implicit allow.
            return self._fail_closed("provider_not_configured")
        try:
            assessment = _validate_assessment(
                await asyncio.wait_for(
                    self.provider.moderate(text, direction),
                    timeout=self.config.timeout_seconds,
                )
            )
            if assessment.action == "mask":
                if assessment.masked_text == text or len(assessment.masked_text or "") > self.config.max_input_chars:
                    return self._fail_closed("invalid_masked_response")
                output_text = assessment.masked_text
            else:
                output_text = text
            return _decision(
                action=assessment.action,
                text=output_text,
                provider=getattr(self.provider, "name", self.config.provider),
                model=assessment.model,
                version=assessment.model_version,
                categories=assessment.categories,
            )
        except asyncio.TimeoutError:
            return self._fail_closed("provider_timeout")
        except ModerationModelError as exc:
            return self._fail_closed(exc.reason)
        except Exception:
            return self._fail_closed("provider_error")

    def _fail_closed(self, reason: str) -> ModerationModelDecision:
        return _decision(
            action="block",
            text=None,
            provider=self.config.provider,
            model=self.config.model,
            version=self.config.model_version,
            reason=reason,
            failed_closed=True,
        )


def create_moderation_model_service(
    config: ModerationModelConfig | None = None,
) -> ModerationModelService:
    return ModerationModelService(config)


async def moderate_with_model(
    text: str,
    direction: ModerationDirection,
    *,
    config: ModerationModelConfig | None = None,
    provider: ModerationProvider | None = None,
) -> ModerationModelDecision:
    return await ModerationModelService(config, provider=provider).moderate(text, direction)


__all__ = [
    "DeterministicMockProvider",
    "HttpModerationProvider",
    "ModerationDirection",
    "ModerationModelConfig",
    "ModerationModelDecision",
    "ModerationModelError",
    "ModerationModelService",
    "ModerationProvider",
    "ProviderAssessment",
    "create_moderation_model_service",
    "model_version_hash",
    "moderate_with_model",
    "validate_provider_endpoint",
]
