"""保存TAから探索理由を返す。研究互換と現行戦略を区別し、投資採否は行わない。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from math import isfinite
from typing import Literal

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.primitives.stocks import normalize_ticker

RESEARCH_POLICY_ID = "technical-discovery-research-v1"
STRATEGY_POLICY_ID = "technical-discovery-v1"
MODEL_IDS = ("technical_analysis", "market_context")
_WINDOW_FIELDS = (
    "window_start_date",
    "window_end_date",
    "window_bars",
    "calendar_differences",
    "comparison_start_date",
    "comparison_end_date",
    "start_date",
    "end_date",
    "reference_date",
    "current_date",
    "prior_start_date",
    "prior_end_date",
    "prior_valid_days",
    "window_basis",
)


def _lookup(payload: JsonValue, path: tuple[str, ...]) -> tuple[str, JsonValue]:
    for key in path:
        if payload is None:
            return "recorded_null", None
        if not isinstance(payload, dict):
            return "invalid_structure", None
        if key not in payload:
            return "not_recorded", None
        payload = payload[key]
    return ("recorded_null" if payload is None else "recorded"), payload


def _object(payload: JsonValue, *path: str) -> dict[str, JsonValue]:
    _, value = _lookup(payload, path)
    return value if isinstance(value, dict) else {}


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


def _observation(
    model: GeneratedModel | None,
    path: tuple[str, ...],
    field: str,
    *,
    expected_date: str | None,
    endpoint_field: str,
    status_required: bool = True,
    unavailable_field: str | None = None,
    window_path: tuple[str, ...] | None = None,
) -> dict[str, JsonValue]:
    section_state, raw = (
        ("model_not_generated", None) if model is None else _lookup(model.payload, path)
    )
    section = raw if isinstance(raw, dict) else {}
    if section_state == "recorded" and not isinstance(raw, dict):
        section_state = "invalid_structure"
    state, raw_value = (
        _lookup(section, (field,)) if section_state == "recorded" else (section_state, None)
    )
    value = _number(raw_value)
    start_field = {
        "window_end_date": "window_start_date",
        "reference_date": "start_date",
        "comparison_end_date": "comparison_start_date",
        "current_date": "prior_start_date",
    }[endpoint_field]
    window = (
        section
        if window_path is None
        else _object({} if model is None else model.payload, *window_path)
    )
    start, end = _date(window.get(start_field)), _date(window.get(endpoint_field))
    reason = None
    if state != "recorded":
        reason = state
    elif value is None:
        reason = "nonfinite_or_non_numeric"
    elif status_required and section.get("status") != "available":
        reason = "source_not_available"
    elif unavailable_field is not None and unavailable_field not in section:
        reason = "source_state_not_recorded"
    elif unavailable_field is not None and section[unavailable_field] is not None:
        reason = "source_not_available"
    elif expected_date is None or window.get(endpoint_field) != expected_date:
        reason = "price_date_mismatch_or_unknown"
    elif start is None or end is None or start > end:
        reason = "window_missing_or_invalid"
    return {
        "source_field": ".".join((*path, field)),
        "source_state": state,
        "value": value,
        "unavailable_reason": reason,
        "source_status": section.get("status") if isinstance(section.get("status"), str) else None,
        "source_unavailable_reason": section.get(unavailable_field or "unavailable_reason")
        if isinstance(section.get(unavailable_field or "unavailable_reason"), str)
        else None,
        "window": {key: window[key] for key in _WINDOW_FIELDS if key in window},
    }


def _condition(
    observation: dict[str, JsonValue], *, below: bool = False, boundary: float = 0
) -> bool | None:
    value = _number(observation["value"])
    if observation["unavailable_reason"] is not None or value is None:
        return None
    return value < boundary if below else value > boundary


def _route(conditions: dict[str, bool | None], *, price_ready: bool) -> dict[str, JsonValue]:
    failed: list[JsonValue] = [name for name, value in conditions.items() if value is False]
    missing: list[JsonValue] = [name for name, value in conditions.items() if value is None]
    # A stale/no-trade row must not become a match, even if its retained indicators look strong.
    state: Literal["matched", "not_matched", "unavailable"] = (
        "unavailable"
        if not price_ready
        else "not_matched"
        if failed
        else "unavailable"
        if missing
        else "matched"
    )
    return {
        "state": state,
        "conditions": dict(conditions),
        "failed_conditions": failed,
        "missing_conditions": missing,
    }


def evaluate_technical_discovery(
    ticker: str,
    *,
    models: tuple[GeneratedModel, ...],
    metadata: GenerationMetadata,
) -> dict[str, JsonValue]:
    """Same-generation inputs only; neither financial eligibility nor a candidate quota is used."""
    ticker = normalize_ticker(ticker)
    if any(model.subject != ticker for model in models):
        raise ValueError("technical discovery requires models for one subject")
    if len({model.model_id for model in models}) != len(models):
        raise ValueError("technical discovery requires unique model IDs")
    by_id = {model.model_id: model for model in models if model.model_id in MODEL_IDS}
    ta, market = by_id.get("technical_analysis"), by_id.get("market_context")
    ta_payload, market_payload = (
        ({} if ta is None else ta.payload),
        ({} if market is None else market.payload),
    )
    price_path = _object(ta_payload, "price_path")
    master = _object(market_payload, "master")
    raw_expected = price_path.get("expected_price_date")
    expected = raw_expected if isinstance(raw_expected, str) else None
    expected_date = _date(expected)
    calendar_valid = expected_date is not None and expected_date <= metadata.as_of
    price_conditions = {
        "common_stock": master.get("is_common_stock") is True,
        "expected_date_known": calendar_valid,
        "expected_date_priced": price_path.get("current_row_state") == "priced",
        "current_freshness": price_path.get("freshness") == "current",
        "current_price_positive": (_number(price_path.get("current_price")) or 0) > 0,
        "adjusted_close_basis": price_path.get("price_basis") == "price_adjusted_close"
        and ta_payload.get("price_basis") == "price_adjusted_close",
        "latest_date_matches_expected": expected is not None
        and price_path.get("latest_observed_price_date") == expected,
    }
    price_ready = all(price_conditions.values())
    observations = {}
    for name in ("R1", "R20"):
        observations[name] = _observation(
            ta,
            ("price_path", "observations", name),
            "value",
            expected_date=expected,
            endpoint_field="window_end_date",
            status_required=False,
            unavailable_field="unavailable_reason",
        )
    observations["window_return_60d"] = _observation(
        ta,
        ("price_path", "observations", "G60"),
        "window_return",
        expected_date=expected,
        endpoint_field="window_end_date",
        status_required=False,
        unavailable_field="window_return_unavailable_reason",
    )
    observations["momentum_12_1"] = _observation(
        ta,
        ("price_momentum",),
        "value",
        expected_date=expected,
        endpoint_field="reference_date",
    )
    for window in (20, 50, 200):
        observations[f"sma_{window}bar_gap"] = _observation(
            ta,
            ("moving_averages", f"sma_{window}bar"),
            "gap_ratio",
            expected_date=expected,
            endpoint_field="window_end_date",
        )
    relative_path = ("price_path_relative", "comparisons", "R20")
    observations["market_relative_20d"] = _observation(
        market,
        relative_path,
        "relative_return",
        expected_date=expected,
        endpoint_field="comparison_end_date",
    )
    r20 = _object(ta_payload, "price_path", "observations", "R20")
    relative = _object(market_payload, *relative_path)
    if observations["market_relative_20d"]["unavailable_reason"] is None and (
        not isinstance(r20.get("window_start_date"), str)
        or relative.get("comparison_start_date") != r20.get("window_start_date")
        or relative.get("comparison_end_date") != r20.get("window_end_date")
    ):
        observations["market_relative_20d"]["unavailable_reason"] = "comparison_window_mismatch"
    if observations["market_relative_20d"]["unavailable_reason"] is None and (
        _object(market_payload, "price_path_relative").get("price_basis") != "price_adjusted_close"
    ):
        observations["market_relative_20d"]["unavailable_reason"] = (
            "price_basis_mismatch_or_unknown"
        )
    observations["turnover_activity_20d"] = _observation(
        market,
        ("price", "turnover_activity_20d"),
        "current_to_prior_mean_ratio",
        expected_date=expected,
        endpoint_field="current_date",
    )
    activity = _object(market_payload, "price", "turnover_activity_20d")
    if observations["turnover_activity_20d"]["unavailable_reason"] is None and (
        activity.get("basis") != "traded_value_yen_not_share_volume"
        or activity.get("window_basis") != "prior_20_common_trading_sessions_excluding_current"
    ):
        observations["turnover_activity_20d"]["unavailable_reason"] = (
            "turnover_basis_mismatch_or_unknown"
        )
    conditions = {
        "market_outperformance": {
            "market_relative_20d_positive": _condition(observations["market_relative_20d"])
        },
        "trend_continuation": {
            name + "_positive": _condition(observations[name])
            for name in (
                "momentum_12_1",
                "R20",
                "window_return_60d",
                "sma_50bar_gap",
                "sma_200bar_gap",
            )
        },
        "trend_pullback": {
            "momentum_12_1_positive": _condition(observations["momentum_12_1"]),
            "sma_200bar_gap_positive": _condition(observations["sma_200bar_gap"]),
            "sma_20bar_gap_negative": _condition(observations["sma_20bar_gap"], below=True),
        },
        "rebound_start": {
            "R20_negative": _condition(observations["R20"], below=True),
            "R1_positive": _condition(observations["R1"]),
        },
        "turnover_expansion": {
            "above_own_prior_mean": _condition(observations["turnover_activity_20d"], boundary=1)
        },
    }
    routes = {name: _route(checks, price_ready=price_ready) for name, checks in conditions.items()}
    return {
        "kind": "technical_discovery_research_diagnostic",
        "policy_id": RESEARCH_POLICY_ID,
        "generation_id": metadata.generation_id,
        "as_of": metadata.as_of.isoformat(),
        "evaluation_time": metadata.evaluation_time.isoformat(),
        "ticker": ticker,
        "expected_price_date": expected,
        "price_context": {
            "ready": price_ready,
            "conditions": dict(price_conditions.items()),
            "current_row_state": price_path.get("current_row_state"),
            "latest_observed_price_date": price_path.get("latest_observed_price_date"),
            "freshness": price_path.get("freshness"),
            "price_basis": price_path.get("price_basis"),
        },
        "market_relative_benchmark": _object(market_payload, "price_path_relative").get(
            "benchmark_ticker"
        ),
        "matched_routes": [name for name, route in routes.items() if route["state"] == "matched"],
        "routes": dict(routes.items()),
        "observations": dict(observations.items()),
        "model_revisions": {name: model.calculation_revision for name, model in by_id.items()},
        "missing_models": [name for name in MODEL_IDS if name not in by_id],
        "detail_sections": ["technical_analysis.price_path", "market_context.relative"],
    }


def _market_down_observation(
    market: GeneratedModel | None,
    *,
    benchmark: JsonValue,
    field: str,
    expected_date: str | None,
) -> dict[str, JsonValue]:
    root = ("relative", "market_conditioned_60d")
    known_benchmark = isinstance(benchmark, str) and bool(benchmark.strip())
    benchmark_key = benchmark if isinstance(benchmark, str) else ""
    observation = _observation(
        market,
        (*root, "benchmarks", benchmark_key, "down"),
        field,
        expected_date=expected_date,
        endpoint_field="window_end_date",
        status_required=False,
        window_path=root,
    )
    payload = {} if market is None else market.payload
    window = _object(payload, *root)
    section = _object(window, "benchmarks", benchmark_key)
    days = _number(_object(section, "down").get("days"))
    reason = observation["unavailable_reason"]
    if not known_benchmark:
        reason = "market_relative_benchmark_unavailable"
        observation["source_field"] = ".".join((*root, "benchmarks"))
    elif reason is None:
        if "unavailable_reason" not in section:
            reason = "source_state_not_recorded"
        elif section.get("status") != "available" or section.get("unavailable_reason") is not None:
            reason = "source_not_available"
        elif window.get("price_basis") != "price_adjusted_close_not_total_return":
            reason = "price_basis_mismatch_or_unknown"
        elif (
            window.get("window_basis") != "common_trading_calendar"
            or window.get("calendar_differences") != 60
        ):
            reason = "condition_window_mismatch_or_unknown"
        elif days is None or days != int(days) or not 0 < days <= 60:
            reason = "no_valid_condition_days"
    observation["unavailable_reason"] = reason
    observation["source_status"] = section.get("status")
    observation["source_unavailable_reason"] = section.get("unavailable_reason")
    observation["benchmark_ticker"] = benchmark if known_benchmark else None
    observation["condition_days"] = days
    return observation


def _saved_section(model: GeneratedModel | None, *path: str) -> dict[str, JsonValue]:
    state, value = ("model_not_generated", None) if model is None else _lookup(model.payload, path)
    return {
        "source_field": ".".join(path),
        "source_state": state,
        "value": deepcopy(value),
    }


def evaluate_technical_strategy(
    ticker: str,
    *,
    models: tuple[GeneratedModel, ...],
    metadata: GenerationMetadata,
) -> dict[str, JsonValue]:
    """広域の探索理由を返す。MAクロス日・買付採否・主体別資金流入は推定しない。"""
    row = evaluate_technical_discovery(ticker, models=models, metadata=metadata)
    by_id = {model.model_id: model for model in models if model.model_id in MODEL_IDS}
    ta, market = by_id.get("technical_analysis"), by_id.get("market_context")
    expected = row["expected_price_date"]
    assert isinstance(expected, str) or expected is None
    observations = _object(row, "observations")
    observations["sma_75bar_gap"] = _observation(
        ta,
        ("moving_averages", "sma_75bar"),
        "gap_ratio",
        expected_date=expected,
        endpoint_field="window_end_date",
    )
    for name, field in (
        ("market_down_own_return_60d", "own_mean_return"),
        ("market_down_excess_return_60d", "mean_excess_return"),
    ):
        observations[name] = _market_down_observation(
            market,
            benchmark=row["market_relative_benchmark"],
            field=field,
            expected_date=expected,
        )
    price_ready = _object(row, "price_context").get("ready") is True
    routes = _object(row, "routes")
    routes["market_down_resilience"] = _route(
        {
            name + "_positive": _condition(_object(observations, name))
            for name in ("market_down_own_return_60d", "market_down_excess_return_60d")
        },
        price_ready=price_ready,
    )
    routes["price_strength_with_activity"] = _route(
        {
            **{
                name + "_positive": _condition(_object(observations, name))
                for name in ("R1", "sma_75bar_gap", "sma_200bar_gap")
            },
            "turnover_above_own_prior_mean": _condition(
                _object(observations, "turnover_activity_20d"), boundary=1
            ),
        },
        price_ready=price_ready,
    )
    matched: list[JsonValue] = [
        name for name, route in routes.items() if _object(route).get("state") == "matched"
    ]
    state = (
        "matched"
        if matched
        else "unavailable"
        if any(_object(route).get("state") == "unavailable" for route in routes.values())
        else "not_matched"
    )
    return {
        **row,
        "kind": "technical_discovery_strategy",
        "policy_id": STRATEGY_POLICY_ID,
        "state": state,
        "matched_routes": matched,
        "routes": routes,
        "observations": observations,
        # These are complementary questions, not extra votes or conditions inferred from averages.
        "supporting_context": {
            "market_conditioned_60d": _saved_section(market, "relative", "market_conditioned_60d"),
            "price_path_60d": _saved_section(ta, "price_path", "observations", "G60"),
        },
    }
