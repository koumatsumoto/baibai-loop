from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.models.market_context.benchmark import (
    BenchmarkTrendState,
    benchmark_trend_from_series,
    classify_benchmark_trend,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

ASOF = date(2026, 5, 29)


def _trend(closes):
    return benchmark_trend_from_series(
        [
            (ASOF - timedelta(days=len(closes) - index - 1), close)
            for index, close in enumerate(closes)
        ],
        asof_date=ASOF,
        benchmark_ticker="1321",
    )


@pytest.mark.parametrize(
    ("change", "state"),
    [
        (0.03, BenchmarkTrendState.UPTREND),
        (-0.03, BenchmarkTrendState.DOWNTREND),
        (0.029, BenchmarkTrendState.NEUTRAL),
        (-0.029, BenchmarkTrendState.NEUTRAL),
        (None, BenchmarkTrendState.UNKNOWN),
    ],
)
def test_trend_classification_boundaries(change, state):
    assert classify_benchmark_trend(change) is state


def test_rising_benchmark_has_current_observation_and_zero_gap():
    snapshot = _trend([100.0 + 0.25 * index for index in range(25)])
    assert snapshot.as_of == snapshot.observation_date == ASOF
    assert snapshot.benchmark_return_20d > 0.03
    assert snapshot.trend_state is BenchmarkTrendState.UPTREND
    assert snapshot.benchmark_gap_from_high == 0


def test_gap_is_negative_after_pullback():
    snapshot = _trend([*[100.0 + 0.5 * index for index in range(20)], 109, 108, 106, 105, 104])
    assert snapshot.benchmark_gap_from_high == pytest.approx(104 / 109.5 - 1)


def test_high_keeps_older_peak_but_caps_at_252_observed_closes():
    snapshot = _trend(
        [*[100.0 + 2 * index for index in range(50)], *[200 - 0.25 * i for i in range(210)]]
    )
    assert snapshot.benchmark_gap_from_high == pytest.approx(147.75 / 200 - 1)
    # A still older peak cannot turn the observed-close window into all history.
    assert _trend([1000.0] + [100.0] * 252).benchmark_gap_from_high == 0


def test_empty_and_short_history_keep_distinct_availability():
    assert _trend([]) is None
    snapshot = _trend([100.0 + index for index in range(5)])
    assert snapshot.benchmark_return_20d is None
    assert snapshot.trend_state is BenchmarkTrendState.UNKNOWN
    assert snapshot.observation_date == ASOF
    assert snapshot.benchmark_gap_from_high == 0


def test_prepared_benchmark_keeps_400_calendar_day_input_window_and_asof(tmp_path):
    bars = [
        PriceBar(ASOF - timedelta(days=401), 1000.0, None),
        PriceBar(ASOF - timedelta(days=400), 200.0, None),
        PriceBar(ASOF - timedelta(days=10), 100.0, None),
        PriceBar(ASOF, 150.0, None),
        PriceBar(ASOF + timedelta(days=1), 1.0, None),
    ]
    prepared = prepare_market_context_from_bars(
        sqlite_path=tmp_path / "unused.sqlite",
        asof_date=ASOF,
        master=None,
        bars_by_ticker={"1321": bars},
        subjects=(),
    )
    snapshot = prepared.benchmark_trend
    assert snapshot.observation_date == ASOF
    assert snapshot.benchmark_gap_from_high == pytest.approx(150 / 200 - 1)
    assert snapshot.benchmark_return_20d is None


def test_shared_benchmark_keeps_split_adjustment_without_event_day_close(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1321", [100.0] * 20 + [50.0] * 5, end_date=ASOF)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=NULL,adjustment_factor=0.5 "
            "WHERE ticker='1321' AND traded_at=?",
            ((ASOF - timedelta(days=4)).isoformat(),),
        )
    prepared = prepare_market_context_from_bars(
        sqlite_path=path,
        asof_date=ASOF,
        master=None,
        bars_by_ticker=read_price_bars(path, start=date.min, end=ASOF),
        subjects=(),
    )
    snapshot = prepared.benchmark_trend
    assert snapshot.benchmark_return_20d == pytest.approx(0)
    assert snapshot.trend_state is BenchmarkTrendState.NEUTRAL
