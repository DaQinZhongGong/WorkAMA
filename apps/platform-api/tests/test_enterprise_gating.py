"""Tests for the enterprise feature gating module.

Covers:
- list_features: returns licensed + available + descriptions / no-license (licensed=[])
- require_sso: success (feature present) / 403 (missing) / wildcard pass
- enterprise_version: returns platform version / returns license summary / no-license
- FEATURE_DESCRIPTIONS: covers all Features constants (excluding UNLIMITED)
- workama_version: single-source version constants

All tests use fake helpers (no real DB / Redis / network).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import enterprise_gating as eg
from workama_version import BUILD_DATE, ENTERPRISE_BUILD, PLATFORM_VERSION


def _actor(*, role="owner", workspace_id="wsp_test", user_id="usr_test") -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=("*",),
    )


def _license_row(**overrides) -> dict:
    base = {
        "id": "lic_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "plan_code": "enterprise",
        "license_key_hash": "hash_xxx",
        "license_key_last_four": "abcd",
        "status": "active",
        "seats": 50,
        "credit_limit": 1_000_000,
        "concurrency_limit": 100,
        "features": [eg.Features.SSO_SAML, eg.Features.ADVANCED_RAG],
        "issued_by": "usr_test",
        "idempotency_key": None,
        "valid_from": datetime.now(UTC) - timedelta(days=10),
        "valid_until": datetime.now(UTC) + timedelta(days=30, hours=1),
        "revoked_at": None,
        "revoke_reason": None,
        "created_at": datetime.now(UTC) - timedelta(days=10),
        "updated_at": datetime.now(UTC) - timedelta(days=10),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(eg.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _patch_fetch(monkeypatch, row):
    """Patch ``enterprise_gating._fetch_active_license`` to return ``row``."""

    async def _fake_fetch(_workspace_id: str):
        return row

    monkeypatch.setattr(eg, "_fetch_active_license", _fake_fetch)


# ============================================================================
# 1. list_features endpoint
# ============================================================================


class TestListFeatures:
    @pytest.mark.asyncio
    async def test_list_features_returns_licensed_available_and_descriptions(self, monkeypatch):
        row = _license_row(features=[eg.Features.SSO_SAML, eg.Features.ADVANCED_RAG])
        _patch_fetch(monkeypatch, row)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/features")

        assert resp.status_code == 200
        body = resp.json()
        assert body["licensed"] == [eg.Features.SSO_SAML, eg.Features.ADVANCED_RAG]
        assert body["plan_code"] == "enterprise"
        # available lists every concrete feature (no wildcard) with a description
        available_names = {item["name"] for item in body["available"]}
        assert eg.Features.SSO_SAML in available_names
        assert eg.Features.ENTERPRISE_RBAC in available_names
        assert eg.Features.UNLIMITED not in available_names
        for item in body["available"]:
            assert item["description"], f"empty description for {item['name']}"

    @pytest.mark.asyncio
    async def test_list_features_no_license_returns_empty_licensed(self, monkeypatch):
        _patch_fetch(monkeypatch, None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/features")

        assert resp.status_code == 200
        body = resp.json()
        assert body["licensed"] == []
        assert body["plan_code"] is None
        # available is still fully populated even without a license
        assert len(body["available"]) == len(eg._all_feature_names())


# ============================================================================
# 2. require_sso (feature gate dependency)
# ============================================================================


class TestRequireSso:
    @pytest.mark.asyncio
    async def test_require_sso_success_with_feature(self):
        # require_sso is the _check_feature dependency returned by require_feature;
        # call it directly with keyword args (mirrors test_license_middleware style).
        license_row = {"id": "lic_1", "features": [eg.Features.SSO_SAML]}
        result = await eg.require_sso(actor=_actor(), license_row=license_row)
        assert result is license_row

    @pytest.mark.asyncio
    async def test_require_sso_403_missing(self):
        license_row = {"id": "lic_1", "features": [eg.Features.ADVANCED_RAG]}
        with pytest.raises(HTTPException) as exc:
            await eg.require_sso(actor=_actor(), license_row=license_row)
        assert exc.value.status_code == 403
        assert eg.Features.SSO_SAML in exc.value.detail

    @pytest.mark.asyncio
    async def test_require_sso_success_with_wildcard(self):
        license_row = {"id": "lic_1", "features": [eg.Features.UNLIMITED]}
        result = await eg.require_sso(actor=_actor(), license_row=license_row)
        assert result is license_row


# ============================================================================
# 3. enterprise_version endpoint
# ============================================================================


class TestEnterpriseVersion:
    @pytest.mark.asyncio
    async def test_enterprise_version_returns_platform_version(self, monkeypatch):
        _patch_fetch(monkeypatch, _license_row())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/version")

        assert resp.status_code == 200
        body = resp.json()
        assert body["platform_version"] == PLATFORM_VERSION
        assert body["enterprise_enabled"] is True
        assert body["build_date"] == BUILD_DATE

    @pytest.mark.asyncio
    async def test_enterprise_version_returns_license_summary(self, monkeypatch):
        row = _license_row(
            plan_code="enterprise-plus",
            features=[eg.Features.SSO_SAML, eg.Features.SIEM_INTEGRATION],
            valid_until=datetime.now(UTC) + timedelta(days=45, hours=1),
        )
        _patch_fetch(monkeypatch, row)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/version")

        assert resp.status_code == 200
        body = resp.json()
        license_summary = body["license"]
        assert license_summary is not None
        assert license_summary["license_id"] == "lic_1"
        assert license_summary["status"] == "active"
        assert license_summary["plan_code"] == "enterprise-plus"
        assert license_summary["days_remaining"] == 45
        assert eg.Features.SSO_SAML in body["features"]
        assert eg.Features.SIEM_INTEGRATION in body["features"]

    @pytest.mark.asyncio
    async def test_enterprise_version_no_license(self, monkeypatch):
        _patch_fetch(monkeypatch, None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/version")

        assert resp.status_code == 200
        body = resp.json()
        assert body["license"] is None
        assert body["features"] == []
        # platform version is reported regardless of license state
        assert body["platform_version"] == PLATFORM_VERSION
        assert body["enterprise_enabled"] is True


# ============================================================================
# 4. FEATURE_DESCRIPTIONS coverage
# ============================================================================


class TestFeatureDescriptions:
    def test_feature_descriptions_covers_all_constants(self):
        # Every concrete Features constant (excluding the "*" wildcard) must
        # have a non-empty description in FEATURE_DESCRIPTIONS.
        concrete_features = {
            getattr(eg.Features, name)
            for name in dir(eg.Features)
            if not name.startswith("_") and name != "UNLIMITED"
        }
        assert concrete_features, "no concrete features discovered"
        for feature in concrete_features:
            assert feature in eg.FEATURE_DESCRIPTIONS, (
                f"missing description for feature {feature!r}"
            )
            assert eg.FEATURE_DESCRIPTIONS[feature], (
                f"empty description for feature {feature!r}"
            )

    def test_feature_descriptions_excludes_unlimited(self):
        # The "*" wildcard is a marker, not a licensable feature -> no description.
        assert eg.Features.UNLIMITED not in eg.FEATURE_DESCRIPTIONS

    def test_available_list_matches_feature_descriptions(self):
        # _all_feature_names() and FEATURE_DESCRIPTIONS keys must be in sync so
        # the /features "available" list is complete and consistent.
        assert set(eg._all_feature_names()) == set(eg.FEATURE_DESCRIPTIONS.keys())


# ============================================================================
# 5. workama_version single-source constants
# ============================================================================


class TestWorkamaVersion:
    def test_workama_version_constants(self):
        # The version module is the single source of truth referenced by the
        # enterprise_version endpoint.
        assert PLATFORM_VERSION == "v7.176"
        assert ENTERPRISE_BUILD is True
        assert BUILD_DATE == "2026-07-31"

    def test_enterprise_gating_uses_workama_version(self):
        # enterprise_gating must expose the same values it imports from
        # workama_version (proves the import path, not the fallback, is in effect).
        assert eg.PLATFORM_VERSION == PLATFORM_VERSION
        assert eg.ENTERPRISE_BUILD is ENTERPRISE_BUILD
        assert eg.BUILD_DATE == BUILD_DATE
