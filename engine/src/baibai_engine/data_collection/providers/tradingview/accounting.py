"""Provider-native batch coverage, independent of forecast value interpretation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

type AccountingReason = Literal[
    "malformed_envelope",
    "malformed_missing_symbols",
    "duplicate_missing_symbol",
    "response_accounting_mismatch",
    "response_count_mismatch",
]


class BatchAccountingError(ValueError):
    def __init__(self, reason: AccountingReason) -> None:
        self.reason = reason
        super().__init__(reason)


def account_batch(
    payload: dict[str, Any], symbols: Sequence[str]
) -> tuple[dict[str, Any], set[str]]:
    """Every requested symbol must have exactly one native data or missing answer."""
    data, missing = payload.get("data"), payload.get("missing")
    if (
        payload.get("success") is not True
        or not isinstance(data, dict)
        or not isinstance(missing, list)
    ):
        raise BatchAccountingError("malformed_envelope")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("symbol"), str) for item in missing
    ):
        raise BatchAccountingError("malformed_missing_symbols")
    names = [item["symbol"] for item in missing]
    if len(names) != len(set(names)):
        raise BatchAccountingError("duplicate_missing_symbol")
    if set(data) & set(names) or set(data) | set(names) != set(symbols):
        raise BatchAccountingError("response_accounting_mismatch")
    if payload.get("count") != len(data) or payload.get("missing_count") != len(names):
        raise BatchAccountingError("response_count_mismatch")
    return data, set(names)
