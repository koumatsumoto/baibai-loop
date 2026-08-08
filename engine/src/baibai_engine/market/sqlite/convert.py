"""Normalization primitives shared by the per-source SQLite store/read helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from baibai_engine.market.jquants import JQuantsProviderError, parse_jquants_code_parts


@dataclass(frozen=True, slots=True)
class NormalizedRows:
    rows: list[tuple[Any, ...]]
    rejected_count: int = 0
    excluded_count: int = 0

    @property
    def skipped_count(self) -> int:
        return self.rejected_count + self.excluded_count

    @property
    def status(self) -> str:
        return "partial" if self.rejected_count else "ok"

    @property
    def error(self) -> str | None:
        if not self.rejected_count:
            return None
        return f"{self.rejected_count} rejected records during normalization"


def code_quality(value: Any) -> tuple[str | None, str]:
    if value in (None, ""):
        return None, "rejected"
    try:
        ticker, common_code = parse_jquants_code_parts(value)
    except JQuantsProviderError:
        return None, "rejected"
    if not common_code:
        return None, "excluded"
    return ticker, "ok"


def normalize_ticker_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        ticker, common_code = parse_jquants_code_parts(value)
    except JQuantsProviderError:
        return None
    if not common_code:
        return None
    return ticker


def is_common_stock_flag(record: Mapping[str, Any]) -> bool:
    if "is_common_stock" in record:
        return bool(record["is_common_stock"])
    code = str(first(record, "Code", "code") or "").strip()
    if len(code) == 5 and not code.endswith("0"):
        return False
    raw_type = to_str_or_none(first(record, "TypeOfDocument", "SecurityType", "security_type"))
    if raw_type is None:
        return True
    return raw_type.lower() in {"common", "common stock", "普通株"}


def first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def to_str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def date_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:10]


def optional_float(value: object) -> float | None:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def optional_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
