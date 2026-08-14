from datetime import UTC, datetime
from decimal import Decimal

from workama_platform.modules.billing.reporting import (
    ReconciliationResult,
    hour_bucket,
    reconcile_totals,
)


def test_hour_bucket_normalizes_to_utc_hour():
    value = datetime(2026, 7, 14, 8, 35, 42, tzinfo=UTC)
    assert hour_bucket(value) == datetime(2026, 7, 14, 8, 0, 0, tzinfo=UTC)


def test_reconciliation_passes_within_point_one_percent():
    result = reconcile_totals(Decimal("100.000000"), Decimal("99.950000"))
    assert result == ReconciliationResult(
        usage_credits=Decimal("100.000000"),
        ledger_credits=Decimal("99.950000"),
        difference=Decimal("0.050000"),
        difference_ratio=Decimal("0.000500"),
        status="passed",
    )


def test_reconciliation_flags_material_difference():
    result = reconcile_totals(Decimal("100.000000"), Decimal("99.000000"))
    assert result.status == "mismatch"
    assert result.difference_ratio == Decimal("0.010000")


def test_zero_totals_reconcile_without_division_error():
    result = reconcile_totals(Decimal("0"), Decimal("0"))
    assert result.status == "passed"
    assert result.difference_ratio == Decimal("0.000000")
