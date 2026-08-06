"""Compare realized forward returns across machine-estimate cohorts.

較正 store の point-in-time panel と forward return を突き合わせ、「機械が上位と見た
群」「buyback carry が clip に貼り付いた群」「PBR 単独 anchor で reversion が cap に
当たった群」が母集団に対してどう実現したかを as-of cohort 単位で出す。

E[r] policy parameter の変更を提案する artifact ではない。screening rules も E[r] も
変えず、判断の前提が実データで支持されるかだけを測る。窓は重複するので有意性は
主張せず、効果量と cohort 勝率で読む。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal, TextIO

import yaml

DEFAULT_CALIBRATION_DIR = Path("data/screening/calibration")
DEFAULT_HORIZONS = ("1y", "3y", "5y")
HORIZON_YEARS: Mapping[str, int] = {"1y": 1, "3y": 3, "5y": 5}
# selection.liquidity と同じ関門。screening rules を読まずにここへ写すと乖離するので、
# 変更時は method/screening-rules の selection.liquidity と突き合わせる。
MIN_MARKET_CAP_OKU = 100.0
MIN_AVG_TURNOVER_OKU = 1.0
MIN_LISTING_SPAN_DAYS = 182.0
BUYBACK_CLIP = 0.05
UPSIDE_CAP = 0.50
DEFAULT_ER_THRESHOLD = 0.085
# cohort 内の群比較は、両群がこの件数を満たすときだけ数える。
MIN_GROUP_ROWS = 10
MIN_POPULATION_ROWS = 100


class SignalCohortMeasurementError(ValueError):
    """Raised when the calibration store cannot support a truthful comparison."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelRow:
    asof: str
    ticker: str
    er_annual: float
    reversion_annual: float
    carry_annual: float
    dividend_yield: float
    buyback_yield: float
    upside_capped: float | None
    earnings_anchor_available: bool
    reduction_streak: int | None
    forward: Mapping[str, float]


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed


def _annualized(price_return: float, years: int) -> float:
    growth: float = (1.0 + price_return) ** (1.0 / years)
    return growth - 1.0


def _load_forward_returns(
    calibration_dir: Path, horizons: Sequence[str]
) -> Mapping[tuple[str, str], Mapping[str, float]]:
    wanted = set(horizons)
    resolved: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    paths = sorted(calibration_dir.glob("forward-*.csv"))
    if not paths:
        raise SignalCohortMeasurementError(f"no forward rows under {calibration_dir}")
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("status") != "resolved":
                    continue
                horizon = row.get("horizon") or ""
                if horizon not in wanted:
                    continue
                price_return = _optional_float(row.get("price_return"))
                if price_return is None:
                    continue
                resolved[(row.get("asof") or "", row.get("ticker") or "")][horizon] = price_return
    return resolved


