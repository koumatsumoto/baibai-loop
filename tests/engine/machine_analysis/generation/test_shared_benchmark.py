"""市場共通のbenchmarkは一度保存し、旧入力や個社の端点へ混ぜない。"""

import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import materialize_generation, publish_generation

from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.workflow.screening_brief import economic_screening_brief
from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.generation.inputs import read_and_generate_models
from baibai_engine.machine_analysis.generation.query import (
    read_generation,
    read_model_inputs,
    read_research_model_inputs,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    project_technical_financial_row,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot

ASOF = date(2026, 10, 23)
AT = datetime(2026, 10, 24, tzinfo=UTC)
TICKERS = ("1001", "1002", "1003")


def source(tmp_path, benchmark_count):
    rows = [
        (ticker, (ASOF - timedelta(days=days)).isoformat(), 100.0, 1000.0)
        for ticker, count in (("1001", 300), ("1002", 5))
        for days in range(2, count + 2)
    ]
    rows.extend(
        ("1321", (ASOF - timedelta(days=days)).isoformat(), 200.0 if days == 230 else 100.0, 1000.0)
        for days in range(1, benchmark_count + 1)
    )
    # Future values cannot turn missing/short current history into a full benchmark.
    rows.append(("1321", (ASOF + timedelta(days=1)).isoformat(), 10000.0, 1000.0))
    return market_store(tmp_path / "market.sqlite", daily_bars=rows)


def generate(path, *, identity="shared", tickers=TICKERS):
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=path.parent / "lake") as snapshot:
        stream = read_and_generate_models(
            generation_id=identity,
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=None,
            as_of=ASOF,
            evaluation_time=AT,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=tickers,
        )
        return materialize_generation(stream)


@pytest.mark.parametrize("benchmark_count", [0, 5, 300])
def test_common_benchmark_keeps_missingness_dates_and_its_own_lookback(tmp_path, benchmark_count):
    path = source(tmp_path, benchmark_count)
    before = path.read_bytes()
    result = generate(path)
    shared = result.result("market_context", "market")
    benchmark = shared.payload["benchmark_trend"]
    assert shared.calculation_revision == "shared-market-context-v16-calendar-volatility"
    if not benchmark_count:
        assert benchmark is None
    else:
        assert benchmark == {
            "as_of": ASOF.isoformat(),
            "benchmark_ticker": "1321",
            "observation_date": (ASOF - timedelta(days=1)).isoformat(),
            "benchmark_return_20d": None if benchmark_count == 5 else 0.0,
            "benchmark_return_60d": None if benchmark_count == 5 else 0.0,
            "benchmark_gap_from_high": 0.0 if benchmark_count == 5 else -0.5,
            "trend_state": "unknown" if benchmark_count == 5 else "neutral",
        }
    for ticker in TICKERS:
        assert "benchmark_trend" not in result.result("market_context", ticker).payload
    assert (
        result.result("market_context", "1001").payload["price"]["resolved_date"]
        == (ASOF - timedelta(days=2)).isoformat()
    )
    assert result.result("market_context", "1003").payload["price"] is None
    assert (
        generate(path, tickers=("1003",))
        .result("market_context", "market")
        .payload["benchmark_trend"]
        == benchmark
    )
    assert path.read_bytes() == before


