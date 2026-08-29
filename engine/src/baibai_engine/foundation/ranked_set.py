"""Resolve the ranked research input at the domain boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

RESEARCH_GATE_CONTRACT_ID = "research-gate-v1"


class RankedSetResolutionError(ValueError):
    pass


def resolve_ranked_set_rows(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], dict[str, Mapping[str, object]]]:
    rows = _unique_rows(payload.get("ranked_set"), source_name="ranked_set")
    if not rows:
        raise RankedSetResolutionError("source selection has no ranked set")
    return tuple(rows), rows


def _unique_rows(value: object, *, source_name: str) -> dict[str, Mapping[str, object]]:
    if value is None:
        return {}
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise RankedSetResolutionError(f"{source_name} must be a sequence")
    rows: dict[str, Mapping[str, object]] = {}
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("ticker"), str)
            or re.fullmatch(r"[0-9A-Z]{4}", str(item["ticker"])) is None
        ):
            raise RankedSetResolutionError(f"{source_name} contains an invalid row")
        ticker = str(item["ticker"])
        if ticker in rows:
            raise RankedSetResolutionError(f"{source_name} contains duplicate ticker {ticker}")
        rows[ticker] = item
    return rows


__all__ = [
    "RESEARCH_GATE_CONTRACT_ID",
    "RankedSetResolutionError",
    "resolve_ranked_set_rows",
]
