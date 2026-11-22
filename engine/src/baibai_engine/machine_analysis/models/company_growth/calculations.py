"""取得済み文書から状態・変化を作る。現在評価のanchor/carry/fallbackを使わない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from baibai_engine.machine_analysis.models.company_growth.models import (
    FINANCIAL_SECTOR_INAPPLICABLE,
    Change,
    Measurement,
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
    accounting_standard,
    certainly_disclosed_by,
    comparable_reporting_basis,
    latest_disclosure_candidates,
    latest_reporting_basis_matches,
    reporting_basis_rows,
    statement_basis,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    ACTUAL_FIELDS,
    annual_cumulative_periods,
    annual_ttm_start,
    comparable_period_lengths,
    compose_ttm_amount,
    is_annual_period,
    period_days,
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
    issuer_capital = field in {"shares_outstanding", "treasury_shares"}
    candidates = latest_disclosure_candidates(
        [r for r in rows if getattr(r, field) is not None or field in r.unresolved_actual_fields]
    )
    if not candidates or any(field in r.unresolved_actual_fields for r in candidates):
        return None
    if len({getattr(r, field) for r in candidates}) != 1:
        return None
    if (
        not issuer_capital
        and len({(statement_basis(r), accounting_standard(r)) for r in candidates}) != 1
    ):
        return None
    row = sorted(candidates, key=lambda r: r.disclosure_number or "")[-1]
    if not issuer_capital and not latest_reporting_basis_matches(rows, row):
        return None
    return row


def _value(
    rows: Sequence[JQuantsFinancialSummary],
    metric: Metric,
    field: str,
    row: JQuantsFinancialSummary | None,
) -> Value:
    if row is None:
        observed = latest_disclosure_candidates(
            [item for item in rows if getattr(item, field) is not None]
        )
        basis_rows = [
            *observed,
            *latest_disclosure_candidates(reporting_basis_rows(rows)),
        ]
        reason = (
            "conflicting_revisions"
            if any(field in r.unresolved_actual_fields for r in rows)
            else "statement_basis_incomparable"
            if observed and len({statement_basis(item) for item in basis_rows}) != 1
            else "accounting_standard_incomparable"
            if observed and len({accounting_standard(item) for item in basis_rows}) != 1
            else "conflicting_revisions"
            if observed
            else "not_observed"
        )
        return Value(reason=reason)
    if metric == "parent_profit" and statement_basis(row) != "consolidated":
        return Value(reason="parent_attribution_unverified", documents=(_document(row),))
    return Value(value=getattr(row, field), documents=(_document(row),))


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
        reason = FINANCIAL_SECTOR_INAPPLICABLE
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


def _closest_groups(
    groups: Sequence[Sequence[JQuantsFinancialSummary]], *, expected_end: date
) -> list[Sequence[JQuantsFinancialSummary]]:
    # 値の揃う期間を探すと、欠測科目だけ比較基準がずれる。
    distance = min(
        (abs(((group[0].period_end or date.min) - expected_end).days) for group in groups),
        default=None,
    )
    return [
        group
        for group in groups
        if abs(((group[0].period_end or date.min) - expected_end).days) == distance
    ]


type _PeriodSources = dict[str, JQuantsFinancialSummary | None]


def _select_operands(
    sources: Sequence[_PeriodSources], field: str
) -> list[JQuantsFinancialSummary]:
    # 別の会計identityを開示順で上書きせず、同じ実期間の値が一致した時だけ使う。
    selected = [source[field] for source in sources]
    if not selected or any(row is None for row in selected):
        return []
    rows = [row for row in selected if row is not None]
    if (
        len({(getattr(row, field), statement_basis(row), accounting_standard(row)) for row in rows})
        != 1
    ):
        return []
    return sorted(rows, key=accounting_period_identity)


def _derived(
    anchor: JQuantsFinancialSummary,
    groups: Sequence[Sequence[JQuantsFinancialSummary]],
    all_rows: Sequence[JQuantsFinancialSummary],
    *,
    reported: dict[Metric, Value],
    period_sources: dict[AccountingPeriodKey, _PeriodSources],
    ttm: bool,
) -> tuple[date, dict[Metric, Value], PeriodStatus]:
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
            {metric: reported[metric] for metric, _ in _AMOUNT_FIELDS},
            "available",
        )
    expected_end = add_months_clamped(anchor.period_end, -12 if ttm else -3)
    start = expected_end + timedelta(days=1)
    selected_groups: list[list[Sequence[JQuantsFinancialSummary]]] = []
    period_reason: PeriodStatus | None = None
    if ttm:
        tolerance_days = 45
        expected_ordinal = expected_end.toordinal()
        prior_fy_end = anchor.period_start - timedelta(days=1)
        prior_fy = _closest_groups(
            [
                g
                for g in groups
                if g[0].fiscal_year_end == prior_fy_end
                and (n := period_days(g[0])) is not None
                and 330 <= n <= 371
            ],
            expected_end=prior_fy_end,
        )
        if prior_fy:
            date_matches = [
                g
                for g in groups
                # 同じ期末許容幅の外は、開始日や期間長を比べるまでもない。
                if (end := g[0].period_end) is not None
                and abs(end.toordinal() - expected_ordinal) <= tolerance_days
                and prior_year_period_matches(
                    g[0], anchor, length_tolerance_ratio=0.1, end_tolerance_days=tolerance_days
                )
            ]
            eligible = annual_cumulative_periods([g[0] for g in date_matches], prior_fy[0][0])
            prior_same = _closest_groups(
                [g for g in date_matches if accounting_period_identity(g[0]) in eligible],
                expected_end=expected_end,
            )
            if prior_same:
                selected_groups = [prior_fy, prior_same]
            elif date_matches:
                period_reason = "accounting_periods_incomparable"
    else:
        start = max(anchor.period_start, start)
        previous = _closest_groups(
            [
                g
                for g in groups
                if g[0].period_end is not None
                and g[0].period_start == anchor.period_start
                and g[0].fiscal_year_end == anchor.fiscal_year_end
                and 70 <= (anchor.period_end - g[0].period_end).days <= 110
            ],
            expected_end=expected_end,
        )
        if previous:
            selected_groups = [previous]
    if not selected_groups:
        period_reason = period_reason or "matching_period_not_observed"
    elif any(
        len({(group[0].period_start, group[0].period_end) for group in candidates}) != 1
        for candidates in selected_groups
    ):
        period_reason = "accounting_periods_incomparable"
    else:
        previous_end = selected_groups[-1][0][0].period_end
        assert previous_end is not None
        if ttm:
            annual, prior_cumulative = selected_groups[0][0][0], selected_groups[-1][0][0]
            annual_start = annual_ttm_start(anchor, annual, prior_cumulative)
            if annual_start is None:
                period_reason = "accounting_periods_incomparable"
            else:
                start = annual_start
        else:
            start = previous_end + timedelta(days=1)
    current_sources = period_sources[accounting_period_identity(anchor)]
    prior_sources = [
        [period_sources[accounting_period_identity(group[0])] for group in cohort]
        for cohort in selected_groups
    ]
    metrics: dict[Metric, Value] = {}
    for metric, field in _AMOUNT_FIELDS:
        current_value = reported[metric]
        if current_value.value is None:
            metrics[metric] = current_value
            continue
        if period_reason is not None:
            metrics[metric] = Value(reason=period_reason)
            continue
        row = current_sources[field]
        assert row is not None
        candidates = [_select_operands(cohort, field) for cohort in prior_sources]
        if any(not candidate for candidate in candidates):
            metrics[metric] = Value(reason="operand_unavailable")
            continue
        sources = [row, *(operand for cohort in candidates for operand in cohort)]
        if not comparable_reporting_basis(sources):
            metrics[metric] = Value(
                reason="statement_basis_incomparable"
                if len({statement_basis(source) for source in sources}) != 1
                else "accounting_standard_incomparable"
            )
            continue
        # 等値の別identityにも変更履歴がある。代表1件の開示順で他候補を隠さない。
        revision_gaps = {
            gap
            for operand in candidates[-1]
            for counterpart in (candidates[0] if ttm else [row])
            if (gap := _cumulative_revision_gap(operand, counterpart, all_rows, field)) is not None
        }
        revision_gap = next(
            (
                reason
                for reason in (
                    "inconsistent_cumulative_revision",
                    "cumulative_revision_order_unresolved",
                )
                if reason in revision_gaps
            ),
            None,
        )
        if revision_gap is not None:
            metrics[metric] = Value(reason=revision_gap)
            continue
        values = [float(getattr(r, field)) for r in (row, *(cohort[0] for cohort in candidates))]
        value = compose_ttm_amount(*values) if ttm else values[0] - values[1]
        metrics[metric] = Value(
            value=value, documents=tuple(dict.fromkeys(_document(r) for r in sources))
        )

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
    # Only complete cohorts share a selection. A historical revision check still
    # selects from the disclosures known at its counterpart's clock.
    sources: dict[AccountingPeriodKey, _PeriodSources] = {
        key: {field: _select(group, field) for _, field in (*_REPORTED_FIELDS, *_BALANCE_FIELDS)}
        for key, group in grouped.items()
    }
    states: list[PeriodState] = []
    for key, group in sorted(grouped.items()):
        selected = sources[key]
        anchor = latest_disclosure_candidates(group)[0]
        assert anchor.period_start is not None
        assert anchor.period_end is not None
        latest_basis = latest_disclosure_candidates(reporting_basis_rows(group))
        bases = {statement_basis(r) for r in latest_basis}
        basis = next(iter(bases)) if len(bases) == 1 else "basis_unresolved"
        standards = {accounting_standard(r) for r in latest_basis}
        standard = next(iter(standards)) if len(standards) == 1 else None
        reported = {m: _value(group, m, f, selected[f]) for m, f in _REPORTED_FIELDS}
        _margin(reported, sector)
        balances = {m: _value(group, m, f, selected[f]) for m, f in _BALANCE_FIELDS}
        observations = [
            Observation(
                period_start=anchor.period_start,
                period_end=anchor.period_end,
                measurement="reported",
                statement_basis=basis,
                accounting_standard=standard,
                metrics=reported,
            ),
            Observation(
                period_start=anchor.period_end,
                period_end=anchor.period_end,
                measurement="balance",
                statement_basis=basis,
                accounting_standard=standard,
                metrics=balances,
            ),
        ]
        for ttm in (False, True):
            start, metrics, period_status = _derived(
                group[0], groups, rows, reported=reported, period_sources=sources, ttm=ttm
            )
            _margin(metrics, sector)
            observations.append(
                Observation(
                    period_start=start,
                    period_end=anchor.period_end,
                    measurement="annual_ttm" if ttm else "quarter",
                    period_status=period_status,
                    statement_basis=basis,
                    accounting_standard=standard,
                    metrics=metrics,
                )
            )
        states.append(
            PeriodState(
                cohort=key,
                fiscal_period=anchor.fiscal_period,
                fiscal_year_end=anchor.fiscal_year_end,
                period_start=anchor.period_start,
                period_end=anchor.period_end,
                observations=tuple(observations),
            )
        )
    return tuple(states)


type _CohortObservation = tuple[PeriodState, Observation]


def _latest_comparable_periods(
    current: Observation,
    by_end: dict[tuple[Measurement, int], list[_CohortObservation]],
) -> list[_CohortObservation]:
    current_days = (current.period_end - current.period_start).days + 1
    if current_days <= 0:
        return []
    # 52/53週暦で許す前後1週間のみを新しい期末から照合する。同終端はすべて残す。
    tolerance = 7
    expected_start = add_months_clamped(current.period_start, -12).toordinal()
    expected_end = add_months_clamped(current.period_end, -12).toordinal()
    for end in range(expected_end + tolerance, expected_end - tolerance - 1, -1):
        candidates = by_end.get((current.measurement, end))
        if candidates is None:
            continue
        priors = [
            (cohort, prior)
            for cohort, prior in candidates
            if abs(prior.period_start.toordinal() - expected_start) <= tolerance
            and comparable_period_lengths(
                end - prior.period_start.toordinal() + 1, current_days, tolerance_ratio=0.1
            )
        ]
        if priors:
            return priors
    return []


def _missing_change_reason(current: Value, *priors: Value | None) -> str:
    # 両端で非該当が明示されたときだけ、前年の欠測・曖昧な比較と区別する。
    if priors and all(
        item is not None and item.value is None and item.reason == FINANCIAL_SECTOR_INAPPLICABLE
        for item in (current, *priors)
    ):
        return FINANCIAL_SECTOR_INAPPLICABLE
    return "operand_unavailable"


def build_changes(states: Sequence[PeriodState]) -> tuple[Change, ...]:
    observations = [(state, item) for state in states for item in state.observations]
    by_end: dict[tuple[Measurement, int], list[_CohortObservation]] = {}
    for state, item in observations:
        by_end.setdefault((item.measurement, item.period_end.toordinal()), []).append((state, item))
    changes: list[Change] = []
    for state, current in observations:
        candidates = _latest_comparable_periods(current, by_end)
        # 同じ最新終端では明示の期区分を先に使う。値やbasisで候補を選び直さない。
        if len(candidates) > 1 and state.fiscal_period in {"1Q", "2Q", "3Q", "FY"}:
            exact = [pair for pair in candidates if pair[0].fiscal_period == state.fiscal_period]
            candidates = exact or candidates
        priors = [prior for _, prior in candidates]
        prior_cohorts = tuple(
            dict.fromkeys(prior.cohort for prior, _ in candidates if prior.cohort is not None)
        )
        prior_end = priors[0].period_end if priors else None
        prior_starts = {p.period_start for p in priors}
        prior_start = next(iter(prior_starts)) if len(prior_starts) == 1 else None
        for metric, value in current.metrics.items():
            # Each filing keeps its own share basis. Its dated amounts are observations,
            # not comparable year-over-year earnings or dilution.
            if metric in {
                "reported_basic_eps",
                "average_shares",
                "issued_shares",
                "treasury_shares",
            }:
                continue
            prior_values = {
                previous.value if (previous := p.metrics.get(metric)) is not None else None
                for p in priors
            }
            before = next(iter(prior_values)) if len(prior_values) == 1 else None
            after = value.value
            reason = None
            if not priors:
                reason = "comparable_prior_period_not_observed"
            elif len(prior_cohorts) > 1 or len(prior_starts) != 1 or len(prior_values) != 1:
                reason = "ambiguous_growth_comparison"
            elif (
                current.statement_basis not in {"consolidated", "nonconsolidated"}
                or any(p.statement_basis != current.statement_basis for p in priors)
                or current.accounting_standard is None
                or any(p.accounting_standard != current.accounting_standard for p in priors)
            ):
                reason = (
                    "statement_basis_incomparable"
                    if current.statement_basis not in {"consolidated", "nonconsolidated"}
                    or any(p.statement_basis != current.statement_basis for p in priors)
                    else "accounting_standard_incomparable"
                )
            elif before is None or after is None:
                reason = _missing_change_reason(value, *(p.metrics.get(metric) for p in priors))
            difference = (
                after - before
                if reason is None and before is not None and after is not None
                else None
            )
            changes.append(
                Change(
                    target_cohort=state.cohort,
                    native_prior_cohorts=prior_cohorts if state.cohort is not None else None,
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


def native_comparison_priors(
    states: Sequence[PeriodState], change: Change
) -> tuple[Observation, ...]:
    """保存した前年operandだけを読む。省略されたcohortを同じ日付から補わない。"""
    cohorts = set(change.native_prior_cohorts or ())
    if not cohorts or len(cohorts) != len(change.native_prior_cohorts or ()):
        return ()
    matches = [
        (state.cohort, item)
        for state in states
        if state.cohort in cohorts
        for item in state.observations
        if item.measurement == change.measurement
        and item.period_start == change.from_start
        and item.period_end == change.from_end
    ]
    if len(matches) != len(cohorts) or {key for key, _ in matches} != cohorts:
        return ()
    return tuple(item for _, item in matches)