def test_new_and_legacy_saved_shapes_survive_correction_and_detached_l3_read(tmp_path):
    path = source(tmp_path, 300)
    current = generate(path)
    benchmark = current.result("market_context", "market").payload["benchmark_trend"]
    legacy_models = []
    for model in current.models:
        if model.model_id == "market_context":
            payload = dict(model.payload)
            if model.subject == "market":
                payload.pop("benchmark_trend")
            else:
                payload["benchmark_trend"] = benchmark
            model = model.model_copy(
                update={
                    "payload": payload,
                    "calculation_revision": "shared-market-context-bar-timing-v6-tradingview",
                }
            )
        legacy_models.append(model)
    legacy = current.model_copy(update={"generation_id": "legacy", "models": tuple(legacy_models)})
    runs = tmp_path / "runs.sqlite"
    for original in (legacy, current):
        publish_generation(original, path=runs)
        assert read_generation(original.generation_id, path=runs) == original
    saved = [
        read_research_model_inputs(original.generation_id, targets=("1001",), path=runs)
        for original in (legacy, current)
    ]
    encoded = [item.model_dump_json() for item in saved]
    projected = [project_generated_inputs(item) for item in saved]
    assert (
        projected[0]["securities"]["1001"]["models"]["market_context"]["benchmark_trend"]
        == benchmark
    )
    assert (
        "benchmark_trend"
        not in projected[0]["shared_subjects"]["market"]["models"]["market_context"]
    )
    assert (
        projected[1]["shared_subjects"]["market"]["models"]["market_context"]["benchmark_trend"]
        == benchmark
    )
    # The literal subject reader must not silently supplement another subject or latest state.
    direct = read_model_inputs(current.generation_id, subjects=("1001",), path=runs)
    assert direct.result("market_context", "market") is None
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=80 WHERE ticker='1321' AND traded_at=?",
            ((ASOF - timedelta(days=1)).isoformat(),),
        )
    corrected = generate(path, identity="corrected")
    changed = corrected.result("market_context", "market").payload["benchmark_trend"]
    assert changed["benchmark_return_20d"] == pytest.approx(-0.2)
    assert changed["benchmark_gap_from_high"] == pytest.approx(-0.6)
    publish_generation(corrected, path=runs)
    for original, before in zip((legacy, current), encoded, strict=True):
        assert (
            read_research_model_inputs(
                original.generation_id, targets=("1001",), path=runs
            ).model_dump_json()
            == before
        )
    path.unlink()
    runs.unlink()
    assert [project_generated_inputs(item) for item in saved] == projected
    assert [item.model_dump_json() for item in saved] == encoded


def activity_source(tmp_path):
    calendar = {
        ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(100)
    }
    dates = sorted(day for day, business in calendar.items() if business)[-61:]
    rows = [
        (ticker, day.isoformat(), 98.0 if day == dates[-2] else 100.0, 1000.0)
        for ticker in ("1001", "1321", "1306")
        for day in dates
    ]
    rows.append(("1001", (ASOF + timedelta(days=1)).isoformat(), 1e6, 1e6))
    path = market_store(tmp_path / "market.sqlite", daily_bars=rows)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?,?)",
            [(day.isoformat(), "1" if business else "0") for day, business in calendar.items()],
        )
        connection.execute("UPDATE jquants_daily_bars SET turnover_value=100000")
        connection.execute(
            "UPDATE jquants_daily_bars SET turnover_value=1000000 WHERE ticker='1001' AND traded_at=?",
            (ASOF.isoformat(),),
        )
    return path, dates


