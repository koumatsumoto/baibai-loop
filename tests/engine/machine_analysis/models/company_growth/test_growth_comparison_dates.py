from datetime import date, timedelta
from itertools import permutations

import pytest

from baibai_engine.machine_analysis.models.company_growth.calculations import build_changes
from baibai_engine.machine_analysis.models.company_growth.models import (
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


@pytest.mark.parametrize(("shift", "comparable"), [(0, True), (7, True), (8, False), (31, False)])
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
