from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.company_growth.calculations import build_changes
from baibai_engine.machine_analysis.company_growth.models import Observation, PeriodState, Value


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
