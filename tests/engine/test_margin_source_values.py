"""Preserve current margin source values beyond the present L2 share calculations."""

import sqlite3
from contextlib import closing
from datetime import date

import pyarrow.parquet as pq
import pytest

from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_reader import JQuantsReader
from baibai_engine.market.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_weekly_margin,
)
from baibai_engine.market.margin_reader import (
    published_margin_balance_dates,
    read_all_issues_daily_margin,
)
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot


@pytest.mark.parametrize("missing", [False, True])
def test_daily_margin_publication_and_values_survive_sqlite_and_lake(tmp_path, missing):
    day = date(2026, 10, 14)
    record: dict[str, object] = {
        "Date": day.isoformat(),
        "Code": "13010",
        "IssType": "2",
        "LongVol": 30,
        "ShrtVol": 12,
        "LongStdVol": 20,
        "LongNegVol": 10,
        "ShrtStdVol": 8,
        "ShrtNegVol": 4,
    }
    source = {
        "PubDate": "2026-10-15",
        "LongVal": 300.25,
        "ShrtVal": 120.5,
        "LongStdVal": 200.0,
        "LongNegVal": 100.25,
        "ShrtStdVal": 80.0,
        "ShrtNegVal": 40.5,
    }
    if not missing:
        record.update(source)
    expected = tuple(None if missing else value for value in source.values())
    columns = (
        "publication_date",
        "long_value",
        "short_value",
        "long_std_value",
        "long_neg_value",
        "short_std_value",
        "short_neg_value",
    )
    database = tmp_path / "market.sqlite"
    store_jquants_all_issues_daily_margin(database, [record], balance_date=day)
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT publication_date, long_value, short_value, long_std_value, "
            "long_neg_value, short_std_value, short_neg_value "
            "FROM jquants_all_issues_daily_margin"
        ).fetchone()
    assert row == expected
    mirror = tmp_path / "lake"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset_name="jquants.all_issues_daily_margin",
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="margin-source-values",
        )
    obj = report.manifest.partitions[0].objects[0]
    rows = pq.ParquetFile(mirror / obj.key).read().to_pylist()
    assert len(rows) == 1
    assert tuple(rows[0][column] for column in columns) == expected
    assert rows[0]["balance_date"] == day.isoformat()
    assert rows[0]["long_vol"] == 30.0
    typed = JQuantsReader(database).get_mkt_all_issues_daily_margin(day)[0]
    assert typed.publication_date == (None if missing else date(2026, 10, 15))


@pytest.mark.parametrize("source_date", [None, "2026-07-17"])
def test_weekly_margin_does_not_invent_requested_balance_date(tmp_path, source_date):
    requested = date(2026, 7, 24)
    record = {"Date": source_date, "Code": "13010", "LongVol": 30, "ShrtVol": 0}
    database = tmp_path / "market.sqlite"
    assert store_jquants_weekly_margin(database, [record], week_end=requested) == 0
    with pytest.raises(JQuantsProviderError, match="incomplete"):
        JQuantsReader(database).get_mkt_margin_interest_week(requested)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_weekly_margin").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM source_coverage WHERE source = 'jquants_weekly_margin'"
        ).fetchone() == ("partial",)


def test_weekly_native_zero_balance_is_not_missing(tmp_path):
    day = date(2026, 7, 24)
    record = {"Date": day.isoformat(), "Code": "13010", "LongVol": 30, "ShrtVol": 0}
    database = tmp_path / "market.sqlite"
    assert store_jquants_weekly_margin(database, [record], week_end=day) == 1
    result = JQuantsReader(database).get_mkt_margin_interest_week(day)[0]
    assert result.week_end == day
    assert result.short_vol == 0.0


def _daily_record(day, code, publication_date):
    return {
        "Date": day.isoformat(),
        "Code": code,
        "PubDate": publication_date,
        "IssType": "2",
        "LongVol": 30,
        "ShrtVol": 12,
        "LongStdVol": 20,
        "LongNegVol": 10,
        "ShrtStdVol": 8,
        "ShrtNegVol": 4,
    }


@pytest.mark.parametrize(
    ("asof", "expected"),
    [
        (date(2026, 9, 28), set()),
        (date(2026, 9, 29), {"1301"}),
        (date(2026, 10, 1), {"1301"}),
        (date(2026, 10, 2), {"1301", "1302"}),
    ],
)
def test_known_publication_is_applied_per_row_not_just_per_balance_date(tmp_path, asof, expected):
    day = date(2026, 9, 25)
    database = tmp_path / "market.sqlite"
    store_jquants_all_issues_daily_margin(
        database,
        [
            _daily_record(day, "13010", "2026-09-28"),
            _daily_record(day, "13020", "2026-10-01"),
        ],
        balance_date=day,
    )
    # The known publication needs no inferred bar calendar. Raw acquisition
    # reads retain both rows; analysis must not leak a later correction through
    # the first ticker's usable balance date.
    assert len(read_all_issues_daily_margin(database, day)) == 2
    assert published_margin_balance_dates(database, asof) == ([(day, "daily")] if expected else [])
    rows = read_all_issues_daily_margin(database, day, publication_asof=asof)
    assert {row.ticker for row in rows} == expected


def test_missing_publication_retains_the_existing_lag_rule(tmp_path):
    day = date(2026, 9, 25)
    database = tmp_path / "market.sqlite"
    store_jquants_all_issues_daily_margin(
        database, [_daily_record(day, "13010", None)], balance_date=day
    )
    asof = date(2026, 9, 29)
    assert published_margin_balance_dates(database, asof) == []
    assert read_all_issues_daily_margin(database, day, publication_asof=asof) == []
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES ('1301',?,100)",
            [("2026-09-28",), ("2026-09-29",)],
        )
    assert published_margin_balance_dates(database, date(2026, 9, 28)) == []
    assert published_margin_balance_dates(database, asof) == [(day, "daily")]
    rows = read_all_issues_daily_margin(database, day, publication_asof=asof)
    assert [row.ticker for row in rows] == ["1301"]


def test_screening_applies_publication_cutoff_to_latest_and_prior_rows(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.screening import margin_inputs

    database = tmp_path / "market.sqlite"
    prior, latest, asof = date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 6)
    for day, dates in ((prior, ("2026-09-28", "2026-10-07")), (latest, ("2026-10-05",) * 2)):
        store_jquants_all_issues_daily_margin(
            database,
            [
                _daily_record(day, code, published)
                for code, published in zip(("13010", "13020"), dates, strict=True)
            ],
            balance_date=day,
        )
    # A one-week fixture isolates propagation to both read paths; production's
    # 26-week sampling policy is unchanged.
    monkeypatch.setattr(margin_inputs, "MARGIN_DELTA_WEEKS", 1)
    current, previous = margin_inputs.read_margin_supply_demand_inputs(database, asof)
    assert set(current) == {"1301", "1302"}
    assert set(previous) == {"1301"}
