"""同一提出書に報告された比較。成長系列や評価倍率の基準にしない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Context, Decimal, Inexact, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat

from baibai_engine.machine_analysis.observations.financials.edinet_comparison_models import (
    ComparativeMetric,
    FilingComparisons,
)
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecIssuerUsdObservation,
    SecRevenueAmbiguity,
    SecStatementComparison,
)

_SEC_CHANGE_CONTEXT = Context(prec=34)


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


def _reported_rate(difference: Decimal | None, prior: Decimal | None) -> Decimal | None:
    return (
        difference / prior if difference is not None and prior is not None and prior > 0 else None
    )


def sec_reported_change(
    row: SecStatementComparison,
) -> tuple[Decimal | None, Decimal | None, tuple[str, ...]]:
    # Compare only the two checked columns, not successive filings or a growth series.
    prior, current = row.prior, row.current
    if not isinstance(prior, SecIssuerUsdObservation) or not isinstance(
        current, SecIssuerUsdObservation
    ):
        reasons = tuple(
            f"{column}:{fact.reason if isinstance(fact, SecRevenueAmbiguity) else fact}"
            for column, fact in (("current", current), ("prior", prior))
            if not isinstance(fact, SecIssuerUsdObservation)
        )
        return None, None, reasons
    if current.concept != prior.concept:
        return None, None, ("incomparable_concepts",)
    # Do not report an ambient-context or rounded subtraction as a native amount.
    with localcontext(_SEC_CHANGE_CONTEXT) as context:
        context.traps[Inexact] = True
        try:
            difference = current.amount - prior.amount
        except Inexact:
            return None, None, ("inexact_difference",)
        except ArithmeticError:
            return None, None, ("arithmetic_unavailable",)
        context.traps[Inexact] = False  # A displayed rate, unlike the difference, may round.
        try:
            rate = _reported_rate(difference, prior.amount)
        except ArithmeticError:
            return difference, None, ("arithmetic_unavailable",)
    return difference, rate, () if rate is not None else ("nonpositive_base",)


def _reported_comparison(observation: FilingComparisons) -> ReportedFilingComparison:
    changes: list[_ReportedFilingChange] = []
    for pair in observation.pairs:
        before, after = pair.comparative, pair.current
        if before is None or after is None:
            continue  # The observation retains the missing/conflict reason.
        prior = Decimal(before.amount) if before.amount is not None else None
        current = Decimal(after.amount) if after.amount is not None else None
        difference = current - prior if prior is not None and current is not None else None
        rate = _reported_rate(difference, prior)
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
                rate=float(rate) if rate is not None else None,
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
