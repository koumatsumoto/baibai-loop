from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.screening.calibration.forward import (
    HORIZONS,
    _FYDividendObservation,
    _ticker_forward_rows,
)
from baibai_engine.market.price_models import JQuantsDailyBar


@pytest.mark.parametrize("missing", [2020, 2021, None])
@pytest.mark.parametrize("dps", [0.0, 50.0])
def test_missing_annual_document_does_not_become_zero_dividend(missing, dps):
    observations = [
        _FYDividendObservation(date(year, 3, 31), date(year, 5, 15), dps, date(year - 1, 4, 1))
        for year in (2020, 2021, 2022)
        if year != missing
    ]
    row = _row(observations)
    assert row.price_return == pytest.approx(0.2)
    if missing is None:
        assert row.total_return_status == "resolved"
        assert row.realized_dividend_sum == 3 * dps
    else:
        assert row.total_return_status == "unresolved_dividend_period_gap"
        assert row.realized_dividend_sum is None
        assert row.total_return is None


def test_short_fiscal_year_with_contiguous_periods_is_not_a_gap():
    row = _row(
        [
            _FYDividendObservation(date(2020, 3, 31), date(2020, 5, 15), 10.0, date(2019, 4, 1)),
            _FYDividendObservation(date(2020, 12, 31), date(2021, 2, 15), 20.0, date(2020, 4, 1)),
            _FYDividendObservation(date(2021, 12, 31), date(2022, 2, 15), 30.0, date(2021, 1, 1)),
            _FYDividendObservation(date(2022, 12, 31), date(2023, 2, 15), 40.0, date(2022, 1, 1)),
        ]
    )
    assert row.total_return_status == "resolved"
    assert row.realized_dividend_sum == 100.0


def _row(observations):
    return _ticker_forward_rows(
        "1000",
        [
            JQuantsDailyBar("1000", date(2020, 1, 31), 1000.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1000", date(2023, 1, 31), 1200.0, None, adjustment_factor=1.0),
        ],
        fy_dividends=observations,
        asofs=[date(2020, 1, 31)],
        horizons=[HORIZONS["3y"]],
        eval_cap=date(2023, 6, 30),
    )[0]


@pytest.mark.parametrize("month", [1, 3])
@pytest.mark.parametrize("first_start_offset", [0, 1, 2])
@pytest.mark.parametrize("dps", [0.0, 50.0])
def test_fiscal_year_end_entry_accepts_next_day_but_not_a_missing_day(
    month, first_start_offset, dps
):
    entry = date(2020, month, 31)
    exit_date = date(2023, month, 31)
    observations = [
        _FYDividendObservation(
            date(year, month, 31),
            date(year, month, 31) + timedelta(days=45),
            dps,
            entry + timedelta(days=first_start_offset)
            if year == 2021
            else date(year - 1, month, 31) + timedelta(days=1),
        )
        for year in (2021, 2022, 2023)
    ]
    row = _ticker_forward_rows(
        "1000",
        [
            JQuantsDailyBar("1000", entry, 1000.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1000", exit_date, 1200.0, None, adjustment_factor=1.0),
        ],
        fy_dividends=observations,
        asofs=[entry],
        horizons=[HORIZONS["3y"]],
        eval_cap=date(2023, 6, 30),
    )[0]
    assert row.price_return == pytest.approx(0.2)
    if first_start_offset <= 1:
        assert row.total_return_status == "resolved"
        assert row.realized_dividend_fy_count == 3
        assert row.realized_dividend_sum == 3 * dps
        assert row.total_return == pytest.approx(0.2 + 3 * dps / 1000)
    else:
        assert row.total_return_status == "unresolved_dividend_period_gap"
        assert row.realized_dividend_sum is None
        assert row.total_return is None