def test_fixed_prices_feed_activity_and_recovery_and_survive_detached_l3_read(tmp_path):
    path, dates = activity_source(tmp_path)
    before = path.read_bytes()
    result = generate(path, tickers=("1001",))
    market = result.result("market_context", "1001").payload
    conditional = market["relative"]["market_conditioned_60d"]
    assert conditional["window_end_date"] == ASOF.isoformat()
    for ticker in ("1321", "1306"):
        comparison = conditional["benchmarks"][ticker]
        assert comparison["status"] == "available"
        assert comparison["down"]["days"] == 1
        assert comparison["down"]["mean_excess_return"] == 0
        turnover = comparison["turnover"]
        assert turnover["market_direction_status"] == "available"
        assert turnover["down"]["days"] == turnover["down"]["valid_turnover_days"] == 1
        assert turnover["down"]["mean_yen"] == turnover["down"]["median_yen"] == 100000
        assert turnover["up"]["mean_yen"] == turnover["up"]["median_yen"] == 1000000
        joint = comparison["down_own_direction_turnover"]
        assert joint["own_up"]["days"] == 0
        assert joint["own_nonpositive"]["mean_yen"] == 100000
    assert market["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] == 10
    technical = result.result("technical_analysis", "1001").payload
    episodes = technical["price_path"]["observations"]["G60"]["path_statistics"][
        "drawdown_episodes"
    ]
    assert episodes["current"] is None
    assert episodes["latest_recovered"]["peak_date"] == dates[-3].isoformat()
    assert episodes["latest_recovered"]["trough_date"] == dates[-2].isoformat()
    assert episodes["latest_recovered"]["recovery_date"] == ASOF.isoformat()
    assert path.read_bytes() == before
    runs = tmp_path / "runs.sqlite"
    publish_generation(result, path=runs)
    fixed = read_research_model_inputs(result.generation_id, targets=("1001",), path=runs)
    encoded = fixed.model_dump_json()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=100 WHERE ticker='1001'")
    corrected = generate(path, identity="corrected", tickers=("1001",))
    new_path = corrected.result("technical_analysis", "1001").payload["price_path"]
    assert new_path["observations"]["G60"]["path_statistics"]["drawdown_episodes"] == {
        "current": None,
        "latest_recovered": None,
    }
    assert (
        read_research_model_inputs(
            result.generation_id, targets=("1001",), path=runs
        ).model_dump_json()
        == encoded
    )
    path.unlink()
    runs.unlink()
    projected = project_generated_inputs(fixed)["securities"]["1001"]["models"]
    assert projected["market_context"] == market
    assert projected["technical_analysis"] == technical
    assert fixed.model_dump_json() == encoded


def test_saved_v14_does_not_gain_direction_turnover_from_new_code(tmp_path):
    path, _ = activity_source(tmp_path)
    current = generate(path, tickers=("1001",))
    models = []
    for model in current.models:
        if model.model_id == "market_context":
            payload = deepcopy(model.payload)
            if model.subject == "1001":
                for comparison in payload["relative"]["market_conditioned_60d"][
                    "benchmarks"
                ].values():
                    comparison.pop("down_own_direction_turnover")
            model = model.model_copy(
                update={
                    "payload": payload,
                    "calculation_revision": "shared-market-context-v14-independent-turnover",
                }
            )
        models.append(model)
    legacy = current.model_copy(update={"generation_id": "v14", "models": tuple(models)})
    runs = tmp_path / "runs.sqlite"
    publish_generation(legacy, path=runs)
    fixed = read_research_model_inputs("v14", targets=("1001",), path=runs)
    encoded = fixed.model_dump_json()
    assert read_generation("v14", path=runs) == legacy
    path.unlink()
    runs.unlink()
    market = project_generated_inputs(fixed)["securities"]["1001"]["models"]["market_context"]
    assert market == legacy.result("market_context", "1001").payload
    assert all(
        "down_own_direction_turnover" not in item
        for item in market["relative"]["market_conditioned_60d"]["benchmarks"].values()
    )
    assert fixed.model_dump_json() == encoded


@pytest.mark.parametrize("missing_close", ["previous", "current", "all"])
@pytest.mark.parametrize("turnover", [0.0, 100000.0])
def test_generation_keeps_turnover_independent_of_missing_own_closes(
    tmp_path, missing_close, turnover
):
    path, dates = activity_source(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET turnover_value=? WHERE ticker='1001'", (turnover,)
        )
        if missing_close == "all":
            connection.execute("UPDATE jquants_daily_bars SET close=NULL WHERE ticker='1001'")
        else:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=NULL WHERE ticker='1001' AND traded_at=?",
                ((dates[-2] if missing_close == "previous" else dates[-1]).isoformat(),),
            )
    result = generate(path, tickers=("1001",))
    market = result.result("market_context", "1001").payload
    for benchmark in market["relative"]["market_conditioned_60d"]["benchmarks"].values():
        assert benchmark["unavailable_reason"] == "incomplete_price_window"
        for direction in ("up", "down"):
            observed = benchmark["turnover"][direction]
            assert observed["status"] == "available"
            assert observed["valid_turnover_days"] == observed["days"] == 1
            assert observed["mean_yen"] == observed["median_yen"] == turnover
    activity = market["price"]["turnover_activity_20d"]
    assert activity["prior_valid_days"] == 20
    assert activity["current_yen"] == activity["prior_mean_yen"] == turnover
    assert activity["unavailable_reason"] == ("zero_prior_mean" if turnover == 0 else None)
    if missing_close == "all":
        assert set(market["price"]) == {"turnover_activity_20d"}
        assert set(market["relative"]) == {"market_conditioned_60d"}
        assert result.result("technical_analysis", "1001").payload["history_end_date"] is None


