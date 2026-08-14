import pytest
import httpx

from workama_platform import worker
from workama_platform.modules import external_apps
from workama_platform.modules.security.service import UrlValidationResult


def test_external_app_endpoint_and_config_validation_block_ssrf_and_secrets():
    body = external_apps.ExternalAppCreate(
        name="Dify",
        provider="dify",
        endpoint="https://dify.example.com/api",
        credential="dify-secret-value",
        config={"timeout_seconds": 30},
    )
    assert body.endpoint == "https://dify.example.com/api"
    assert body.config["timeout_seconds"] == 30
    with pytest.raises(ValueError):
        external_apps.ExternalAppCreate(name="Local", provider="fastgpt", endpoint="http://127.0.0.1:8080", config={})
    with pytest.raises(ValueError):
        external_apps.ExternalAppCreate(name="Secret", provider="ragflow", endpoint="https://ragflow.example.com", config={"api_key": "secret"})

    controlled = external_apps.ExternalAppCreate(
        name="Controlled Dify",
        provider="dify",
        endpoint="mock://dify/smoke",
        config={"timeout_seconds": 30},
    )
    assert controlled.endpoint == "mock://dify/smoke"
    assert external_apps._execution_mode(controlled.endpoint) == "controlled_mock"

    http_test = external_apps.ExternalAppCreate(
        name="HTTP Test Dify",
        provider="dify",
        endpoint="https://provider.example.com/invoke",
        config={"execution_mode": "http_test", "timeout_seconds": 1, "max_retries": 2, "backoff_ms": 0},
    )
    assert external_apps._execution_mode(http_test.endpoint, http_test.config) == "http_test"
    external_http = external_apps.ExternalAppCreate(
        name="External HTTP Dify",
        provider="dify",
        endpoint="https://staging.example.com/invoke",
        credential="staging-credential",
        config={"execution_mode": "external_http", "max_retries": 2, "backoff_ms": 0},
    )
    assert external_apps._execution_mode(external_http.endpoint, external_http.config) == "external_http"
    assert external_apps.external_http_block_reason(
        {"provider": "dify", "endpoint": external_http.endpoint, "config": external_http.config, "credential_hash": None}
    ) == "staging_credential_required"
    with pytest.raises(ValueError):
        external_apps.ExternalAppCreate(
            name="HTTP Header Test",
            provider="dify",
            endpoint="https://provider.example.com/invoke",
            config={"execution_mode": "http_test", "headers": {"x-test": "value"}},
        )
    with pytest.raises(ValueError):
        external_apps.ExternalAppCreate(
            name="Too Many Retries",
            provider="dify",
            endpoint="https://provider.example.com/invoke",
            config={"execution_mode": "http_test", "max_retries": 3},
        )


def test_external_app_view_is_hash_only_and_masks_endpoint():
    view = external_apps._app_view(
        {
            "id": "extapp_1",
            "name": "Dify",
            "provider": "dify",
            "endpoint": "https://dify.example.com/api/v1",
            "credential_hash": "hashed",
            "credential_last4": "alue",
            "config": {},
            "status": "active",
            "enabled": True,
            "version": 1,
            "created_at": None,
            "updated_at": None,
        }
    )
    assert view["endpoint"] == "https://dify.example.com/api/v1"
    assert view["credential_configured"] is True
    assert "credential_hash" not in view
    assert "credential" not in view


def test_marketplace_models_require_controlled_artifacts_and_safe_manifests():
    template = external_apps.TemplateCreate(
        name="support-agent",
        display_name="Support Agent",
        template_type="assistant",
        version="1.0.0",
        manifest={"entrypoint": "mock://template/support-agent"},
        artifact_ref="local://template/support-agent/1.0.0",
    )
    assert template.artifact_ref.startswith("local://")
    copy = external_apps.TemplateCopy(idempotency_key="copy-1")
    assert copy.target_workspace_id is None
    with pytest.raises(ValueError):
        external_apps.TemplateCreate(
            name="unsafe-agent",
            display_name="Unsafe Agent",
            template_type="skill",
            version="1",
            manifest={"source": "https://evil.example.com/skill.tgz"},
            artifact_ref="https://evil.example.com/skill.tgz",
        )


def test_invocation_payload_is_bounded_and_replay_key_is_required():
    body = external_apps.ExternalInvocationCreate(operation="chat", payload={"message": "hello"}, idempotency_key="invoke-1")
    assert body.operation == "chat"
    with pytest.raises(ValueError):
        external_apps.ExternalInvocationCreate(operation="chat", payload={"authorization": "Bearer secret"}, idempotency_key="invoke-2")
    with pytest.raises(ValueError):
        external_apps.ExternalInvocationCreate(operation="chat", payload={}, idempotency_key="")


