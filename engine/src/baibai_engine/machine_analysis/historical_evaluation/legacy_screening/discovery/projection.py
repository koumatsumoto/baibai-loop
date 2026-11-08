"""Pure projections over saved Stock Analyses, with no rules, store or clock reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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
