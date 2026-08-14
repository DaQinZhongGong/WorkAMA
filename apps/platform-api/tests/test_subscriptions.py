import pytest
from decimal import Decimal

from workama_platform.modules.subscriptions import (
    PLAN_CATALOG,
    OrderCreateRequest,
    RefundCreateRequest,
    _mock_signature,
    router,
)
from workama_platform.modules.billing.router import router as billing_router


def test_plan_catalog_matches_billing_baseline():
    assert set(PLAN_CATALOG) == {"free", "pro", "team", "enterprise"}
    assert PLAN_CATALOG["free"]["quotas"]["members"] == 1
    assert PLAN_CATALOG["pro"]["monthly_price"] == 99
    assert PLAN_CATALOG["team"]["quotas"]["agent_concurrency"] == 10
    assert PLAN_CATALOG["enterprise"]["quotas"]["members"] is None


def test_price_values_are_non_negative():
    assert all(Decimal(str(plan["monthly_price"])) >= 0 for plan in PLAN_CATALOG.values())


def test_commercial_order_models_enforce_type_specific_amounts():
    order = OrderCreateRequest(order_type="subscription", plan_code="pro", idempotency_key="order-test-123")
    assert order.amount is None
    credit_order = OrderCreateRequest(order_type="credits", amount=Decimal("10.00"), idempotency_key="order-test-456")
    assert credit_order.credits is None
    with pytest.raises(ValueError):
        OrderCreateRequest(order_type="subscription", idempotency_key="order-test-789")


def test_refund_requires_a_target():
    with pytest.raises(ValueError):
        RefundCreateRequest(reason="duplicate", idempotency_key="refund-test-123")


def test_mock_provider_signature_is_content_bound():
    payload = b'{"event_id":"evt-123456","payment_id":"pay-123456","status":"succeeded"}'
    assert _mock_signature(payload) == _mock_signature(payload)
    assert _mock_signature(payload) != _mock_signature(payload + b" ")


def test_commercial_billing_routes_are_explicit():
    routes = {(route.path, tuple(route.methods or ())) for route in router.routes}
    assert ("/api/v1/billing/orders", ("POST",)) in routes
    assert ("/api/v1/billing/providers/{provider}/callbacks", ("POST",)) in routes
    assert ("/api/v1/billing/refunds", ("POST",)) in routes
    billing_routes = {(route.path, tuple(route.methods or ())) for route in billing_router.routes}
    assert ("/api/v1/billing/grants", ("GET",)) in billing_routes
