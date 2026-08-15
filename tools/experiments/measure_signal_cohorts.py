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
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal, TextIO

import yaml

from baibai_engine.screening.calibration.panel import PanelRow as StoredPanelRow
from baibai_engine.screening.calibration.store import (
    CalibrationCacheError,
    published_cohorts,
    read_forward,
    read_panel,
    resolve_calibration_bundle,
)

DEFAULT_CALIBRATION_DIR = Path("stores/screening/calibration")
DEFAULT_HORIZONS = ("1y", "3y", "5y")
HORIZON_YEARS: Mapping[str, int] = {"1y": 1, "3y": 3, "5y": 5}
# selection.liquidity と同じ関門。screening rules を読まずにここへ写すと乖離するので、
# 変更時は method/screening/rules の selection.liquidity と突き合わせる。
MIN_MARKET_CAP_OKU = 100.0
MIN_AVG_TURNOVER_OKU = 1.0
MIN_LISTING_SPAN_DAYS = 182.0
BUYBACK_CLIP = 0.05
UPSIDE_CAP = 0.50
DEFAULT_ER_THRESHOLD = 0.085
# `price` は終値だけ、`total` は窓内に実現した配当を足したもの。carry は配当と自己株買いで
# できているので、その効果量を price だけで測ると払われた現金の分だけ小さく出る。
type MetricBasis = Literal["price", "total"]
_METRIC_BASIS_LABEL: Mapping[MetricBasis, str] = {
    "price": "price_return_only",
    "total": "total_return_realized_dividends",
}
# total は窓内に FY 配当の観測が要る。窓が短いほど届かず、3m / 6m では price 側の半分にも
# 満たない。母数比がこの水準を割る horizon は、両 basis の中央値を並べて読ませない。
MIN_TOTAL_BASIS_COVERAGE = 0.75
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
    buyback_yield: float | None
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


def _annualized(cumulative_return: float, years: int) -> float:
    growth: float = (1.0 + cumulative_return) ** (1.0 / years)
    return growth - 1.0


def _load_forward_returns(
    calibration_dir: Path, horizons: Sequence[str], *, basis: MetricBasis
) -> tuple[Mapping[tuple[str, str], Mapping[str, float]], Mapping[str, Mapping[str, int]]]:
    """Resolved forward returns on the requested basis, plus what each basis resolves.

    The two bases do not cover the same rows: `total` additionally needs realized
    dividends over the window, so it resolves fewer. Returning both counts is what
    keeps a `total` median from being read as the same sample measured differently.
    """
    wanted = set(horizons)
    resolved: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    counts: dict[str, dict[str, int]] = {horizon: {"price": 0, "total": 0} for horizon in horizons}
    asofs = published_cohorts(calibration_dir)
    if not asofs:
        raise SignalCohortMeasurementError(f"no forward rows under {calibration_dir}")
    for asof in asofs:
        for row in read_forward(calibration_dir, asof):
            if row.horizon not in wanted:
                continue
            price = row.price_return if row.resolved else None
            total = row.total_return if row.total_return_status == "resolved" else None
            if price is not None:
                counts[row.horizon]["price"] += 1
            if total is not None:
                counts[row.horizon]["total"] += 1
            value = price if basis == "price" else total
            if value is None:
                continue
            resolved[(row.asof, row.ticker)][row.horizon] = value
    return resolved, counts


def require_single_rules_hash(calibration_dir: Path) -> str:
    """この世代が名乗る screening rules の identity。

    rules を動かした後に一部だけ再構築すると、別の母集団定義で作られた月が混ざる。混ぜて
    平均しても値は出てしまい、しかも権威ありげな percentile として報告へ載る。混在の拒否は
    bundle の組み立てが持つので、ここは manifest が名乗る identity をそのまま読む — 各 cohort
    の行を開き直して数え直すと、authority ではない側で同じ判断をやり直すことになる。
    """

    try:
        bundle = resolve_calibration_bundle(calibration_dir)
    except CalibrationCacheError as exc:
        raise SignalCohortMeasurementError(str(exc)) from exc
    hashes = {
        entry.panel.measurement_policy.rules_hash for entry in bundle.manifest.cohorts.values()
    }
    if not hashes:
        raise SignalCohortMeasurementError(f"no panel metadata under {calibration_dir}")
    if len(hashes) > 1:
        raise SignalCohortMeasurementError(
            f"panels mix screening rules revisions: {', '.join(sorted(hashes))}"
        )
    return next(iter(hashes))


