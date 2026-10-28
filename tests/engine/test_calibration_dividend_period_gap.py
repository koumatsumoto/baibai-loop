from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.screening_sqlite import insert_financial_record

from baibai_engine.machine_analysis.historical_evaluation.forward import (
    HORIZONS,
    _FYDividendObservation,
    _load_fy_dividends,
    _ticker_forward_rows,
)
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.storage.observations.schema import open_connection


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


@pytest.mark.parametrize("dps", [0.0, 50.0])
@pytest.mark.parametrize("successor_dps", [None, 0.0, 999.0])
def test_successor_period_proves_missing_last_completed_fy(dps, successor_dps):
    observations = [
        _FYDividendObservation(date(year, 3, 31), date(year, 5, 15), dps, date(year - 1, 4, 1))
        for year in (2020, 2021)
    ]
    observations.append(
        _FYDividendObservation(
            date(2023, 3, 31), date(2023, 5, 15), successor_dps, date(2022, 4, 1)
        )
    )
    row = _row(observations)
    assert row.price_return == pytest.approx(0.2)
    assert row.total_return_status == "unresolved_dividend_period_gap"
    assert row.realized_dividend_sum is None
    assert row.total_return is None


@pytest.mark.parametrize("successor_start", [None, date(2022, 4, 1), date(2023, 2, 2)])
def test_successor_does_not_invent_gap_or_add_future_dividend(successor_start):
    observations = [
        _FYDividendObservation(date(year, 3, 31), date(year, 5, 15), 10.0, date(year - 1, 4, 1))
        for year in (2020, 2021, 2022)
    ]
    observations.append(
        _FYDividendObservation(date(2023, 3, 31), date(2023, 5, 15), 999.0, successor_start)
    )
    row = _row(observations)
    assert row.total_return_status == "resolved"
    assert row.realized_dividend_fy_count == 3
    assert row.realized_dividend_sum == 30.0
    assert row.total_return == pytest.approx(0.23)


def test_exit_next_day_period_start_proves_missing_short_fy():
    row = _row(
        [
            _FYDividendObservation(date(2020, 3, 31), date(2020, 5, 15), 10.0, date(2019, 4, 1)),
            _FYDividendObservation(date(2021, 3, 31), date(2021, 5, 15), 10.0, date(2020, 4, 1)),
            _FYDividendObservation(date(2022, 3, 31), date(2022, 5, 15), 10.0, date(2021, 4, 1)),
            _FYDividendObservation(date(2023, 3, 31), date(2023, 5, 15), 999.0, date(2023, 2, 1)),
        ]
    )
    assert row.total_return_status == "unresolved_dividend_period_gap"
    assert row.total_return is None


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


@pytest.mark.parametrize(
    "successor_case", ["missing", "conflict", "zero", "positive", "later_partial"]
)
def test_saved_successor_boundary_survives_missing_or_conflicting_dps(tmp_path, successor_case):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        for year in (2020, 2021, 2023):
            amounts = [10.0]
            if year == 2023:
                amounts = {
                    "missing": [None],
                    "later_partial": [None],
                    "conflict": [10.0, 20.0],
                    "zero": [0.0],
                    "positive": [999.0],
                }[successor_case]
            for index, amount in enumerate(amounts):
                insert_financial_record(
                    conn,
                    {
                        "Code": "10000",
                        "DiscNo": f"{year}-{index}",
                        "DiscDate": f"{year}-05-15",
                        "DiscTime": "15:00",
                        "CurPerType": "FY",
                        "CurFYEn": f"{year}-03-31",
                        "CurPerSt": f"{year - 1}-04-01",
                        "CurPerEn": f"{year}-03-31",
                        "DivAnn": amount,
                    },
                )
        if successor_case == "later_partial":
            insert_financial_record(
                conn,
                {
                    "Code": "10000",
                    "DiscNo": "2023-partial",
                    "DiscDate": "2023-05-16",
                    "CurPerType": "FY",
                    "CurFYEn": "2023-03-31",
                    "DivAnn": None,
                },
            )
        observations = _load_fy_dividends(conn, "1000", cutoff=date(2023, 6, 30))
    assert observations[-1].period_start == date(2022, 4, 1)
    if successor_case in {"missing", "conflict", "later_partial"}:
        assert observations[-1].dps_actual_annual is None
    row = _row(observations)
    assert row.price_return == pytest.approx(0.2)
    assert row.total_return_status == "unresolved_dividend_period_gap"
    assert row.total_return is None


@pytest.mark.parametrize("starts", [[None], ["2022-04-01", "2022-05-01"], ["2024-04-01"]])
def test_missing_dps_does_not_invent_an_unknown_or_conflicting_boundary(tmp_path, starts):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        for index, start in enumerate(starts):
            insert_financial_record(
                conn,
                {
                    "Code": "10000",
                    "DiscNo": f"2023-{index}",
                    "DiscDate": "2023-05-15",
                    "DiscTime": "15:00",
                    "CurPerType": "FY",
                    "CurFYEn": "2023-03-31",
                    "CurPerSt": start,
                    "CurPerEn": "2023-03-31",
                    "DivAnn": None,
                },
            )
        observations = _load_fy_dividends(conn, "1000", cutoff=date(2023, 6, 30))
    assert observations[0].period_start is None


def test_explicit_invalid_boundary_revision_does_not_restore_old_boundary(tmp_path):
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        for day, start in [("2023-05-15", "2022-04-01"), ("2023-05-16", "2024-04-01")]:
            insert_financial_record(
                conn,
                {
                    "Code": "10000",
                    "DiscNo": day,
                    "DiscDate": day,
                    "CurPerType": "FY",
                    "CurFYEn": "2023-03-31",
                    "CurPerSt": start,
                    "CurPerEn": "2023-03-31",
                    "DivAnn": None,
                },
            )
        observations = _load_fy_dividends(conn, "1000", cutoff=date(2023, 6, 30))
    assert observations[0].period_start is None