@pytest.mark.parametrize("turnover", [None, -1.0, float("inf"), "absent"])
def test_generation_does_not_fill_missing_turnover_or_erase_independent_prices(tmp_path, turnover):
    path, dates = activity_source(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        if turnover == "absent":
            connection.execute(
                "DELETE FROM jquants_daily_bars WHERE ticker='1001' AND traded_at=?",
                (dates[-2].isoformat(),),
            )
        else:
            connection.execute(
                "UPDATE jquants_daily_bars SET turnover_value=? WHERE ticker='1001' AND traded_at=?",
                (turnover, dates[-2].isoformat()),
            )
    result = generate(path, tickers=("1001",))
    market = result.result("market_context", "1001").payload
    for benchmark in market["relative"]["market_conditioned_60d"]["benchmarks"].values():
        assert benchmark["status"] == ("unavailable" if turnover == "absent" else "available")
        down = benchmark["turnover"]["down"]
        assert down["unavailable_reason"] == "missing_or_invalid_turnover"
        assert down["valid_turnover_days"] == 0
        assert down["mean_yen"] is down["median_yen"] is None
        assert benchmark["turnover"]["up"]["mean_yen"] == 1000000
    activity = market["price"]["turnover_activity_20d"]
    assert activity["prior_valid_days"] == 19
    assert activity["unavailable_reason"] == "incomplete_turnover_window"
    assert activity["current_yen"] == 1000000


def test_turnover_uses_the_sealed_source_and_not_future_values_or_live_corrections(tmp_path):
    path, dates = activity_source(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=NULL WHERE ticker='1001' AND traded_at=?",
            (dates[-2].isoformat(),),
        )
        connection.execute(
            "UPDATE jquants_daily_bars SET turnover_value=1e12 WHERE traded_at>?",
            (ASOF.isoformat(),),
        )
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=tmp_path / "lake") as snapshot:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE jquants_daily_bars SET turnover_value=777000")
        result = materialize_generation(
            read_and_generate_models(
                generation_id="sealed",
                market_snapshot=snapshot,
                market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
                economic_input=None,
                as_of=ASOF,
                evaluation_time=AT,
                ttm_rules=DEFAULT_TTM_RULES,
                tickers=("1001",),
            )
        )
    market = result.result("market_context", "1001").payload
    assert (
        market["relative"]["market_conditioned_60d"]["benchmarks"]["1321"]["turnover"]["down"][
            "mean_yen"
        ]
        == 100000
    )
    assert market["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] == 10
    assert market["price"]["turnover_activity_20d"]["current_date"] == ASOF.isoformat()
    corrected = generate(path, identity="corrected", tickers=("1001",))
    assert (
        corrected.result("market_context", "1001").payload["price"]["turnover_activity_20d"][
            "current_to_prior_mean_ratio"
        ]
        == 1
    )


def test_legacy_absent_price_blocks_are_not_backfilled_from_new_turnover_observations(tmp_path):
    path, _ = activity_source(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=NULL WHERE ticker='1001'")
    current = generate(path, tickers=("1001",))
    legacy_models = tuple(
        model.model_copy(
            update={
                "calculation_revision": "shared-market-context-v13-conditional-turnover",
                "payload": {**model.payload, "price": None, "relative": None},
            }
        )
        if model.model_id == "market_context" and model.subject == "1001"
        else model
        for model in current.models
    )
    legacy = current.model_copy(update={"generation_id": "legacy", "models": legacy_models})
    runs = tmp_path / "runs.sqlite"
    publish_generation(legacy, path=runs)
    old_saved = read_research_model_inputs("legacy", targets=("1001",), path=runs)
    old_bytes = old_saved.model_dump_json()
    publish_generation(current, path=runs)
    saved = read_research_model_inputs(current.generation_id, targets=("1001",), path=runs)
    new_bytes = saved.model_dump_json()
    assert read_research_model_inputs("legacy", targets=("1001",), path=runs).model_dump_json() == (
        old_bytes
    )
    path.unlink()
    runs.unlink()
    for fixed, expected_bytes in ((old_saved, old_bytes), (saved, new_bytes)):
        assert fixed.model_dump_json() == expected_bytes
        projected = project_generated_inputs(fixed)["securities"]["1001"]["models"]
        price = projected["market_context"]["price"]
        if fixed is old_saved:
            assert price is None
            assert projected["market_context"]["relative"] is None
        else:
            assert set(price) == {"turnover_activity_20d"}
            assert price["turnover_activity_20d"]["current_to_prior_mean_ratio"] == 10


def test_sma75_fixed_generation_survives_correction_without_legacy_backfill(tmp_path):
    path = source(tmp_path, 300)
    current = generate(path, tickers=("1001",))
    technical = current.result("technical_analysis", "1001")
    revision = "sma20-50-75-200-bb20-wilder-rsi14-price-path-v5-short-recovery"
    assert technical.calculation_revision == technical.payload["calculation_revision"] == revision
    assert technical.payload["moving_averages"]["sma_75bar"] == {
        "status": "available",
        "window_bars": 75,
        "window_start_date": (ASOF - timedelta(days=76)).isoformat(),
        "window_end_date": (ASOF - timedelta(days=2)).isoformat(),
        "value": 100.0,
        "gap_ratio": 0.0,
    }
    legacy_payload = deepcopy(technical.payload)
    del legacy_payload["moving_averages"]["sma_75bar"]
    legacy_revision = "sma20-50-200-bb20-wilder-rsi14-price-path-v4"
    legacy_payload["calculation_revision"] = legacy_revision
    legacy_model = technical.model_copy(
        update={"payload": legacy_payload, "calculation_revision": legacy_revision}
    )
    legacy = current.model_copy(
        update={
            "generation_id": "legacy",
            "models": tuple(
                legacy_model if model is technical else model for model in current.models
            ),
        }
    )
    runs = tmp_path / "runs.sqlite"
    for original in (legacy, current):
        publish_generation(original, path=runs)
    fixed = [
        read_research_model_inputs(original.generation_id, targets=("1001",), path=runs)
        for original in (legacy, current)
    ]
    encoded = [item.model_dump_json() for item in fixed]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=80 WHERE ticker='1001' AND traded_at=?",
            ((ASOF - timedelta(days=2)).isoformat(),),
        )
    corrected = generate(path, identity="corrected", tickers=("1001",))
    assert corrected.result("technical_analysis", "1001").payload["moving_averages"]["sma_75bar"][
        "value"
    ] == pytest.approx((74 * 100 + 80) / 75)
    for original, before in zip((legacy, current), encoded, strict=True):
        assert (
            read_research_model_inputs(
                original.generation_id, targets=("1001",), path=runs
            ).model_dump_json()
            == before
        )
    path.unlink()
    runs.unlink()
    for saved, expected in zip(fixed, (legacy_model, technical), strict=True):
        projected = project_generated_inputs(saved)["securities"]["1001"]
        assert (
            projected["calculation_revisions"]["technical_analysis"]
            == expected.calculation_revision
        )
        assert projected["models"]["technical_analysis"] == expected.payload
    assert "sma_75bar" not in legacy_model.payload["moving_averages"]
    assert [item.model_dump_json() for item in fixed] == encoded


def test_short_recovery_generation_keeps_policy_and_legacy_detached_inputs(tmp_path):
    path, dates = activity_source(tmp_path)
    current = generate(path, tickers=("1001",))
    technical = current.result("technical_analysis", "1001")
    short = technical.payload["price_path"]["observations"]["R20"]["path_statistics"]
    assert short["status"] == "available"
    assert short["drawdown_episodes"]["latest_recovered"]["recovery_date"] == ASOF.isoformat()
    assert short["drawdown_episodes"]["latest_recovered"]["trough_date"] == dates[-2].isoformat()
    legacy_payload = deepcopy(technical.payload)
    del legacy_payload["price_path"]["observations"]["R20"]["path_statistics"]
    legacy_revision = "sma20-50-75-200-bb20-wilder-rsi14-price-path-v4"
    legacy_payload["calculation_revision"] = legacy_revision
    legacy_payload["price_path"]["calculation_revision"] = "common-calendar-observed-close-path-v3"
    legacy_model = technical.model_copy(
        update={"payload": legacy_payload, "calculation_revision": legacy_revision}
    )
    legacy = current.model_copy(
        update={
            "generation_id": "legacy-short-recovery-absent",
            "models": tuple(
                legacy_model if model is technical else model for model in current.models
            ),
        }
    )
    rows = []
    for generation in (legacy, current):
        metadata = GenerationMetadata.model_validate(generation.model_dump(exclude={"models"}))
        models = tuple(model for model in generation.models if model.subject == "1001")
        row = project_technical_financial_row("1001", models=models, metadata=metadata)
        row.technical.pop("model_revisions")
        row.financial_paths.pop("model_revisions")
        rows.append(row)
        coordinates = project_company("1001", models=models, metadata=metadata)
        brief = economic_screening_brief(
            {
                "policy": {},
                "eligible_count": 0,
                "cards": [{"ticker": "1001", "paths": [], "context": coordinates.context}],
            }
        )
        observed = brief["cards"][0]["context"]["price_path"]["observations"]["R20"]
        if generation is current:
            assert observed["path_statistics"] == short
        else:
            assert "path_statistics" not in observed
    assert rows[0] == rows[1]
    runs = tmp_path / "runs.sqlite"
    for generation in (legacy, current):
        publish_generation(generation, path=runs)
    saved = [
        read_research_model_inputs(g.generation_id, targets=("1001",), path=runs)
        for g in (legacy, current)
    ]
    encoded = [item.model_dump_json() for item in saved]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=100 WHERE ticker='1001'")
    corrected = generate(path, identity="corrected-short-recovery", tickers=("1001",))
    assert corrected.result("technical_analysis", "1001").payload["price_path"]["observations"][
        "R20"
    ]["path_statistics"]["drawdown_episodes"] == {"current": None, "latest_recovered": None}
    for generation, before in zip((legacy, current), encoded, strict=True):
        assert (
            read_research_model_inputs(
                generation.generation_id, targets=("1001",), path=runs
            ).model_dump_json()
            == before
        )
    path.unlink()
    runs.unlink()
    for fixed, model in zip(saved, (legacy_model, technical), strict=True):
        projected = project_generated_inputs(fixed)["securities"]["1001"]["models"][
            "technical_analysis"
        ]
        assert projected == model.payload
    assert [item.model_dump_json() for item in saved] == encoded


def test_calendar_volatility_is_fixed_and_v15_is_not_recomputed_on_detached_read(tmp_path):
    path, dates = activity_source(tmp_path)
    complete = generate(path, identity="complete", tickers=("1001",))
    complete_price = complete.result("market_context", "1001").payload["price"]
    assert complete_price["realized_vol_20d"] > 0
    assert complete_price["realized_vol_20d_context"]["window_start_date"] == dates[-21].isoformat()
    assert complete_price["realized_vol_20d_context"]["valid_close_count"] == 21
    # Twenty observed returns still exist, but one common session has no close.
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=NULL WHERE ticker='1001' AND traded_at=?",
            (dates[-2].isoformat(),),
        )
    current = generate(path, identity="current", tickers=("1001",))
    price = current.result("market_context", "1001").payload["price"]
    assert price["realized_vol_20d"] is None
    assert price["realized_vol_20d_context"]["unavailable_reason"] == "incomplete_close_window"
    assert price["realized_vol_20d_context"]["valid_close_count"] == 20
    assert price["return_20d"] == 0.0
    legacy_models = []
    for model in current.models:
        if model.model_id == "market_context":
            payload = deepcopy(model.payload)
            if model.subject == "1001":
                payload["price"].pop("realized_vol_20d_context")
                payload["price"]["realized_vol_20d"] = 0.123
            model = model.model_copy(
                update={
                    "payload": payload,
                    "calculation_revision": "shared-market-context-v15-down-own-direction-turnover",
                }
            )
        legacy_models.append(model)
    legacy = current.model_copy(update={"generation_id": "v15", "models": tuple(legacy_models)})
    runs = tmp_path / "runs.sqlite"
    fixed = []
    for result in (complete, current, legacy):
        publish_generation(result, path=runs)
        assert read_generation(result.generation_id, path=runs) == result
        fixed.append(read_research_model_inputs(result.generation_id, targets=("1001",), path=runs))
    encoded = [item.model_dump_json() for item in fixed]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=100 WHERE ticker='1001'")
    correction = generate(path, identity="corrected", tickers=("1001",))
    assert correction.result("market_context", "1001").payload["price"]["realized_vol_20d"] == 0.0
    publish_generation(correction, path=runs)
    for item, before in zip(fixed, encoded, strict=True):
        assert (
            read_research_model_inputs(
                item.generation_id, targets=("1001",), path=runs
            ).model_dump_json()
            == before
        )
    path.unlink()
    runs.unlink()
    shown = [
        project_generated_inputs(item)["securities"]["1001"]["models"]["market_context"]["price"]
        for item in fixed
    ]
    assert shown[0] == complete_price
    assert shown[1] == price
    assert shown[2]["realized_vol_20d"] == 0.123
    assert "realized_vol_20d_context" not in shown[2]
    assert [item.model_dump_json() for item in fixed] == encoded