def _load_panel(
    calibration_dir: Path,
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> list[PanelRow]:
    asofs = published_cohorts(calibration_dir)
    if not asofs:
        raise SignalCohortMeasurementError(f"no panel rows under {calibration_dir}")
    rows: list[PanelRow] = []
    for asof in asofs:
        for stored in read_panel(calibration_dir, asof):
            row = _panel_row(stored, forward)
            if row is not None:
                rows.append(row)
    if not rows:
        raise SignalCohortMeasurementError("panel rows carry no liquidity-passing candidates")
    return rows


def _panel_row(
    stored: StoredPanelRow,
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> PanelRow | None:
    market_cap = stored.market_cap_oku
    turnover = stored.avg_turnover_oku
    listing_span = stored.listing_span_days
    if market_cap is None or market_cap < MIN_MARKET_CAP_OKU:
        return None
    if turnover is None or turnover < MIN_AVG_TURNOVER_OKU:
        return None
    if listing_span is None or listing_span < MIN_LISTING_SPAN_DAYS:
        return None
    if stored.er_annual is None:
        return None
    share_change = stored.net_share_change_yoy
    # 欠測を 0 と読むと「株数が動かなかった」と「株数変化が分からない」が control 群へ
    # 一緒に入る。buyback 比較では欠測を None のまま持ち、どちらの群にも入れない。
    buyback = None if share_change is None else max(-BUYBACK_CLIP, min(BUYBACK_CLIP, -share_change))
    return PanelRow(
        asof=stored.asof,
        ticker=stored.ticker,
        er_annual=stored.er_annual,
        reversion_annual=stored.er_reversion_annual or 0.0,
        carry_annual=stored.er_carry_annual or 0.0,
        dividend_yield=stored.dividend_yield or 0.0,
        buyback_yield=buyback,
        upside_capped=stored.er_upside_capped,
        earnings_anchor_available=bool(
            (stored.per_forward is not None and stored.per_forward > 0)
            or (stored.per_trailing is not None and stored.per_trailing > 0)
        ),
        reduction_streak=stored.share_count_reduction_streak,
        forward=forward.get((stored.asof, stored.ticker), {}),
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
    known = [row for row in rows if row.buyback_yield is not None]
    clipped = [row for row in known if (row.buyback_yield or 0.0) >= BUYBACK_CLIP - 1e-9]
    partial = [row for row in known if 0.0 < (row.buyback_yield or 0.0) < BUYBACK_CLIP - 1e-9]
    non_positive = [row for row in known if (row.buyback_yield or 0.0) <= 0.0]
    unknown = [row for row in rows if row.buyback_yield is None]
    return {
        "horizon": horizon,
        "groups": [
            _group_summary(clipped, horizon, label="buyback_yield_at_clip"),
            _group_summary(partial, horizon, label="buyback_yield_partial"),
            _group_summary(non_positive, horizon, label="buyback_yield_non_positive"),
            _group_summary(unknown, horizon, label="share_change_unobserved"),
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
    reducing = [row for row in rows if (row.buyback_yield or 0.0) > 0.005]
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


def _resolved_asof_range(rows: Sequence[PanelRow], horizon: str) -> Mapping[str, object]:
    """その horizon が解決している as-of の範囲と件数。

    forward 窓が長いほど解決済み as-of は古い側へ寄る。5y は entry がほぼ 1 つの局面に
    偏るので、範囲を出さずに cohort 数だけを見ると独立確認に見えてしまう。
    """

    asofs = sorted({row.asof for row in rows if horizon in row.forward})
    if not asofs:
        return {"asof_start": None, "asof_end": None, "asof_count": 0}
    return {"asof_start": asofs[0], "asof_end": asofs[-1], "asof_count": len(asofs)}


def _basis_coverage(
    counts: Mapping[str, Mapping[str, int]], horizons: Sequence[str]
) -> Mapping[str, Mapping[str, object]]:
    coverage: dict[str, Mapping[str, object]] = {}
    for horizon in horizons:
        price = counts[horizon]["price"]
        total = counts[horizon]["total"]
        ratio = None if price == 0 else round(total / price, 3)
        coverage[horizon] = {
            "price_resolved_rows": price,
            "total_resolved_rows": total,
            "total_to_price_ratio": ratio,
            # 両 basis の中央値を「同じ群の別 basis」として並べてよいかの表明。
            "bases_comparable": ratio is not None and ratio >= MIN_TOTAL_BASIS_COVERAGE,
        }
    return coverage


def build_measurement(
    *,
    calibration_dir: Path,
    horizons: Sequence[str],
    er_threshold: float,
    basis: MetricBasis = "price",
    asof_from: str | None = None,
    asof_to: str | None = None,
) -> Mapping[str, object]:
    for horizon in horizons:
        if horizon not in HORIZON_YEARS:
            raise SignalCohortMeasurementError(f"unsupported horizon: {horizon}")
    rules_hash = require_single_rules_hash(calibration_dir)
    forward, basis_counts = _load_forward_returns(calibration_dir, horizons, basis=basis)
    rows = _load_panel(calibration_dir, forward)
    if asof_from is not None:
        rows = [row for row in rows if row.asof >= asof_from]
    if asof_to is not None:
        rows = [row for row in rows if row.asof <= asof_to]
    if not rows:
        raise SignalCohortMeasurementError("the requested as-of window carries no panel rows")
    asofs = sorted({row.asof for row in rows})
    return {
        "kind": "signal-cohort-measurement",
        "calibration_dir": str(calibration_dir),
        "metric_basis": _METRIC_BASIS_LABEL[basis],
        "basis_coverage": _basis_coverage(basis_counts, horizons),
        "population": "liquidity_passing_panel_rows",
        "rules_hash": rules_hash,
        # cohort 比較は両群がこの件数を満たす as-of だけを数える。候補が薄い月は
        # treatment が痩せて落ちるため、閾値そのものが標本を選ぶ。読むときは併記する。
        "min_group_rows": MIN_GROUP_ROWS,
        "asof_window_requested": {"from": asof_from, "to": asof_to},
        "panel_asof_start": asofs[0],
        "panel_asof_end": asofs[-1],
        "panel_asof_count": len(asofs),
        "panel_rows": len(rows),
        "resolved_asof_range": {
            horizon: _resolved_asof_range(rows, horizon) for horizon in horizons
        },
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
            (
                "price basis は配当を含まない。carry は配当と自己株買いでできているので、"
                "その効果量は price basis では払われた現金の分だけ小さく出る。"
            ),
            (
                "total basis は窓内の実現配当だけを足し、端 FY の月割りをしない。母数は "
                "price basis より小さく、その比は basis_coverage に出る。"
                "bases_comparable が false の horizon で両 basis の中央値を並べない。"
            ),
            "解決した forward だけを数える。廃止で系列が切れた銘柄は母集団から落ちる。",
            "cohort の実現値は当時の市況を含む。母集団との差だけが regime 統制された量である。",
            (
                "cohort 一致は min_group_rows に依存する。treatment が痩せる月ほど落ちるので、"
                "閾値を変えた値も見てから読む。"
            ),
            (
                "解決済み as-of は horizon が長いほど古い側へ偏る。resolved_asof_range を"
                "見ずに cohort 数だけを比べない。"
            ),
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
    parser.add_argument(
        "--basis",
        choices=("price", "total"),
        default="price",
        help="realized return basis:終値のみか、窓内の実現配当を含めるか（既定: price）",
    )
    parser.add_argument("--asof-from", help="この as-of 以降の panel だけを使う (YYYY-MM-DD)")
    parser.add_argument("--asof-to", help="この as-of 以前の panel だけを使う (YYYY-MM-DD)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    horizons = tuple(args.horizons) if args.horizons else DEFAULT_HORIZONS
    try:
        payload = build_measurement(
            calibration_dir=args.calibration_dir,
            horizons=horizons,
            er_threshold=args.er_threshold,
            basis=args.basis,
            asof_from=args.asof_from,
            asof_to=args.asof_to,
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
