from __future__ import annotations

from datetime import date

import pytest

from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_short_sale_reports
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import read_short_sale_reports
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


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
        "SSName": reporter,
        "DICName": "",
        "FundName": "",
        "ShrtPosToSO": ratio,
        "ShrtPosShares": 0 if ratio == 0 else 600_000,
        "ShrtPosUnits": 0 if ratio == 0 else 6_000,
        "PrevRptDate": None,
        "PrevRptRatio": None,
        "Notes": notes,
    }


def test_same_date_reporter_rows_preserve_distinct_values(tmp_path) -> None:
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
        requested_start=date(2019, 1, 1),
        requested_end=date(2019, 1, 31),
    )

    stored = read_short_sale_reports(sqlite_path, date(2019, 1, 1), date(2019, 1, 31))

    assert stored is not None
    assert len(stored) == 3
    assert [row.source_ordinal for row in stored if row.ticker == "7203"] == [0, 1]
    assert [row.short_ratio for row in stored if row.ticker == "7203"] == [0.006, 0.008]
    assert [row.short_ratio for row in stored if row.ticker == "6758"] == [0.007]


def test_short_sale_range_read_distinguishes_empty_coverage_from_missing(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    start = date(2019, 1, 1)
    end = date(2019, 1, 2)
    store_jquants_short_sale_reports(sqlite_path, [], requested_start=start, requested_end=end)

    assert read_short_sale_reports(sqlite_path, start, end) == []
    assert read_short_sale_reports(sqlite_path, start, date(2019, 1, 3)) is None


def test_empty_refetch_replaces_short_sale_rows(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    start = date(2019, 1, 1)
    end = date(2019, 1, 31)
    store_jquants_short_sale_reports(
        sqlite_path,
        [_report(disclosed_at="2019-01-10", calculated_at="2019-01-08")],
        requested_start=start,
        requested_end=end,
    )

    assert (
        store_jquants_short_sale_reports(sqlite_path, [], requested_start=start, requested_end=end)
        == 0
    )

    conn = open_connection(sqlite_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM jquants_short_sale_reports").fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    assert read_short_sale_reports(sqlite_path, start, end) == []


def test_provider_nan_values_are_normalized_as_missing(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    record = _report(disclosed_at="2019-01-10", calculated_at="2019-01-08")
    record.update(
        {
            "DICName": float("nan"),
            "FundName": float("nan"),
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


@pytest.mark.parametrize("ratio", [None, -0.001])
@pytest.mark.parametrize("notes", [None, "corrected report"])
def test_unusable_latest_ratio_is_not_inferred_as_zero(tmp_path, ratio, notes) -> None:
    path = tmp_path / "market.sqlite"
    records = [
        _report(disclosed_at="2019-01-10", calculated_at="2019-01-08"),
        _report(disclosed_at="2019-01-12", calculated_at="2019-01-11", ratio=ratio, notes=notes),
        _report(disclosed_at="2019-01-12", calculated_at="2019-01-11", code="67580"),
    ]
    store_jquants_short_sale_reports(
        path,
        records,
        requested_start=date(2019, 1, 1),
        requested_end=date(2019, 1, 31),
    )
    stored = read_short_sale_reports(path, date(2019, 1, 12), date(2019, 1, 12))
    assert stored is not None
    row = next(row for row in stored if row.ticker == "7203")
    assert row.short_ratio == ratio
    assert row.notes == notes


@pytest.mark.parametrize("ratio", [0.0, 0.003, 0.007])
def test_numeric_correction_uses_reported_value_not_notes(tmp_path, ratio) -> None:
    path = tmp_path / "market.sqlite"
    records = [
        _report(disclosed_at="2019-01-10", calculated_at="2019-01-08"),
        _report(
            disclosed_at="2019-01-12",
            calculated_at="2019-01-08",
            ratio=ratio,
            notes="correction of the earlier report",
        ),
    ]
    store_jquants_short_sale_reports(
        path,
        records,
        requested_start=date(2019, 1, 1),
        requested_end=date(2019, 1, 31),
    )
    stored = read_short_sale_reports(path, date(2019, 1, 1), date(2019, 1, 31))
    assert stored is not None
    assert len(stored) == 2
    assert stored[0].short_ratio == 0.006
    assert stored[1].short_ratio == ratio
    assert stored[1].calculated_at == date(2019, 1, 8)
    assert stored[1].disclosed_at == date(2019, 1, 12)
    assert stored[1].notes == "correction of the earlier report"
