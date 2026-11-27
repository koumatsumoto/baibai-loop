from datetime import date, timedelta
from itertools import permutations

import pytest
from tests.engine.machine_analysis.models.company_growth.test_company_growth import (
    amount,
    change,
    document,
    observation,
    states,
)

from baibai_engine.machine_analysis.models.company_growth.calculations import build_changes
from baibai_engine.machine_analysis.models.company_growth.models import (
    FINANCIAL_SECTOR_INAPPLICABLE,
    Observation,
    PeriodState,
    Value,
)


def period(start, end, value):
    return PeriodState(
        fiscal_period="2Q",
        fiscal_year_end=None,
        period_start=start,
        period_end=end,
        observations=(
            Observation(
                period_start=start,
                period_end=end,
                measurement="quarter",
                statement_basis="consolidated",
                accounting_standard="JP",
                metrics={"sales": Value(value=value)},
            ),
        ),
    )


@pytest.mark.parametrize(
    ("shift", "comparable"),
    [(-8, False), (-7, True), (0, True), (7, True), (8, False), (31, False)],
)
def test_yoy_does_not_treat_a_shifted_fiscal_quarter_as_the_same_season(shift, comparable):
    prior = period(
        date(2025, 6, 1) + timedelta(days=shift),
        date(2025, 8, 31) + timedelta(days=shift),
        100,
    )
    current = period(date(2026, 6, 1), date(2026, 8, 31), 120)
    change = build_changes((prior, current))[-1]
    assert change.rate == (0.2 if comparable else None)
    assert change.difference == (20 if comparable else None)
    assert change.reason == (None if comparable else "comparable_prior_period_not_observed")
    assert current.observations[0].metrics["sales"].value == 120
    assert prior.observations[0].metrics["sales"].value == 100


def test_53_week_quarter_can_differ_by_one_week_without_a_fiscal_month_shift():
    prior = period(date(2025, 6, 1), date(2025, 9, 7), 100)
    current = period(date(2026, 6, 1), date(2026, 8, 31), 120)
    assert build_changes((prior, current))[-1].rate == 0.2


def test_leap_month_end_and_incompatible_newer_interval_keep_the_exact_prior():
    current = period(date(2025, 1, 31), date(2025, 2, 28), 120)
    # Feb 28 was month end in 2025, so Mar 7 is exactly seven days after the
    # prior leap-year month end. A newer, incomparable interval must not win.
    edge = period(date(2024, 2, 7), date(2024, 3, 7), 100)
    earlier = period(date(2024, 1, 31), date(2024, 2, 29), 70)
    change = build_changes((edge, earlier, current))[-1]
    assert (change.from_end, change.from_value) == (edge.period_end, 100)
    too_long = period(date(2024, 1, 25), edge.period_end, 999)
    change = build_changes((too_long, earlier, current))[-1]
    assert (change.from_end, change.from_value) == (earlier.period_end, 70)


@pytest.mark.parametrize(
    ("other_value", "reason"),
    [(100, "ambiguous_growth_comparison"), (None, "operand_unavailable")],
)
def test_prior_tie_does_not_choose_a_good_value_or_fall_back_to_an_older_end(other_value, reason):
    older = period(date(2025, 4, 1), date(2025, 6, 23), 50)
    missing = period(date(2025, 4, 1), date(2025, 6, 30), None)
    other = period(date(2025, 4, 1), date(2025, 6, 30), other_value)
    current = period(date(2026, 4, 1), date(2026, 6, 30), 120)
    for ordered in permutations((older, missing, other, current)):
        change = next(item for item in build_changes(ordered) if item.to_end == current.period_end)
        assert (change.from_start, change.from_end) == (date(2025, 4, 1), date(2025, 6, 30))
        assert change.from_value is None
        assert change.to_value == 120
        assert change.difference is None
        assert change.reason == reason


