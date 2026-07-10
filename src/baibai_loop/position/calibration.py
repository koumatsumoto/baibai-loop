from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from baibai_loop.foundation.coerce import mapping_or_empty, optional_float
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.market.bars import JQuantsDailyBar

from .benchmark import PortfolioBenchmark, compute_forward_performance
from .trades import TradeRecord, load_open_trades

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class CalibrationTelemetry:
    asof: date
    benchmark_ticker: str
    positions: tuple[dict[str, object], ...]
    aggregate: dict[str, object]
    coverage: dict[str, object]
    warnings: tuple[str, ...]


def build_calibration_telemetry(
    root: Path,
    *,
    asof: date,
    bars: Sequence[JQuantsDailyBar],
    benchmark_ticker: str,
    market_warnings: Sequence[str] = (),
) -> CalibrationTelemetry:
    trades = load_open_trades(root)
    performance = compute_forward_performance(trades, asof, bars, benchmark_ticker)
    position_rows = _position_rows(root, trades, performance)
    aggregate = _aggregate(position_rows, performance)
    warnings = tuple(market_warnings) + performance.warnings
    coverage = _coverage(position_rows, bars_loaded=bool(bars), warnings=warnings)
    return CalibrationTelemetry(
        asof=asof,
        benchmark_ticker=benchmark_ticker,
        positions=tuple(position_rows),
        aggregate=aggregate,
        coverage=coverage,
        warnings=warnings,
    )


def telemetry_to_payload(telemetry: CalibrationTelemetry) -> dict[str, object]:
    return {
        "asof": telemetry.asof.isoformat(),
        "benchmark_proxy": telemetry.benchmark_ticker,
        "note": (
            "valuation_zone and action are mechanical drafts for monthly "
            "review_valuation / estimate_calibration; they are not automatic exit decisions."
        ),
        "positions": list(telemetry.positions),
        "aggregate": telemetry.aggregate,
        "coverage": telemetry.coverage,
        "warnings": list(telemetry.warnings),
    }


