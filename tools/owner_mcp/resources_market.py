"""調査工程へmarketのstore-local factだけを見せる。"""

from __future__ import annotations

from typing import Any

from baibai_engine.read_api.market import stored_market_rows

from .resource_types import Page, Paths, record


def market_page(
    paths: Paths,
    filters: dict[str, Any],
    after: list[str | int | float] | None,
    limit: int,
    *,
    kind: str,
) -> Page:
    rows = stored_market_rows(paths.market, kind=kind, filters=filters, after=after, limit=limit)
    key = (
        ("source", "coverage_key")
        if kind == "source_coverage"
        else ("snapshot_month_end", "ticker")
    )
    return Page([record(row, key, key) for row in rows])


def market_get(paths: Paths, selector: dict[str, Any], *, kind: str) -> Page:
    return market_page(paths, selector, None, 1, kind=kind)
