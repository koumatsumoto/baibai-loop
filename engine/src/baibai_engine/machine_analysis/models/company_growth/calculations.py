"""取得済み文書から状態・変化を作る。現在評価のanchor/carry/fallbackを使わない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    Metric,
    Observation,
    PeriodState,
    PeriodStatus,
    SourceDocument,
    Value,
)
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    AccountingPeriodKey,
    accounting_period_identity,
    certainly_disclosed_by,
    latest_disclosure_candidates,
    statement_basis,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    ACTUAL_FIELDS,
    annual_ttm_start,
    compose_ttm_amount,
    is_annual_period,
    period_days,
    prior_year_dates_match,
    prior_year_period_matches,
    resolve_actual_conflicts,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.primitives.dates import add_months_clamped

# Source names map meanings, not fallback priorities. NP is parent profit only for group statements.
_AMOUNT_FIELDS: tuple[tuple[Metric, str], ...] = (
    ("sales", "sales"),
    ("operating_profit", "operating_profit"),
    ("parent_profit", "profit"),
)
_REPORTED_FIELDS: tuple[tuple[Metric, str], ...] = (
    *_AMOUNT_FIELDS,
    ("reported_basic_eps", "cumulative_eps"),
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
    "証券・商品先物取引業",
    "保険業",
    "その他金融業",
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
            else "statement_basis_incomparable"
            if any(getattr(r, field) is not None for r in rows)
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


def _cumulative_revision_gap(
    operand: JQuantsFinancialSummary,
    other: JQuantsFinancialSummary,
    all_rows: Sequence[JQuantsFinancialSummary],
    field: str,
) -> str | None:
    """Identify changed overlapping cumulatives without inventing unknown same-day order."""
    if certainly_disclosed_by(operand, other):
        return None
    old = _select(
        [
            r
            for r in all_rows
            if accounting_period_identity(r) == accounting_period_identity(operand)
            and certainly_disclosed_by(r, other)
        ],
        field,
    )
    same_day_unknown = operand.disclosed_at == other.disclosed_at and (
        not operand.disclosed_time or not other.disclosed_time
    )
    if old is None:
        # No changed revision was observed; the unknown time alone is not a conflict.
        return None if same_day_unknown else "inconsistent_cumulative_revision"
    if getattr(old, field) == getattr(operand, field):
        return None
    return (
        "cumulative_revision_order_unresolved"
        if same_day_unknown
        else "inconsistent_cumulative_revision"
    )


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


def _closest_group(
    groups: Sequence[Sequence[JQuantsFinancialSummary]], *, expected_end: date
) -> Sequence[JQuantsFinancialSummary] | None:
    # Choose the measurement interval before inspecting any metric's availability.
    return min(
        groups,
        key=lambda group: (
            abs(((group[0].period_end or date.min) - expected_end).days),
            accounting_period_identity(group[0]),
        ),
        default=None,
    )


def _derived(
    current: Sequence[JQuantsFinancialSummary],
    groups: Sequence[Sequence[JQuantsFinancialSummary]],
    all_rows: Sequence[JQuantsFinancialSummary],
    *,
    ttm: bool,
) -> tuple[date, dict[Metric, Value], PeriodStatus]:
    anchor = current[0]
    assert anchor.period_start is not None
    assert anchor.period_end is not None
    days = period_days(anchor)
    if days is not None and (
        330 <= days <= 371 and is_annual_period(anchor.period_start, anchor.period_end)
        if ttm
        else 70 <= days <= 110
    ):
        return (
            anchor.period_start,
            {m: _value(current, m, f) for m, f in _AMOUNT_FIELDS},
            "available",
        )
    start = (
        add_months_clamped(anchor.period_end, -12) + timedelta(days=1)
        if ttm
        else max(anchor.period_start, add_months_clamped(anchor.period_end, -3) + timedelta(days=1))
    )
    selected_groups: list[Sequence[JQuantsFinancialSummary]] = []
    if ttm:
        prior_same = _closest_group(
            [
                g
                for g in groups
                if prior_year_period_matches(
                    g[0], anchor, length_tolerance_ratio=0.1, end_tolerance_days=45
                )
            ],
            expected_end=add_months_clamped(anchor.period_end, -12),
        )
        prior_fy = _closest_group(
            [
                g
                for g in groups
                if g[0].fiscal_year_end == anchor.period_start - timedelta(days=1)
                and (n := period_days(g[0])) is not None
                and 330 <= n <= 371
            ],
            expected_end=anchor.period_start - timedelta(days=1),
        )
        if prior_same is not None and prior_fy is not None:
            selected_groups = [prior_fy, prior_same]
    else:
        previous = _closest_group(
            [
                g
                for g in groups
                if g[0].period_end is not None
                and g[0].period_start == anchor.period_start
                and g[0].fiscal_year_end == anchor.fiscal_year_end
                and 70 <= (anchor.period_end - g[0].period_end).days <= 110
            ],
            expected_end=add_months_clamped(anchor.period_end, -3),
        )
        if previous is not None:
            selected_groups = [previous]
    period_reason: PeriodStatus | None = None
    if not selected_groups:
        period_reason = "matching_period_not_observed"
    else:
        previous_end = selected_groups[-1][0].period_end
        assert previous_end is not None
        if ttm:
            annual, prior_cumulative = selected_groups[0][0], selected_groups[-1][0]
            annual_start = annual_ttm_start(anchor, annual, prior_cumulative)
            if annual_start is None:
                period_reason = "accounting_periods_incomparable"
            else:
                start = annual_start
        else:
            start = previous_end + timedelta(days=1)
    metrics: dict[Metric, Value] = {}
    for metric, field in _AMOUNT_FIELDS:
        current_value = _value(current, metric, field)
        if current_value.value is None:
            metrics[metric] = current_value
            continue
        if period_reason is not None:
            metrics[metric] = Value(reason=period_reason)
            continue
        row = _select(current, field)
        assert row is not None
        candidates = [_select(group, field) for group in selected_groups]
        if any(candidate is None for candidate in candidates):
            metrics[metric] = Value(reason="operand_unavailable")
            continue
        operands = [candidate for candidate in candidates if candidate is not None]
        sources = [row, *operands]
        if not _compatible(sources):
            metrics[metric] = Value(reason="statement_basis_incomparable")
            continue
        revision_gap = (
            _cumulative_revision_gap(operands[-1], operands[0], all_rows, field)
            if ttm
            else _cumulative_revision_gap(operands[0], row, all_rows, field)
        )
        if revision_gap is not None:
            metrics[metric] = Value(reason=revision_gap)
            continue
        values = [float(getattr(r, field)) for r in sources]
        value = compose_ttm_amount(*values) if ttm else values[0] - values[1]
        metrics[metric] = Value(value=value, documents=tuple(_document(r) for r in sources))

    return start, metrics, period_reason or "available"


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
            start, metrics, period_status = _derived(group, groups, rows, ttm=ttm)
            _margin(metrics, sector)
            observations.append(
                Observation(
                    period_start=start,
                    period_end=anchor.period_end,
                    measurement="annual_ttm" if ttm else "quarter",
                    period_status=period_status,
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
    # 52/53週暦の最大1週差は許すが、決算月変更を同季節の前年比にしない。
    return (
        prior.measurement == current.measurement
        and prior.statement_basis == current.statement_basis
        and current.statement_basis in {"consolidated", "nonconsolidated"}
        and prior_year_dates_match(
            prior.period_start,
            prior.period_end,
            current.period_start,
            current.period_end,
            length_tolerance_ratio=0.1,
            end_tolerance_days=7,
        )
    )


def build_changes(states: Sequence[PeriodState]) -> tuple[Change, ...]:
    observations = [o for s in states for o in s.observations]
    changes: list[Change] = []
    for current in observations:
        candidates = [p for p in observations if _observation_matches(p, current)]
        prior_end = max((p.period_end for p in candidates), default=None)
        # Native accounting identities can share an actual interval. Keep every
        # candidate at the selected end before inspecting metric values; neither
        # input order nor an older good value may resolve a conflicting prior.
        priors = [p for p in candidates if p.period_end == prior_end]
        prior_starts = {p.period_start for p in priors}
        prior_start = next(iter(prior_starts)) if len(prior_starts) == 1 else None
        for metric, value in current.metrics.items():
            prior_values = {
                previous.value if (previous := p.metrics.get(metric)) is not None else None
                for p in priors
            }
            before = next(iter(prior_values)) if len(prior_values) == 1 else None
            after = value.value
            reason = None
            if metric in {
                "reported_basic_eps",
                "average_shares",
                "issued_shares",
                "treasury_shares",
            }:
                reason = "share_basis_unverified"
            elif not priors:
                reason = "comparable_prior_period_not_observed"
            elif len(prior_starts) != 1 or len(prior_values) != 1:
                reason = "ambiguous_growth_comparison"
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
                    from_start=prior_start,
                    from_end=prior_end,
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
