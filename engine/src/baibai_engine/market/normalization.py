"""Source-row normalization shared by market projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from baibai_engine.collection.identifiers import SecurityCodeError, parse_security_code_parts
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_decode import is_missing_scalar as _is_missing_scalar
from baibai_engine.market.jquants_decode import to_float as to_float


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
        ticker, common_code = parse_security_code_parts(value)
    except SecurityCodeError:
        return None, "rejected"
    if not common_code:
        return None, "excluded"
    return ticker, "ok"


def normalize_ticker_or_none(value: Any) -> str | None:
    return code_quality(value)[0]


def first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if value is None or (isinstance(value, str) and value == ""):
            continue
        return value
    return None


def to_str_or_none(value: Any) -> str | None:
    if _is_missing_scalar(value):
        return None
    return str(value).strip()


def date_iso(value: Any) -> str | None:
    if _is_missing_scalar(value):
        return None
    text = str(value)
    return text[:10]


def distinct_source_rows(
    rows: Iterable[tuple[Any, ...]], *, identity_columns: int, source: str
) -> list[tuple[Any, ...]]:
    """Collapse repeated observations, but never choose a conflicting value by row order."""
    by_identity: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in rows:
        identity = row[:identity_columns]
        if by_identity.setdefault(identity, row) != row:
            raise JQuantsProviderError(f"conflicting {source} observation: {identity}")
    return list(by_identity.values())
