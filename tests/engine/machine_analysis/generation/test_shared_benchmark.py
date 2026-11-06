"""市場共通のbenchmarkは一度保存し、旧入力や個社の端点へ混ぜない。"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store

from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.machine_analysis.generation.inputs import read_and_generate_models
from baibai_engine.machine_analysis.generation.query import (
    read_model_inputs,
    read_research_model_inputs,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
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
        return read_and_generate_models(
            generation_id=identity,
            market_snapshot=snapshot,
            market_release=InputRelease(release_id="fixed", manifest_sha256="a" * 64),
            economic_input=None,
            as_of=ASOF,
            evaluation_time=AT,
            ttm_rules=DEFAULT_TTM_RULES,
            tickers=tickers,
        ).materialize()


@pytest.mark.parametrize("benchmark_count", [0, 5, 300])
def test_common_benchmark_keeps_missingness_dates_and_its_own_lookback(tmp_path, benchmark_count):
    path = source(tmp_path, benchmark_count)
    before = path.read_bytes()
    result = generate(path)
    shared = result.result("market_context", "market")
    benchmark = shared.payload["benchmark_trend"]
    assert shared.calculation_revision == "shared-market-context-bar-timing-v7-tradingview"
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
    store = ModelGenerationStore(runs)
    for original in (legacy, current):
        store.publish(original)
        assert store.read(original.generation_id) == original
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
    store.publish(corrected)
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
