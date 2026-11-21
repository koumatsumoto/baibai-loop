"""最新のPDF組替比較を既存の状態・変化へ接続し、過去文書を改変しない。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal

from baibai_engine.machine_analysis.models.company_growth.calculations import (
    _margin,
    _missing_change_reason,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    Measurement,
    Metric,
    Observation,
    PeriodState,
    SourceDocument,
    Value,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    is_annual_period,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    PdfFinancialSelection,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    Metric as PdfMetric,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import PdfStatement

_INCOME_METRICS: tuple[PdfMetric, ...] = ("sales", "operating_profit", "parent_profit")


def _source(item: PdfStatement) -> SourceDocument:
    assert item.published_on is not None
    return SourceDocument(
        disclosure_number=item.disclosure_number,
        disclosed_at=item.published_on,
        disclosed_time=None,
        document_type="PDFDisplayAnnualReference_Consolidated_JP"
        if item.annual_reference
        else f"PDFDisplayFinancialStatements_Consolidated_{item.accounting_standard}",
        source_url=item.source_url,
        source_sha256=item.source_sha256,
    )


def _metrics(
    amounts: Mapping[PdfMetric, int],
    sources: tuple[SourceDocument, ...],
    reason: str | None = None,
    *,
    sector: str | None = None,
    precision: Literal[
        "published_display_amounts",
        "derived_from_truncated_display_amounts",
        "derived_from_display_amounts_rounding_unspecified",
    ] = ("published_display_amounts"),
) -> dict[Metric, Value]:
    result: dict[Metric, Value] = {
        metric: Value(
            value=amounts.get(metric),
            reason=reason,
            documents=sources,
            precision=precision,
        )
        for metric in _INCOME_METRICS
    }
    _margin(result, sector)
    if result["operating_margin"].value is not None:
        result["operating_margin"] = result["operating_margin"].model_copy(
            update={
                "precision": precision
                if precision != "published_display_amounts"
                else "derived_from_truncated_display_amounts"
            }
        )
    return result


def apply_pdf_periods(
    states: Sequence[PeriodState], selected: PdfFinancialSelection | None, *, sector: str | None
) -> tuple[PeriodState, ...]:
    if selected is None:
        return tuple(states)
    if selected.current is None:
        if not selected.blocks_legacy_ttm:
            return tuple(states)
        return tuple(
            state.model_copy(
                update={
                    "observations": tuple(
                        observation.model_copy(
                            update={
                                "metrics": {
                                    metric: Value(reason=selected.reason)
                                    if metric
                                    in {
                                        "sales",
                                        "operating_profit",
                                        "parent_profit",
                                        "operating_margin",
                                    }
                                    else value
                                    for metric, value in observation.metrics.items()
                                }
                            }
                        )
                        if observation.measurement == "annual_ttm"
                        else observation
                        for observation in state.observations
                    )
                }
            )
            if (state.period_start, state.period_end)
            == (selected.period_start, selected.period_end)
            else state
            for state in states
        )
    current = selected.current
    assert current.current_start is not None
    assert current.current_end is not None
    sources = (_source(current),) + ((_source(selected.annual),) if selected.annual else ())
    annual_metrics = _metrics(
        selected.amounts_yen,
        sources,
        selected.reason,
        sector=sector,
        precision=selected.precision,
    )
    reported_metrics = _metrics(
        {pair.metric: pair.current * 1_000_000 for pair in current.pairs},
        (_source(current),),
        sector=sector,
    )
    result = []
    for state in states:
        if (state.period_start, state.period_end) != (current.current_start, current.current_end):
            result.append(state)
            continue
        observations = []
        for observation in state.observations:
            if (
                observation.statement_basis != current.statement_basis
                or observation.accounting_standard != current.accounting_standard
            ):
                observations.append(observation)
                continue
            metrics = dict(observation.metrics)
            if observation.measurement == "annual_ttm":
                metrics.update(annual_metrics)
                observation = observation.model_copy(
                    update={
                        "period_start": selected.period_start or observation.period_start,
                        "period_status": "available"
                        if selected.status == "available"
                        else "matching_period_not_observed",
                    }
                )
            elif observation.measurement == "reported" or (
                observation.measurement == "quarter" and state.fiscal_period == "1Q"
            ):
                metrics.update(reported_metrics)
            observations.append(observation.model_copy(update={"metrics": metrics}))
        result.append(state.model_copy(update={"observations": tuple(observations)}))
    return tuple(result)


def apply_pdf_changes(
    changes: Sequence[Change],
    selected: PdfFinancialSelection | None,
    *,
    states: Sequence[PeriodState],
    sector: str | None,
) -> tuple[Change, ...]:
    if selected is None:
        return tuple(changes)
    if not selected.comparative_verified:
        return _unverified_annual_comparative(changes, selected, states)
    if selected.current is None:
        if not selected.blocks_legacy_ttm:
            return tuple(changes)
        return tuple(
            change.model_copy(
                update={
                    "from_value": None,
                    "difference": None,
                    "rate": None,
                    "margin_points": None,
                    "sign_change": None,
                    "reason": selected.reason,
                }
            )
            if (change.to_start, change.to_end) == (selected.period_start, selected.period_end)
            and change.measurement in {"reported", "quarter"}
            and change.metric in {"sales", "operating_profit", "parent_profit", "operating_margin"}
            else change
            for change in changes
        )
    current = selected.current
    assert current.current_start is not None
    assert current.current_end is not None
    before = {pair.metric: pair.comparative * 1_000_000 for pair in current.pairs}
    after = {pair.metric: pair.current * 1_000_000 for pair in current.pairs}
    prior_metrics = _metrics(before, (_source(current),), sector=sector)
    current_metrics = _metrics(after, (_source(current),), sector=sector)
    result = []
    for change in changes:
        if (
            change.to_start != current.current_start
            or change.to_end != current.current_end
            or not _statement_comparison_measurement(current, change.measurement)
            or change.metric not in current_metrics
            or (
                change.measurement == "quarter"
                and (current.current_end - current.current_start).days > 100
            )
        ):
            result.append(change)
            continue
        prior, value = prior_metrics[change.metric].value, current_metrics[change.metric].value
        difference = value - prior if value is not None and prior is not None else None
        result.append(
            change.model_copy(
                update={
                    "from_start": current.comparative_start,
                    "from_end": current.comparative_end,
                    "from_value": prior,
                    "to_value": value,
                    "difference": difference,
                    "rate": difference / prior
                    if difference is not None
                    and prior is not None
                    and prior > 0
                    and change.metric != "operating_margin"
                    else None,
                    "margin_points": difference * 100
                    if difference is not None and change.metric == "operating_margin"
                    else None,
                    "sign_change": (
                        f"{'positive' if prior > 0 else 'negative' if prior < 0 else 'zero'}"
                        f"_to_{'positive' if value > 0 else 'negative' if value < 0 else 'zero'}"
                    )
                    if prior is not None and value is not None
                    else None,
                    "reason": _missing_change_reason(
                        current_metrics[change.metric], prior_metrics[change.metric]
                    )
                    if difference is None
                    else "nonpositive_base"
                    if prior is not None and prior <= 0
                    else None,
                    "precision": "derived_from_truncated_display_amounts",
                    "source_sha256": current.source_sha256,
                }
            )
        )
    return tuple(result)


def _unverified_annual_comparative(
    changes: Sequence[Change], selected: PdfFinancialSelection, states: Sequence[PeriodState]
) -> tuple[Change, ...]:
    current = selected.current
    if (
        current is None
        or selected.status != "available"
        or selected.annual_revision_basis != "latest_reconfirmed_unchanged_amounts"
        or not _statement_comparison_measurement(current, "annual_ttm")
    ):
        return tuple(changes)
    before = {pair.metric: pair.comparative * 1_000_000 for pair in current.pairs}
    matching = [
        (index, change)
        for index, change in enumerate(changes)
        if change.measurement in {"reported", "annual_ttm"}
        and (change.to_start, change.to_end) == (current.current_start, current.current_end)
        and (change.from_start, change.from_end)
        == (current.comparative_start, current.comparative_end)
        and change.reason in {None, "nonpositive_base"}
    ]
    # 当期額不変は前年比較の再確認ではない。丸め一単位内は確定矛盾にしない。
    affected = {
        index
        for index, change in matching
        if change.metric in before
        and change.from_value is not None
        and abs(Decimal(str(change.from_value)) - before[change.metric]) > 1_000_000
        and _prior_predates_pdf(change, current, states)
    }
    margin_measurements = {
        change.measurement
        for index, change in matching
        if index in affected and change.metric in {"sales", "operating_profit"}
    }
    affected.update(
        index
        for index, change in matching
        if change.metric == "operating_margin" and change.measurement in margin_measurements
    )
    return tuple(
        change.model_copy(
            update={
                "from_value": None,
                "difference": None,
                "rate": None,
                "margin_points": None,
                "sign_change": None,
                "reason": "restated_comparative_unverified_after_revision",
            }
        )
        if index in affected
        else change
        for index, change in enumerate(changes)
    )


def _prior_predates_pdf(
    change: Change, current: PdfStatement, states: Sequence[PeriodState]
) -> bool:
    priors = [
        value
        for state in states
        for observation in state.observations
        if observation.measurement == change.measurement
        and (observation.period_start, observation.period_end)
        == (change.from_start, change.from_end)
        and (value := observation.metrics.get(change.metric)) is not None
        and value.value == change.from_value
    ]
    # 後発の前年訂正を古いPDFで否定しない。同日の前後もPDFの日付から推測しない。
    return (
        current.published_on is not None
        and bool(priors)
        and all(
            value.documents
            and all(source.disclosed_at < current.published_on for source in value.documents)
            for value in priors
        )
    )


def _statement_comparison_measurement(current: PdfStatement, measurement: Measurement) -> bool:
    if measurement == "annual_ttm":
        # 年度そのものの窓だけ。同じFYを旧nativeの前年値へ戻して増減を反転させない。
        return (
            current.current_start is not None
            and current.current_end is not None
            and is_annual_period(current.current_start, current.current_end)
        )
    return measurement in {"reported", "quarter"}


def pdf_comparison_prior(
    selected: PdfFinancialSelection | None, observation: Observation, change: Change
) -> Observation | None:
    if selected is None or selected.current is None:
        return None
    current = selected.current
    if (
        change.source_sha256 != current.source_sha256
        or (change.from_start, change.from_end)
        != (current.comparative_start, current.comparative_end)
        or (change.to_start, change.to_end) != (current.current_start, current.current_end)
        or not _statement_comparison_measurement(current, change.measurement)
        or current.comparative_start is None
        or current.comparative_end is None
    ):
        return None
    # The saved change is sourced from this same statement's restated prior column.
    source = (_source(current),)
    metrics: dict[Metric, Value] = {
        pair.metric: Value(
            value=pair.comparative * 1_000_000,
            documents=source,
            precision="published_display_amounts",
        )
        for pair in current.pairs
    }
    if change.metric == "operating_margin":
        sales, operating = metrics["sales"].value, metrics["operating_profit"].value
        metrics["operating_margin"] = Value(
            value=operating / sales
            if operating is not None and sales is not None and sales > 0
            else None,
            documents=source,
            precision="derived_from_truncated_display_amounts"
            if current.rounding == "truncate_below_unit"
            else "derived_from_display_amounts_rounding_unspecified",
        )
    return observation.model_copy(
        update={
            "period_start": current.comparative_start,
            "period_end": current.comparative_end,
            "metrics": metrics,
        }
    )
