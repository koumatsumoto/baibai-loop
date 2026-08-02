"""Read-only long-horizon diagnostic for shareholder-return components."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import fmean, median

import yaml

from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    CalibrationCacheError,
    read_forward,
    read_panel,
)

TRAP_EXCESS_THRESHOLD = -0.20
DECILES = 10
MIN_CONTROL_GROUP = 5
CONTROL_FIELDS = (
    "dividend_yield",
    "per_trailing",
    "pbr",
    "market_cap_oku",
    "avg_turnover_oku",
    "price_change_60d",
)
COMPONENTS = ("share_count_reduction", "dps_guidance_up")


@dataclass(slots=True)
class _ControlValues:
    median_deltas: list[float]
    trap_deltas: list[float]
    matched_weight: int = 0
    available_cohorts: int = 0


def _group_stats(values: Sequence[float]) -> dict[str, int | float | None]:
    if not values:
        return {"n": 0, "median_excess": None, "mean_excess": None, "trap_rate": None}
    return {
        "n": len(values),
        "median_excess": round(median(values), 6),
        "mean_excess": round(fmean(values), 6),
        "trap_rate": round(sum(value < TRAP_EXCESS_THRESHOLD for value in values) / len(values), 4),
    }


def _difference(left: object, right: object) -> float | None:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return None
    return round(float(left) - float(right), 6)


def _deltas(
    positive: Mapping[str, object], negative: Mapping[str, object]
) -> dict[str, float | None]:
    return {
        "median_excess_delta": _difference(
            positive.get("median_excess"), negative.get("median_excess")
        ),
        "mean_excess_delta": _difference(positive.get("mean_excess"), negative.get("mean_excess")),
        "trap_rate_delta": _difference(positive.get("trap_rate"), negative.get("trap_rate")),
    }


def _component_value(row: PanelRow, component: str) -> bool | None:
    if component == "share_count_reduction":
        streak = row.share_count_reduction_streak
        return None if streak is None else streak >= 1
    if component == "dps_guidance_up":
        return row.dps_guidance_up
    raise ValueError(f"unknown component: {component}")


def _stratified_control(
    positive: Sequence[PanelRow],
    negative: Sequence[PanelRow],
    excess: Mapping[str, float],
    *,
    field_name: str,
) -> dict[str, int | float | None]:
    rows = [row for row in (*positive, *negative) if getattr(row, field_name) is not None]
    if not rows:
        return _empty_control()
    split = median(float(getattr(row, field_name)) for row in rows)
    positive_tickers = {row.ticker for row in positive}
    weighted_median = 0.0
    weighted_trap = 0.0
    matched_weight = 0
    strata_used = 0
    for lower_side in (True, False):
        stratum = [row for row in rows if (float(getattr(row, field_name)) <= split) is lower_side]
        positive_values = [excess[row.ticker] for row in stratum if row.ticker in positive_tickers]
        negative_values = [
            excess[row.ticker] for row in stratum if row.ticker not in positive_tickers
        ]
        if len(positive_values) < MIN_CONTROL_GROUP or len(negative_values) < MIN_CONTROL_GROUP:
            continue
        delta = _deltas(_group_stats(positive_values), _group_stats(negative_values))
        median_delta = delta["median_excess_delta"]
        trap_delta = delta["trap_rate_delta"]
        if median_delta is None or trap_delta is None:
            continue
        weight = min(len(positive_values), len(negative_values))
        strata_used += 1
        matched_weight += weight
        weighted_median += median_delta * weight
        weighted_trap += trap_delta * weight
    if not matched_weight:
        return _empty_control()
    return {
        "strata_used": strata_used,
        "matched_weight": matched_weight,
        "stratified_median_excess_delta": round(weighted_median / matched_weight, 6),
        "stratified_trap_rate_delta": round(weighted_trap / matched_weight, 6),
    }


def _empty_control() -> dict[str, int | float | None]:
    return {
        "strata_used": 0,
        "matched_weight": 0,
        "stratified_median_excess_delta": None,
        "stratified_trap_rate_delta": None,
    }


def evaluate_cohort(
    panel: Sequence[PanelRow],
    forwards: Sequence[ForwardReturnRow],
    *,
    asof: str,
    horizon: str,
) -> dict[str, object]:
    returns = {
        row.ticker: row.price_return
        for row in forwards
        if row.horizon == horizon and row.status == "resolved" and row.price_return is not None
    }
    population = [row for row in panel if row.in_population and row.ticker in returns]
    population_median = median(float(returns[row.ticker]) for row in population)
    excess = {row.ticker: float(returns[row.ticker]) - population_median for row in population}
    per_rows = sorted(
        (row for row in population if row.per_trailing is not None and row.per_trailing > 0),
        key=lambda row: (float(row.per_trailing or 0.0), row.ticker),
    )
    low_valuation = per_rows[: int(2 * len(per_rows) / DECILES)]
    components: dict[str, object] = {}
    for component in COMPONENTS:
        observed = [row for row in low_valuation if _component_value(row, component) is not None]
        positive = [row for row in observed if _component_value(row, component) is True]
        negative = [row for row in observed if _component_value(row, component) is False]
        positive_stats = _group_stats([excess[row.ticker] for row in positive])
        negative_stats = _group_stats([excess[row.ticker] for row in negative])
        components[component] = {
            "eligible_n": len(observed),
            "true": positive_stats,
            "false": negative_stats,
            **_deltas(positive_stats, negative_stats),
            "controls": {
                field_name: _stratified_control(
                    positive,
                    negative,
                    excess,
                    field_name=field_name,
                )
                for field_name in CONTROL_FIELDS
            },
        }
    return {
        "asof": asof,
        "horizon": horizon,
        "population_resolved": len(population),
        "population_median_return": round(population_median, 6),
        "low_valuation_n": len(low_valuation),
        "components": components,
    }


def aggregate_cohorts(cohorts: Sequence[Mapping[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for component in COMPONENTS:
        median_deltas: list[float] = []
        mean_deltas: list[float] = []
        trap_deltas: list[float] = []
        true_n = 0
        false_n = 0
        controls = {
            field_name: _ControlValues(median_deltas=[], trap_deltas=[])
            for field_name in CONTROL_FIELDS
        }
        for cohort in cohorts:
            component_map = _cohort_component(cohort, component)
            if component_map is None:
                continue
            _append_number(component_map.get("median_excess_delta"), median_deltas)
            _append_number(component_map.get("mean_excess_delta"), mean_deltas)
            _append_number(component_map.get("trap_rate_delta"), trap_deltas)
            true_n += _group_n(component_map.get("true"))
            false_n += _group_n(component_map.get("false"))
            component_controls = component_map.get("controls")
            if not isinstance(component_controls, dict):
                continue
            for field_name, accumulator in controls.items():
                control = component_controls.get(field_name)
                if not isinstance(control, dict) or not control.get("matched_weight"):
                    continue
                median_delta = control.get("stratified_median_excess_delta")
                trap_delta = control.get("stratified_trap_rate_delta")
                if not isinstance(median_delta, int | float) or not isinstance(
                    trap_delta, int | float
                ):
                    continue
                accumulator.available_cohorts += 1
                accumulator.matched_weight += int(control["matched_weight"])
                accumulator.median_deltas.append(float(median_delta))
                accumulator.trap_deltas.append(float(trap_delta))
        result[component] = {
            "cohorts": len(median_deltas),
            "true_n": true_n,
            "false_n": false_n,
            "mean_median_excess_delta": _mean(median_deltas),
            "median_delta_positive_share": (
                round(sum(value > 0 for value in median_deltas) / len(median_deltas), 4)
                if median_deltas
                else None
            ),
            "mean_mean_excess_delta": _mean(mean_deltas),
            "mean_trap_rate_delta": _mean(trap_deltas),
            "controls": {
                field_name: {
                    "available_cohorts": values.available_cohorts,
                    "matched_weight": values.matched_weight,
                    "mean_stratified_median_excess_delta": _mean(values.median_deltas),
                    "mean_stratified_trap_rate_delta": _mean(values.trap_deltas),
                }
                for field_name, values in controls.items()
            },
        }
    return result


def _cohort_component(cohort: Mapping[str, object], component: str) -> Mapping[str, object] | None:
    components = cohort.get("components")
    if not isinstance(components, dict):
        return None
    value = components.get(component)
    return value if isinstance(value, dict) else None


def _append_number(value: object, target: list[float]) -> None:
    if isinstance(value, int | float):
        target.append(float(value))


def _group_n(value: object) -> int:
    return int(value.get("n", 0)) if isinstance(value, dict) else 0


def _mean(values: Sequence[float]) -> float | None:
    return round(fmean(values), 6) if values else None


def build_artifact(
    calibration_dir: Path,
    *,
    asofs: Sequence[str],
    horizons: Sequence[str],
) -> dict[str, object]:
    panels: dict[str, list[PanelRow]] = {}
    forwards: dict[str, list[ForwardReturnRow]] = {}
    inputs: dict[str, object] = {}
    for asof in asofs:
        asof_date = date.fromisoformat(asof)
        panels[asof] = read_panel(calibration_dir, asof_date)
        forwards[asof] = read_forward(calibration_dir, asof_date)
        panel_path = calibration_dir / f"panel-{asof}.csv"
        forward_path = calibration_dir / f"forward-{asof}.csv"
        inputs[asof] = {
            "panel_sha256": _sha256(panel_path),
            "forward_sha256": _sha256(forward_path),
        }
    results: dict[str, object] = {}
    for horizon in horizons:
        cohorts = [
            evaluate_cohort(
                panels[asof],
                forwards[asof],
                asof=asof,
                horizon=horizon,
            )
            for asof in asofs
        ]
        results[horizon] = {"cohorts": cohorts, "aggregate": aggregate_cohorts(cohorts)}
    return {
        "kind": "share-return-component-diagnostic",
        "metric_basis": "price_return_only",
        "trap_excess_threshold": TRAP_EXCESS_THRESHOLD,
        "asofs": list(asofs),
        "horizons": list(horizons),
        "inputs": inputs,
        "results": results,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--asof", action="append", required=True, dest="asofs")
    parser.add_argument("--horizon", action="append", required=True, dest="horizons")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifact = build_artifact(
            args.calibration_dir,
            asofs=tuple(dict.fromkeys(args.asofs)),
            horizons=tuple(dict.fromkeys(args.horizons)),
        )
    except (CalibrationCacheError, OSError, ValueError) as exc:
        print(f"component diagnostic: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(
        yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    print(f"component diagnostic: wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
