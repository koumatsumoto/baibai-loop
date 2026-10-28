import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.engine.test_edinet_metric_eligibility import BASE, RECORDED, TARGET, _seed
from tests.helpers.lake_release import market_store

import baibai_engine.machine_analysis.generation.inputs as generation_reader
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
    TTMRules,
)
from baibai_engine.machine_analysis.generation.runners.technical_analysis import technical_analysis
from baibai_engine.machine_analysis.observations.prices.calendar_context import price_calendar_basis
from baibai_engine.market.edinet_metrics.store import EDINETMetricBaselineError
from baibai_engine.storage.lake.writer import sealed_sqlite_snapshot


def test_generation_reads_shared_snapshots_and_uses_new_release_not_old_origin(tmp_path, mocker):
    path = market_store(tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),))
    spies = [
        mocker.spy(generation_reader, name)
        for name in (
            "read_price_bars",
            "read_financial_documents",
            "read_observation_population",
            "read_price_day_states",
        )
    ]
    mocker.patch(
        "baibai_engine.machine_analysis.models.market_context.security.read_price_bars",
        side_effect=AssertionError("must reuse shared prices"),
    )
    mocker.patch(
        "baibai_engine.machine_analysis.models.market_context.snapshot.read_price_bars",
        side_effect=AssertionError("must reuse shared prices"),
    )
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="generated-from-new-release",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="new-release", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=date(2026, 1, 5),
            evaluation_time=datetime(2026, 1, 6, tzinfo=UTC),
            ttm_rules=TTMRules(
                full_year_min_days=330,
                full_year_max_days=370,
                period_length_tolerance_ratio=0.1,
                period_end_tolerance_days=45,
            ),
            tickers=("1234", "5678"),
        )
    result = result.materialize()
    assert result.inputs["market_snapshot"].sha256 == snapshot.ref.sha256
    assert result.inputs["market_snapshot"].reference == snapshot.ref.source_id
    assert len(result.models) == 11
    assert result.inputs["market"].reference == "new-release"
    assert (
        result.result("company_growth", "1234").payload["input_release"]["release_id"]
        == "new-release"
    )
    assert result.result("market_context", "market").payload["reading"] is None
    assert [spy.call_count for spy in spies] == [1, 1, 1, 1]


@pytest.mark.parametrize("benchmark_mixed", [False, True])
def test_shared_price_path_matches_single_query_and_keeps_old_mc_and_adv(
    tmp_path, mocker, benchmark_mixed
):
    as_of = date(2026, 10, 26)
    calendar = {
        as_of - timedelta(days=n): (as_of - timedelta(days=n)).weekday() < 5 for n in range(500)
    }
    dates = sorted(day for day, business in calendar.items() if business)[-253:]
    rows = tuple(
        (ticker, day.isoformat(), (100.0 if day != as_of else 75.0), 1.0)
        for ticker in ("7800", "1321")
        for day in dates
    )
    path = market_store(tmp_path / "market.sqlite", daily_bars=rows)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [(day.isoformat(), "1" if business else "0") for day, business in calendar.items()],
        )
        connection.execute(
            "UPDATE jquants_daily_bars SET open=close,high=close,low=close,volume=100,"
            "turnover_value=100000000"
        )
        if benchmark_mixed:
            connection.execute(
                "UPDATE jquants_daily_bars SET open=NULL WHERE ticker='1321' AND traded_at=?",
                (as_of.isoformat(),),
            )
    before = path.read_bytes()
    reader = mocker.spy(generation_reader, "read_price_day_states")
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="price-path-fixture",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=as_of,
            evaluation_time=datetime(2026, 10, 27, tzinfo=UTC),
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("7800",),
        ).materialize()
    assert reader.call_count == 1
    technical = result.result("technical_analysis", "7800").payload
    assert technical == technical_analysis(path, ticker="7800", as_of=as_of)
    assert technical["price_path"]["annotation"]["matched_refs"] == ["R1", "R20", "G60"]
    market = result.result("market_context", "7800").payload
    assert market["price"]["avg_turnover_20d_oku"] == 1
    assert market["price"]["return_20d"] == -0.25
    assert market["price"]["high_52w"] == 100
    comparison = market["price_path_relative"]["comparisons"]["G252"]
    if benchmark_mixed:
        assert comparison["relative_return"] is None
        assert comparison["status"] == "missing_benchmark_endpoint"
    else:
        assert comparison["relative_return"] == 0
    assert path.read_bytes() == before


def test_calendar_requires_contiguous_dates_without_coupling_y_to_m_history():
    sunday = date(2026, 10, 25)
    calendar = {
        sunday - timedelta(days=n): (sunday - timedelta(days=n)).weekday() < 5 for n in range(400)
    }
    friday = date(2026, 10, 23)
    expected, dates = price_calendar_basis(calendar, as_of=sunday)
    assert expected == friday
    assert len(dates) >= 253
    del calendar[sunday - timedelta(days=100)]
    expected, dates = price_calendar_basis(calendar, as_of=sunday)
    assert expected == friday
    assert dates == ()
    del calendar[sunday]
    assert price_calendar_basis(calendar, as_of=sunday) == (None, ())


def test_calendar_short_history_and_current_holiday_gap_are_distinct():
    friday, saturday, sunday = date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)
    assert price_calendar_basis({friday: True}, as_of=friday) == (friday, (friday,))
    assert price_calendar_basis({friday: True, sunday: False}, as_of=sunday) == (
        None,
        (),
    )
    assert price_calendar_basis({friday: True, saturday: False, sunday: False}, as_of=sunday) == (
        friday,
        (friday,),
    )


@pytest.mark.parametrize("corrupt", [False, True])
def test_edinet_uses_one_fixed_shared_read_and_preserves_delayed_source_contract(
    tmp_path, mocker, corrupt
):
    path = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(
            ("7203", TARGET.isoformat(), 10.0, 1.0),
            ("8306", TARGET.isoformat(), 20.0, 1.0),
        ),
    )
    _seed(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE edinet_metrics SET consolidation_basis='consolidated'")
        if corrupt:
            connection.execute("DELETE FROM edinet_metrics WHERE ticker='8306'")
    reader = mocker.spy(generation_reader.EDINETReader, "load_metric_records")
    mocker.patch("requests.Session", side_effect=AssertionError("must not fetch EDINET"))
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        arguments = dict(
            generation_id="fixed-edinet",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="new-release", manifest_sha256="a" * 64),
            economic_path=None,
            economic_identity=None,
            as_of=TARGET,
            evaluation_time=datetime(2026, 4, 28, tzinfo=UTC),
            ttm_rules=TTMRules(
                full_year_min_days=330,
                full_year_max_days=370,
                period_length_tolerance_ratio=0.1,
                period_end_tolerance_days=45,
            ),
            tickers=("7203", "8306"),
        )
        if corrupt:
            with pytest.raises(EDINETMetricBaselineError, match="row count mismatch"):
                generation_reader.read_and_generate_models(**arguments)
            return
        stream = generation_reader.read_and_generate_models(**arguments)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE edinet_metrics SET cash=999")
    result = stream.materialize()
    assert reader.call_count == 1
    assert result.inputs["market_snapshot"].sha256 == snapshot.ref.sha256
    for ticker in ("7203", "8306"):
        financial = result.result("company_valuation", ticker).payload["financial"]
        assert financial["cash"] == 123.0
        assert financial["edinet_snapshot_asof"] == BASE.isoformat()
        assert financial["edinet_snapshot_recorded_at_utc"] == RECORDED
        assert financial["edinet_source_doc_id"] in ("S100SAVED", "S100OTHER")
