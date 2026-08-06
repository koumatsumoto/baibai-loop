"""Place today's candidate supply inside its own history.

「割安が今どれくらい供給されているか」を、機械が自分の過去と比べて答える。cycle が
購入ゼロで終わったときに「市況で候補が無いのか、pipeline が拾えていないのか」を
分けるための座標であり、売買 signal でも regime 判定でもない。

座標は 2 つあり、**単一の supply 状態へ畳まない** — doctrine 柱 5(b)。

- **selection 上位 5 の平均 E[r]** — 当日 run と同じ基準で比較できる。
- **screen 通過かつ流動性通過かつ E[r] が hurdle 以上の件数** — 月次 panel でしか
  算出できないので、最新月末 panel の値を遅行座標として併記する。

2 つが逆を向くことは普通に起きる。畳んで 1 語にすると、その食い違い自体が消える。
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from statistics import fmean
from typing import TextIO

import yaml

from tools.measure_signal_cohorts import (
    SignalCohortMeasurementError,
    require_single_rules_hash,
)

DEFAULT_CALIBRATION_DIR = Path("data/screening/calibration")
DEFAULT_RUNS_DB = Path("data/screening/runs.sqlite")
# selection.liquidity と同じ関門。method/screening-rules の値と揃える。
MIN_MARKET_CAP_OKU = 100.0
MIN_AVG_TURNOVER_OKU = 1.0
MIN_LISTING_SPAN_DAYS = 182.0
TOP_N = 5
DEFAULT_HURDLE = 0.085


class SupplyContextError(ValueError):
    """Raised when the stores cannot support a truthful comparison."""


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _percentile_rank(history: Sequence[float], value: float) -> float:
    """history のうち value を下回る要素が占める割合。両端は 0 / 100 になる。

    history は「比較対象の過去」であり、value 自身を含めない。2 座標で母数の作り方が
    違うと同じ percentile という語が別物を指すので、どちらも同じ定義で計算する。
    """

    below = sum(1 for item in history if item < value)
    return round(below / len(history) * 100, 1)


def _history_median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _panel_history(
    calibration_dir: Path, *, hurdle: float
) -> tuple[list[tuple[str, float]], list[tuple[str, int]]]:
    top5: list[tuple[str, float]] = []
    counts: list[tuple[str, int]] = []
    paths = sorted(calibration_dir.glob("panel-*.csv"))
    if not paths:
        raise SupplyContextError(f"no panel rows under {calibration_dir}")
    for path in paths:
        asof = path.name.removeprefix("panel-").removesuffix(".csv")
        ranked: list[tuple[int, float]] = []
        clearing = 0
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                estimate = _optional_float(row.get("er_annual"))
                if estimate is None:
                    continue
                rank = _optional_float(row.get("selection_rank"))
                if rank is not None:
                    ranked.append((int(rank), estimate))
                if (
                    row.get("pass_screen") in {"True", "true", "1"}
                    and estimate >= hurdle
                    and (_optional_float(row.get("market_cap_oku")) or 0.0) >= MIN_MARKET_CAP_OKU
                    and (_optional_float(row.get("avg_turnover_oku")) or 0.0)
                    >= MIN_AVG_TURNOVER_OKU
                    and (_optional_float(row.get("listing_span_days")) or 0.0)
                    >= MIN_LISTING_SPAN_DAYS
                ):
                    clearing += 1
        ranked.sort()
        if len(ranked) >= TOP_N:
            top5.append((asof, fmean(estimate for _, estimate in ranked[:TOP_N])))
        counts.append((asof, clearing))
    if not top5:
        raise SupplyContextError("no panel carries a selection ranking")
    return top5, counts


def _current_top5(runs_db: Path, *, selection_id: str | None) -> tuple[str, str, float]:
    if not runs_db.exists():
        raise SupplyContextError(f"run store is unavailable: {runs_db}")
    connection = sqlite3.connect(f"file:{runs_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if selection_id is None:
            row = connection.execute(
                "SELECT selection_id, run_revision_id FROM screening_selection "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT selection_id, run_revision_id FROM screening_selection "
                "WHERE selection_id = ?",
                (selection_id,),
            ).fetchone()
        if row is None:
            raise SupplyContextError("run store carries no selection")
        asof = connection.execute(
            "SELECT asof_date FROM screening_run WHERE run_revision_id = ?",
            (row["run_revision_id"],),
        ).fetchone()
        estimates: list[float] = []
        for entry in connection.execute(
            "SELECT ticker FROM selection_entry WHERE selection_id = ? ORDER BY ordinal LIMIT ?",
            (row["selection_id"], TOP_N),
        ):
            candidate = connection.execute(
                "SELECT payload FROM screening_candidate WHERE run_revision_id = ? AND ticker = ?",
                (row["run_revision_id"], entry["ticker"]),
            ).fetchone()
            if candidate is None:
                continue
            metrics = json.loads(str(candidate["payload"])).get("metrics") or {}
            estimate = metrics.get("er_annual")
            if isinstance(estimate, int | float):
                estimates.append(float(estimate))
    finally:
        connection.close()
    if len(estimates) < TOP_N:
        raise SupplyContextError(
            f"selection carries only {len(estimates)} ranked candidates with an estimate"
        )
    return str(row["selection_id"]), str(asof["asof_date"] if asof else ""), fmean(estimates)


def build_supply_context(
    *,
    calibration_dir: Path,
    runs_db: Path,
    selection_id: str | None,
    hurdle: float,
) -> dict[str, object]:
    top5_history, count_history = _panel_history(calibration_dir, hurdle=hurdle)
    resolved_selection_id, run_asof, current_top5 = _current_top5(
        runs_db, selection_id=selection_id
    )
    rules_hash = require_single_rules_hash(calibration_dir)
    latest_count_asof, latest_count = count_history[-1]
    top5_values = [value for _, value in top5_history]
    # 件数座標は最新月末 panel 自身の値なので、順位付けの母数からは外す。top-5 は当日 run の
    # 値で panel に含まれないため、そちらは元から外れている。両者で母数の作り方を揃える。
    count_values = [float(value) for _, value in count_history[:-1]]
    if not count_values:
        raise SupplyContextError("a single panel cannot place its own count in history")
    return {
        "kind": "supply-context",
        "rules_hash": rules_hash,
        "hurdle_annual_ratio": hurdle,
        "history_panels": len(top5_history),
        "history_asof_start": top5_history[0][0],
        "history_asof_end": top5_history[-1][0],
        "selection_top5_mean_er": {
            "basis": "current_selection_top5",
            "selection_id": resolved_selection_id,
            "run_asof": run_asof,
            "value": round(current_top5, 4),
            "history_median": round(_history_median(top5_values), 4),
            "percentile_rank": _percentile_rank(top5_values, current_top5),
        },
        "hurdle_clearing_count": {
            "basis": "latest_month_end_panel",
            "panel_asof": latest_count_asof,
            "value": latest_count,
            "history_median": _history_median(count_values),
            "percentile_rank": _percentile_rank(count_values, float(latest_count)),
        },
        "reading": (
            "2 座標は別の基準で測っており、逆を向くことがある。単一の supply 状態へ畳まない。"
            "低い percentile は「今は候補が薄い」を示すが、候補が無いことの証明ではない。"
        ),
    }


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report where today's candidate supply sits inside its own panel history. "
            "Read-only: never changes screening rules, estimates, or stored runs."
        )
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--runs-db", type=Path, default=DEFAULT_RUNS_DB)
    parser.add_argument("--selection-id")
    parser.add_argument("--hurdle", type=float, default=DEFAULT_HURDLE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    try:
        payload = build_supply_context(
            calibration_dir=args.calibration_dir,
            runs_db=args.runs_db,
            selection_id=args.selection_id,
            hurdle=args.hurdle,
        )
    except (SupplyContextError, SignalCohortMeasurementError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, file=stdout or sys.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
