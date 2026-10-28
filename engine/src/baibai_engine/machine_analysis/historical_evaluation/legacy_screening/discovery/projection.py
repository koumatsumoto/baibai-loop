"""Pure projections over saved Stock Analyses, with no rules, store or clock reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite

from baibai_engine.primitives.coerce import metric_map, optional_float

from .contracts import (
    StockCandidateContext,
)


def candidate_rows(
    stock_analyses: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Keep original rows and values; ticker order is non-economic canonical order."""
    return tuple(
        sorted(
            (
                row
                for row in stock_analyses
                if StockCandidateContext.model_validate(row.get("candidate")).reasons
            ),
            key=lambda row: str(row["ticker"]),
        )
    )


def reinvestment_coordinates(row: Mapping[str, object]) -> dict[str, float | None]:
    """Show each known coordinate even when the row cannot satisfy selection gates."""
    metrics = metric_map(row.get("metrics"))
    operating_profit = _finite(metrics.get("operating_profit_ttm"))
    sales = _positive(metrics.get("sales_ttm"))
    equity = _finite(metrics.get("same_state_equity_yen"))
    debt = _finite(metrics.get("debt"))
    cash = _finite(metrics.get("cash"))
    capital = (
        _positive(equity + debt - cash)
        if equity is not None and debt is not None and cash is not None
        else None
    )
    return {
        "p_s_sector_gap": _finite(metrics.get("p_s_sector_gap")),
        "operating_return_on_capital_proxy": (
            operating_profit / capital
            if operating_profit is not None and capital is not None
            else None
        ),
        "sales_yoy": _finite(metrics.get("sales_yoy")),
        "operating_margin": (
            operating_profit / sales if operating_profit is not None and sales is not None else None
        ),
        "fcf_yield": _finite(metrics.get("fcf_yield")),
    }


def _finite(value: object) -> float | None:
    parsed = optional_float(value)
    return parsed if parsed is not None and isfinite(parsed) else None


def _positive(value: object) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0 else None