def _position_rows(
    root: Path,
    trades: Sequence[TradeRecord],
    performance: PortfolioBenchmark,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for trade, benchmark in zip(trades, performance.positions, strict=True):
        position_front = _position_front(root, trade.position_id)
        thesis_front = _thesis_front(root, position_front)
        estimate_calibration = mapping_or_empty(position_front.get("estimate_calibration"))
        payoff = _payoff(thesis_front)
        fair_value = optional_float(payoff.get("fair_value_yen"))
        current_price = benchmark.eval_price
        fv_gap = (
            _ratio_to_pct(current_price / fair_value - 1) if current_price and fair_value else None
        )
        valuation_zone = _valuation_zone(current_price, fair_value)
        rows.append(
            {
                "ticker": trade.ticker,
                "name": trade.name,
                "entry_date": trade.entry_date.isoformat(),
                "quantity": trade.quantity,
                "entry_price": _round_float(trade.entry_price),
                "entry_expected_upside_pct": _entry_estimate(
                    estimate_calibration, payoff, "entry_expected_upside_pct", "expected_upside_pct"
                ),
                "entry_expected_yield_pct": _entry_estimate(
                    estimate_calibration, payoff, "entry_expected_yield_pct", "expected_yield_pct"
                ),
                "thesis_fair_value_yen": _round_float(fair_value),
                "current_price_yen": _round_float(current_price),
                "current_return_pct": _ratio_to_pct(benchmark.return_ratio),
                "benchmark_return_pct": _ratio_to_pct(benchmark.benchmark_return),
                "relative_return_pct": _ratio_to_pct(benchmark.relative),
                "fv_gap_pct": fv_gap,
                "valuation_zone": valuation_zone,
                "action": _draft_action(valuation_zone),
            }
        )
    return rows


def _position_front(root: Path, position_id: str) -> Mapping[str, Any]:
    for path in sorted((root / "records/04-position").rglob("*.md")):
        front = _read_front_matter(path)
        if front.get("position_id") == position_id:
            return front
    return {}


def _thesis_front(root: Path, position_front: Mapping[str, Any]) -> Mapping[str, Any]:
    thesis_ref = position_front.get("thesis_ref")
    if not isinstance(thesis_ref, str):
        return {}
    return _read_front_matter(root / thesis_ref)


def _read_front_matter(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return {}
    payload = safe_load(match.group(1))
    return payload if isinstance(payload, Mapping) else {}


def _payoff(thesis_front: Mapping[str, Any]) -> Mapping[str, object]:
    thesis_payoff = thesis_front.get("thesis_payoff")
    if isinstance(thesis_payoff, Mapping):
        return thesis_payoff
    payoff = thesis_front.get("payoff")
    return payoff if isinstance(payoff, Mapping) else {}


def _entry_estimate(
    estimate_calibration: Mapping[str, object],
    payoff: Mapping[str, object],
    calibration_key: str,
    payoff_key: str,
) -> float | None:
    value = optional_float(estimate_calibration.get(calibration_key))
    if value is None:
        value = optional_float(payoff.get(payoff_key))
    return _round_float(value)


def _valuation_zone(current_price: float | None, fair_value: float | None) -> str | None:
    if current_price is None or fair_value is None:
        return None
    if current_price <= fair_value * 0.9:
        return "cheap"
    if current_price <= fair_value * 1.1:
        return "fair"
    return "rich"


def _draft_action(valuation_zone: str | None) -> str | None:
    if valuation_zone is None:
        return None
    return "sell" if valuation_zone == "rich" else "hold"


def _aggregate(
    position_rows: Sequence[Mapping[str, object]],
    performance: PortfolioBenchmark,
) -> dict[str, object]:
    zone_counts = _counts(position_rows, "valuation_zone", keys=("cheap", "fair", "rich"))
    action_counts = _counts(position_rows, "action", keys=("hold", "sell"))
    return {
        "position_count": len(position_rows),
        "total_entry_notional_yen": _round_float(performance.total_notional),
        "total_gross_pnl_yen": _round_float(performance.total_gross_pnl)
        if performance.portfolio_return is not None
        else None,
        "current_return_pct": _ratio_to_pct(performance.portfolio_return),
        "benchmark_return_pct": _ratio_to_pct(performance.benchmark_return),
        "relative_return_pct": _ratio_to_pct(performance.relative),
        "valuation_zone_counts": zone_counts,
        "draft_action_counts": action_counts,
    }


def _coverage(
    position_rows: Sequence[Mapping[str, object]],
    *,
    bars_loaded: bool,
    warnings: Sequence[str],
) -> dict[str, object]:
    total = len(position_rows)
    return {
        "positions_total": total,
        "jquants_bars_loaded": bars_loaded,
        "current_price_count": _nonnull_count(position_rows, "current_price_yen"),
        "benchmark_return_count": _nonnull_count(position_rows, "benchmark_return_pct"),
        "relative_return_count": _nonnull_count(position_rows, "relative_return_pct"),
        "thesis_fair_value_count": _nonnull_count(position_rows, "thesis_fair_value_yen"),
        "fv_gap_count": _nonnull_count(position_rows, "fv_gap_pct"),
        "valuation_zone_count": _nonnull_count(position_rows, "valuation_zone"),
        "warnings_count": len(warnings),
    }


def _counts(
    rows: Sequence[Mapping[str, object]],
    key: str,
    *,
    keys: Sequence[str],
) -> dict[str, int]:
    counts = dict.fromkeys(keys, 0)
    counts["null"] = 0
    for row in rows:
        value = row.get(key)
        if isinstance(value, str) and value in counts:
            counts[value] += 1
        else:
            counts["null"] += 1
    return counts


def _nonnull_count(rows: Sequence[Mapping[str, object]], key: str) -> int:
    return sum(1 for row in rows if row.get(key) is not None)


def _ratio_to_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return _round_float(value * 100)


def _round_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)
