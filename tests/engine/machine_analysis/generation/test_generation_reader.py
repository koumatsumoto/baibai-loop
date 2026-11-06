import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.edinet_saved_inputs import BASE, RECORDED, TARGET, _seed
from tests.helpers.lake_release import market_store

import baibai_engine.machine_analysis.generation.inputs as generation_reader
from baibai_engine.machine_analysis.generation.runners.technical_analysis import technical_analysis
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
    TTMRules,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    EDINETMetricBaselineError,
)
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot


def test_generation_reads_shared_snapshots_and_uses_new_release_not_old_origin(tmp_path, mocker):
    path = market_store(tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),))
    spies = [
        mocker.spy(generation_reader, name)
        for name in (
            "read_price_bars",
            "read_financial_documents",
            "read_observation_population",
            "read_price_day_states",
            "read_trend_inputs",
        )
    ]
    mocker.patch(
        "baibai_engine.machine_analysis.generation.runners.market_snapshot.read_price_bars",
        side_effect=AssertionError("must reuse shared prices"),
    )
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="generated-from-new-release",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="new-release", manifest_sha256="a" * 64),
            economic_input=None,
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
    assert result.subjects == ("1234", "5678")
    result = result.materialize()
    assert result.inputs["market_snapshot"].sha256 == snapshot.ref.sha256
    assert result.inputs["market_snapshot"].reference == snapshot.ref.source_id
    company_model_ids = {
        "company_financials",
        "company_growth",
        "technical_analysis",
        "external_assessment",
        "market_context",
    }
    stock_models = {
        (model.subject, model.model_id)
        for model in result.models
        if model.subject in ("1234", "5678")
    }
    assert stock_models == {
        (ticker, model_id) for ticker in ("1234", "5678") for model_id in company_model_ids
    }
    shared_models = {
        (model.subject, model.model_id)
        for model in result.models
        if model.subject not in ("1234", "5678")
    }
    assert shared_models == {
        ("market", "market_context"),
        ("industry:jp-pharmaceutical-wholesale", "industry_trends"),
        ("industry:jp-information-services", "industry_trends"),
        ("market:global", "social_trends"),
    }
    assert len({model.model_id for model in result.models}) == 7
    assert {subject for subject, plan in result.subject_plan.items() if plan.scope == "stock"} == {
        "1234",
        "5678",
    }
    for ticker in ("1234", "5678"):
        assert set(result.subject_plan[ticker].model_ids) == company_model_ids
    for subject, model_id in shared_models - {("market", "market_context")}:
        payload = result.result(model_id, subject).payload
        assert payload["input_manifest"]["release_id"] == "new-release"
        assert payload["coverage"]["state"] == "not_collected"
    assert result.inputs["market"].reference == "new-release"
    assert (
        result.result("company_growth", "1234").payload["input_release"]["release_id"]
        == "new-release"
    )
    assert result.result("market_context", "market").payload["reading"] is None
    assert [spy.call_count for spy in spies] == [1, 1, 1, 1, 1]
    assert spies[2].call_args.kwargs["include_tradingview"] is True
    external = result.result("external_assessment", "1234")
    assert external.calculation_revision == "same-series-v6"
    assert external.payload["tradingview"]["availability"] == "not_observed"


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
            economic_input=None,
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
    resolved = price_calendar(calendar, as_of=sunday)
    assert resolved.expected_price_date == friday
    assert len(resolved.trading_dates) >= 253
    del calendar[sunday - timedelta(days=100)]
    resolved = price_calendar(calendar, as_of=sunday)
    assert resolved.expected_price_date == friday
    assert resolved.trading_dates == ()
    assert resolved.windows[20] is not None
    del calendar[sunday]
    resolved = price_calendar(calendar, as_of=sunday)
    assert resolved.expected_price_date is None
    assert resolved.trading_dates == ()


def test_calendar_short_history_and_current_holiday_gap_are_distinct():
    friday, saturday, sunday = date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)
    for calendar, expected, dates in (
        ({friday: True}, friday, (friday,)),
        ({friday: True, sunday: False}, None, ()),
        ({friday: True, saturday: False, sunday: False}, friday, (friday,)),
    ):
        resolved = price_calendar(calendar, as_of=max(calendar))
        assert resolved.expected_price_date == expected
        assert resolved.trading_dates == dates


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
            economic_input=None,
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
        financial = result.result("company_financials", ticker).payload["financial"]
        assert financial["cash"] == 123.0
        assert financial["edinet_snapshot_asof"] == BASE.isoformat()
        assert financial["edinet_snapshot_recorded_at_utc"] == RECORDED
        assert financial["edinet_source_doc_id"] in ("S100SAVED", "S100OTHER")


def test_generation_retains_provider_capital_from_the_same_expected_price_day(tmp_path):
    path = market_store(tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET market_cap_million_yen=200 WHERE ticker='1234' AND traded_at='2026-01-05'"
        )
        connection.executemany(
            "INSERT OR REPLACE INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [
                (
                    (date(2026, 1, 5) - timedelta(days=n)).isoformat(),
                    "1" if (date(2026, 1, 5) - timedelta(days=n)).weekday() < 5 else "0",
                )
                for n in range(500)
            ],
        )
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        generated = generation_reader.read_and_generate_models(
            generation_id="provider-capital",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=None,
            as_of=date(2026, 1, 5),
            evaluation_time=datetime(2026, 1, 6, tzinfo=UTC),
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1234",),
        ).materialize()
    observation = generated.result("company_financials", "1234").payload[
        "provider_capital_observation"
    ]
    assert observation == {
        "price_date": "2026-01-05",
        "close_yen": 10.0,
        "market_cap_million_yen": 200.0,
    }


