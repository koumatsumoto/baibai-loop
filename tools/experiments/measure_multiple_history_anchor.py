"""Compare the shipped price-history anchor against an anchor built from real multiples.

`_valuation_history` fixes fundamentals at their latest value and moves only the adjusted
close, so the self side of the FV anchor is a price history expressed in multiple units:
for every price-proportional axis `self_median / current` collapses to
`median(750 closes) / current`. The calibration panel, by contrast, holds a point-in-time
multiple per cohort, so laying the cohorts side by side gives a real — if monthly —
multiple history. This measures whether an anchor built that way explains forward returns
better than the one that ships.

The rules are fixed in
``reports/studies/2026-08-17-self-range-vs-multiple-history/preregistration.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median

from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    read_forward,
    read_panel,
)

_HORIZONS = ("3y", "5y")
# 現行の 750 営業日窓に対応する月次 cohort 数と、その 2/3 を最低本数にする。
_HISTORY_COHORTS = 36
_MIN_HISTORY = 24
_UPSIDE_CAP = 0.50
_REALIZATION_RATE = 0.10
_TRAP_EXCESS = -0.20
_MIN_CONTROL_GROUP = 20
_MIN_ROWS_PER_COHORT = 30


@dataclass(frozen=True, slots=True)
class _Row:
    ticker: str
    shipped: float
    history: float
    forward: float
    dividend_yield: float | None


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 + 1
        for index in order[position : end + 1]:
            ranks[index] = shared
        position = end + 1
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3:
        return None
    x, y = _rank(left), _rank(right)
    mean_x, mean_y = sum(x) / len(x), sum(y) / len(y)
    dx = [value - mean_x for value in x]
    dy = [value - mean_y for value in y]
    denominator = (sum(v * v for v in dx) * sum(v * v for v in dy)) ** 0.5
    if denominator == 0:
        return None
    covariance = sum(a * b for a, b in zip(dx, dy, strict=True))
    return float(covariance / denominator)


def _quintile_spread(values: Sequence[float], forwards: Sequence[float]) -> float | None:
    if len(values) < 10:
        return None
    # E[r] は大きいほど良いので降順。先頭が best になる。
    ordered = sorted(zip(values, forwards, strict=True), key=lambda pair: pair[0], reverse=True)
    size = max(len(ordered) // 5, 1)
    best = [forward for _, forward in ordered[:size]]
    worst = [forward for _, forward in ordered[-size:]]
    return sum(best) / len(best) - sum(worst) / len(worst)


def _earnings_multiple(row: PanelRow) -> tuple[float | None, float | None]:
    """production と同じ順序: per_forward を先に、無ければ per_trailing。"""

    if row.per_forward is not None and row.per_forward > 0:
        return float(row.per_forward), (
            None if row.smg_per_forward is None else float(row.smg_per_forward)
        )
    if row.per_trailing is not None and row.per_trailing > 0:
        return float(row.per_trailing), (
            None if row.smg_per_trailing is None else float(row.smg_per_trailing)
        )
    return None, None


def _axis_upside(
    current: float | None, gap: float | None, history: Sequence[float]
) -> float | None:
    """min(sector 中央値, 自己の倍率履歴中央値) を anchor にした implied upside。"""

    if current is None or current <= 0 or len(history) < _MIN_HISTORY:
        return None
    self_anchor = median(history)
    anchors = [self_anchor] if self_anchor > 0 else []
    if gap is not None and gap > -1.0:
        sector_anchor = current / (1.0 + gap)
        if sector_anchor > 0:
            anchors.append(sector_anchor)
    if not anchors:
        return None
    return min(anchors) / current - 1.0


def _history_upside(
    row: PanelRow, earnings_history: Sequence[float], asset_history: Sequence[float]
) -> float | None:
    earnings_current, earnings_gap = _earnings_multiple(row)
    earnings = _axis_upside(earnings_current, earnings_gap, earnings_history)
    asset = _axis_upside(
        None if row.pbr is None else float(row.pbr),
        None if row.smg_pbr is None else float(row.smg_pbr),
        asset_history,
    )
    parts = [value for value in (earnings, asset) if value is not None]
    if not parts:
        return None
    return sum(parts) / len(parts)


def _clip(value: float, bound: float) -> float:
    return max(-bound, min(bound, value))


def _cohort_asofs(root: Path) -> list[date]:
    pointer = root / "lake/pointers/calibration/current.json"
    bundle_id = json.loads(pointer.read_text(encoding="utf-8"))["current"]["bundle_id"]
    manifest = root / f"lake/manifests/calibration-bundles/{bundle_id}.json"
    panel = json.loads(manifest.read_text(encoding="utf-8"))["datasets"]["calibration.panel"]
    inventory = json.loads((root / panel["manifest_key"]).read_text(encoding="utf-8"))[
        "cohort_inventory"
    ]
    return sorted(date.fromisoformat(key) for key in inventory)


def _dividend_stratified_delta(
    rows: Sequence[_Row], best: set[str], cohort_median: float
) -> tuple[float, float] | None:
    usable = [row for row in rows if row.dividend_yield is not None]
    if not usable:
        return None
    split = median(float(row.dividend_yield or 0.0) for row in usable)
    weighted_excess = 0.0
    weighted_trap = 0.0
    matched = 0
    for lower_side in (True, False):
        stratum = [
            row for row in usable if (float(row.dividend_yield or 0.0) <= split) is lower_side
        ]
        inside = [row.forward - cohort_median for row in stratum if row.ticker in best]
        outside = [row.forward - cohort_median for row in stratum if row.ticker not in best]
        if len(inside) < _MIN_CONTROL_GROUP or len(outside) < _MIN_CONTROL_GROUP:
            continue
        weight = min(len(inside), len(outside))
        matched += weight
        weighted_excess += weight * (median(inside) - median(outside))
        weighted_trap += weight * (
            sum(1 for value in inside if value < _TRAP_EXCESS) / len(inside)
            - sum(1 for value in outside if value < _TRAP_EXCESS) / len(outside)
        )
    if not matched:
        return None
    return weighted_excess / matched, weighted_trap / matched


def _best_decile(rows: Sequence[_Row], *, use_history: bool) -> set[str]:
    ordered = sorted(
        rows, key=lambda row: row.history if use_history else row.shipped, reverse=True
    )
    size = max(len(ordered) // 10, 1)
    return {row.ticker for row in ordered[:size]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    root = args.calibration_dir
    asofs = _cohort_asofs(root)
    panels = {asof: read_panel(root, asof) for asof in asofs}
    midpoint = len(asofs) // 2
    windows = {"all": asofs, "design": asofs[:midpoint], "confirm": asofs[midpoint:]}

    report: dict[str, object] = {
        "windows": {
            name: {
                "cohorts": len(values),
                "first": values[0].isoformat() if values else None,
                "last": values[-1].isoformat() if values else None,
            }
            for name, values in windows.items()
        },
        "horizons": {},
    }

    for horizon in _HORIZONS:
        per_cohort: list[dict[str, object]] = []
        rows_total = 0
        rows_short_history = 0
        for index, asof in enumerate(asofs):
            if index < _MIN_HISTORY:
                continue
            window = asofs[max(0, index - _HISTORY_COHORTS) : index]
            earnings: dict[str, list[float]] = {}
            assets: dict[str, list[float]] = {}
            for past in window:
                for row in panels[past]:
                    value, _ = _earnings_multiple(row)
                    if value is not None:
                        earnings.setdefault(row.ticker, []).append(value)
                    if row.pbr is not None and row.pbr > 0:
                        assets.setdefault(row.ticker, []).append(float(row.pbr))
            current = {row.ticker: row for row in panels[asof] if row.in_population}
            observations: list[_Row] = []
            for forward_row in read_forward(root, asof):
                if forward_row.horizon != horizon or not forward_row.resolved:
                    continue
                if forward_row.price_return is None:
                    continue
                panel_row = current.get(forward_row.ticker)
                if panel_row is None or panel_row.er_annual is None:
                    continue
                rows_total += 1
                upside = _history_upside(
                    panel_row,
                    earnings.get(forward_row.ticker, ()),
                    assets.get(forward_row.ticker, ()),
                )
                if upside is None:
                    rows_short_history += 1
                    continue
                carry = (
                    0.0 if panel_row.er_carry_annual is None else float(panel_row.er_carry_annual)
                )
                observations.append(
                    _Row(
                        ticker=forward_row.ticker,
                        shipped=float(panel_row.er_annual),
                        history=_REALIZATION_RATE * _clip(upside, _UPSIDE_CAP) + carry,
                        forward=float(forward_row.price_return),
                        dividend_yield=(
                            None
                            if panel_row.dividend_yield is None
                            else float(panel_row.dividend_yield)
                        ),
                    )
                )
            if len(observations) < _MIN_ROWS_PER_COHORT:
                continue
            forwards = [row.forward for row in observations]
            cohort_median = median(forwards)
            entry: dict[str, object] = {
                "asof": asof.isoformat(),
                "rows": len(observations),
                "ic_shipped": _spearman([row.shipped for row in observations], forwards),
                "ic_history": _spearman([row.history for row in observations], forwards),
                "spread_shipped": _quintile_spread([row.shipped for row in observations], forwards),
                "spread_history": _quintile_spread([row.history for row in observations], forwards),
                "anchor_correlation": _spearman(
                    [row.shipped for row in observations], [row.history for row in observations]
                ),
            }
            for label, use_history in (("shipped", False), ("history", True)):
                control = _dividend_stratified_delta(
                    observations, _best_decile(observations, use_history=use_history), cohort_median
                )
                entry[f"control_excess_{label}"] = None if control is None else round(control[0], 6)
                entry[f"control_trap_{label}"] = None if control is None else round(control[1], 6)
            per_cohort.append(entry)

        summary: dict[str, object] = {
            "rows_with_shipped_er": rows_total,
            "rows_without_enough_history": rows_short_history,
            "short_history_share": (
                round(rows_short_history / rows_total, 4) if rows_total else None
            ),
        }
        keys = (
            "ic_shipped",
            "ic_history",
            "spread_shipped",
            "spread_history",
            "anchor_correlation",
            "control_excess_shipped",
            "control_excess_history",
            "control_trap_shipped",
            "control_trap_history",
        )
        for window_name, window_asofs in windows.items():
            member = {value.isoformat() for value in window_asofs}
            subset = [entry for entry in per_cohort if entry["asof"] in member]
            summary[f"{window_name}_cohorts"] = len(subset)
            for key in keys:
                values = [
                    float(entry[key])  # type: ignore[arg-type]
                    for entry in subset
                    if entry[key] is not None
                ]
                summary[f"{window_name}_median_{key}"] = (
                    round(median(values), 6) if values else None
                )
        summary["cohorts_detail"] = per_cohort
        report["horizons"][horizon] = summary  # type: ignore[index]

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