def test_controlled_execution_is_deterministic_and_does_not_send_provider_request():
    first = external_apps._controlled_execution("dify", "chat", {"message": "hello"}, "hash-1")
    second = external_apps._controlled_execution("dify", "chat", {"message": "hello"}, "hash-1")
    assert first == second
    assert first["execution"] == "controlled_mock"
    assert first["provider_request_sent"] is False
    assert first["output_text"] == "mock:dify:chat:hello"


@pytest.mark.asyncio
async def test_http_test_execution_posts_bounded_request_and_redacts_response():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"answer": "ok", "api_key": "should-not-persist", "nested": {"password": "secret"}},
            request=request,
        )

    result = await external_apps._http_test_execution(
        "dify",
        "https://provider.example.com/invoke",
        "chat",
        {"message": "hello"},
        "input-hash",
        {"execution_mode": "http_test", "max_retries": 1, "backoff_ms": 0},
        transport=httpx.MockTransport(handler),
    )
    assert result["success"] is True
    assert result["attempts"] == 1
    assert result["result"]["provider_request_sent"] is True
    assert result["result"]["response"]["api_key"] == "[REDACTED]"
    assert result["result"]["response"]["nested"]["password"] == "[REDACTED]"
    assert len(requests) == 1
    assert requests[0].headers["idempotency-key"] == "input-hash"
    assert b"credential" not in requests[0].content.lower()


@pytest.mark.asyncio
async def test_http_test_execution_retries_transient_provider_failure_only_with_bound():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "busy"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    result = await external_apps._http_test_execution(
        "fastgpt",
        "https://provider.example.com/invoke",
        "run",
        {},
        "retry-hash",
        {"execution_mode": "http_test", "max_retries": 1, "backoff_ms": 0},
        transport=httpx.MockTransport(handler),
    )
    assert result["success"] is True
    assert result["attempts"] == 2
    assert attempts == 2


@pytest.mark.asyncio
async def test_http_test_execution_stops_on_non_retryable_status_and_rejects_unsafe_endpoint():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"error": "bad request"}, request=request)

    result = await external_apps._http_test_execution(
        "ragflow",
        "https://provider.example.com/invoke",
        "run",
        {},
        "bad-hash",
        {"execution_mode": "http_test", "max_retries": 2, "backoff_ms": 0},
        transport=httpx.MockTransport(handler),
    )
    assert result["success"] is False
    assert result["error_code"] == "provider_http_400"
    assert result["attempts"] == 1
    assert attempts == 1

    unsafe = await external_apps._http_test_execution(
        "ragflow", "http://127.0.0.1:8080/invoke", "run", {}, "unsafe", {"execution_mode": "http_test"}
    )
    assert unsafe["error_code"] == "unsafe_endpoint"
    assert unsafe["attempts"] == 0


