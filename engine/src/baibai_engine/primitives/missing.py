"""Recognize missing scalar markers without importing a dataframe library."""

from __future__ import annotations

from typing import Any


def is_missing_scalar(value: Any) -> bool:
    """Recognize scalar missing markers produced by JSON and pandas without importing pandas."""

    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "NaT", "nan", "null", "<NA>"}
    if str(value).strip() in {"NaT", "nan", "<NA>"}:
        return True
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return False