def test_holder_source_failure_does_not_hide_independent_models(tmp_path, monkeypatch):
    path = market_store(tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),))

    def fail(*_args, **_kwargs):
        raise ValueError("unsupported holder source")

    monkeypatch.setattr(generation_reader, "read_holder_capital", fail)
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="partial",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=None,
            as_of=date(2026, 1, 5),
            evaluation_time=datetime(2026, 1, 6, tzinfo=UTC),
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1234",),
        ).materialize()
    assert (
        result.result("company_financials", "1234").payload["holder_capital_evidence"]["coverage"]
        == "unavailable"
    )
    assert result.result("company_growth", "1234") is not None


def test_shared_market_model_contains_retained_tradingview_observations(tmp_path):
    from tests.helpers.r2_store import MemoryR2Store
    from tests.helpers.tradingview_market import CLOCK, capture

    from baibai_engine.data_collection.providers.tradingview.market_receipts import (
        retain_market_capture,
    )
    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.economic_inputs import (
        rebuild_economic_input,
        retain_economic_input,
    )

    market = market_store(
        tmp_path / "market.sqlite", daily_bars=(("1234", "2026-01-05", 10.0, 1.0),)
    )
    economic = tmp_path / "economic.sqlite"
    with closing(initialize_database(economic)) as c:
        retain_market_capture(c, capture(receipt_id="quote"))
        retain_market_capture(
            c, capture(receipt_id="bar", bars=True, received=CLOCK + timedelta(minutes=1))
        )
    store = MemoryR2Store()
    identity = retain_economic_input(
        store, economic, as_of=date(2026, 1, 5), evaluation_time=CLOCK + timedelta(hours=1)
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as snapshot:
        result = generation_reader.read_and_generate_models(
            generation_id="with-tv",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=rebuild_economic_input(store, economic, identity),
            as_of=date(2026, 1, 5),
            evaluation_time=CLOCK + timedelta(hours=1),
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1234",),
        ).materialize()
    common = result.result("market_context", "market")
    tv = common.payload["tradingview_market"]
    assert tv["quotes"][0]["receipt_id"] == "quote"
    assert tv["bars"][0]["receipt_id"] == "bar"
    assert "tradingview" in common.calculation_revision


@pytest.mark.parametrize("changed", ["as_of", "evaluation_time"])
def test_saved_economic_clocks_must_match_generation(tmp_path, changed):
    from tests.helpers.r2_store import MemoryR2Store

    from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
    from baibai_engine.machine_analysis.generation.economic_inputs import (
        rebuild_economic_input,
        retain_economic_input,
    )

    economic = tmp_path / "economic.sqlite"
    initialize_database(economic).close()
    store = MemoryR2Store()
    day = date(2026, 1, 5)
    evaluated = datetime(2026, 1, 6, tzinfo=UTC)
    identity = retain_economic_input(store, economic, as_of=day, evaluation_time=evaluated)
    fixed = rebuild_economic_input(store, economic, identity)
    market = market_store(tmp_path / "market.sqlite")
    with (
        sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as snapshot,
        pytest.raises(ValueError, match="economic input clocks differ"),
    ):
        generation_reader.read_and_generate_models(
            generation_id="clock-mismatch",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=fixed,
            as_of=day + timedelta(days=1) if changed == "as_of" else day,
            evaluation_time=evaluated + timedelta(seconds=1)
            if changed == "evaluation_time"
            else evaluated,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1301",),
        )


@pytest.mark.parametrize("late", [None, "seal", "evaluation"])
def test_generation_binds_edinet_selection_clock_without_changing_sql_readers(tmp_path, late):
    from tests.helpers.r2_store import MemoryR2Store

    from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
    from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
    from baibai_engine.machine_analysis.generation.edinet_inputs import (
        read_edinet_input,
        retain_edinet_input,
    )

    store = MemoryR2Store()
    lists = FixedEdinetLists(store, {"format": "edinet-list-native-v1", "receipts": []})
    archives = FixedEdinetArchives(
        store,
        {
            "format": "edinet-archive-native-v1",
            "archives": [],
            "acquisition_scope": {"since": None, "doc_ids": [], "legacy_origins": []},
        },
    )
    market = market_store(tmp_path / "market.sqlite")
    before = market.read_bytes()
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "lake") as snapshot:
        at = snapshot.sealed_at
        saved = retain_edinet_input(store, lists, archives, selected_at=at)
        fixed = read_edinet_input(store, saved)
        if late == "seal":
            from dataclasses import replace

            snapshot = replace(snapshot, sealed_at=at - timedelta(microseconds=1))
        arguments = dict(
            generation_id="native-edinet",
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=None,
            edinet_input=fixed,
            as_of=at.date(),
            evaluation_time=at - timedelta(microseconds=1) if late == "evaluation" else at,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=("1301",),
        )
        if late:
            with pytest.raises(ValueError, match="EDINET input was selected after"):
                generation_reader.read_and_generate_models(**arguments)
        else:
            result = generation_reader.read_and_generate_models(**arguments).materialize()
            assert result.inputs["edinet"] == saved
    assert market.read_bytes() == before
