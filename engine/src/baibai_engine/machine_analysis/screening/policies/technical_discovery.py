"""保存済みの株価・市場モデルから探索用の分類を読む。売買の判断ではない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.primitives.stocks import normalize_ticker


@dataclass(frozen=True, slots=True)
class TechnicalDiscovery:
    state: Literal["matched", "not_matched", "unavailable"]
    expected_price_date: str | None
    price_ready: bool


def _section(payload: dict[str, JsonValue], *path: str) -> dict[str, JsonValue]:
    for key in path:
        value = payload.get(key)
        if not isinstance(value, dict):
            return {}
        payload = value
    return payload


def _number(value: JsonValue) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
        else None
    )


def _date(value: JsonValue) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _window(section: dict[str, JsonValue], expected: str, start_field: str, end_field: str) -> bool:
    if section.get(end_field) != expected:
        return False
    start, end = _date(section.get(start_field)), _date(expected)
    return start is not None and end is not None and start <= end


def _value(
    section: dict[str, JsonValue],
    field: str,
    expected: str,
    *,
    start: str = "window_start_date",
    end: str = "window_end_date",
    reason_field: str | None = None,
) -> float | None:
    # 価格経路は理由欄の明示null、それ以外は各モデルのavailableが観測済みの印。
    available = (
        section.get("status") == "available"
        if reason_field is None
        else reason_field in section and section[reason_field] is None
    )
    if not available or not _window(section, expected, start, end):
        return None
    return _number(section.get(field))


def _above(value: float | None, boundary: float = 0) -> bool | None:
    return None if value is None else value > boundary


def _negative(value: float | None) -> bool | None:
    return None if value is None else value < 0


def _all(*conditions: bool | None) -> bool | None:
    return False if False in conditions else None if None in conditions else True


def _market_outperformance(
    relative: dict[str, JsonValue], own_r20: dict[str, JsonValue], expected: str
) -> bool | None:
    comparison = _section(relative, "comparisons", "R20")
    if (
        relative.get("price_basis") != "price_adjusted_close"
        or comparison.get("comparison_start_date") != own_r20.get("window_start_date")
        or comparison.get("comparison_end_date") != own_r20.get("window_end_date")
    ):
        return None
    return _above(
        _value(
            comparison,
            "relative_return",
            expected,
            start="comparison_start_date",
            end="comparison_end_date",
        )
    )


def _turnover_expansion(market: dict[str, JsonValue], expected: str) -> bool | None:
    row = _section(market, "price", "turnover_activity_20d")
    if (
        row.get("basis") != "traded_value_yen_not_share_volume"
        or row.get("window_basis") != "prior_20_common_trading_sessions_excluding_current"
    ):
        return None
    return _above(
        _value(
            row,
            "current_to_prior_mean_ratio",
            expected,
            start="prior_start_date",
            end="current_date",
        ),
        1,
    )


def _market_down_resilience(
    market: dict[str, JsonValue], benchmark: JsonValue, expected: str
) -> bool | None:
    if not isinstance(benchmark, str) or not benchmark.strip():
        return None
    window = _section(market, "relative", "market_conditioned_60d")
    source = _section(window, "benchmarks", benchmark)
    down = _section(source, "down")
    days = _number(down.get("days"))
    if (
        source.get("status") != "available"
        or "unavailable_reason" not in source
        or source["unavailable_reason"] is not None
        or not _window(window, expected, "window_start_date", "window_end_date")
        or window.get("price_basis") != "price_adjusted_close_not_total_return"
        or window.get("window_basis") != "common_trading_calendar"
        or window.get("calendar_differences") != 60
        or days is None
        or not days.is_integer()
        or not 0 < days <= 60
    ):
        return None
    return _all(
        _above(_number(down.get("own_mean_return"))),
        _above(_number(down.get("mean_excess_return"))),
    )


def evaluate_technical_strategy(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> TechnicalDiscovery:
    ticker = normalize_ticker(ticker)
    if any(model.subject != ticker for model in models):
        raise ValueError("technical discovery requires models for one subject")
    by_id = {model.model_id: model.payload for model in models}
    if len(by_id) != len(models):
        raise ValueError("technical discovery requires unique model IDs")

    ta, market = by_id.get("technical_analysis", {}), by_id.get("market_context", {})
    path = _section(ta, "price_path")
    raw_expected = path.get("expected_price_date")
    expected = raw_expected if isinstance(raw_expected, str) else None
    day = _date(expected)
    if (
        expected is None
        or day is None
        or day > metadata.as_of
        or _section(market, "master").get("is_common_stock") is not True
        or path.get("current_row_state") != "priced"
        or path.get("freshness") != "current"
        or (_number(path.get("current_price")) or 0) <= 0
        or path.get("price_basis") != "price_adjusted_close"
        or ta.get("price_basis") != "price_adjusted_close"
        or path.get("latest_observed_price_date") != expected
    ):
        return TechnicalDiscovery("unavailable", expected, False)

    observations = _section(path, "observations")
    own_r20 = _section(observations, "R20")
    r20 = _value(own_r20, "value", expected, reason_field="unavailable_reason")
    r1 = _value(_section(observations, "R1"), "value", expected, reason_field="unavailable_reason")
    g60 = _value(
        _section(observations, "G60"),
        "window_return",
        expected,
        reason_field="window_return_unavailable_reason",
    )
    momentum = _above(
        _value(
            _section(ta, "price_momentum"),
            "value",
            expected,
            start="start_date",
            end="reference_date",
        )
    )
    moving = _section(ta, "moving_averages")
    sma20, sma50, sma200 = (
        _value(_section(moving, f"sma_{bars}bar"), "gap_ratio", expected) for bars in (20, 50, 200)
    )
    above200 = _above(sma200)
    relative = _section(market, "price_path_relative")
    routes = (
        _market_outperformance(relative, own_r20, expected),
        _all(momentum, _above(r20), _above(g60), _above(sma50), above200),
        _all(momentum, above200, _negative(sma20)),
        _all(_negative(r20), _above(r1)),
        _turnover_expansion(market, expected),
        _market_down_resilience(market, relative.get("benchmark_ticker"), expected),
    )
    return TechnicalDiscovery(
        "matched" if True in routes else "unavailable" if None in routes else "not_matched",
        expected,
        True,
    )
