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

from baibai_loop.market.benchmark import TOPIX_ETF_PROXY

from .forward import ForwardReturnRow
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

# total return 近似の配当 accrual: entry 時点の直近実績年間 DPS 利回りを保有年数
# で按分して price return に加算する (権利落ち月の特定はしない)。銘柄横断の比較が
# 目的なので、支払月の 1-2 か月のずれは cross-section にほぼ影響しない。
# dividend_yield 欠損 (開示なし) は 0 として扱い、coverage を cohort に開示する。
HORIZON_YEARS: Mapping[str, float] = {"3m": 0.25, "6m": 0.5, "12m": 1.0}


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
    sector_subset: Sequence[str] | None = None,
    sector_subset_axes: Sequence[str] | None = None,
) -> dict[str, object]:
    """Evaluate all cohorts and aggregate per horizon.

    ``panels`` / ``forwards`` は asof (ISO 文字列) を key にする。
    """
    sector_subset_values = _normalize_sector_subset(sector_subset)
    sector_subset_axis_values = _normalize_axis_subset(sector_subset_axes)
    per_horizon: dict[str, object] = {}
    for horizon in horizons:
        cohort_results: list[dict[str, object]] = []
        sector_subset_results: list[dict[str, object]] = []
        for asof in sorted(panels):
            cohort = _evaluate_cohort(
                panels[asof], forwards.get(asof, ()), asof=asof, horizon=horizon
            )
            if cohort is not None:
                cohort_results.append(cohort)
                if sector_subset_values:
                    axes = cohort.get("axes")
                    sector_subset_cohort = _evaluate_sector_subset_cohort(
                        panels[asof],
                        forwards.get(asof, ()),
                        asof=asof,
                        horizon=horizon,
                        sectors=sector_subset_values,
                        axis_names=sector_subset_axis_values,
                        all_population_axes=axes if isinstance(axes, dict) else {},
                    )
                    if sector_subset_cohort is not None:
                        sector_subset_results.append(sector_subset_cohort)
        horizon_result: dict[str, object] = {
            "cohorts": cohort_results,
            "aggregate": _aggregate(cohort_results),
        }
        if sector_subset_values:
            horizon_result["sector_subset_diagnostics"] = {
                "sectors": list(sector_subset_values),
                "axes": list(sector_subset_axis_values) if sector_subset_axis_values else "all",
                "cohorts": sector_subset_results,
                "aggregate": _aggregate_sector_subset(sector_subset_results),
            }
        per_horizon[horizon] = horizon_result
    return per_horizon


