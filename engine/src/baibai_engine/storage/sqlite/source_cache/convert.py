"""Optional numeric scalar conversion for SQLite reads."""

from __future__ import annotations


def optional_float(value: object) -> float | None:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None
