from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ReservationState:
    estimated: Decimal
    actual: Decimal | None
    frozen: Decimal


@dataclass(frozen=True)
class ReservationResult:
    status: str
    frozen: Decimal
    refund: Decimal


def estimate_cost(prompt_tokens: int, max_tokens: int, price: dict) -> Decimal:
    base = (
        Decimal(prompt_tokens) * price["input_per_million"]
        + Decimal(max_tokens) * price["output_per_million"]
    ) / Decimal(1_000_000)
    return (base * (Decimal(1) + price["markup_percent"] / Decimal(100))).quantize(
        Decimal("0.000001")
    )


def settle_reservation_amounts(state: ReservationState, release: bool = False) -> ReservationResult:
    if release:
        return ReservationResult("released", Decimal("0"), state.estimated)
    if state.actual is None or state.actual < 0:
        raise ValueError("actual settlement is required")
    if state.actual > state.frozen:
        raise ValueError("actual cost exceeds frozen reservation")
    return ReservationResult("settled", Decimal("0"), state.frozen - state.actual)
