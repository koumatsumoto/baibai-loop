"""Shared coercion helpers for loosely-typed record payload values.

Loosely-typed record payloads arrive as ``object`` graphs.
These helpers normalize them defensively: wrong
shapes coerce to a neutral value instead of raising, so callers stay total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date


def dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def dict_sequence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def metric_map(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def float_or(value: object, default: float) -> float:
    number = optional_float(value)
    return number if number is not None else default


def int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def parse_iso_date(value: object) -> date | None:
    # The length guard rejects compact forms like "20260608" that
    # date.fromisoformat would otherwise accept; records always use
    # YYYY-MM-DD (optionally with a datetime suffix).
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