def _evaluate_cohort(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    asof: str,
    horizon: str,
) -> dict[str, object] | None:
    context = _cohort_excess_context(panel, forward_rows, horizon=horizon)
    if context is None:
        return None
    population = context.population
    excess = context.excess

    axes: dict[str, object] = {}
    for spec in AXES:
        axes_result = _evaluate_axis(spec, population, excess)
        if axes_result is not None:
            axes[spec.name] = axes_result

    return {
        "asof": asof,
        "horizon": horizon,
        "population_resolved": len(population),
        "dividend_yield_coverage": context.dividend_yield_coverage,
        "population_median_return": round(context.population_median_return, 6),
        "benchmark_price_return": (
            round(context.benchmark_price_return, 6)
            if context.benchmark_price_return is not None
            else None
        ),
        "stale_price_count": context.stale_price_count,
        "axes": axes,
        "selection": _evaluate_selection(population, excess),
        "gates": _evaluate_gates(population, excess),
        "reversion": _evaluate_reversion(population, excess),
        "er_calibration": _evaluate_er_calibration(population, excess, years=context.horizon_years),
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
        if row.horizon != horizon or not row.resolved or row.price_return is None:
            continue
        price_returns[row.ticker] = row.price_return
        if row.stale_price:
            stale_count += 1

    population = [row for row in panel if row.in_population and row.ticker in price_returns]
    if len(population) < MIN_AXIS_SAMPLE:
        return None
    years = HORIZON_YEARS.get(horizon, 0.0)
    returns = {
        row.ticker: price_returns[row.ticker] + (row.dividend_yield or 0.0) * years
        for row in population
    }
    dividend_coverage = sum(1 for row in population if row.dividend_yield is not None)
    population_median_return = median(returns[row.ticker] for row in population)
    excess = {row.ticker: returns[row.ticker] - population_median_return for row in population}
    benchmark_return = price_returns.get(TOPIX_ETF_PROXY)
    return _CohortExcessContext(
        population=population,
        excess=excess,
        population_median_return=population_median_return,
        benchmark_price_return=benchmark_return,
        stale_price_count=stale_count,
        dividend_yield_coverage=dividend_coverage,
        horizon_years=years,
    )


def _evaluate_sector_subset_cohort(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    asof: str,
    horizon: str,
    sectors: Sequence[str],
    axis_names: Sequence[str],
    all_population_axes: Mapping[str, object],
) -> dict[str, object] | None:
    """指定 sector subset の軸診断を、全母集団 excess と同じ基準で計算する。"""
    context = _cohort_excess_context(panel, forward_rows, horizon=horizon)
    if context is None:
        return None
    population = context.population
    excess = context.excess

    sector_set = set(sectors)
    subset = [row for row in population if row.sector_33 in sector_set]
    axes: dict[str, object] = {}
    for spec in _axis_specs(axis_names):
        subset_axis = _evaluate_sector_subset_axis(spec, subset, excess)
        if subset_axis is None:
            continue
        all_axis = all_population_axes.get(spec.name)
        all_trap = (
            _numeric(all_axis.get("best_decile_trap_rate")) if isinstance(all_axis, dict) else None
        )
        subset_trap = _numeric(subset_axis.get("best_decile_trap_rate"))
        axes[spec.name] = {
            "n": subset_axis["n"],
            "rank_ic": subset_axis["rank_ic"],
            "best_decile_trap_rate": subset_axis["best_decile_trap_rate"],
            "all_population_best_decile_trap_rate": (
                round(all_trap, 4) if all_trap is not None else None
            ),
            "best_decile_trap_rate_delta_vs_all_population": (
                round(subset_trap - all_trap, 4)
                if subset_trap is not None and all_trap is not None
                else None
            ),
        }
    return {
        "asof": asof,
        "horizon": horizon,
        "all_population_resolved": len(population),
        "subset_population_resolved": len(subset),
        "axes": axes,
    }


def _evaluate_sector_subset_axis(
    spec: AxisSpec,
    subset: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object] | None:
    """小さめの sector subset 向け軸診断。

    全母集団の axis 評価は decile の安定性を優先して 100 件を下限にする。
    sector subset は金融のように月次 cohort が 100 件未満になり得るため、
    IC と top decile trap の最低限の診断に絞り、IC 計算下限の 30 件で出す。
    """
    pairs = [
        (value * spec.direction, excess[row.ticker])
        for row in subset
        if (value := getattr(row, spec.name)) is not None
    ]
    if len(pairs) < MIN_IC_SAMPLE:
        return None
    ic = _spearman(pairs)
    best_values = _decile_values(pairs)[-1]
    return {
        "n": len(pairs),
        "rank_ic": round(ic, 4) if ic is not None else None,
        "best_decile_trap_rate": (
            round(
                sum(1 for value in best_values if value < TRAP_EXCESS_THRESHOLD) / len(best_values),
                4,
            )
            if best_values
            else None
        ),
    }


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
    return result


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
    """機械 E[r] の予測 vs 実現の座標。

    quintile ごとに「予測リターン (er_annual x 保有年数)」と「実現 median excess」を
    並べる。excess は対母集団中央値なので、予測側も母集団平均 E[r] を引いた相対値で
    比較できるよう mean_er_predicted をそのまま出し、読み手が両方を見られる形にする。
    """
    entries = [
        (row.er_annual, excess[row.ticker]) for row in population if row.er_annual is not None
    ]
    if len(entries) < MIN_AXIS_SAMPLE:
        return {}
    entries.sort(key=lambda item: item[0])
    quintiles: list[dict[str, object]] = []
    step = len(entries) / 5
    for index in range(5):
        chunk = entries[int(index * step) : int((index + 1) * step)]
        if not chunk:
            continue
        quintiles.append(
            {
                "mean_er_predicted": round(fmean(er for er, _ in chunk) * years, 6),
                "median_excess": round(median(e for _, e in chunk), 6),
                "n": len(chunk),
            }
        )
    return {"n": len(entries), "horizon_years": years, "er_quintiles": quintiles}


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


def _aggregate_sector_subset(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    """sector subset 診断の cohort 横断集計。design/confirm 表へ転記する粒度に絞る。"""
    if not cohorts:
        return {"cohort_count": 0}
    subset_ns = [
        int(value)
        for cohort in cohorts
        if isinstance(value := cohort.get("subset_population_resolved"), int)
    ]
    axis_summary: dict[str, object] = {}
    for spec in AXES:
        ns: list[int] = []
        ics: list[float] = []
        subset_traps: list[float] = []
        all_population_traps: list[float] = []
        trap_deltas: list[float] = []
        for cohort in cohorts:
            axes = cohort.get("axes")
            if not isinstance(axes, dict):
                continue
            axis = axes.get(spec.name)
            if not isinstance(axis, dict):
                continue
            n = axis.get("n")
            if isinstance(n, int):
                ns.append(n)
            if (ic := _numeric(axis.get("rank_ic"))) is not None:
                ics.append(ic)
            if (trap := _numeric(axis.get("best_decile_trap_rate"))) is not None:
                subset_traps.append(trap)
            if (
                all_population_trap := _numeric(axis.get("all_population_best_decile_trap_rate"))
            ) is not None:
                all_population_traps.append(all_population_trap)
            if (
                trap_delta := _numeric(axis.get("best_decile_trap_rate_delta_vs_all_population"))
            ) is not None:
                trap_deltas.append(trap_delta)
        if not ns:
            continue
        axis_summary[spec.name] = {
            "cohorts": len(ns),
            "mean_n": round(fmean(ns), 1),
            "mean_rank_ic": round(fmean(ics), 4) if ics else None,
            "ic_positive_share": (
                round(sum(1 for ic in ics if ic > 0) / len(ics), 4) if ics else None
            ),
            "mean_best_decile_trap_rate": (round(fmean(subset_traps), 4) if subset_traps else None),
            "mean_all_population_best_decile_trap_rate": (
                round(fmean(all_population_traps), 4) if all_population_traps else None
            ),
            "mean_best_decile_trap_rate_delta_vs_all_population": (
                round(fmean(trap_deltas), 4) if trap_deltas else None
            ),
        }
    return {
        "cohort_count": len(cohorts),
        "mean_subset_population_resolved": round(fmean(subset_ns), 1) if subset_ns else 0,
        "axes": axis_summary,
    }


def _normalize_sector_subset(sector_subset: Sequence[str] | None) -> tuple[str, ...]:
    if not sector_subset:
        return ()
    sectors: list[str] = []
    seen: set[str] = set()
    for raw in sector_subset:
        sector = raw.strip()
        if not sector or sector in seen:
            continue
        seen.add(sector)
        sectors.append(sector)
    return tuple(sectors)


def _normalize_axis_subset(axis_names: Sequence[str] | None) -> tuple[str, ...]:
    if not axis_names:
        return ()
    known = {spec.name for spec in AXES}
    axes: list[str] = []
    seen: set[str] = set()
    for raw in axis_names:
        axis = raw.strip()
        if not axis or axis in seen or axis not in known:
            continue
        seen.add(axis)
        axes.append(axis)
    return tuple(axes)


def _axis_specs(axis_names: Sequence[str]) -> tuple[AxisSpec, ...]:
    if not axis_names:
        return AXES
    selected = set(axis_names)
    return tuple(spec for spec in AXES if spec.name in selected)


def _numeric(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
