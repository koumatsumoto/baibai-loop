"""Shared coercion helpers for loosely-typed YAML payload values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _dict_sequence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _metric_map(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _float_or(value: object, default: float) -> float:
    number = _number(value)
    return number if number is not None else default


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_from_datetime_prefix(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return _parse_date(value)
