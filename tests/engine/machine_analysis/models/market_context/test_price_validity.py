"""不正価格を旧日へずらさず、依存する端点・窓だけを欠測にする。"""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.market_context.benchmark import (
    benchmark_trend_from_series,
)
from baibai_engine.machine_analysis.models.market_context.history import trailing_return
from baibai_engine.machine_analysis.models.market_context.security import (
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    _Series,
    calculate_market_snapshot,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

DAY = date(2026, 11, 2)
DAYS = tuple(DAY - timedelta(days=20 - index) for index in range(21))


@pytest.mark.parametrize("invalid", [0.0, -100.0, float("nan"), float("inf")])
def test_invalid_endpoint_cannot_become_a_market_return(invalid):
    assert trailing_return([100.0, invalid], 1) is None
    assert trailing_return([invalid, 100.0], 1) is None
    assert trailing_return([100.0, invalid, 110.0], 2) == pytest.approx(0.1)


@pytest.mark.parametrize("invalid", [0.0, -100.0, float("nan"), float("inf")])
def test_individual_market_keeps_invalid_latest_date_and_independent_peer(invalid):
    bars = [PriceBar(day, 100.0 if day != DAY else invalid, 2e8) for day in DAYS]
    # The sector comparison uses the same target dates; it does not seek an older peer.
    from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
    from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster

    master = MasterSnapshot(
        DAY,
        {"1301": SecurityMaster("1301", "synthetic", "Prime", "機械", True)},
    )
    result = calculate_security_market_context(
        ticker="1301",
        bars=bars,
        master=master,
        benchmark_ticker="1321",
        benchmark_prices=dict.fromkeys(DAYS, 100.0),
        peer_prices=({DAYS[0]: 100.0, DAY: 110.0}, {DAYS[0]: 100.0, DAY: invalid}),
        self_range_window_bars=750,
    )
    price = result["price"]
    assert price["resolved_date"] == DAY.isoformat()
    assert price["close"] is None
    assert price["return_20d"] is None
    assert price["high_52w"] is None
    assert price["low_52w"] is None
    assert price["self_range_percentile"] is None
    assert price["realized_vol_20d"] is None
    assert price["avg_turnover_20d_oku"] == 2.0
    relative = result["relative"]
    assert relative["relative_20d"] is None
    assert relative["sector"]["peer_count"] == 1
    assert relative["sector"]["peer_median_return_20d"] == pytest.approx(0.1)
    assert relative["sector"]["relative_return_20d"] is None


def test_invalid_interior_price_does_not_cancel_independent_endpoint_return():
    bars = [PriceBar(day, 0.0 if index == 10 else 100.0, None) for index, day in enumerate(DAYS)]
    result = calculate_security_market_context(
        ticker="1301",
        bars=bars,
        master=None,
        benchmark_ticker="1321",
        benchmark_prices=dict.fromkeys(DAYS, 100.0),
        peer_prices=(),
        self_range_window_bars=750,
    )
    assert result["price"]["close"] == 100.0
    assert result["price"]["return_20d"] == 0.0
    assert result["relative"]["relative_20d"] == 0.0
    assert result["price"]["high_52w"] is None
    assert result["price"]["realized_vol_20d"] is None


@pytest.mark.parametrize("invalid", [0.0, -100.0, float("nan"), float("inf")])
def test_invalid_benchmark_retains_its_target_date_without_a_false_trend(invalid):
    series = [(day, 100.0 if day != DAY else invalid) for day in DAYS]
    trend = benchmark_trend_from_series(series, asof_date=DAY, benchmark_ticker="1321")
    assert trend is not None
    assert trend.observation_date == DAY
    assert trend.benchmark_return_20d is None
    assert trend.benchmark_gap_from_high is None
    assert trend.trend_state == "unknown"
    point = calculate_market_snapshot(
        asof_date=DAY,
        history_weeks=1,
        benchmark_ticker="1321",
        min_breadth_sample=1,
        master=None,
        masters_by_date={},
        series_by_ticker={"1321": _Series(list(DAYS), [price for _, price in series])},
    )["points"][0]
    assert point["observation_date"] == DAY.isoformat()
    assert point["benchmark_return_20d"] is None
    assert point["benchmark_trend"] == "unknown"