@pytest.mark.asyncio
async def test_external_http_worker_claims_retries_and_completes_with_mock_transport(monkeypatch):
    class Result:
        def __init__(self, rows=None):
            self.rows = list(rows or [])

        async def fetchall(self):
            return self.rows

        async def fetchone(self):
            return self.rows[0] if self.rows else None

    class Transaction:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Connection:
        def __init__(self, item):
            self.item = item
            self.statements = []

        def transaction(self):
            return Transaction(self)

        async def execute(self, statement, params=()):
            self.statements.append((statement, params))
            if "FROM pf_external_app_invocation i" in statement:
                return Result([dict(self.item)])
            if "RETURNING id" in statement:
                return Result([{"id": self.item["id"]}])
            return Result()

    class Pool:
        def __init__(self, connections):
            self.connections = iter(connections)

        def connection(self):
            connection = next(self.connections)

            class Context:
                async def __aenter__(self):
                    return connection

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return Context()

    async def allow_public_dns(_endpoint):
        return UrlValidationResult(True)

    async def no_metering(*_args, **_kwargs):
        return None

    monkeypatch.setattr(external_apps, "validate_resolved_outbound_url", allow_public_dns)
    monkeypatch.setattr(worker, "settle_meter_in_transaction", no_metering)
    monkeypatch.setattr(external_apps, "_EXTERNAL_HTTP_LIVE_MAX_RETRIES", 0)
    attempts = 0
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        requests.append(request)
        if attempts == 1:
            return httpx.Response(503, json={"error": "busy"}, request=request)
        return httpx.Response(200, json={"answer": "ok", "api_key": "must redact"}, request=request)

    base_item = {
        "id": "extinv-worker-1",
        "app_id": "extapp-worker-1",
        "workspace_id": "workspace-1",
        "operation": "chat",
        "input_hash": "worker-input-hash",
        "payload": {"message": "hello"},
        "status": "queued",
        "execution_mode": "external_http",
        "result": {},
        "error_code": None,
        "attempt": 0,
        "max_attempts": 2,
        "next_attempt_at": None,
        "claimed_at": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "provider": "dify",
        "endpoint": "https://staging.example.com/invoke",
        "config": {"execution_mode": "external_http", "max_retries": 1, "backoff_ms": 0},
        "credential_hash": "hash-only",
    }
    transport = httpx.MockTransport(handler)

    first_claim = Connection(base_item)
    first_finalize = Connection(base_item)
    monkeypatch.setattr(worker, "pool", Pool([first_claim, first_finalize]))
    first = await worker.process_pending_external_app_invocations("platform-worker-test", transport=transport)
    assert first == {"claimed": 1, "succeeded": 0, "retried": 1, "failed": 0, "blocked": 0}
    assert any("FOR UPDATE OF i SKIP LOCKED" in statement for statement, _ in first_claim.statements)
    assert any("lease_owner=%s" in statement for statement, _ in first_claim.statements)
    assert next(params[0] for statement, params in first_finalize.statements if "SET status=%s" in statement) == "queued"

    second_item = {**base_item, "attempt": 1}
    second_claim = Connection(second_item)
    second_finalize = Connection(second_item)
    monkeypatch.setattr(worker, "pool", Pool([second_claim, second_finalize]))
    second = await worker.process_pending_external_app_invocations("platform-worker-test", transport=transport)
    assert second == {"claimed": 1, "succeeded": 1, "retried": 0, "failed": 0, "blocked": 0}
    assert next(params[0] for statement, params in second_finalize.statements if "SET status=%s" in statement) == "succeeded"
    assert attempts == 2
    assert requests[0].headers["x-workama-execution-mode"] == "external_http"
    assert "authorization" not in requests[0].headers
    assert b"hash-only" not in requests[0].content
    assert second_finalize.statements[-1][1][-1] == "platform-worker-test"


@pytest.mark.asyncio
async def test_external_http_worker_rejects_redirects_and_bounded_responses(monkeypatch):
    async def allow_public_dns(_endpoint):
        return UrlValidationResult(True)

    monkeypatch.setattr(external_apps, "validate_resolved_outbound_url", allow_public_dns)

    async def redirect(_request):
        return httpx.Response(302, headers={"location": "https://evil.example.com"})

    redirected = await external_apps.external_http_execution(
        "ragflow", "https://staging.example.com/invoke", "run", {}, "redirect-hash",
        {"execution_mode": "external_http"}, transport=httpx.MockTransport(redirect),
    )
    assert redirected["error_code"] == "provider_http_302"
    assert redirected["retryable"] is False

    async def oversized(_request):
        return httpx.Response(200, content=b"x" * (external_apps._HTTP_MAX_RESPONSE_BYTES + 1))

    too_large = await external_apps.external_http_execution(
        "fastgpt", "https://staging.example.com/invoke", "run", {}, "large-hash",
        {"execution_mode": "external_http"}, transport=httpx.MockTransport(oversized),
    )
    assert too_large["error_code"] == "response_too_large"
    assert too_large["success"] is False


def test_routes_cover_external_apps_invocations_and_marketplace_lifecycle():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in external_apps.router.routes}
    assert ("/api/v1/external-apps", ("GET",)) in paths
    assert ("/api/v1/external-apps", ("POST",)) in paths
    assert ("/api/v1/external-apps/{app_id}/invocations", ("POST",)) in paths
    assert ("/api/v1/external-apps/{app_id}/invocations", ("GET",)) in paths
    assert ("/api/v1/marketplace/templates", ("GET",)) in paths
    assert ("/api/v1/marketplace/templates/{template_id}/reviews", ("POST",)) in paths
    assert ("/api/v1/marketplace/templates/{template_id}/publish", ("POST",)) in paths
    assert ("/api/v1/marketplace/templates/{template_id}/copies", ("POST",)) in paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_has_idempotency_boundaries():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await external_apps.ensure_external_apps_schema(Connection())
    schema = "\n".join(statements)
    for table in ("pf_external_app", "pf_external_app_invocation", "pf_marketplace_template", "pf_marketplace_copy"):
        assert table in schema
    for field in ("credential_hash", "credential_last4", "input_hash", "payload", "execution_mode", "attempt", "max_attempts", "response_code", "next_attempt_at", "lease_owner", "lease_expires_at", "artifact_ref", "review_status"):
        assert field in schema
    assert "UNIQUE(app_id,idempotency_key)" in schema
    assert "UNIQUE(template_id,target_workspace_id,idempotency_key)" in schema
