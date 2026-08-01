"""較正の評価指標: rank IC・decile・selection replay・トラップ率・gate 条件付き spread・収束実現。

統計の誠実性 (docs/doctrine.md の計測経路):
- cohort (月次 asof) は forward 窓が重複し独立でないため、有意性検定は行わず
  「効果量 (median/mean excess) と cohort 勝率」で報告する。
- 超過リターンの一次基準は流動性母集団の中央値 (選定スキルの直接計測) 。
  benchmark ETF は市況文脈の参考値。
- 累積リターン・年率・シャープ等の track record 系は出力しない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import fmean, median
from typing import cast

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY

from .forward import TOTAL_RETURN_BASIS, ForwardReturnRow
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

QUALITY_HIGH_MIN_COUNT = 5
QUALITY_LOW_MAX_COUNT = 3
MIN_QUALITY_CONTROL_GROUP = 5
QUALITY_CONTROL_FIELDS: tuple[str, ...] = (
    "market_cap_oku",
    "avg_turnover_oku",
    "pbr",
    "per_trailing",
    "dividend_yield",
    "price_change_60d",
)
RETURN_CHANGE_CONTROL_FIELDS: tuple[str, ...] = (
    "dividend_yield",
    "per_trailing",
    "pbr",
    "market_cap_oku",
    "avg_turnover_oku",
    "price_change_60d",
)
RETURN_CHANGE_COMPONENT_FIELDS: tuple[str, ...] = (
    "dps_streak_up",
    "dps_guidance_up",
    "dividend_initiation",
)
MARGIN_DEADLINE_SHARE_EXCLUDE_AT_OR_ABOVE = 0.75
MARGIN_HYPOTHESIS_AXES: tuple[str, ...] = (
    "margin_short_to_adv",
    "margin_long_to_adv_mcap_quintile_percentile",
)
MARGIN_CONTROL_FIELDS: tuple[str, ...] = (
    "market_cap_oku",
    "avg_turnover_oku",
    "per_trailing",
    "dividend_yield",
    "close",
    "price_change_60d",
    "realized_volatility_60d",
    "sector_33",
)


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


@dataclass(slots=True)
class _QualityControlAccumulator:
    median_deltas: list[float]
    trap_deltas: list[float]
    matched_weight: int = 0
    cohorts: int = 0


@dataclass(slots=True)
class _ComponentAccumulator:
    median_deltas: list[float]
    mean_deltas: list[float]
    trap_deltas: list[float]
    true_n: int = 0
    false_n: int = 0


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
    AxisSpec(name="quality_signal_count", direction=1),
    AxisSpec(name="dps_yoy_latest", direction=1),
    AxisSpec(name="share_count_reduction_streak", direction=1),
    AxisSpec(name="price_change_60d", direction=-1),
    AxisSpec(name="gap_from_52w_low", direction=-1),
    # 需給軸。方向は事前登録として先に宣言する (計測結果を見てから向きを決めない)。
    # margin_long_to_adv / margin_long_share は「買い方が混雑しているほど将来リターンは
    # 劣後する」= 低いほど良い。margin_long_delta_26w は「半年で信用買いが減った
    # 後ほど良い」= 低いほど良い。margin_std_long_share は「期日を持つ overhang が
    # 多いほど劣後する」= 低いほど良い。いずれも計測前の仮説であり、採否は
    # dated report の採用基準で決める。
    AxisSpec(name="margin_long_to_adv", direction=-1),
    AxisSpec(name="margin_short_to_adv", direction=-1),
    AxisSpec(name="margin_long_share", direction=-1),
    AxisSpec(name="margin_long_delta_26w", direction=-1),
    AxisSpec(name="margin_std_long_share", direction=-1),
    AxisSpec(name="margin_long_to_adv_mcap_quintile_percentile", direction=-1),
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
    future_horizon = [row for row in unresolved if row.status == "unresolved_future_horizon"]
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
        # Whether those exclusions could have produced the cohort's conclusions.
        "delisting_exclusion": delisting_exclusion_sensitivity(
            panel, forward_rows, horizon=horizon
        ),
        "priced_master_without_universe": priced_master_without_universe_sensitivity(
            panel, forward_rows, horizon=horizon
        ),
        "future_horizon_count": len(future_horizon),
        # The classes above are an allowlist, so a status none of them names would
        # pass without a blocker. The residual makes that impossible.
        "unclassified_unresolved_count": (
            len(unresolved)
            - len(entry_not_listed)
            - len(entry_price_gap)
            - len(unpriced_exit)
            - len(future_horizon)
        ),
        "candidate_partition_complete": observed_tickers == expected_tickers,
        "candidate_forward_missing_count": len(expected_tickers - observed_tickers),
        "candidate_forward_extra_count": len(observed_tickers - expected_tickers),
        "adjustment_factor_coverage": _adjustment_factor_status(candidate_rows),
        "total_return_resolved_count": sum(
            1 for row in candidate_rows if row.total_return_status == "resolved"
        ),
        "total_return_status_counts": _status_counts(
            row.total_return_status for row in candidate_rows
        ),
    }
    if context is None:
        return {
            "asof": asof,
            "horizon": horizon,
            "metric_basis": "price_return_only",
            "metric_bases": ["price_return_only", "fy_actual_dividend_total_return"],
            "coverage": coverage,
            "metric_calculation_status": "unresolved",
            "axes": {},
            "selection": {},
            "gates": {},
            "reversion": {},
            "crowded_value": {},
            "quality_interaction": {},
            "shareholder_return_change": {},
            "margin_deadline_gate": {},
            "margin_supply_demand_hypotheses": {},
            "er_calibration": {},
            "er_level_calibration": {},
        }
    population = context.population
    excess = context.excess

    axes: dict[str, object] = {}
    for spec in AXES:
        axes_result = _evaluate_axis(spec, population, excess)
        if axes_result is not None:
            axes[spec.name] = axes_result
    selection = _evaluate_selection(population, excess)
    margin_deadline_gate = _evaluate_margin_deadline_gate(panel, excess)
    margin_hypotheses = _evaluate_margin_supply_demand_hypotheses(population, excess)
    quality_interaction = _evaluate_quality_interaction(population, excess)
    return_change = _evaluate_shareholder_return_change(population, excess)
    er_calibration = _evaluate_er_calibration(
        population, excess, years=require_horizon(horizon).months / 12
    )
    er_level_calibration = _evaluate_er_level_calibration(population, forward_rows, horizon=horizon)
    metric_statuses = {
        key: ("eligible" if isinstance(value, dict) and value.get("n", 0) else "unresolved")
        for key, value in selection.items()
    }
    metric_statuses["er_calibration"] = "eligible" if er_calibration else "unresolved"
    metric_statuses["er_level_calibration"] = "eligible" if er_level_calibration else "unresolved"
    metric_statuses["quality_interaction"] = (
        "eligible" if quality_interaction.get("eligible_n", 0) else "unresolved"
    )
    metric_statuses["shareholder_return_change"] = (
        "eligible" if return_change.get("eligible_n", 0) else "unresolved"
    )
    gate_top10 = margin_deadline_gate.get("top10")
    metric_statuses["margin_deadline_gate_top10"] = (
        "eligible"
        if isinstance(gate_top10, dict)
        and gate_top10.get("complete") is True
        and isinstance(gate_top10.get("baseline"), dict)
        and gate_top10["baseline"].get("n", 0)
        and isinstance(gate_top10.get("variant"), dict)
        and gate_top10["variant"].get("n", 0)
        else "unresolved"
    )

    return {
        "asof": asof,
        "horizon": horizon,
        "population_resolved": len(population),
        "metric_basis": "price_return_only",
        "metric_bases": ["price_return_only", "fy_actual_dividend_total_return"],
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
        "crowded_value": _evaluate_crowded_value(population, excess),
        "quality_interaction": quality_interaction,
        "shareholder_return_change": return_change,
        "margin_deadline_gate": margin_deadline_gate,
        "margin_supply_demand_hypotheses": margin_hypotheses,
        "er_calibration": er_calibration,
        "er_level_calibration": er_level_calibration,
    }


def _status_counts(statuses: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _resolved_price_returns(
    forward_rows: Sequence[ForwardReturnRow], *, horizon: str
) -> tuple[dict[str, float], int]:
    price_returns: dict[str, float] = {}
    stale_count = 0
    for row in forward_rows:
        if row.horizon != horizon or row.status != "resolved" or row.price_return is None:
            continue
        price_returns[row.ticker] = row.price_return
        if row.stale_price:
            stale_count += 1
    return price_returns, stale_count


def _context_from_returns(
    panel: Sequence[PanelRow],
    price_returns: Mapping[str, float],
    *,
    horizon: str,
    stale_count: int,
) -> _CohortExcessContext | None:
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


def _cohort_excess_context(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> _CohortExcessContext | None:
    price_returns, stale_count = _resolved_price_returns(forward_rows, horizon=horizon)
    return _context_from_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)


# Names whose series ends inside the window carry no exit value, so they leave the
# cohort silently, and the numbers the cohort reports are computed from the survivors
# alone. Rather than block every cohort that has one, that reported conclusion is
# compared against itself recomputed with the missing names given a value from each
# end of the plausible range: a total loss, and the return the rest of the cohort had.
# The reported value is part of the comparison because it is the one the authority
# gate consumes: a conclusion that holds under both imputations but not as reported is
# precisely a conclusion the exclusion produced.
_DELISTING_IMPUTATIONS: tuple[str, ...] = ("total_loss", "neutral")
_TOTAL_LOSS_RETURN = -1.0
_SENSITIVITY_METRICS: tuple[str, ...] = (
    "recommended_rank_top5",
    "recommended_rank_top10",
    "er_calibration",
)
OPTIONAL_SENSITIVITY_METRICS: tuple[str, ...] = ("margin_deadline_gate_top10",)
_ALL_SENSITIVITY_METRICS = (*_SENSITIVITY_METRICS, *OPTIONAL_SENSITIVITY_METRICS)


def _direction_signs(
    context: _CohortExcessContext,
    panel: Sequence[PanelRow],
    *,
    horizon: str,
) -> dict[str, float | None]:
    """The sign-bearing quantity of each conclusion the authority gate reads."""
    selection = _evaluate_selection(context.population, context.excess)
    signs: dict[str, float | None] = {}
    for key in ("recommended_rank_top5", "recommended_rank_top10"):
        group = selection.get(key)
        value = group.get("median_excess") if isinstance(group, dict) else None
        signs[key] = value if isinstance(value, int | float) else None
    calibration = _evaluate_er_calibration(
        context.population, context.excess, years=require_horizon(horizon).months / 12
    )
    quintiles = calibration.get("er_quintiles")
    if isinstance(quintiles, list) and len(quintiles) >= 2:
        top = quintiles[-1].get("median_realized_price_excess")
        bottom = quintiles[0].get("median_realized_price_excess")
        signs["er_calibration"] = (
            top - bottom
            if isinstance(top, int | float) and isinstance(bottom, int | float)
            else None
        )
    else:
        signs["er_calibration"] = None
    gate = _evaluate_margin_deadline_gate(panel, context.excess).get("top10")
    gate_delta = gate.get("median_excess_delta") if isinstance(gate, dict) else None
    signs["margin_deadline_gate_top10"] = (
        float(gate_delta) if isinstance(gate_delta, int | float) else None
    )
    return signs


def _signs_for_returns(
    panel: Sequence[PanelRow],
    price_returns: Mapping[str, float],
    *,
    horizon: str,
    stale_count: int,
) -> dict[str, float | None]:
    context = _context_from_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)
    if context is None:
        return dict.fromkeys(_ALL_SENSITIVITY_METRICS)
    return _direction_signs(context, panel, horizon=horizon)


def delisting_exclusion_sensitivity(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> dict[str, object]:
    """Say whether the names without an exit value could have produced the conclusions.

    The two imputations bracket the exclusion from below: a delisted name is given
    either nothing or what the cohort as a whole returned. A takeover settles above
    the neutral case, so the bracket bounds how far the exclusion can have pushed a
    conclusion down, not up; that limit is stated in the pre-registration rather than
    hidden here.
    """
    price_returns, stale_count = _resolved_price_returns(forward_rows, horizon=horizon)
    in_population = {row.ticker for row in panel if row.in_population}
    excluded = sorted(
        {
            row.ticker
            for row in forward_rows
            if row.horizon == horizon
            and row.status in {"unresolved_missing_exit", "unresolved_stale_exit"}
            and row.ticker in in_population
        }
    )
    if not excluded:
        return {
            "excluded_count": 0,
            "direction_stable": True,
            "metric_direction_stable": dict.fromkeys(_ALL_SENSITIVITY_METRICS, True),
            "as_reported": {},
            "imputations": {},
        }

    neutral = median(price_returns.values()) if price_returns else 0.0
    as_reported = _signs_for_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)
    imputed: dict[str, dict[str, float | None]] = {}
    for name in _DELISTING_IMPUTATIONS:
        value = _TOTAL_LOSS_RETURN if name == "total_loss" else neutral
        augmented = dict(price_returns)
        for ticker in excluded:
            augmented[ticker] = value
        imputed[name] = _signs_for_returns(
            panel, augmented, horizon=horizon, stale_count=stale_count
        )

    metric_stability = _metric_direction_stability(as_reported, imputed)
    stable = all(metric_stability[metric] for metric in _SENSITIVITY_METRICS)
    return {
        "excluded_count": len(excluded),
        "neutral_return": round(neutral, 6),
        "direction_stable": stable,
        "metric_direction_stable": metric_stability,
        "as_reported": as_reported,
        "imputations": imputed,
    }


def priced_master_without_universe_sensitivity(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> dict[str, object]:
    """Bound the conclusions' sensitivity to priced rows the screen could not evaluate.

    These rows have observed forward returns but no valuation metrics or rank. They
    already contribute to the reported population median. The two imputations alter
    only that return contribution; they do not invent a rank or E[r] value.
    """
    targets = sorted(
        row.ticker
        for row in panel
        if row.population_coverage_status == "priced_master_without_universe"
    )
    if not targets:
        return {
            "excluded_count": 0,
            "resolved_target_count": 0,
            "resolution_complete": True,
            "direction_stable": True,
            "metric_direction_stable": dict.fromkeys(_ALL_SENSITIVITY_METRICS, True),
            "as_reported": {},
            "imputations": {},
        }

    price_returns, stale_count = _resolved_price_returns(forward_rows, horizon=horizon)
    resolved_targets = [ticker for ticker in targets if ticker in price_returns]
    population_tickers = {row.ticker for row in panel if row.in_population}
    population_returns = [
        value for ticker, value in price_returns.items() if ticker in population_tickers
    ]
    neutral = median(population_returns) if population_returns else 0.0
    as_reported = _signs_for_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)
    imputed: dict[str, dict[str, float | None]] = {}
    for name, value in (("total_loss", _TOTAL_LOSS_RETURN), ("neutral", neutral)):
        augmented = dict(price_returns)
        for ticker in targets:
            augmented[ticker] = value
        imputed[name] = _signs_for_returns(
            panel, augmented, horizon=horizon, stale_count=stale_count
        )

    metric_stability = _metric_direction_stability(as_reported, imputed)
    stable = len(resolved_targets) == len(targets) and all(
        metric_stability[metric] for metric in _SENSITIVITY_METRICS
    )
    return {
        "excluded_count": len(targets),
        "resolved_target_count": len(resolved_targets),
        "resolution_complete": len(resolved_targets) == len(targets),
        "neutral_return": round(neutral, 6),
        "direction_stable": stable,
        "metric_direction_stable": metric_stability,
        "as_reported": as_reported,
        "imputations": imputed,
    }


def _metric_direction_stability(
    as_reported: Mapping[str, float | None],
    imputed: Mapping[str, Mapping[str, float | None]],
) -> dict[str, bool]:
    stability: dict[str, bool] = {}
    for metric in _ALL_SENSITIVITY_METRICS:
        values = [
            as_reported[metric],
            *(imputed[name][metric] for name in _DELISTING_IMPUTATIONS),
        ]
        present = [value for value in values if value is not None]
        # No conclusion under any case cannot have been produced by the exclusion;
        # whether the metric is reportable is handled by metric_statuses.
        stability[metric] = not present or (
            len(present) == len(values) and len({value > 0 for value in present}) == 1
        )
    return stability


def _adjustment_factor_status(rows: Sequence[ForwardReturnRow]) -> str:
    """Say whether every bar behind these rows carried a split adjustment factor.

    This is the only corporate-action question the bar store can answer. Actions
    it does not adjust — a merger's consideration, a rights offering — leave no
    local trace, so the residual is disclosed in the reference doc instead of
    being folded into this value. ``unknown`` means no row reported a factor at
    all and is kept distinct from a factor that is present but incomplete.
    """
    values = {str(row.adjustment_factor_coverage) for row in rows}
    if not values or "unknown" in values:
        return "unknown"
    return "complete" if values == {"complete"} else "incomplete"


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


def _evaluate_quality_interaction(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    er_rows = sorted(
        (row for row in population if row.er_annual is not None),
        key=lambda row: (row.er_annual or 0.0, row.ticker),
    )
    if not er_rows:
        return _empty_quality_interaction()
    top_start = int((DECILES - 1) * len(er_rows) / DECILES)
    er_top_decile = er_rows[top_start:]
    eligible = [row for row in er_top_decile if row.quality_signal_count is not None]
    high = [row for row in eligible if (row.quality_signal_count or 0) >= QUALITY_HIGH_MIN_COUNT]
    low = [row for row in eligible if (row.quality_signal_count or 0) <= QUALITY_LOW_MAX_COUNT]
    high_stats = _group_stats([excess[row.ticker] for row in high])
    low_stats = _group_stats([excess[row.ticker] for row in low])
    controls: dict[str, object] = {}
    result: dict[str, object] = {
        "er_top_decile_n": len(er_top_decile),
        "eligible_n": len(eligible),
        "high": high_stats,
        "low": low_stats,
        **_quality_group_deltas(high_stats, low_stats),
        "controls": controls,
    }
    for field_name in QUALITY_CONTROL_FIELDS:
        controls[field_name] = _stratified_quality_control(high, low, excess, field_name=field_name)
    return result


def _empty_quality_interaction() -> dict[str, object]:
    empty = _group_stats(())
    return {
        "er_top_decile_n": 0,
        "eligible_n": 0,
        "high": empty,
        "low": empty,
        "median_excess_delta": None,
        "mean_excess_delta": None,
        "trap_rate_delta": None,
        "controls": {
            field_name: {
                "strata_used": 0,
                "matched_weight": 0,
                "stratified_median_excess_delta": None,
                "stratified_trap_rate_delta": None,
            }
            for field_name in QUALITY_CONTROL_FIELDS
        },
    }


def _quality_group_deltas(
    high: Mapping[str, object], low: Mapping[str, object]
) -> dict[str, float | None]:
    return {
        "median_excess_delta": _rounded_difference(
            high.get("median_excess"), low.get("median_excess")
        ),
        "mean_excess_delta": _rounded_difference(high.get("mean_excess"), low.get("mean_excess")),
        "trap_rate_delta": _rounded_difference(high.get("trap_rate"), low.get("trap_rate")),
    }


def _rounded_difference(high: object, low: object) -> float | None:
    if not isinstance(high, int | float) or not isinstance(low, int | float):
        return None
    return round(float(high) - float(low), 6)


def _stratified_quality_control(
    high: Sequence[PanelRow],
    low: Sequence[PanelRow],
    excess: Mapping[str, float],
    *,
    field_name: str,
) -> dict[str, object]:
    rows = [row for row in (*high, *low) if getattr(row, field_name) is not None]
    if not rows:
        return {
            "strata_used": 0,
            "matched_weight": 0,
            "stratified_median_excess_delta": None,
            "stratified_trap_rate_delta": None,
        }
    split = median(float(getattr(row, field_name)) for row in rows)
    weighted_median_delta = 0.0
    weighted_trap_delta = 0.0
    matched_weight = 0
    strata_used = 0
    high_tickers = {row.ticker for row in high}
    for lower_side in (True, False):
        stratum = [row for row in rows if (float(getattr(row, field_name)) <= split) is lower_side]
        high_values = [excess[row.ticker] for row in stratum if row.ticker in high_tickers]
        low_values = [excess[row.ticker] for row in stratum if row.ticker not in high_tickers]
        if (
            len(high_values) < MIN_QUALITY_CONTROL_GROUP
            or len(low_values) < MIN_QUALITY_CONTROL_GROUP
        ):
            continue
        high_stats = _group_stats(high_values)
        low_stats = _group_stats(low_values)
        deltas = _quality_group_deltas(high_stats, low_stats)
        median_delta = deltas["median_excess_delta"]
        trap_delta = deltas["trap_rate_delta"]
        if median_delta is None or trap_delta is None:
            continue
        weight = min(len(high_values), len(low_values))
        matched_weight += weight
        strata_used += 1
        weighted_median_delta += weight * median_delta
        weighted_trap_delta += weight * trap_delta
    return {
        "strata_used": strata_used,
        "matched_weight": matched_weight,
        "stratified_median_excess_delta": (
            round(weighted_median_delta / matched_weight, 6) if matched_weight else None
        ),
        "stratified_trap_rate_delta": (
            round(weighted_trap_delta / matched_weight, 6) if matched_weight else None
        ),
    }


def _evaluate_shareholder_return_change(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    per_rows = sorted(
        (row for row in population if row.per_trailing is not None and row.per_trailing > 0),
        key=lambda row: (row.per_trailing or 0.0, row.ticker),
    )
    band_end = int(2 * len(per_rows) / DECILES)
    low_valuation = per_rows[:band_end]
    eligible = [row for row in low_valuation if row.shareholder_return_change is not None]
    change = [row for row in eligible if row.shareholder_return_change is True]
    no_change = [row for row in eligible if row.shareholder_return_change is False]
    change_stats = _group_stats([excess[row.ticker] for row in change])
    no_change_stats = _group_stats([excess[row.ticker] for row in no_change])
    controls = {
        field_name: _stratified_quality_control(
            change,
            no_change,
            excess,
            field_name=field_name,
        )
        for field_name in RETURN_CHANGE_CONTROL_FIELDS
    }
    components: dict[str, object] = {}
    for field_name in RETURN_CHANGE_COMPONENT_FIELDS:
        observed = [row for row in low_valuation if getattr(row, field_name) is not None]
        positive = [row for row in observed if getattr(row, field_name) is True]
        negative = [row for row in observed if getattr(row, field_name) is False]
        positive_stats = _group_stats([excess[row.ticker] for row in positive])
        negative_stats = _group_stats([excess[row.ticker] for row in negative])
        components[field_name] = {
            "eligible_n": len(observed),
            "true": positive_stats,
            "false": negative_stats,
            **_quality_group_deltas(positive_stats, negative_stats),
        }
    return {
        "low_valuation_n": len(low_valuation),
        "eligible_n": len(eligible),
        "change": change_stats,
        "no_change": no_change_stats,
        **_quality_group_deltas(change_stats, no_change_stats),
        "controls": controls,
        "components": components,
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


def _evaluate_margin_deadline_gate(
    panel: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    """Replay the fixed deadline-heavy exclusion without changing candidate facts."""
    ranked = sorted(
        (row for row in panel if row.in_population and row.recommended_rank is not None),
        key=lambda row: (row.recommended_rank or 0, row.ticker),
    )
    variant = [
        row
        for row in ranked
        if row.margin_std_long_share is None
        or row.margin_std_long_share < MARGIN_DEADLINE_SHARE_EXCLUDE_AT_OR_ABOVE
    ]
    result: dict[str, object] = {
        "excluded_count": len(ranked) - len(variant),
    }
    for top_n in (5, 10):
        baseline_rows = ranked[:top_n]
        variant_rows = variant[:top_n]
        baseline = _group_stats(
            [excess[row.ticker] for row in baseline_rows if row.ticker in excess]
        )
        gated = _group_stats([excess[row.ticker] for row in variant_rows if row.ticker in excess])
        baseline_median = baseline.get("median_excess")
        gated_median = gated.get("median_excess")
        baseline_trap = baseline.get("trap_rate")
        gated_trap = gated.get("trap_rate")
        result[f"top{top_n}"] = {
            "complete": len(baseline_rows) == top_n and len(variant_rows) == top_n,
            "changed": tuple(row.ticker for row in baseline_rows)
            != tuple(row.ticker for row in variant_rows),
            "baseline": baseline,
            "variant": gated,
            "median_excess_delta": _rounded_delta(gated_median, baseline_median),
            "trap_rate_delta": _rounded_delta(gated_trap, baseline_trap),
        }
    result["top5_changed"] = bool(cast(dict[str, object], result["top5"])["changed"])
    result["top10_changed"] = bool(cast(dict[str, object], result["top10"])["changed"])
    return result


def _evaluate_margin_supply_demand_hypotheses(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for axis_name in MARGIN_HYPOTHESIS_AXES:
        spec = next(spec for spec in AXES if spec.name == axis_name)
        controls: dict[str, object] = {}
        for control_name in MARGIN_CONTROL_FIELDS:
            if (
                axis_name == "margin_long_to_adv_mcap_quintile_percentile"
                and control_name == "market_cap_oku"
            ):
                controls[control_name] = {"normalized_in_axis": True}
                continue
            controls[control_name] = _stratified_axis_control(
                population,
                excess,
                axis_name=axis_name,
                direction=spec.direction,
                control_name=control_name,
            )
        result[axis_name] = {
            "eligible_n": sum(1 for row in population if getattr(row, axis_name) is not None),
            "controls": controls,
        }
    return result


def _stratified_axis_control(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
    *,
    axis_name: str,
    direction: int,
    control_name: str,
) -> dict[str, object]:
    rows = [
        row
        for row in population
        if getattr(row, axis_name) is not None and getattr(row, control_name) is not None
    ]
    if not rows:
        return _empty_axis_control()
    strata: list[list[PanelRow]]
    if control_name == "sector_33":
        by_sector: dict[str, list[PanelRow]] = {}
        for row in rows:
            by_sector.setdefault(row.sector_33, []).append(row)
        strata = [by_sector[key] for key in sorted(by_sector)]
    else:
        ordered = sorted(rows, key=lambda row: (getattr(row, control_name), row.ticker))
        strata = [[] for _ in range(5)]
        for index, row in enumerate(ordered):
            strata[min(index * 5 // len(ordered), 4)].append(row)

    median_spreads: list[tuple[float, int]] = []
    trap_deltas: list[tuple[float, int]] = []
    for stratum in strata:
        ordered_axis = sorted(
            stratum,
            key=lambda row: (getattr(row, axis_name) * direction, row.ticker),
        )
        midpoint = len(ordered_axis) // 2
        worst = ordered_axis[:midpoint]
        best = ordered_axis[midpoint:]
        if len(best) < 5 or len(worst) < 5:
            continue
        best_stats = _group_stats([excess[row.ticker] for row in best])
        worst_stats = _group_stats([excess[row.ticker] for row in worst])
        median_delta = _rounded_delta(
            best_stats.get("median_excess"), worst_stats.get("median_excess")
        )
        trap_delta = _rounded_delta(best_stats.get("trap_rate"), worst_stats.get("trap_rate"))
        if median_delta is None or trap_delta is None:
            continue
        weight = min(len(best), len(worst))
        median_spreads.append((median_delta, weight))
        trap_deltas.append((trap_delta, weight))
    matched_weight = sum(weight for _, weight in median_spreads)
    return {
        "strata_used": len(median_spreads),
        "matched_weight": matched_weight,
        "stratified_median_excess_spread": _weighted_mean(median_spreads),
        "stratified_trap_rate_delta": _weighted_mean(trap_deltas),
    }


def _empty_axis_control() -> dict[str, object]:
    return {
        "strata_used": 0,
        "matched_weight": 0,
        "stratified_median_excess_spread": None,
        "stratified_trap_rate_delta": None,
    }


def _weighted_mean(values: Sequence[tuple[float, int]]) -> float | None:
    weight = sum(item_weight for _, item_weight in values)
    if not weight:
        return None
    return round(sum(value * item_weight for value, item_weight in values) / weight, 6)


def _rounded_delta(left: object, right: object) -> float | None:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return None
    return round(float(left) - float(right), 6)


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


def _evaluate_crowded_value(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object]:
    """Split the cheap end of the cohort by how crowded the margin long side is.

    A valuation screen cannot see who is already positioned, so a name can look
    cheap while the buyers who made it cheap are still holding it. This is the
    direct test of that: inside the best E[r] decile, the names with the largest
    margin long balance relative to their trading volume are compared against the
    rest of that decile. The crowded half underperforming is what a value trap
    made of positioning looks like, and it is a separate question from whether the
    axis ranks the whole cross-section.
    """
    ranked = [
        (row.er_annual, row)
        for row in population
        if row.er_annual is not None and row.margin_long_to_adv is not None
    ]
    if len(ranked) < MIN_AXIS_SAMPLE:
        return {}
    ranked.sort(key=lambda item: item[0])
    cheap = [row for _, row in ranked[len(ranked) - len(ranked) // DECILES :]]
    if len(cheap) < 2:
        return {}
    crowding = sorted(cheap, key=lambda row: row.margin_long_to_adv or 0.0)
    midpoint = len(crowding) // 2
    uncrowded = [excess[row.ticker] for row in crowding[:midpoint]]
    crowded = [excess[row.ticker] for row in crowding[midpoint:]]
    stats_uncrowded = _group_stats(uncrowded)
    stats_crowded = _group_stats(crowded)
    crowded_median = stats_crowded.get("median_excess")
    uncrowded_median = stats_uncrowded.get("median_excess")
    return {
        "crowded": stats_crowded,
        "uncrowded": stats_uncrowded,
        "crowded_minus_uncrowded": (
            round(crowded_median - uncrowded_median, 6)
            if isinstance(crowded_median, int | float) and isinstance(uncrowded_median, int | float)
            else None
        ),
    }


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


def _evaluate_er_level_calibration(
    population: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> dict[str, object]:
    """Compare absolute annual E[r] with FY-dividend total return by quintile."""
    years = require_horizon(horizon).months / 12
    total_rows = {
        row.ticker: row
        for row in forward_rows
        if row.horizon == horizon
        and row.status == "resolved"
        and row.total_return_status == "resolved"
        and row.total_return_basis == TOTAL_RETURN_BASIS
        and row.realized_dividend_sum is not None
        and row.realized_dividend_fy_count > 0
        and row.price_return is not None
        and row.total_return is not None
    }
    entries: list[tuple[PanelRow, float, float]] = []
    for row in population:
        forward = total_rows.get(row.ticker)
        if (
            forward is None
            or row.er_annual is None
            or row.er_reversion_annual is None
            or row.er_carry_annual is None
        ):
            continue
        assert forward.price_return is not None
        assert forward.total_return is not None
        price_annual = _annualize_return(forward.price_return, years=years)
        total_annual = _annualize_return(forward.total_return, years=years)
        if price_annual is None or total_annual is None:
            continue
        entries.append((row, price_annual, total_annual))
    if len(entries) < MIN_AXIS_SAMPLE:
        return {}

    entries.sort(key=lambda item: float(item[0].er_annual or 0.0))
    quintiles: list[dict[str, object]] = []
    step = len(entries) / 5
    for index in range(5):
        chunk = entries[int(index * step) : int((index + 1) * step)]
        if not chunk:
            continue
        predicted = median(float(row.er_annual or 0.0) for row, _, _ in chunk)
        realized_total = median(total for _, _, total in chunk)
        quintiles.append(
            {
                "median_predicted_er_annual": round(predicted, 6),
                "median_realized_total_return_annual": round(realized_total, 6),
                "calibration_error_annual": round(realized_total - predicted, 6),
                "median_predicted_reversion_annual": round(
                    median(float(row.er_reversion_annual or 0.0) for row, _, _ in chunk),
                    6,
                ),
                "median_predicted_carry_annual": round(
                    median(float(row.er_carry_annual or 0.0) for row, _, _ in chunk), 6
                ),
                "median_realized_price_return_annual": round(
                    median(price for _, price, _ in chunk), 6
                ),
                "median_realized_dividend_contribution_annual": round(
                    median(total - price for _, price, total in chunk), 6
                ),
                "n": len(chunk),
            }
        )
    return {
        "n": len(entries),
        "horizon_years": years,
        "prediction_basis": "er_annual_absolute",
        "realized_basis": "fy_actual_dividend_total_return_annualized_absolute",
        "calibration_error_basis": "realized_minus_predicted",
        "component_basis": {
            "predicted_reversion": "er_reversion_annual",
            "predicted_carry": "er_carry_annual_dividend_plus_buyback",
            "realized_price": "adjusted_close_price_return_annualized",
            "realized_dividend": "annualized_total_minus_annualized_price",
        },
        "er_quintiles": quintiles,
    }


def _annualize_return(value: float, *, years: float) -> float | None:
    if years <= 0 or value < -1:
        return None
    annualized = (1 + value) ** (1 / years) - 1
    return float(annualized) if isfinite(annualized) else None


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
        decile_spreads: list[float] = []
        total_n = 0
        for cohort in cohorts:
            axes = cohort.get("axes")
            if not isinstance(axes, dict):
                continue
            axis = axes.get(spec.name)
            if not isinstance(axis, dict):
                continue
            axis_n = axis.get("n")
            if isinstance(axis_n, int):
                total_n += axis_n
            ic = axis.get("rank_ic")
            if isinstance(ic, int | float):
                ics.append(float(ic))
            best = axis.get("best_decile_median_excess")
            if isinstance(best, int | float):
                best_medians.append(float(best))
            trap = axis.get("best_decile_trap_rate")
            if isinstance(trap, int | float):
                trap_rates.append(float(trap))
            spread = axis.get("decile_spread_median")
            if isinstance(spread, int | float):
                decile_spreads.append(float(spread))
        if not ics and not best_medians:
            continue
        axis_summary[spec.name] = {
            "cohorts": len(ics),
            "total_n": total_n,
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
            "mean_decile_spread_median": (
                round(fmean(decile_spreads), 6) if decile_spreads else None
            ),
            "decile_spread_positive_share": (
                round(
                    sum(1 for value in decile_spreads if value > 0) / len(decile_spreads),
                    4,
                )
                if decile_spreads
                else None
            ),
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
        "quality_interaction": _aggregate_quality_interaction(cohorts),
        "shareholder_return_change": _aggregate_shareholder_return_change(cohorts),
        "margin_deadline_gate": _aggregate_margin_deadline_gate(cohorts),
        "margin_supply_demand_hypotheses": _aggregate_margin_hypotheses(cohorts),
    }


def _aggregate_margin_deadline_gate(
    cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    total_excluded = 0
    for cohort in cohorts:
        gate = cohort.get("margin_deadline_gate")
        if isinstance(gate, dict) and isinstance(gate.get("excluded_count"), int):
            total_excluded += int(gate["excluded_count"])
    result["excluded_count"] = total_excluded
    for top_n in (5, 10):
        median_deltas: list[float] = []
        trap_deltas: list[float] = []
        baseline_n = 0
        variant_n = 0
        changed = 0
        complete = 0
        for cohort in cohorts:
            gate = cohort.get("margin_deadline_gate")
            if not isinstance(gate, dict):
                continue
            comparison = gate.get(f"top{top_n}")
            if not isinstance(comparison, dict):
                continue
            if comparison.get("complete") is True:
                complete += 1
            if comparison.get("changed") is True:
                changed += 1
            baseline = comparison.get("baseline")
            variant = comparison.get("variant")
            if isinstance(baseline, dict) and isinstance(baseline.get("n"), int):
                baseline_n += int(baseline["n"])
            if isinstance(variant, dict) and isinstance(variant.get("n"), int):
                variant_n += int(variant["n"])
            _append_numeric(comparison.get("median_excess_delta"), median_deltas)
            _append_numeric(comparison.get("trap_rate_delta"), trap_deltas)
        result[f"top{top_n}"] = {
            "paired_cohorts": len(median_deltas),
            "complete_cohorts": complete,
            "baseline_n": baseline_n,
            "variant_n": variant_n,
            "changed_cohorts": changed,
            "changed_cohort_share": (
                round(changed / len(median_deltas), 4) if median_deltas else None
            ),
            "mean_median_excess_delta": (round(fmean(median_deltas), 6) if median_deltas else None),
            "mean_trap_rate_delta": (round(fmean(trap_deltas), 6) if trap_deltas else None),
        }
    return result


def _aggregate_margin_hypotheses(
    cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for axis_name in MARGIN_HYPOTHESIS_AXES:
        control_values = {
            field_name: _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
            for field_name in MARGIN_CONTROL_FIELDS
        }
        eligible_n = 0
        for cohort in cohorts:
            hypotheses = cohort.get("margin_supply_demand_hypotheses")
            if not isinstance(hypotheses, dict):
                continue
            axis = hypotheses.get(axis_name)
            if not isinstance(axis, dict):
                continue
            if isinstance(axis.get("eligible_n"), int):
                eligible_n += int(axis["eligible_n"])
            controls = axis.get("controls")
            if not isinstance(controls, dict):
                continue
            for field_name, accumulator in control_values.items():
                control = controls.get(field_name)
                if not isinstance(control, dict) or control.get("normalized_in_axis") is True:
                    continue
                median_value = control.get("stratified_median_excess_spread")
                trap_value = control.get("stratified_trap_rate_delta")
                if not isinstance(median_value, int | float) or not isinstance(
                    trap_value, int | float
                ):
                    continue
                accumulator.median_deltas.append(float(median_value))
                accumulator.trap_deltas.append(float(trap_value))
                accumulator.cohorts += 1
                weight = control.get("matched_weight")
                if isinstance(weight, int):
                    accumulator.matched_weight += weight
        controls_summary = _control_summaries(control_values)
        if axis_name == "margin_long_to_adv_mcap_quintile_percentile":
            cast(dict[str, object], controls_summary["market_cap_oku"])["normalized_in_axis"] = True
        result[axis_name] = {
            "eligible_n": eligible_n,
            "controls": controls_summary,
        }
    return result


def _aggregate_quality_interaction(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    median_deltas: list[float] = []
    mean_deltas: list[float] = []
    trap_deltas: list[float] = []
    high_n = 0
    low_n = 0
    control_values = {
        field_name: _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
        for field_name in QUALITY_CONTROL_FIELDS
    }
    for cohort in cohorts:
        interaction = cohort.get("quality_interaction")
        if not isinstance(interaction, dict):
            continue
        high = interaction.get("high")
        low = interaction.get("low")
        if isinstance(high, dict) and isinstance(high.get("n"), int):
            high_n += int(high["n"])
        if isinstance(low, dict) and isinstance(low.get("n"), int):
            low_n += int(low["n"])
        _append_numeric(interaction.get("median_excess_delta"), median_deltas)
        _append_numeric(interaction.get("mean_excess_delta"), mean_deltas)
        _append_numeric(interaction.get("trap_rate_delta"), trap_deltas)
        controls = interaction.get("controls")
        if not isinstance(controls, dict):
            continue
        for field_name in QUALITY_CONTROL_FIELDS:
            control = controls.get(field_name)
            if not isinstance(control, dict):
                continue
            summary = control_values[field_name]
            median_value = control.get("stratified_median_excess_delta")
            trap_value = control.get("stratified_trap_rate_delta")
            if isinstance(median_value, int | float) and isinstance(trap_value, int | float):
                summary.median_deltas.append(float(median_value))
                summary.trap_deltas.append(float(trap_value))
                summary.cohorts += 1
                weight = control.get("matched_weight")
                if isinstance(weight, int):
                    summary.matched_weight += weight

    controls_summary: dict[str, object] = {}
    for field_name, values in control_values.items():
        controls_summary[field_name] = {
            "cohorts": values.cohorts,
            "matched_weight": values.matched_weight,
            "mean_stratified_median_excess_delta": (
                round(fmean(values.median_deltas), 6) if values.median_deltas else None
            ),
            "mean_stratified_trap_rate_delta": (
                round(fmean(values.trap_deltas), 6) if values.trap_deltas else None
            ),
        }
    return {
        "cohorts": len(median_deltas),
        "high_n": high_n,
        "low_n": low_n,
        "mean_median_excess_delta": (round(fmean(median_deltas), 6) if median_deltas else None),
        "median_delta_positive_share": (
            round(sum(1 for value in median_deltas if value > 0) / len(median_deltas), 4)
            if median_deltas
            else None
        ),
        "mean_mean_excess_delta": round(fmean(mean_deltas), 6) if mean_deltas else None,
        "mean_trap_rate_delta": round(fmean(trap_deltas), 6) if trap_deltas else None,
        "controls": controls_summary,
    }


def _aggregate_shareholder_return_change(
    cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    median_deltas: list[float] = []
    mean_deltas: list[float] = []
    trap_deltas: list[float] = []
    change_n = 0
    no_change_n = 0
    control_values = {
        field_name: _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
        for field_name in RETURN_CHANGE_CONTROL_FIELDS
    }
    component_values = {
        field_name: _ComponentAccumulator(median_deltas=[], mean_deltas=[], trap_deltas=[])
        for field_name in RETURN_CHANGE_COMPONENT_FIELDS
    }
    for cohort in cohorts:
        interaction = cohort.get("shareholder_return_change")
        if not isinstance(interaction, dict):
            continue
        change = interaction.get("change")
        no_change = interaction.get("no_change")
        if isinstance(change, dict) and isinstance(change.get("n"), int):
            change_n += int(change["n"])
        if isinstance(no_change, dict) and isinstance(no_change.get("n"), int):
            no_change_n += int(no_change["n"])
        _append_numeric(interaction.get("median_excess_delta"), median_deltas)
        _append_numeric(interaction.get("mean_excess_delta"), mean_deltas)
        _append_numeric(interaction.get("trap_rate_delta"), trap_deltas)
        controls = interaction.get("controls")
        if isinstance(controls, dict):
            _collect_control_values(controls, control_values)
        components = interaction.get("components")
        if isinstance(components, dict):
            _collect_component_values(components, component_values)

    return {
        "cohorts": len(median_deltas),
        "change_n": change_n,
        "no_change_n": no_change_n,
        "mean_median_excess_delta": (round(fmean(median_deltas), 6) if median_deltas else None),
        "median_delta_positive_share": (
            round(sum(1 for value in median_deltas if value > 0) / len(median_deltas), 4)
            if median_deltas
            else None
        ),
        "mean_mean_excess_delta": round(fmean(mean_deltas), 6) if mean_deltas else None,
        "mean_trap_rate_delta": round(fmean(trap_deltas), 6) if trap_deltas else None,
        "controls": _control_summaries(control_values),
        "components": _component_summaries(component_values),
    }


def _collect_control_values(
    controls: Mapping[str, object],
    target: Mapping[str, _QualityControlAccumulator],
) -> None:
    for field_name, summary in target.items():
        control = controls.get(field_name)
        if not isinstance(control, dict):
            continue
        median_value = control.get("stratified_median_excess_delta")
        trap_value = control.get("stratified_trap_rate_delta")
        if not isinstance(median_value, int | float) or not isinstance(trap_value, int | float):
            continue
        summary.median_deltas.append(float(median_value))
        summary.trap_deltas.append(float(trap_value))
        summary.cohorts += 1
        weight = control.get("matched_weight")
        if isinstance(weight, int):
            summary.matched_weight += weight


def _control_summaries(
    values: Mapping[str, _QualityControlAccumulator],
) -> dict[str, object]:
    return {
        field_name: {
            "cohorts": summary.cohorts,
            "matched_weight": summary.matched_weight,
            "mean_stratified_median_excess_delta": (
                round(fmean(summary.median_deltas), 6) if summary.median_deltas else None
            ),
            "mean_stratified_trap_rate_delta": (
                round(fmean(summary.trap_deltas), 6) if summary.trap_deltas else None
            ),
        }
        for field_name, summary in values.items()
    }


def _collect_component_values(
    components: Mapping[str, object],
    target: Mapping[str, _ComponentAccumulator],
) -> None:
    for field_name, summary in target.items():
        component = components.get(field_name)
        if not isinstance(component, dict):
            continue
        _append_numeric(component.get("median_excess_delta"), summary.median_deltas)
        _append_numeric(component.get("mean_excess_delta"), summary.mean_deltas)
        _append_numeric(component.get("trap_rate_delta"), summary.trap_deltas)
        true_group = component.get("true")
        if isinstance(true_group, dict) and isinstance(true_group.get("n"), int):
            summary.true_n += int(true_group["n"])
        false_group = component.get("false")
        if isinstance(false_group, dict) and isinstance(false_group.get("n"), int):
            summary.false_n += int(false_group["n"])


def _component_summaries(values: Mapping[str, _ComponentAccumulator]) -> dict[str, object]:
    summaries: dict[str, object] = {}
    for field_name, value in values.items():
        summaries[field_name] = {
            "cohorts": len(value.median_deltas),
            "true_n": value.true_n,
            "false_n": value.false_n,
            "mean_median_excess_delta": (
                round(fmean(value.median_deltas), 6) if value.median_deltas else None
            ),
            "mean_mean_excess_delta": (
                round(fmean(value.mean_deltas), 6) if value.mean_deltas else None
            ),
            "mean_trap_rate_delta": (
                round(fmean(value.trap_deltas), 6) if value.trap_deltas else None
            ),
        }
    return summaries


def _append_numeric(value: object, target: list[float]) -> None:
    if isinstance(value, int | float):
        target.append(float(value))
