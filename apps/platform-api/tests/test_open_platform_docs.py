import pytest
from unittest.mock import AsyncMock, patch

from workama_platform.modules import open_platform


def test_public_docs_model_fields():
    doc = open_platform.OpenPlatformDocCreate(slug="quickstart", title="Quick Start", content="## Hello")
    assert doc.slug == "quickstart"
    assert doc.doc_type == "guide"


def test_public_docs_patch_allows_partial():
    patch = open_platform.OpenPlatformDocPatch(title="Updated")
    assert patch.title == "Updated"
    assert patch.slug is None


def test_public_router_has_docs_endpoint():
    paths = {route.path for route in open_platform.public_router.routes}
    assert "/api/v1/public/docs" in paths


def test_admin_router_has_open_platform_docs_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in open_platform.router.routes}
    assert ("/api/v1/open-platform/docs", ("GET",)) in paths
    assert ("/api/v1/open-platform/docs", ("POST",)) in paths
    assert ("/api/v1/open-platform/docs/{doc_id}", ("GET",)) in paths
    assert ("/api/v1/open-platform/docs/{doc_id}", ("PATCH",)) in paths
    assert ("/api/v1/open-platform/docs/{doc_id}", ("DELETE",)) in paths


def test_schema_includes_open_platform_doc_table():
    assert any("pf_open_platform_doc" in s for s in open_platform.SCHEMA_STATEMENTS)
    assert any("UNIQUE(workspace_id, slug)" in s for s in open_platform.SCHEMA_STATEMENTS)


def test_open_platform_doc_create_defaults():
    doc = open_platform.OpenPlatformDocCreate(slug="api-ref", title="API Reference")
    assert doc.content == ""
    assert doc.doc_type == "guide"
    assert doc.sort_order == 0
    assert doc.status == "published"


def test_open_platform_doc_create_supports_all_doc_types():
    for dt in ("guide", "api_reference", "sdk", "quickstart", "webhook", "oauth"):
        doc = open_platform.OpenPlatformDocCreate(slug="x", title="X", doc_type=dt)
        assert doc.doc_type == dt


def test_open_platform_doc_patch_supports_all_status_values():
    for status in ("draft", "published", "archived"):
        patch = open_platform.OpenPlatformDocPatch(status=status)
        assert patch.status == status


def test_open_platform_doc_create_rejects_empty_slug():
    with pytest.raises(ValueError):
        open_platform.OpenPlatformDocCreate(slug="", title="X")


def test_open_platform_doc_create_rejects_long_title():
    with pytest.raises(ValueError):
        open_platform.OpenPlatformDocCreate(slug="x", title="x" * 300)


def test_public_docs_list_endpoint_is_get():
    for route in open_platform.public_router.routes:
        if route.path == "/api/v1/public/docs":
            assert "GET" in route.methods
            return
    pytest.fail("public docs endpoint not found")


@pytest.mark.asyncio
async def test_list_public_docs_returns_structure():
    mock_rows = [
        {"slug": "quickstart", "title": "Quick Start", "content": "# Hello", "doc_type": "quickstart", "sort_order": 0, "status": "published", "created_at": None, "updated_at": None},
    ]

    class MockResult:
        async def fetchall(self):
            return mock_rows

    class MockConn:
        async def execute(self, *args, **kwargs):
            return MockResult()

    with patch.object(open_platform.pool, "connection") as mock_pool:
        mock_pool.return_value.__aenter__ = AsyncMock(return_value=MockConn())
        mock_pool.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await open_platform.list_public_docs()
        assert "openapi_url" in result
        assert "sdk_downloads" in result
        assert "quickstart_url" in result
        assert "docs" in result


def test_schema_has_open_platform_doc_index():
    schema = "\n".join(open_platform.SCHEMA_STATEMENTS)
    assert "idx_open_platform_doc_published" in schema


def test_oauth_client_create_requires_authorization_code():
    with pytest.raises(ValueError):
        open_platform.OAuthClientCreate(
            name="Test",
            redirect_uris=["https://example.com/callback"],
            grant_types=["client_credentials"],
        )


def test_webhook_create_validates_events():
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="https://example.com/hook", events=["unknown.event"])


def test_webhook_create_accepts_wildcard_event():
    wh = open_platform.WebhookCreate(url="https://example.com/hook", events=["*"])
    assert wh.events == ["*"]
