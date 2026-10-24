"""取得済み文書から状態・変化を作る。現在評価のanchor/carry/fallbackを使わない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.market.financial_documents import (
    AccountingPeriodKey,
    accounting_period_identity,
    latest_disclosure_candidates,
    statement_basis,
)
from baibai_engine.market.financial_periods import (
    ACTUAL_FIELDS,
    compose_ttm_amount,
    period_days,
    prior_year_period_matches,
    resolve_actual_conflicts,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary

from .models import Change, Metric, Observation, PeriodState, SourceDocument, Value

# Source names map meanings, not fallback priorities. NP is parent profit only for group statements.
_AMOUNT_FIELDS: tuple[tuple[Metric, str], ...] = (
    ("sales", "sales"),
    ("operating_profit", "operating_profit"),
    ("parent_profit", "profit"),
)
_REPORTED_FIELDS: tuple[tuple[Metric, str], ...] = (
    *_AMOUNT_FIELDS,
    ("reported_basic_eps", "eps_ttm"),
    ("average_shares", "average_shares"),
    ("operating_cash_flow", "cfo"),
)
_BALANCE_FIELDS: tuple[tuple[Metric, str], ...] = (
    ("issued_shares", "shares_outstanding"),
    ("treasury_shares", "treasury_shares"),
    ("net_assets", "equity"),
    ("total_assets", "total_assets"),
)
_FINANCIAL_SECTORS = {
    "銀行業",
    "証券、商品先物取引業",
    "保険業",
    "その他金融業",
    "7050",
    "7100",
    "7150",
    "7200",
}


def _document(row: JQuantsFinancialSummary) -> SourceDocument:
    return SourceDocument(
        disclosure_number=row.disclosure_number,
        disclosed_at=row.disclosed_at,
        disclosed_time=row.disclosed_time,
        document_type=row.document_type,
    )


def _select(rows: Sequence[JQuantsFinancialSummary], field: str) -> JQuantsFinancialSummary | None:
    candidates = latest_disclosure_candidates(
        [r for r in rows if getattr(r, field) is not None or field in r.unresolved_actual_fields]
    )
    if not candidates or any(field in r.unresolved_actual_fields for r in candidates):
        return None
    if len({(getattr(r, field), statement_basis(r)) for r in candidates}) != 1:
        return None
    row = sorted(candidates, key=lambda r: r.disclosure_number or "")[-1]
    bases = {statement_basis(r) for r in latest_disclosure_candidates(rows)}
    if bases != {statement_basis(row)}:
        return None
    return row


def _value(rows: Sequence[JQuantsFinancialSummary], metric: Metric, field: str) -> Value:
    row = _select(rows, field)
    if row is None:
        reason = (
            "conflicting_revisions"
            if any(field in r.unresolved_actual_fields for r in rows)
            else "not_observed"
        )
        return Value(reason=reason)
    if metric == "parent_profit" and statement_basis(row) != "consolidated":
        return Value(reason="parent_attribution_unverified", documents=(_document(row),))
    return Value(value=getattr(row, field), documents=(_document(row),))


def _compatible(rows: Sequence[JQuantsFinancialSummary]) -> bool:
    return len({statement_basis(r) for r in rows}) == 1 and statement_basis(rows[0]) in {
        "consolidated",
        "nonconsolidated",
    }


def _later_changed(
    operand: JQuantsFinancialSummary,
    other: JQuantsFinancialSummary,
    all_rows: Sequence[JQuantsFinancialSummary],
    field: str,
) -> bool:
    """A known changed correction cannot be subtracted from an uncorrected later cumulative."""
    if operand.disclosed_at <= other.disclosed_at:
        return False
    old = _select(
        [
            r
            for r in all_rows
            if accounting_period_identity(r) == accounting_period_identity(operand)
            and r.disclosed_at <= other.disclosed_at
        ],
        field,
    )
    return old is None or getattr(old, field) != getattr(operand, field)


def _margin(metrics: dict[Metric, Value], sector: str | None) -> None:
    sales, profit = metrics["sales"], metrics["operating_profit"]
    reason = None
    if sector is None:
        reason = "sector_unverified"
    elif sector in _FINANCIAL_SECTORS:
        reason = "not_applicable_to_financial_sector"
    elif sales.value is None or profit.value is None:
        reason = "operand_unavailable"
    elif sales.value <= 0:
        reason = "nonpositive_sales"
    if reason is not None:
        metrics["operating_margin"] = Value(reason=reason)
        return
    assert sales.value is not None
    assert profit.value is not None
    metrics["operating_margin"] = Value(
        value=profit.value / sales.value,
        documents=tuple(dict.fromkeys((*sales.documents, *profit.documents))),
    )


def _derived(
    current: Sequence[JQuantsFinancialSummary],
    groups: Sequence[Sequence[JQuantsFinancialSummary]],
    all_rows: Sequence[JQuantsFinancialSummary],
    *,
    ttm: bool,
) -> tuple[date, dict[Metric, Value]]:
    anchor = current[0]
    assert anchor.period_start is not None
        assert anchor.period_end is not None
    start = (
        add_months_clamped(anchor.period_end, -12) + timedelta(days=1)
        if ttm
        else anchor.period_start
    )
    metrics: dict[Metric, Value] = {}
    for metric, field in _AMOUNT_FIELDS:
        row = _select(current, field)
        if row is None:
            metrics[metric] = Value(reason="operand_unavailable")
            continue
        days = period_days(row)
        if days is not None and (330 <= days <= 370 if ttm else 70 <= days <= 110):
            metrics[metric] = _value(current, metric, field)
            if ttm:
                start = anchor.period_start
            continue
        operands: list[JQuantsFinancialSummary] = []
        if ttm:
            for candidates in groups:
                candidate = _select(candidates, field)
                if candidate is not None and prior_year_period_matches(
                    candidate, row, length_tolerance_ratio=0.1, end_tolerance_days=45
                ):
                    operands.append(candidate)
            prior_same = max(operands, key=accounting_period_identity, default=None)
            prior_fy = None
            if anchor.fiscal_year_end is not None:
                prior_fy_end = add_months_clamped(anchor.fiscal_year_end, -12)
                fy_rows = [
                    r
                    for group in groups
                    for r in group
                    if r.fiscal_year_end == prior_fy_end
                    and (n := period_days(r)) is not None
                    and 330 <= n <= 370
                ]
                prior_fy = _select(fy_rows, field)
            operands = [] if prior_same is None or prior_fy is None else [prior_fy, prior_same]
        else:
            for candidates in groups:
                candidate = _select(candidates, field)
                if (
                    candidate is not None
                    and candidate.period_end is not None
                    and candidate.period_start == anchor.period_start
                    and candidate.fiscal_year_end == anchor.fiscal_year_end
                    and 70 <= (anchor.period_end - candidate.period_end).days <= 110
                ):
                    operands.append(candidate)
            operands = [max(operands, key=accounting_period_identity)] if operands else []
        if not operands:
            metrics[metric] = Value(reason="matching_period_not_observed")
            continue
        previous = operands[-1]
        assert previous.period_end is not None
        start = previous.period_end + timedelta(days=1)
        if ttm:
            annual = operands[0]
            assert annual.period_end is not None
            if (
                annual.period_start != previous.period_start
                or abs((anchor.period_start - annual.period_end).days - 1) > 45
            ):
                metrics[metric] = Value(reason="accounting_periods_incomparable")
                continue
        sources = [row, *operands]
        if not _compatible(sources):
            metrics[metric] = Value(reason="statement_basis_incomparable")
        elif metric == "parent_profit" and statement_basis(row) != "consolidated":
            metrics[metric] = Value(reason="parent_attribution_unverified")
        elif (not ttm and _later_changed(operands[0], row, all_rows, field)) or (
            ttm and _later_changed(operands[-1], operands[0], all_rows, field)
        ):
            metrics[metric] = Value(reason="inconsistent_cumulative_revision")
        else:
            values = [float(getattr(r, field)) for r in sources]
            value = compose_ttm_amount(*values) if ttm else values[0] - values[1]
            metrics[metric] = Value(value=value, documents=tuple(_document(r) for r in sources))
    return start, metrics


def build_period_states(
    documents: Sequence[JQuantsFinancialSummary], *, sector: str | None
) -> tuple[PeriodState, ...]:
    valid = [
        r
        for r in documents
        if period_days(r) is not None
        and (r.unresolved_actual_fields or any(getattr(r, f) is not None for f in ACTUAL_FIELDS))
    ]
    rows = resolve_actual_conflicts(valid)
    grouped: dict[AccountingPeriodKey, list[JQuantsFinancialSummary]] = {}
    for row in rows:
        grouped.setdefault(accounting_period_identity(row), []).append(row)
    groups = list(grouped.values())
    states: list[PeriodState] = []
    for _, group in sorted(grouped.items()):
        anchor = latest_disclosure_candidates(group)[0]
        assert anchor.period_start is not None
        assert anchor.period_end is not None
        bases = {statement_basis(r) for r in latest_disclosure_candidates(group)}
        basis = next(iter(bases)) if len(bases) == 1 else "basis_unresolved"
        reported = {m: _value(group, m, f) for m, f in _REPORTED_FIELDS}
        _margin(reported, sector)
        balances = {m: _value(group, m, f) for m, f in _BALANCE_FIELDS}
        observations = [
            Observation(
                period_start=anchor.period_start,
                period_end=anchor.period_end,
                measurement="reported",
                statement_basis=basis,
                metrics=reported,
            ),
            Observation(
                period_start=anchor.period_end,
                period_end=anchor.period_end,
                measurement="balance",
                statement_basis=basis,
                metrics=balances,
            ),
        ]
        for ttm in (False, True):
            start, metrics = _derived(group, groups, rows, ttm=ttm)
            _margin(metrics, sector)
            observations.append(
                Observation(
                    period_start=start,
                    period_end=anchor.period_end,
                    measurement="annual_ttm" if ttm else "quarter",
                    statement_basis=basis,
                    metrics=metrics,
                )
            )
        states.append(
            PeriodState(
                fiscal_period=anchor.fiscal_period,
                fiscal_year_end=anchor.fiscal_year_end,
                period_start=anchor.period_start,
                period_end=anchor.period_end,
                observations=tuple(observations),
            )
        )
    return tuple(states)


def _observation_matches(prior: Observation, current: Observation) -> bool:
    prior_days = (prior.period_end - prior.period_start).days + 1
    current_days = (current.period_end - current.period_start).days + 1
    return (
        prior.measurement == current.measurement
        and prior.statement_basis == current.statement_basis
        and current.statement_basis in {"consolidated", "nonconsolidated"}
        and abs(prior_days - current_days) <= max(1, round(current_days * 0.1))
        and abs((prior.period_end - add_months_clamped(current.period_end, -12)).days) <= 45
        and abs((prior.period_start - add_months_clamped(current.period_start, -12)).days) <= 45
    )


def build_changes(states: Sequence[PeriodState]) -> tuple[Change, ...]:
    observations = [o for s in states for o in s.observations]
    changes: list[Change] = []
    for current in observations:
        candidates = [p for p in observations if _observation_matches(p, current)]
        prior = max(candidates, key=lambda o: o.period_end, default=None)
        for metric, value in current.metrics.items():
            previous = prior.metrics.get(metric) if prior is not None else None
            before = previous.value if previous is not None else None
            after = value.value
            reason = None
            if metric in {
                "reported_basic_eps",
                "average_shares",
                "issued_shares",
                "treasury_shares",
            }:
                reason = "share_basis_unverified"
            elif prior is None:
                reason = "comparable_prior_period_not_observed"
            elif before is None or after is None:
                reason = "operand_unavailable"
            difference = (
                after - before
                if reason is None and before is not None and after is not None
                else None
            )
            changes.append(
                Change(
                    metric=metric,
                    measurement=current.measurement,
                    from_start=prior.period_start if prior else None,
                    from_end=prior.period_end if prior else None,
                    to_start=current.period_start,
                    to_end=current.period_end,
                    from_value=before,
                    to_value=after,
                    difference=difference,
                    rate=(
                        difference / before
                        if difference is not None
                        and before is not None
                        and before > 0
                        and metric != "operating_margin"
                        else None
                    ),
                    margin_points=difference * 100
                    if metric == "operating_margin" and difference is not None
                    else None,
                    sign_change=(
                        f"{'positive' if before > 0 else 'negative' if before < 0 else 'zero'}"
                        f"_to_{'positive' if after > 0 else 'negative' if after < 0 else 'zero'}"
                        if difference is not None and before is not None and after is not None
                        else None
                    ),
                    reason=reason
                    or ("nonpositive_base" if before is not None and before <= 0 else None),
                )
            )
    return tuple(changes)
