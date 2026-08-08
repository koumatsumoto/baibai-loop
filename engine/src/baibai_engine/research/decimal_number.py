"""Render a Decimal as the narrowest JSON number that keeps its value."""

from __future__ import annotations

from decimal import Decimal

__all__ = ["decimal_to_number"]


def decimal_to_number(value: Decimal) -> float | int:
    """Emit an int for a whole yen amount so payloads do not carry a stray ``.0``."""

    if value == value.to_integral_value():
        return int(value)
    return float(value)
