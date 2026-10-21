"""Render whole Decimals as ints and fractional Decimals as approximate floats."""

from __future__ import annotations

from decimal import Decimal

__all__ = ["decimal_to_number"]


def decimal_to_number(value: Decimal) -> float | int:
    """Emit integral values as ints without changing the quantity's unit."""

    if value == value.to_integral_value():
        return int(value)
    return float(value)
