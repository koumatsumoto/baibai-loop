"""Shared scalar conversions at record-reading boundaries."""

from __future__ import annotations


def string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
