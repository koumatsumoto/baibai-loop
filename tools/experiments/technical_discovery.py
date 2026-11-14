"""保存TAの五つの探索入口を比較する読取専用診断。日次政策・採否・売買を変更しない。

符号条件は研究仮説であり、効果を実証したscoreではない。財務を先に絞らず、
全stock planの非合致と未判定も残す。合致後の財務・成長選別とL3調査は別工程。
現在窓の状態であって新規イベント・日次差分の検出ではなく、合致数で順位を作らない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.filesystem import write_text_new

POLICY_ID = "technical-discovery-research-v1"
_MODEL_IDS = ("technical_analysis", "market_context")
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
    start, end = _date(section.get(start_field)), _date(section.get(endpoint_field))
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
    elif expected_date is None or section.get(endpoint_field) != expected_date:
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
        "window": {key: section[key] for key in _WINDOW_FIELDS if key in section},
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
    by_id = {model.model_id: model for model in models if model.model_id in _MODEL_IDS}
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
        "policy_id": POLICY_ID,
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
        "missing_models": [name for name in _MODEL_IDS if name not in by_id],
        "detail_sections": ["technical_analysis.price_path", "market_context.relative"],
    }


def inspect_technical_discovery(*, generation_id: str, runs_path: Path) -> dict[str, JsonValue]:
    """Read every planned stock, including missing model rows; never substitute latest."""
    with stream_generation_subjects(generation_id, subjects=(), path=runs_path) as header:
        if header is None:
            raise ValueError("unknown model generation")
        metadata = header[0]
    if metadata.subject_plan is None:
        raise ValueError("technical discovery requires a saved stock plan")
    subjects = tuple(
        sorted(subject for subject, plan in metadata.subject_plan.items() if plan.scope == "stock")
    )
    rows = []
    with stream_generation_subjects(
        generation_id, subjects=subjects, model_ids=_MODEL_IDS, path=runs_path
    ) as saved:
        if saved is None:
            raise ValueError("unknown model generation")
        metadata, stream = saved
        current = next(stream, None)
        for subject in subjects:
            models = () if current is None or current[0] != subject else current[1]
            rows.append(evaluate_technical_discovery(subject, models=models, metadata=metadata))
            if current is not None and current[0] == subject:
                current = next(stream, None)
    counts: Counter[str] = Counter({"stocks": 0, "matched_any_route": 0})
    for row in rows:
        counts["stocks"] += 1
        counts["matched_any_route"] += bool(row["matched_routes"])
        for name, route in _object(row, "routes").items():
            counts[f"{name}.{_object(route).get('state')}"] += 1
    return {
        "kind": "technical_discovery_research_diagnostic",
        "policy_id": POLICY_ID,
        "generation_id": generation_id,
        "as_of": metadata.as_of.isoformat(),
        "evaluation_time": metadata.evaluation_time.isoformat(),
        "summary": dict(sorted(counts.items())),
        "rows": list(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--runs-path", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="new private JSON file; never overwrite")
    args = parser.parse_args(argv)
    try:
        if args.out is not None and (args.out.exists() or args.out.is_symlink()):
            raise FileExistsError("output already exists")
        result = inspect_technical_discovery(
            generation_id=args.generation_id, runs_path=args.runs_path
        )
        content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.out is None:
            sys.stdout.write(content)
        else:
            write_text_new(args.out, content)
        return 0
    except (EOFError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"error: technical discovery failed ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
