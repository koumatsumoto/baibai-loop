"""較正の評価指標: rank IC・decile・selection replay・トラップ率・gate 条件付き spread・収束実現。

統計の誠実性 (docs/doctrine.md の計測経路):
- cohort (月次 asof) は forward 窓が重複し独立でないため、有意性検定は行わず
  「効果量 (median/mean excess) と cohort 勝率」で報告する。
- 超過リターンの一次基準は流動性母集団の中央値 (選定スキルの直接計測) 。
  benchmark ETF は市況文脈の参考値。
- 累積リターン・年率・シャープ等の track record 系は出力しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from statistics import fmean, median

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY

from .forward import ForwardReturnRow
from .horizons import require_horizon
from .panel import PanelRow

# 割安 decile / top-N の「バリュートラップ」判定: 母集団中央値に 20pt 以上劣後。
TRAP_EXCESS_THRESHOLD = -0.20

# 軸評価に要求する最小標本数 (cohort x 軸ごと) 。下回る軸はその cohort で skip。
MIN_AXIS_SAMPLE = 100

# IC 計算に要求する最小標本数。
MIN_IC_SAMPLE = 30

DECILES = 10

SELECTION_TOP_NS: tuple[int, ...] = (5, 10, 20)

# gate 条件付き spread の対象 gate (業績悪化 gate。rule_config の deterioration
# threshold と同じ -0.3 を事前固定で用いる) 。
DETERIORATION_THRESHOLD = -0.3


@dataclass(frozen=True, slots=True, kw_only=True)
class AxisSpec:
    """評価軸。direction=+1 は「高いほど良い(割安)」、-1 は「低いほど良い」。"""

    name: str
    direction: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _CohortExcessContext:
    population: list[PanelRow]
    excess: dict[str, float]
    population_median_return: float
    benchmark_price_return: float | None
    stale_price_count: int
    dividend_yield_coverage: int
    horizon_years: float


AXES: tuple[AxisSpec, ...] = (
    AxisSpec(name="per_forward", direction=-1),
    AxisSpec(name="per_trailing", direction=-1),
    AxisSpec(name="pbr", direction=-1),
    AxisSpec(name="ev_ebitda", direction=-1),
    AxisSpec(name="p_s", direction=-1),
    AxisSpec(name="pcfr", direction=-1),
    AxisSpec(name="ocf_yield", direction=1),
    AxisSpec(name="fcf_yield", direction=1),
    AxisSpec(name="net_cash_to_market_cap", direction=1),
    AxisSpec(name="cash_to_market_cap", direction=1),
    AxisSpec(name="equity_ratio", direction=1),
    AxisSpec(name="dividend_yield", direction=1),
    AxisSpec(name="er_annual", direction=1),
    AxisSpec(name="er_reversion_annual", direction=1),
    AxisSpec(name="er_carry_annual", direction=1),
    AxisSpec(name="smg_per_forward", direction=-1),
    AxisSpec(name="smg_per_trailing", direction=-1),
    AxisSpec(name="smg_pbr", direction=-1),
    AxisSpec(name="smg_ev_ebitda", direction=-1),
    AxisSpec(name="smg_p_s", direction=-1),
    AxisSpec(name="srp_per_forward", direction=-1),
    AxisSpec(name="srp_per_trailing", direction=-1),
    AxisSpec(name="srp_pbr", direction=-1),
    AxisSpec(name="srp_ev_ebitda", direction=-1),
    AxisSpec(name="srp_p_s", direction=-1),
    AxisSpec(name="net_share_change_yoy", direction=-1),
    AxisSpec(name="accruals_to_assets", direction=-1),
    AxisSpec(name="price_change_60d", direction=-1),
    AxisSpec(name="gap_from_52w_low", direction=-1),
)

# gate 条件付き評価を行う「割安軸」 (この軸の best decile 内で gate を比較する) 。
GATE_BASE_AXES: tuple[str, ...] = ("per_trailing", "pbr", "ocf_yield")

# 収束実現 (implied upside → realized) を測る sector 相対 gap 軸。
REVERSION_AXES: tuple[str, ...] = ("smg_per_trailing", "smg_pbr", "smg_ev_ebitda")


def evaluate_cohorts(
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    *,
    horizons: Sequence[str],
) -> dict[str, object]:
    """Evaluate all cohorts and aggregate per horizon.

    ``panels`` / ``forwards`` は asof (ISO 文字列) を key にする。
    """
    per_horizon: dict[str, object] = {}
    for horizon in horizons:
        require_horizon(horizon)
        cohort_results: list[dict[str, object]] = []
        for asof in sorted(panels):
            cohort_results.append(
                _evaluate_cohort(panels[asof], forwards.get(asof, ()), asof=asof, horizon=horizon)
            )
        per_horizon[horizon] = {
            "authority": require_horizon(horizon).authority,
            "cohorts": cohort_results,
            "aggregate": _aggregate(cohort_results),
        }
    return per_horizon


def _evaluate_cohort(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    asof: str,
    horizon: str,
) -> dict[str, object]:
    context = _cohort_excess_context(panel, forward_rows, horizon=horizon)
    all_rows = [row for row in forward_rows if row.horizon == horizon]
    candidate_rows = [row for row in all_rows if row.ticker != TOPIX_ETF_PROXY]
    unresolved = [row for row in candidate_rows if row.status != "resolved"]
    unresolved_reasons: dict[str, int] = {}
    for row in unresolved:
        unresolved_reasons[row.status] = unresolved_reasons.get(row.status, 0) + 1
    # The panel records the last close at or before asof, so it is the authority on
    # whether a name was priced then. A forward row that found no entry for a name
    # the panel priced is a tradeable name dropped from the measurement, not a name
    # that was absent from the market — the two must not share a bucket, because only
    # the first can bias the cohort.
    priced_at_asof = {row.ticker for row in panel if row.close is not None}
    missing_entry = [row for row in unresolved if row.status == "unresolved_missing_entry"]
    entry_not_listed = [row for row in missing_entry if row.ticker not in priced_at_asof]
    entry_price_gap = [row for row in missing_entry if row.ticker in priced_at_asof]
    unpriced_exit = [
        row
        for row in unresolved
        if row.status in {"unresolved_missing_exit", "unresolved_stale_exit"}
    ]
    population_expected = [row for row in panel if row.in_population]
    expected_tickers = {row.ticker for row in panel}
    observed_tickers = {row.ticker for row in candidate_rows}
    coverage = {
        "master_population_count": len(panel),
        "policy_excluded_count": 0,
        "policy_exclusion_reason_counts": {},
        "candidate_population_count": len(panel),
        "liquid_population_count": len(population_expected),
        "entry_eligible_count": sum(
            1 for row in candidate_rows if row.status != "unresolved_missing_entry"
        ),
        "forward_rows_count": len(candidate_rows),
        "resolved_count": sum(1 for row in candidate_rows if row.status == "resolved"),
        "data_unresolved_count": len(unresolved),
        "data_unresolved_reason_counts": unresolved_reasons,
        # Unresolved rows are not one kind of defect. A name that was not listed
        # at asof is a correct exclusion; a name priced earlier but absent at
        # asof would be a silently dropped tradeable name; a name whose series
        # ends inside the window is the survivorship exposure that needs an exit
        # value. Only the last two can bias a cohort, so the authority gate
        # reads these counts rather than the undivided total.
        "entry_not_listed_count": len(entry_not_listed),
        "entry_price_gap_count": len(entry_price_gap),
        "unpriced_exit_count": len(unpriced_exit),
        "future_horizon_count": sum(
            1 for row in unresolved if row.status == "unresolved_future_horizon"
        ),
        "candidate_partition_complete": observed_tickers == expected_tickers,
        "candidate_forward_missing_count": len(expected_tickers - observed_tickers),
        "candidate_forward_extra_count": len(observed_tickers - expected_tickers),
        "delisting_coverage_status": ("unpriced_exit" if unpriced_exit else "complete"),
        "corporate_action_event_coverage_status": _corporate_action_status(
            candidate_rows, terminated=bool(unpriced_exit)
        ),
        "adjustment_factor_coverage": _coverage_status(
            candidate_rows, "adjustment_factor_coverage"
        ),
    }
    if context is None:
        return {
            "asof": asof,
            "horizon": horizon,
            "metric_basis": "price_return_only",
            "coverage": coverage,
            "metric_calculation_status": "unresolved",
            "axes": {},
            "selection": {},
            "gates": {},
            "reversion": {},
            "er_calibration": {},
        }
    population = context.population
    excess = context.excess

    axes: dict[str, object] = {}
    for spec in AXES:
        axes_result = _evaluate_axis(spec, population, excess)
        if axes_result is not None:
            axes[spec.name] = axes_result
    selection = _evaluate_selection(population, excess)
    er_calibration = _evaluate_er_calibration(
        population, excess, years=require_horizon(horizon).months / 12
    )
    metric_statuses = {
        key: ("eligible" if isinstance(value, dict) and value.get("n", 0) else "unresolved")
        for key, value in selection.items()
    }
    metric_statuses["er_calibration"] = "eligible" if er_calibration else "unresolved"

    return {
        "asof": asof,
        "horizon": horizon,
        "population_resolved": len(population),
        "metric_basis": "price_return_only",
        "coverage": coverage,
        "metric_calculation_status": "resolved",
        "metric_statuses": metric_statuses,
        "population_median_return": round(context.population_median_return, 6),
        "benchmark_price_return": (
            round(context.benchmark_price_return, 6)
            if context.benchmark_price_return is not None
            else None
        ),
        "stale_price_count": context.stale_price_count,
        "axes": axes,
        "selection": selection,
        "gates": _evaluate_gates(population, excess),
        "reversion": _evaluate_reversion(population, excess),
        "er_calibration": er_calibration,
    }


def _cohort_excess_context(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> _CohortExcessContext | None:
    price_returns: dict[str, float] = {}
    stale_count = 0
    for row in forward_rows:
        if row.horizon != horizon or row.status != "resolved" or row.price_return is None:
            continue
        price_returns[row.ticker] = row.price_return
        if row.stale_price:
            stale_count += 1

    population = [row for row in panel if row.in_population and row.ticker in price_returns]
    if len(population) < MIN_AXIS_SAMPLE:
        return None
    years = require_horizon(horizon).months / 12
    returns = {row.ticker: price_returns[row.ticker] for row in population}
    population_median_return = median(returns[row.ticker] for row in population)
    excess = {row.ticker: returns[row.ticker] - population_median_return for row in population}
    benchmark_return = price_returns.get(TOPIX_ETF_PROXY)
    return _CohortExcessContext(
        population=population,
        excess=excess,
        population_median_return=population_median_return,
        benchmark_price_return=benchmark_return,
        stale_price_count=stale_count,
        dividend_yield_coverage=0,
        horizon_years=years,
    )


def _coverage_status(rows: Sequence[ForwardReturnRow], name: str) -> str:
    values = {str(getattr(row, name)) for row in rows}
    if not values:
        return "unknown"
    if "unknown" in values:
        return "unknown"
    return "complete" if values == {"complete"} else "incomplete"


def _corporate_action_status(rows: Sequence[ForwardReturnRow], *, terminated: bool) -> str:
    """Say whether the price series reflects the actions that moved it.

    Two things are locally checkable: that split adjustment factors accompany
    every bar, and that no listing ended inside the window. A listing that ends
    is the merger / exchange class, whose consideration J-Quants documents as
    unadjusted, so it is named separately from a missing factor. Actions that
    neither adjust the series nor end the listing (a rights offering, say) have
    no local source at all — ``complete`` therefore means "no locally detectable
    unsupported action", and that residual is disclosed in the reference doc
    rather than hidden inside this value.
    """
    if terminated:
        return "terminated_listing"
    factor = _coverage_status(rows, "adjustment_factor_coverage")
    return "complete" if factor == "complete" else "unadjusted_factor"


def _evaluate_axis(
    spec: AxisSpec,
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object] | None:
    pairs = [
        (value * spec.direction, excess[row.ticker])
        for row in population
        if (value := getattr(row, spec.name)) is not None
    ]
    if len(pairs) < MIN_AXIS_SAMPLE:
        return None
    ic = _spearman(pairs)
    decile_values = _decile_values(pairs)
    deciles = [
        {"decile": index + 1, **_group_stats(values)} for index, values in enumerate(decile_values)
    ]
    best_values = decile_values[-1]
    worst_values = decile_values[0]
    best_median = median(best_values) if best_values else None
    worst_median = median(worst_values) if worst_values else None
    return {
        "n": len(pairs),
        "rank_ic": round(ic, 4) if ic is not None else None,
        "best_decile_median_excess": round(best_median, 6) if best_median is not None else None,
        "best_decile_mean_excess": round(fmean(best_values), 6) if best_values else None,
        "best_decile_trap_rate": (
            round(
                sum(1 for value in best_values if value < TRAP_EXCESS_THRESHOLD) / len(best_values),
                4,
            )
            if best_values
            else None
        ),
        "decile_spread_median": (
            round(best_median - worst_median, 6)
            if best_median is not None and worst_median is not None
            else None
        ),
        "deciles": deciles,
    }


def _evaluate_selection(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for rank_field in ("recommended_rank", "selection_rank"):
        for top_n in SELECTION_TOP_NS:
            values = [
                excess[row.ticker]
                for row in population
                if (rank := getattr(row, rank_field)) is not None and rank <= top_n
            ]
            key = f"{rank_field}_top{top_n}"
            result[key] = _group_stats(values)
    # 仮想 replay: E[r] 降順の順位付け (H3 の比較対象)。er_ranked は
    # pass_screen かつ er_annual 非 null の集合を並べ替える。er_population は
    # screen gate を外した母集団全体からの選抜 (gate 自体の付加価値の診断)。
    screen_passers = sorted(
        (row for row in population if row.pass_screen and row.er_annual is not None),
        key=lambda row: row.er_annual or 0.0,
        reverse=True,
    )
    population_by_er = sorted(
        (row for row in population if row.er_annual is not None),
        key=lambda row: row.er_annual or 0.0,
        reverse=True,
    )
    for top_n in SELECTION_TOP_NS:
        result[f"er_ranked_top{top_n}"] = _group_stats(
            [excess[row.ticker] for row in screen_passers[:top_n]]
        )
        result[f"er_population_top{top_n}"] = _group_stats(
            [excess[row.ticker] for row in population_by_er[:top_n]]
        )
    # 仮想 replay: reversion 主導の順位付け (#480 H-R1/H-R2)。er_ranked と同じ
    # screen 通過集合の key 差し替えで、carry 偏重が top-N の forward excess に
    # 与える影響を分離する。view score は Baibai App 表示 blend と同型 (品質 flag
    # 減点は panel に無いため除外) 。
    reversion_passers = sorted(
        (row for row in population if row.pass_screen and row.er_reversion_annual is not None),
        key=lambda row: row.er_reversion_annual or 0.0,
        reverse=True,
    )
    reversion_carry_passers = sorted(
        (row for row in population if row.pass_screen and row.er_reversion_annual is not None),
        key=_reversion_plus_capped_carry,
        reverse=True,
    )
    for top_n in SELECTION_TOP_NS:
        result[f"reversion_ranked_top{top_n}"] = _group_stats(
            [excess[row.ticker] for row in reversion_passers[:top_n]]
        )
        result[f"reversion_carry_ranked_top{top_n}"] = _group_stats(
            [excess[row.ticker] for row in reversion_carry_passers[:top_n]]
        )
    return result


def _reversion_plus_capped_carry(row: PanelRow) -> float:
    """Rank by reversion at full weight plus carry at half, capped at 15%/y.

    A ranking hypothesis under calibration, not a score any surface displays: carry
    is a holding-period return rather than a gap to close, and a carry beyond the cap
    is a special dividend or a data anomaly that would otherwise dominate the order.
    Its top-N excess return is compared against ranking by reversion alone.
    """

    return (row.er_reversion_annual or 0.0) + 0.5 * min(row.er_carry_annual or 0.0, 0.15)


def _evaluate_gates(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object]:
    """割安 decile 内で deterioration gate の通過 / 非通過を比較する。"""
    result: dict[str, object] = {}
    for axis_name in GATE_BASE_AXES:
        spec = next(spec for spec in AXES if spec.name == axis_name)
        rows = [
            (value * spec.direction, row)
            for row in population
            if (value := getattr(row, axis_name)) is not None
        ]
        if len(rows) < MIN_AXIS_SAMPLE:
            continue
        rows.sort(key=lambda item: item[0])
        cutoff = len(rows) - len(rows) // DECILES
        best_decile = [row for _, row in rows[cutoff:]]
        passed = [
            excess[row.ticker]
            for row in best_decile
            if row.operating_profit_yoy is None
            or row.operating_profit_yoy > DETERIORATION_THRESHOLD
        ]
        blocked = [
            excess[row.ticker]
            for row in best_decile
            if row.operating_profit_yoy is not None
            and row.operating_profit_yoy <= DETERIORATION_THRESHOLD
        ]
        result[axis_name] = {
            "gate_pass": _group_stats(passed),
            "gate_blocked": _group_stats(blocked),
        }
    return result


def _evaluate_reversion(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object]:
    """sector 中央値倍率への収束が horizon 内にどれだけ実現したかの座標。

    implied_upside = 中央値倍率まで戻った場合の価格上昇率 = -smg / (1 + smg)
    (smg = (own - median) / median)。upside の分位ごとに実現 excess を並べ、
    E[r] の収束年数較正 (WU3) の入力にする。
    """
    result: dict[str, object] = {}
    for axis_name in REVERSION_AXES:
        entries: list[tuple[float, float]] = []
        for row in population:
            smg = getattr(row, axis_name)
            if smg is None or smg >= 0 or smg <= -0.95:
                continue
            implied_upside = -smg / (1 + smg)
            entries.append((implied_upside, excess[row.ticker]))
        if len(entries) < MIN_AXIS_SAMPLE:
            continue
        entries.sort(key=lambda item: item[0])
        quintiles: list[dict[str, object]] = []
        step = len(entries) / 5
        for index in range(5):
            chunk = entries[int(index * step) : int((index + 1) * step)]
            if not chunk:
                continue
            quintiles.append(
                {
                    "mean_implied_upside": round(fmean(v for v, _ in chunk), 6),
                    "median_excess": round(median(e for _, e in chunk), 6),
                    "n": len(chunk),
                }
            )
        result[axis_name] = {"n": len(entries), "upside_quintiles": quintiles}
    return result


def _evaluate_er_calibration(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
    *,
    years: float,
) -> dict[str, object]:
    """価格収束 E[r] の予測 vs price-only 実現値を相対 basis で比較する。"""
    entries = [
        (row.er_reversion_annual, excess[row.ticker])
        for row in population
        if row.er_reversion_annual is not None
    ]
    if len(entries) < MIN_AXIS_SAMPLE:
        return {}
    population_median_reversion = median(reversion for reversion, _ in entries)
    entries.sort(key=lambda item: item[0])
    quintiles: list[dict[str, object]] = []
    step = len(entries) / 5
    for index in range(5):
        chunk = entries[int(index * step) : int((index + 1) * step)]
        if not chunk:
            continue
        predicted_excess = median(
            (reversion - population_median_reversion) * years for reversion, _ in chunk
        )
        realized_excess = median(realized for _, realized in chunk)
        quintiles.append(
            {
                "median_predicted_reversion_excess": round(predicted_excess, 6),
                "median_realized_price_excess": round(realized_excess, 6),
                "calibration_error": round(realized_excess - predicted_excess, 6),
                "n": len(chunk),
            }
        )
    return {
        "n": len(entries),
        "horizon_years": years,
        "prediction_basis": "er_reversion_annual_relative_to_population_median",
        "realized_basis": "price_return_relative_to_population_median",
        "calibration_error_basis": "realized_minus_predicted",
        "population_median_reversion_annual": round(population_median_reversion, 6),
        "er_quintiles": quintiles,
    }


def _group_stats(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"n": 0, "median_excess": None, "mean_excess": None, "trap_rate": None}
    return {
        "n": len(values),
        "median_excess": round(median(values), 6),
        "mean_excess": round(fmean(values), 6),
        "trap_rate": round(
            sum(1 for value in values if value < TRAP_EXCESS_THRESHOLD) / len(values), 4
        ),
    }


def _decile_values(pairs: Sequence[tuple[float, float]]) -> list[list[float]]:
    """decile 1 (index 0) = 軸の最悪側、decile 10 (index -1) = 最良 (割安) 側の excess 群。"""
    ordered = sorted(pairs, key=lambda item: item[0])
    step = len(ordered) / DECILES
    return [
        [excess for _, excess in ordered[int(index * step) : int((index + 1) * step)]]
        for index in range(DECILES)
    ]


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_IC_SAMPLE:
        return None
    xs = _average_ranks([x for x, _ in pairs])
    ys = _average_ranks([y for _, y in pairs])
    mean_x = fmean(xs)
    mean_y = fmean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (sqrt(var_x) * sqrt(var_y))


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        tie_end = index
        while tie_end + 1 < len(order) and values[order[tie_end + 1]] == values[order[index]]:
            tie_end += 1
        average_rank = (index + tie_end) / 2 + 1
        for position in range(index, tie_end + 1):
            ranks[order[position]] = average_rank
        index = tie_end + 1
    return ranks


def _aggregate(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    """cohort 横断の集計: 効果量の平均と cohort 勝率 (有意性は主張しない) 。"""
    if not cohorts:
        return {"cohort_count": 0}
    axis_summary: dict[str, object] = {}
    for spec in AXES:
        ics: list[float] = []
        best_medians: list[float] = []
        trap_rates: list[float] = []
        for cohort in cohorts:
            axes = cohort.get("axes")
            if not isinstance(axes, dict):
                continue
            axis = axes.get(spec.name)
            if not isinstance(axis, dict):
                continue
            ic = axis.get("rank_ic")
            if isinstance(ic, int | float):
                ics.append(float(ic))
            best = axis.get("best_decile_median_excess")
            if isinstance(best, int | float):
                best_medians.append(float(best))
            trap = axis.get("best_decile_trap_rate")
            if isinstance(trap, int | float):
                trap_rates.append(float(trap))
        if not ics and not best_medians:
            continue
        axis_summary[spec.name] = {
            "cohorts": len(ics),
            "mean_rank_ic": round(fmean(ics), 4) if ics else None,
            "ic_positive_share": (
                round(sum(1 for ic in ics if ic > 0) / len(ics), 4) if ics else None
            ),
            "mean_best_decile_median_excess": (
                round(fmean(best_medians), 6) if best_medians else None
            ),
            "best_decile_win_share": (
                round(sum(1 for value in best_medians if value > 0) / len(best_medians), 4)
                if best_medians
                else None
            ),
            "mean_best_decile_trap_rate": (round(fmean(trap_rates), 4) if trap_rates else None),
        }

    selection_summary: dict[str, object] = {}
    selection_key_set: set[str] = set()
    for cohort in cohorts:
        selection = cohort.get("selection")
        if isinstance(selection, dict):
            selection_key_set.update(selection)
    selection_keys = sorted(selection_key_set)
    for key in selection_keys:
        medians: list[float] = []
        means: list[float] = []
        traps: list[float] = []
        ns: list[int] = []
        for cohort in cohorts:
            selection = cohort.get("selection")
            if not isinstance(selection, dict):
                continue
            stats = selection.get(key)
            if not isinstance(stats, dict) or not stats.get("n"):
                continue
            ns.append(int(stats["n"]))
            if isinstance(stats.get("median_excess"), int | float):
                medians.append(float(stats["median_excess"]))
            if isinstance(stats.get("mean_excess"), int | float):
                means.append(float(stats["mean_excess"]))
            if isinstance(stats.get("trap_rate"), int | float):
                traps.append(float(stats["trap_rate"]))
        selection_summary[key] = {
            "cohorts": len(ns),
            "mean_n": round(fmean(ns), 1) if ns else 0,
            "mean_median_excess": round(fmean(medians), 6) if medians else None,
            "median_win_share": (
                round(sum(1 for value in medians if value > 0) / len(medians), 4)
                if medians
                else None
            ),
            "mean_mean_excess": round(fmean(means), 6) if means else None,
            "mean_trap_rate": round(fmean(traps), 4) if traps else None,
        }

    return {
        "cohort_count": len(cohorts),
        "axes": axis_summary,
        "selection": selection_summary,
    }
