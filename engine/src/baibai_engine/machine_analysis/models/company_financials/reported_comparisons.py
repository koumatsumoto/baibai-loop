"""同一EDINET文書に報告された比較。成長系列や評価倍率の基準にしない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat

from baibai_engine.machine_analysis.observations.financials.edinet_comparison_models import (
    ComparativeMetric,
    FilingComparisons,
)


class _ReportedFilingChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: ComparativeMetric
    measurement: Literal["reported"] = "reported"
    comparison: Literal["year_over_year"] = "year_over_year"
    from_start: date
    from_end: date
    to_start: date
    to_end: date
    from_value: FiniteFloat | None
    to_value: FiniteFloat | None
    difference: FiniteFloat | None
    rate: FiniteFloat | None
    # 成長系列の旧共用形式にあったnullを、保存形式を変えずに残す。
    margin_points: None = None
    sign_change: None = None
    reason: str | None


class ReportedFilingComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: FilingComparisons
    changes: tuple[_ReportedFilingChange, ...]
    read_semantics: Literal["each_document_vintage_not_latest_or_ttm"] = (
        "each_document_vintage_not_latest_or_ttm"
    )


def _reported_comparison(observation: FilingComparisons) -> ReportedFilingComparison:
    changes: list[_ReportedFilingChange] = []
    for pair in observation.pairs:
        before, after = pair.comparative, pair.current
        if before is None or after is None:
            continue  # The observation retains the missing/conflict reason.
        prior = Decimal(before.amount) if before.amount is not None else None
        current = Decimal(after.amount) if after.amount is not None else None
        difference = current - prior if prior is not None and current is not None else None
        changes.append(
            _ReportedFilingChange(
                metric=pair.metric,
                from_start=before.period_start,
                from_end=before.period_end,
                to_start=after.period_start,
                to_end=after.period_end,
                from_value=float(prior) if prior is not None else None,
                to_value=float(current) if current is not None else None,
                difference=float(difference) if difference is not None else None,
                rate=float(difference / prior)
                if difference is not None and prior is not None and prior > 0
                else None,
                reason=pair.reason
                or ("nonpositive_base" if prior is not None and prior <= 0 else None),
            )
        )
    return ReportedFilingComparison(observation=observation, changes=tuple(changes))


def reported_filing_comparisons(
    comparisons: Sequence[FilingComparisons], *, ticker: str, as_of: date
) -> tuple[ReportedFilingComparison, ...]:
    if any(item.ticker != ticker for item in comparisons):
        raise ValueError("reported comparisons contain a different ticker")
    return tuple(
        _reported_comparison(item)
        for item in comparisons
        if item.source_submit_datetime.date() <= as_of
    )
