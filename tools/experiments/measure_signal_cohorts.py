"""Compare realized forward returns across machine-estimate cohorts.

較正 store に保存された as-of panel と forward return を突き合わせ、「機械が上位と見た
群」「historical share-count contribution が clip に貼り付いた群」「PBR 単独 anchor で
reversion が cap に当たった群」が母集団に対してどう実現したかを as-of cohort 単位で出す。

E[r] policy parameter の変更を提案する artifact ではない。screening rules も E[r] も
変えず、判断の前提が実データで支持されるかだけを測る。窓は重複するので有意性は
主張せず、群間の観測差と cohort ごとの差の向きで読む。過去時点での情報利用可能性は
元データの vintage に依存し、この集計だけでは保証しない。
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
    fixed_current_snapshot,
    published_cohorts,
    read_forward,
    read_panel,
    read_panel_meta,
)

DEFAULT_CALIBRATION_DIR = Path("stores/screening/calibration")
DEFAULT_HORIZONS = ("1y", "3y", "5y")
HORIZON_YEARS: Mapping[str, int] = {"1y": 1, "3y": 3, "5y": 5}
SHARE_COUNT_YIELD_CLIP = 0.05
UPSIDE_CAP = 0.50
DEFAULT_ER_THRESHOLD = 0.085
# `price` は終値だけ、`total` は窓内に実現した配当を足したもの。
# 株数変化の寄与は自己株買いの実行額や配当収入そのものではない。
type MetricBasis = Literal["price", "total"]
_METRIC_BASIS_LABEL: Mapping[MetricBasis, str] = {
    "price": "price_return_only",
    "total": "total_return_realized_dividends",
}
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
    share_count_yield: float | None
    upside_capped: float | None
    earnings_anchor_available: bool
    reduction_streak: int | None
    forward: Mapping[str, float]


def _annualized(cumulative_return: float, years: int) -> float:
    growth: float = (1.0 + cumulative_return) ** (1.0 / years)
    return growth - 1.0


def _load_forward_returns(
    snapshot: Path,
    horizons: Sequence[str],
    *,
    basis: MetricBasis,
) -> tuple[
    Mapping[tuple[str, str], Mapping[str, float]],
    Mapping[str, Mapping[str, set[tuple[str, str]]]],
]:
    """Resolved forward returns on the requested basis, plus what each basis resolves.

    The two bases do not cover the same rows: `total` additionally needs realized
    dividends over the window, so it resolves fewer. Returning both row sets is what
    keeps a `total` median from being read as the same sample measured differently.
    """
    wanted = set(horizons)
    resolved: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    counts: dict[str, dict[str, set[tuple[str, str]]]] = {
        horizon: {"price": set(), "total": set()} for horizon in horizons
    }
    asofs = published_cohorts(snapshot)
    if not asofs:
        raise SignalCohortMeasurementError(f"no forward rows under {snapshot}")
    for asof in asofs:
        for row in read_forward(snapshot, asof):
            if row.horizon not in wanted:
                continue
            price = row.price_return if row.resolved else None
            total = row.total_return if row.total_return_status == "resolved" else None
            if price is not None:
                counts[row.horizon]["price"].add((row.asof, row.ticker))
            if total is not None:
                counts[row.horizon]["total"].add((row.asof, row.ticker))
            value = price if basis == "price" else total
            if value is None:
                continue
            resolved[(row.asof, row.ticker)][row.horizon] = value
    return resolved, counts


def require_single_rules_hash(snapshot: Path) -> str:
    """公開 cohort のメタデータを読み、単一の rules identity を確認する。"""

    hashes = {
        str(read_panel_meta(snapshot, asof)["rules_hash"]) for asof in published_cohorts(snapshot)
    }
    if not hashes:
        raise SignalCohortMeasurementError(f"snapshot {snapshot} has no cohorts")
    if len(hashes) > 1:
        raise SignalCohortMeasurementError(
            f"panels mix screening rules revisions: {', '.join(sorted(hashes))}"
        )
    return next(iter(hashes))


def _load_panel(
    snapshot: Path,
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> list[PanelRow]:
    asofs = published_cohorts(snapshot)
    if not asofs:
        raise SignalCohortMeasurementError(f"no panel rows under {snapshot}")
    rows: list[PanelRow] = []
    for asof in asofs:
        for stored in read_panel(snapshot, asof):
            row = _panel_row(stored, forward)
            if row is not None:
                rows.append(row)
    if not rows:
        raise SignalCohortMeasurementError("panel rows carry no stored population candidates")
    return rows


def _panel_row(
    stored: StoredPanelRow,
    forward: Mapping[tuple[str, str], Mapping[str, float]],
) -> PanelRow | None:
    if not stored.in_population:
        return None
    if stored.er_annual is None:
        return None
    share_change = stored.net_share_change_yoy
    # 欠測を 0 と読むと「株数が動かなかった」と「株数変化が分からない」が control 群へ
    # 一緒に入る。株数変化の比較では欠測を None のまま持ち、どちらの群にも入れない。
    share_count_yield = (
        None
        if share_change is None
        else max(
            -SHARE_COUNT_YIELD_CLIP,
            min(SHARE_COUNT_YIELD_CLIP, -share_change),
        )
    )
    return PanelRow(
        asof=stored.asof,
        ticker=stored.ticker,
        er_annual=stored.er_annual,
        share_count_yield=share_count_yield,
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


def _share_count_comparison(rows: Sequence[PanelRow], horizon: str) -> Mapping[str, object]:
    known = [row for row in rows if row.share_count_yield is not None]
    clipped = [
        row for row in known if (row.share_count_yield or 0.0) >= SHARE_COUNT_YIELD_CLIP - 1e-9
    ]
    partial = [
        row for row in known if 0.0 < (row.share_count_yield or 0.0) < SHARE_COUNT_YIELD_CLIP - 1e-9
    ]
    non_positive = [row for row in known if (row.share_count_yield or 0.0) <= 0.0]
    unknown = [row for row in rows if row.share_count_yield is None]
    return {
        "horizon": horizon,
        "groups": [
            _group_summary(clipped, horizon, label="share_count_yield_at_clip"),
            _group_summary(partial, horizon, label="share_count_yield_partial"),
            _group_summary(non_positive, horizon, label="share_count_yield_non_positive"),
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
        row
        for row in rows
        if not row.earnings_anchor_available and row.upside_capped is not None and not capped(row)
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


def _share_count_continuity_comparison(
    rows: Sequence[PanelRow], horizon: str
) -> Mapping[str, object]:
    """株数減少が単発か継続かで forward が変わるかを見る。

    連続 FY 数は保存された株数減少の履歴であり、取得枠の残高や消化完了を表さない。
    """
    reducing = [row for row in rows if (row.share_count_yield or 0.0) > 0.005]
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
    counts: Mapping[str, Mapping[str, set[tuple[str, str]]]],
    horizons: Sequence[str],
    panel_keys: set[tuple[str, str]],
) -> Mapping[str, Mapping[str, object]]:
    coverage: dict[str, Mapping[str, object]] = {}
    for horizon in horizons:
        price_keys = counts[horizon]["price"] & panel_keys
        total_keys = counts[horizon]["total"] & panel_keys
        price = len(price_keys)
        total = len(total_keys)
        ratio = None if price == 0 else round(total / price, 3)
        coverage[horizon] = {
            "price_resolved_rows": price,
            "total_resolved_rows": total,
            "total_to_price_ratio": ratio,
            # 件数比が近いだけでは同じ標本ではない。行の identity まで一致を要する。
            "bases_comparable": bool(price_keys) and price_keys == total_keys,
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
    try:
        with fixed_current_snapshot(calibration_dir) as snapshot:
            rules_hash = require_single_rules_hash(snapshot)
            forward, basis_counts = _load_forward_returns(snapshot, horizons, basis=basis)
            rows = _load_panel(snapshot, forward)
    except CalibrationCacheError as exc:
        raise SignalCohortMeasurementError(str(exc)) from exc
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
        "calibration_snapshot": "current",
        "metric_basis": _METRIC_BASIS_LABEL[basis],
        "basis_coverage": _basis_coverage(
            basis_counts, horizons, {(row.asof, row.ticker) for row in rows}
        ),
        "population": "stored_in_population_panel_rows",
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
            "share_count_component": [
                _share_count_comparison(rows, horizon) for horizon in horizons
            ],
            "share_count_continuity": [
                _share_count_continuity_comparison(rows, horizon) for horizon in horizons
            ],
            "equity_anchor": [_anchor_comparison(rows, horizon) for horizon in horizons],
        },
        "integrity": [
            "forward 窓は重なるので独立でない。有意性と track record を主張しない。",
            (
                "price basis は配当を含まない。株数変化の寄与は自己株買いの実行額ではなく、"
                "この群比較から carry の因果効果は測定できない。"
            ),
            (
                "total basis は窓内の実現配当だけを足し、端 FY の月割りをしない。母数は "
                "price basis より小さく、その比は basis_coverage に出る。"
                "bases_comparable は選択された panel 内で行 identity が一致するときだけ true。"
                "異なる標本の中央値差を配当の寄与と解釈しない。"
            ),
            "解決した forward だけを数える。廃止で系列が切れた銘柄は母集団から落ちる。",
            "同じ as-of の比較でも業種・規模・リスク構成は揃っておらず、因果効果ではない。",
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