def test_same_prior_end_and_value_do_not_resolve_conflicting_actual_starts():
    prior = period(date(2025, 4, 1), date(2025, 6, 30), 100)
    shifted = period(date(2025, 4, 2), date(2025, 6, 30), 100)
    current = period(date(2026, 4, 1), date(2026, 6, 30), 120)
    for ordered in permutations((prior, shifted, current)):
        change = next(item for item in build_changes(ordered) if item.to_end == current.period_end)
        assert change.from_start is None
        assert change.from_end == date(2025, 6, 30)
        assert change.from_value == 100
        assert change.difference is None
        assert change.reason == "ambiguous_growth_comparison"


def test_sector_inapplicability_never_overrides_a_missing_or_incomparable_prior():
    def margin_period(year, reason, *, basis="consolidated"):
        item = period(date(year, 4, 1), date(year, 6, 30), None)
        observation = item.observations[0].model_copy(
            update={"statement_basis": basis, "metrics": {"operating_margin": Value(reason=reason)}}
        )
        return item.model_copy(update={"observations": (observation,)})

    na = FINANCIAL_SECTOR_INAPPLICABLE
    current = margin_period(2026, na)
    known, unknown = margin_period(2025, na), margin_period(2025, "sector_unverified")
    for priors, reason in (
        ((), "comparable_prior_period_not_observed"),
        ((known,), na),
        ((unknown,), "operand_unavailable"),
        ((known, unknown), "operand_unavailable"),
        ((margin_period(2025, na, basis="nonconsolidated"),), "statement_basis_incomparable"),
    ):
        comparison = build_changes((*priors, current))[-1]
        assert (comparison.difference, comparison.reason) == (None, reason)


@pytest.mark.parametrize("reported_shares", [None, 1_000])
@pytest.mark.parametrize(
    ("kind", "basis", "standard", "unavailable"),
    [
        (
            "1QFinancialStatements_NonConsolidated_JP",
            "nonconsolidated",
            "JP",
            "statement_basis_incomparable",
        ),
        (
            "1QFinancialStatements_Consolidated_IFRS",
            "consolidated",
            "IFRS",
            "accounting_standard_incomparable",
        ),
        ("1QFinancialStatements_Consolidated_JP", "consolidated", "JP", None),
    ],
)
def test_latest_statement_scope_is_independent_of_unrelated_share_field(
    kind, basis, standard, unavailable, reported_shares
):
    prior = document(2023, 1, 80, 11, 5)
    original = document(2024, 1, 100, 10, 7, ShOutFY=1_000)
    correction = document(
        2024,
        1,
        None,
        DiscNo="scope-only",
        DiscDate="2024-09-01",
        DocType=kind,
        ShOutFY=reported_shares,
    )
    cohorts = states([prior, original, correction])
    latest = cohorts[-1]
    reported = observation(latest, "reported")
    assert (reported.statement_basis, reported.accounting_standard) == (basis, standard)
    for metric, prior_amount, current_amount in (("sales", 80, 100), ("parent_profit", 5, 7)):
        reading = reported.metrics[metric]
        comparison = change(build_changes(cohorts), latest.period_end, metric)
        if unavailable is None:
            assert reading.value == current_amount
            assert comparison.rate == pytest.approx((current_amount - prior_amount) / prior_amount)
        else:
            assert reading.value is comparison.rate is comparison.difference is None
            assert reading.reason == comparison.reason == unavailable
    assert amount(latest, "balance", "issued_shares").value == 1_000


@pytest.mark.parametrize("notice", ["EarnForecastRevision", "DividendForecastRevision"])
def test_notice_without_actuals_cannot_change_scope_or_create_period(notice):
    original = document(2024, 1, 0, 0)
    revision = document(2024, 1, None, DiscNo="revision", DiscDate="2024-09-01", DocType=notice)
    unobserved = document(2024, 2, None, DocType="2QFinancialStatements_NonConsolidated_IFRS")
    (cohort,) = states([original, revision, unobserved])
    reported = observation(cohort, "reported")
    assert (reported.statement_basis, reported.accounting_standard) == ("consolidated", "JP")
    assert reported.metrics["sales"].value == 0
    assert reported.metrics["parent_profit"].value is None
