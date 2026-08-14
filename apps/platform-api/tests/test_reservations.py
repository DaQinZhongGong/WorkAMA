from __future__ import annotations

from decimal import Decimal

import pytest

from workama_platform.modules.billing.reservations import (
    ReservationState,
    estimate_cost,
    settle_reservation_amounts,
)


def test_estimate_cost_uses_prompt_and_max_tokens_with_markup():
    cost = estimate_cost(
        prompt_tokens=100,
        max_tokens=900,
        price={
            "input_per_million": Decimal("1"),
            "output_per_million": Decimal("2"),
            "markup_percent": Decimal("10"),
        },
    )

    assert cost == Decimal("0.002090")


def test_settlement_releases_unused_frozen_amount():
    result = settle_reservation_amounts(
        ReservationState(estimated=Decimal("10"), actual=Decimal("3"), frozen=Decimal("10")),
    )

    assert result.status == "settled"
    assert result.frozen == Decimal("0")
    assert result.refund == Decimal("7")


def test_release_returns_full_estimate_without_usage():
    result = settle_reservation_amounts(
        ReservationState(estimated=Decimal("10"), actual=None, frozen=Decimal("10")),
        release=True,
    )

    assert result.status == "released"
    assert result.refund == Decimal("10")
    assert result.frozen == Decimal("0")


def test_settlement_rejects_actual_cost_above_available_frozen_balance():
    with pytest.raises(ValueError, match="frozen reservation"):
        settle_reservation_amounts(
            ReservationState(estimated=Decimal("3"), actual=Decimal("4"), frozen=Decimal("3")),
        )
