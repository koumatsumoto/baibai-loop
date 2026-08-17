"""Compare the two share-count axes on the rows where both are answerable.

The canonical evaluation scores each axis on its own non-null rows. That is the right
default, but it cannot answer this question: `tradable_share_change_yoy` is unanswerable
wherever the treasury count is unobservable, so scoring the two axes on different
populations would let a selection difference read as a signal difference. This restricts
both to the rows where both are defined, and reports the rows only the old axis can see
as a separate group rather than folding them in.

The rules this measurement follows are fixed in
``reports/studies/2026-08-17-tradable-share-change/preregistration.md``.
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

from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    read_forward,
    read_panel,
)

_AXES = ("net_share_change_yoy", "tradable_share_change_yoy")
# 事前登録どおり「縮むほど良い」。両軸で同じ向きなので、順位相関の符号はそのまま比較できる。
_DIRECTION = -1
_HORIZONS = ("3y", "5y")
_MIN_ROWS_PER_COHORT = 30


# 正本の評価と同じ閾値。excess がこれを下回る行を trap とする。
_TRAP_EXCESS = -0.20
_MIN_CONTROL_GROUP = 20


@dataclass(frozen=True, slots=True)
class _Observation:
    ticker: str
    old: float
    new: float | None
    forward: float
    dividend_yield: float | None


def _rank(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not manufacture an ordering the data does not have."""

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
    """Mean forward of the best fifth minus the worst fifth, under the declared direction."""

    if len(values) < 10:
        return None
    # 良さ = direction * value で、大きいほど良い。降順に並べて先頭が best になる。
    # 昇順のまま先頭を取ると best と worst が入れ替わり、符号だけが反転した値が出る。
    ordered = sorted(
        zip(values, forwards, strict=True), key=lambda pair: _DIRECTION * pair[0], reverse=True
    )
    size = max(len(ordered) // 5, 1)
    best = [forward for _, forward in ordered[:size]]
    worst = [forward for _, forward in ordered[-size:]]
    return sum(best) / len(best) - sum(worst) / len(worst)


def _observations(root: Path, asof: date, horizon: str) -> list[_Observation]:
    panel = {row.ticker: row for row in read_panel(root, asof) if row.in_population}
    rows: list[_Observation] = []
    for forward_row in read_forward(root, asof):
        if forward_row.horizon != horizon or not forward_row.resolved:
            continue
        if forward_row.price_return is None:
            continue
        panel_row = panel.get(forward_row.ticker)
        if panel_row is None or panel_row.net_share_change_yoy is None:
            continue
        rows.append(
            _Observation(
                ticker=forward_row.ticker,
                old=float(panel_row.net_share_change_yoy),
                new=(
                    None
                    if panel_row.tradable_share_change_yoy is None
                    else float(panel_row.tradable_share_change_yoy)
                ),
                forward=float(forward_row.price_return),
                dividend_yield=(
                    None if panel_row.dividend_yield is None else float(panel_row.dividend_yield)
                ),
            )
        )
    return rows


def _best_decile(rows: Sequence[_Observation], *, use_new: bool) -> set[str]:
    """事前登録した向き (縮むほど良い) の上位 1 割の ticker。"""

    ordered = sorted(
        rows,
        key=lambda row: _DIRECTION * (float(row.new or 0.0) if use_new else row.old),
        reverse=True,
    )
    size = max(len(ordered) // 10, 1)
    return {row.ticker for row in ordered[:size]}


def _dividend_stratified_delta(
    rows: Sequence[_Observation], best: set[str], cohort_median: float
) -> tuple[float, float] | None:
    """配当利回りの中央で層に割り、層ごとに best と残りの差を取って重みづけ平均する。

    carry のもう一方の成分で層別しないと、「高配当を拾っているだけ」が株数軸の効果に
    見える。`share_count_reduction_streak` が過去にここで negative になっている。
    """

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


def _cohort_asofs(root: Path) -> list[date]:
    pointer = root / "lake/pointers/calibration/current.json"
    bundle_id = json.loads(pointer.read_text(encoding="utf-8"))["current"]["bundle_id"]
    manifest = root / f"lake/manifests/calibration-bundles/{bundle_id}.json"
    panel = json.loads(manifest.read_text(encoding="utf-8"))["datasets"]["calibration.panel"]
    inventory = json.loads((root / panel["manifest_key"]).read_text(encoding="utf-8"))[
        "cohort_inventory"
    ]
    return sorted(date.fromisoformat(key) for key in inventory)


def _control_entries(rows: Sequence[_Observation]) -> dict[str, object]:
    cohort_median = median(row.forward for row in rows)
    entries: dict[str, object] = {}
    for label, use_new in (("old", False), ("new", True)):
        control = _dividend_stratified_delta(
            rows, _best_decile(rows, use_new=use_new), cohort_median
        )
        entries[f"control_excess_{label}"] = None if control is None else round(control[0], 6)
        entries[f"control_trap_{label}"] = None if control is None else round(control[1], 6)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    root = args.calibration_dir
    asofs = _cohort_asofs(root)
    # 正本 (estimate-calibration.md) の規則: cohort を時間で design / confirm に 2 分割し、
    # 両方で同方向・基準充足のときだけ採用する。片側のみは不確定、両側逆は棄却。
    midpoint = len(asofs) // 2
    windows = {"all": asofs, "design": asofs[:midpoint], "confirm": asofs[midpoint:]}
    report: dict[str, object] = {"horizons": {}, "windows": {}}
    for window_name, window_asofs in windows.items():
        report["windows"][window_name] = {  # type: ignore[index]
            "cohorts": len(window_asofs),
            "first": window_asofs[0].isoformat() if window_asofs else None,
            "last": window_asofs[-1].isoformat() if window_asofs else None,
        }
    for horizon in _HORIZONS:
        per_cohort: list[dict[str, object]] = []
        only_old_rows = 0
        both_rows = 0
        for asof in asofs:
            rows = _observations(root, asof, horizon)
            if not rows:
                continue
            both = [row for row in rows if row.new is not None]
            only_old_rows += len(rows) - len(both)
            both_rows += len(both)
            if len(both) < _MIN_ROWS_PER_COHORT:
                continue
            forwards = [row.forward for row in both]
            old = [_DIRECTION * row.old for row in both]
            new = [_DIRECTION * float(row.new or 0.0) for row in both]
            per_cohort.append(
                {
                    "asof": asof.isoformat(),
                    "rows": len(both),
                    "ic_old": _spearman(old, forwards),
                    "ic_new": _spearman(new, forwards),
                    "spread_old": _quintile_spread([row.old for row in both], forwards),
                    "spread_new": _quintile_spread(
                        [float(row.new or 0.0) for row in both], forwards
                    ),
                    "axis_correlation": _spearman(
                        [row.old for row in both], [float(row.new or 0.0) for row in both]
                    ),
                    **_control_entries(both),
                }
            )
        summary: dict[str, object] = {
            "cohorts": len(per_cohort),
            "rows_both_axes": both_rows,
            "rows_old_axis_only": only_old_rows,
            "coverage_share_of_old": (
                round(both_rows / (both_rows + only_old_rows), 4)
                if both_rows + only_old_rows
                else None
            ),
        }
        for key in (
            "ic_old",
            "ic_new",
            "spread_old",
            "spread_new",
            "axis_correlation",
            "control_excess_old",
            "control_excess_new",
            "control_trap_old",
            "control_trap_new",
        ):
            values = [
                float(entry[key])  # type: ignore[arg-type]
                for entry in per_cohort
                if entry[key] is not None
            ]
            summary[f"median_{key}"] = round(median(values), 6) if values else None
            if key.startswith(("ic_", "spread_", "control_")):
                summary[f"positive_share_{key}"] = (
                    round(sum(1 for value in values if value > 0) / len(values), 4)
                    if values
                    else None
                )
        for window_name, window_asofs in windows.items():
            member = {value.isoformat() for value in window_asofs}
            subset = [entry for entry in per_cohort if entry["asof"] in member]
            for key in ("ic_old", "ic_new", "spread_old", "spread_new"):
                values = [
                    float(entry[key])  # type: ignore[arg-type]
                    for entry in subset
                    if entry[key] is not None
                ]
                summary[f"{window_name}_median_{key}"] = (
                    round(median(values), 6) if values else None
                )
            summary[f"{window_name}_cohorts"] = len(subset)
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
