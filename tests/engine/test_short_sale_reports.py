from __future__ import annotations

from datetime import date

import pytest

from baibai_engine.market.sqlite import EmptyRangeReplacementError, open_connection
from baibai_engine.screening.sqlite_cache import store_jquants_short_sale_reports
from baibai_engine.screening.sqlite_reader import (
    SHORT_SALE_REPORT_DATASET_FLOOR,
    read_reported_short_metrics,
    read_short_sale_reports,
)


def _report(
    *,
    disclosed_at: str,
    calculated_at: str,
    code: str = "72030",
    reporter: str = "Reporter A",
    ratio: float | None = 0.006,
    notes: str | None = None,
) -> dict[str, object]:
    return {
        "DiscDate": disclosed_at,
        "CalcDate": calculated_at,
        "Code": code,
        "ShortSellerName": reporter,
        "DiscretionaryInvestmentContractorName": "",
        "InvestmentFundName": "",
        "ShortPositionsToSharesOutstandingRatio": ratio,
        "ShortPositionsInSharesNumber": 600_000,
        "ShortPositionsInTradingUnitsNumber": 6_000,
        "PrevRptDate": None,
        "ShortPositionsInPreviousReportingRatio": None,
        "Notes": notes,
    }


def test_reported_short_metrics_are_point_in_time_latest_reporter_states(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    records = [
        _report(disclosed_at="2019-01-10", calculated_at="2019-01-08", ratio=0.006),
        _report(
            disclosed_at="2019-02-10",
            calculated_at="2019-02-08",
            ratio=None,
            notes="previous report cancelled",
        ),
        _report(
            disclosed_at="2019-02-15",
            calculated_at="2019-02-13",
            reporter="Reporter B",
            ratio=0.007,
        ),
        _report(
            disclosed_at="2020-01-10",
            calculated_at="2020-01-08",
            code="67580",
            reporter="Future Reporter",
            ratio=0.009,
        ),
        _report(
            disclosed_at="2019-02-20",
            calculated_at="2019-02-18",
            code="83060",
            reporter="Reporter C",
            ratio=0.004,
        ),
    ]
    store_jquants_short_sale_reports(
        sqlite_path,
        records,
        requested_start=SHORT_SALE_REPORT_DATASET_FLOOR,
        requested_end=date(2020, 1, 31),
    )

    metrics = read_reported_short_metrics(sqlite_path, date(2019, 3, 1))

    assert metrics is not None
    assert metrics["7203"].ratio == pytest.approx(0.007)
    assert metrics["7203"].breadth == 1
    assert metrics["7203"].latest_disclosed_at == date(2019, 2, 15)
    assert "6758" not in metrics
    assert "8306" not in metrics


def test_reported_short_metrics_fail_closed_before_continuous_dataset_coverage(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    store_jquants_short_sale_reports(
        sqlite_path,
        [_report(disclosed_at="2019-01-10", calculated_at="2019-01-08")],
        requested_start=date(2018, 1, 1),
        requested_end=date(2019, 3, 1),
    )

    assert read_reported_short_metrics(sqlite_path, date(2019, 3, 1)) is None


def test_latest_reporter_tie_is_preserved_and_only_that_ticker_fails_closed(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    records = [
        _report(disclosed_at="2019-01-10", calculated_at="2019-01-08", ratio=0.006),
        _report(disclosed_at="2019-01-10", calculated_at="2019-01-08", ratio=0.008),
        _report(
            disclosed_at="2019-01-10",
            calculated_at="2019-01-08",
            code="67580",
            reporter="Reporter B",
            ratio=0.007,
        ),
    ]
    store_jquants_short_sale_reports(
        sqlite_path,
        records,
        requested_start=SHORT_SALE_REPORT_DATASET_FLOOR,
        requested_end=date(2019, 1, 31),
    )

    stored = read_short_sale_reports(
        sqlite_path, SHORT_SALE_REPORT_DATASET_FLOOR, date(2019, 1, 31)
    )
    metrics = read_reported_short_metrics(sqlite_path, date(2019, 1, 31))

    assert stored is not None
    assert len(stored) == 3
    assert [row.source_ordinal for row in stored if row.ticker == "7203"] == [0, 1]
    assert metrics is not None
    assert metrics["7203"] is None
    assert metrics["6758"] is not None


def test_short_sale_range_read_distinguishes_empty_coverage_from_missing(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    start = date(2019, 1, 1)
    end = date(2019, 1, 2)
    store_jquants_short_sale_reports(sqlite_path, [], requested_start=start, requested_end=end)

    assert read_short_sale_reports(sqlite_path, start, end) == []
    assert read_short_sale_reports(sqlite_path, start, date(2019, 1, 3)) is None


def test_empty_refetch_does_not_delete_short_sale_rows(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    start = date(2019, 1, 1)
    end = date(2019, 1, 31)
    store_jquants_short_sale_reports(
        sqlite_path,
        [_report(disclosed_at="2019-01-10", calculated_at="2019-01-08")],
        requested_start=start,
        requested_end=end,
    )

    with pytest.raises(EmptyRangeReplacementError):
        store_jquants_short_sale_reports(sqlite_path, [], requested_start=start, requested_end=end)

    conn = open_connection(sqlite_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM jquants_short_sale_reports").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_provider_nan_values_are_normalized_as_missing(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    record = _report(disclosed_at="2019-01-10", calculated_at="2019-01-08")
    record.update(
        {
            "DiscretionaryInvestmentContractorName": float("nan"),
            "InvestmentFundName": float("nan"),
            "PrevRptDate": float("nan"),
        }
    )
    store_jquants_short_sale_reports(
        sqlite_path,
        [record],
        requested_start=date(2019, 1, 10),
        requested_end=date(2019, 1, 10),
    )

    rows = read_short_sale_reports(sqlite_path, date(2019, 1, 10), date(2019, 1, 10))

    assert rows is not None
    assert rows[0].discretionary_investment_contractor_name == ""
    assert rows[0].investment_fund_name == ""
    assert rows[0].previous_reported_at is None