def _load_panel(
    calibration_dir: Path,
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> list[PanelRow]:
    paths = sorted(calibration_dir.glob("panel-*.csv"))
    if not paths:
        raise SignalCohortMeasurementError(f"no panel rows under {calibration_dir}")
    rows: list[PanelRow] = []
    for path in paths:
        asof = path.name.removeprefix("panel-").removesuffix(".csv")
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                row = _panel_row(asof, raw, forward)
                if row is not None:
                    rows.append(row)
    if not rows:
        raise SignalCohortMeasurementError("panel rows carry no liquidity-passing candidates")
    return rows


def _panel_row(
    asof: str,
    raw: Mapping[str, str],
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> PanelRow | None:
    market_cap = _optional_float(raw.get("market_cap_oku"))
    turnover = _optional_float(raw.get("avg_turnover_oku"))
    listing_span = _optional_float(raw.get("listing_span_days"))
    if market_cap is None or market_cap < MIN_MARKET_CAP_OKU:
        return None
    if turnover is None or turnover < MIN_AVG_TURNOVER_OKU:
        return None
    if listing_span is None or listing_span < MIN_LISTING_SPAN_DAYS:
        return None
    er_annual = _optional_float(raw.get("er_annual"))
    if er_annual is None:
        return None
    share_change = _optional_float(raw.get("net_share_change_yoy"))
    buyback = 0.0 if share_change is None else max(-BUYBACK_CLIP, min(BUYBACK_CLIP, -share_change))
    per_forward = _optional_float(raw.get("per_forward"))
    per_trailing = _optional_float(raw.get("per_trailing"))
    streak = _optional_float(raw.get("share_count_reduction_streak"))
    ticker = raw.get("ticker") or ""
    return PanelRow(
        asof=asof,
        ticker=ticker,
        er_annual=er_annual,
        reversion_annual=_optional_float(raw.get("er_reversion_annual")) or 0.0,
        carry_annual=_optional_float(raw.get("er_carry_annual")) or 0.0,
        dividend_yield=_optional_float(raw.get("dividend_yield")) or 0.0,
        buyback_yield=buyback,
        upside_capped=_optional_float(raw.get("er_upside_capped")),
        earnings_anchor_available=bool(
            (per_forward is not None and per_forward > 0)
            or (per_trailing is not None and per_trailing > 0)
        ),
        reduction_streak=None if streak is None else int(streak),
        forward=forward.get((asof, ticker), {}),
    )


def _median_annualized(rows: Iterable[PanelRow], horizon: str) -> tuple[int, float | None]:
    years = HORIZON_YEARS[horizon]
    values = [_annualized(row.forward[horizon], years) for row in rows if horizon in row.forward]
    if not values:
        return 0, None
    return len(values), median(values)


def _cohort_win_rate(
    treatment: Sequence[PanelRow],
    control: Sequence[PanelRow],
    horizon: str,
    *,
    min_control_rows: int,
) -> Mapping[str, object]:
    by_asof_treatment: dict[str, list[PanelRow]] = defaultdict(list)
    by_asof_control: dict[str, list[PanelRow]] = defaultdict(list)
    for row in treatment:
        by_asof_treatment[row.asof].append(row)
    for row in control:
        by_asof_control[row.asof].append(row)
    wins = 0
    compared = 0
    differences: list[float] = []
    for asof in sorted(by_asof_treatment):
        treated_count, treated_median = _median_annualized(by_asof_treatment[asof], horizon)
        control_count, control_median = _median_annualized(by_asof_control.get(asof, []), horizon)
        if treated_count < MIN_GROUP_ROWS or control_count < min_control_rows:
            continue
        if treated_median is None or control_median is None:
            continue
        compared += 1
        difference = treated_median - control_median
        differences.append(difference)
        wins += difference > 0
    return {
        "cohorts_compared": compared,
        "cohorts_treatment_ahead": wins,
        "median_difference_pct_points": (
            None if not differences else round(median(differences) * 100, 2)
        ),
    }


def _group_summary(rows: Sequence[PanelRow], horizon: str, *, label: str) -> Mapping[str, object]:
    count, value = _median_annualized(rows, horizon)
    return {
        "group": label,
        "resolved_rows": count,
        "median_annualized_pct": None if value is None else round(value * 100, 2),
    }


def _high_estimate_comparison(
    rows: Sequence[PanelRow], horizon: str, *, er_threshold: float
) -> Mapping[str, object]:
    treatment = [row for row in rows if row.er_annual >= er_threshold]
    return {
        "horizon": horizon,
        "er_threshold": er_threshold,
        "groups": [
            _group_summary(treatment, horizon, label=f"er_annual>={er_threshold}"),
            _group_summary(rows, horizon, label="liquidity_passing_population"),
        ],
        "cohort_agreement": _cohort_win_rate(
            treatment, rows, horizon, min_control_rows=MIN_POPULATION_ROWS
        ),
    }


def _buyback_comparison(rows: Sequence[PanelRow], horizon: str) -> Mapping[str, object]:
    clipped = [row for row in rows if row.buyback_yield >= BUYBACK_CLIP - 1e-9]
    partial = [row for row in rows if 0.0 < row.buyback_yield < BUYBACK_CLIP - 1e-9]
    non_positive = [row for row in rows if row.buyback_yield <= 0.0]
    return {
        "horizon": horizon,
        "groups": [
            _group_summary(clipped, horizon, label="buyback_yield_at_clip"),
            _group_summary(partial, horizon, label="buyback_yield_partial"),
            _group_summary(non_positive, horizon, label="buyback_yield_non_positive"),
        ],
        "cohort_agreement": _cohort_win_rate(
            clipped, non_positive, horizon, min_control_rows=MIN_GROUP_ROWS
        ),
    }


def _anchor_comparison(rows: Sequence[PanelRow], horizon: str) -> Mapping[str, object]:
    def capped(row: PanelRow) -> bool:
        return row.upside_capped is not None and row.upside_capped >= UPSIDE_CAP - 1e-9

    equity_only_capped = [row for row in rows if not row.earnings_anchor_available and capped(row)]
    equity_only_uncapped = [
        row for row in rows if not row.earnings_anchor_available and not capped(row)
    ]
    earnings_capped = [row for row in rows if row.earnings_anchor_available and capped(row)]
    return {
        "horizon": horizon,
        "groups": [
            _group_summary(equity_only_capped, horizon, label="equity_anchor_only_at_cap"),
            _group_summary(equity_only_uncapped, horizon, label="equity_anchor_only_below_cap"),
            _group_summary(earnings_capped, horizon, label="earnings_anchor_at_cap"),
            _group_summary(rows, horizon, label="liquidity_passing_population"),
        ],
        "cohort_agreement": _cohort_win_rate(
            equity_only_capped, rows, horizon, min_control_rows=MIN_POPULATION_ROWS
        ),
    }


def _buyback_continuity_comparison(rows: Sequence[PanelRow], horizon: str) -> Mapping[str, object]:
    """株数減少が単発か継続かで forward が変わるかを見る。

    EDINET の取得枠状態は 2025-08 以降しか観測できないので、連続 FY 数を代理変数にして
    80 か月へ広げる。単発 = 枠を消化し終えた状態に近い。
    """
    reducing = [row for row in rows if row.buyback_yield > 0.005]
    single = [row for row in reducing if row.reduction_streak == 1]
    repeated = [
        row for row in reducing if row.reduction_streak is not None and row.reduction_streak >= 2
    ]
    unknown = [row for row in reducing if row.reduction_streak is None]
    return {
        "horizon": horizon,
        "groups": [
            _group_summary(single, horizon, label="reduction_streak_single_year"),
            _group_summary(repeated, horizon, label="reduction_streak_repeated"),
            _group_summary(unknown, horizon, label="reduction_streak_unknown"),
            _group_summary(rows, horizon, label="liquidity_passing_population"),
        ],
        "cohort_agreement": _cohort_win_rate(
            repeated, single, horizon, min_control_rows=MIN_GROUP_ROWS
        ),
    }


ComparisonName = Literal[
    "high_estimate", "buyback_component", "buyback_continuity", "equity_anchor"
]


def build_measurement(
    *,
    calibration_dir: Path,
    horizons: Sequence[str],
    er_threshold: float,
) -> Mapping[str, object]:
    for horizon in horizons:
        if horizon not in HORIZON_YEARS:
            raise SignalCohortMeasurementError(f"unsupported horizon: {horizon}")
    forward = _load_forward_returns(calibration_dir, horizons)
    rows = _load_panel(calibration_dir, forward)
    asofs = sorted({row.asof for row in rows})
    return {
        "kind": "signal-cohort-measurement",
        "calibration_dir": str(calibration_dir),
        "metric_basis": "price_return_only",
        "population": "liquidity_passing_panel_rows",
        "panel_asof_start": asofs[0],
        "panel_asof_end": asofs[-1],
        "panel_asof_count": len(asofs),
        "panel_rows": len(rows),
        "comparisons": {
            "high_estimate": [
                _high_estimate_comparison(rows, horizon, er_threshold=er_threshold)
                for horizon in horizons
            ],
            "buyback_component": [_buyback_comparison(rows, horizon) for horizon in horizons],
            "buyback_continuity": [
                _buyback_continuity_comparison(rows, horizon) for horizon in horizons
            ],
            "equity_anchor": [_anchor_comparison(rows, horizon) for horizon in horizons],
        },
        "integrity": [
            "forward 窓は重なるので独立でない。有意性と track record を主張しない。",
            "price-only であり配当を含まない。",
            "解決した forward だけを数える。廃止で系列が切れた銘柄は母集団から落ちる。",
            "cohort の実現値は当時の市況を含む。母集団との差だけが regime 統制された量である。",
        ],
    }


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare realized forward returns across machine-estimate cohorts. "
            "Read-only: never changes screening rules, E[r], or stored runs."
        )
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--horizon", action="append", dest="horizons", choices=DEFAULT_HORIZONS)
    parser.add_argument("--er-threshold", type=float, default=DEFAULT_ER_THRESHOLD)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    horizons = tuple(args.horizons) if args.horizons else DEFAULT_HORIZONS
    try:
        payload = build_measurement(
            calibration_dir=args.calibration_dir,
            horizons=horizons,
            er_threshold=args.er_threshold,
        )
    except (SignalCohortMeasurementError, OSError) as error:
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
