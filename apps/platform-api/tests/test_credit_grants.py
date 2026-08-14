from datetime import UTC, datetime
from decimal import Decimal

from workama_platform.modules.billing.grants import month_period, quantize_credits


def test_month_period_is_utc_and_rolls_year_boundary():
    start, end = month_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))

    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_credit_amounts_use_six_decimal_places():
    assert quantize_credits("12.3456789") == Decimal("12.345679")
