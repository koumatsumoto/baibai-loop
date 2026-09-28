"""Security Analysisの軸とquality bucketを評価する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean, median

from baibai_engine.screening.calibration.evaluation.policy import (
    _SECTOR_MEDIAN_AXIS_PREFIX,
    ASSET_BACKED_CONTROL_FIELDS,
    ASSET_BACKED_THRESHOLD,
    AXES,
    DECILES,
    DETERIORATION_THRESHOLD,
    GATE_BASE_AXES,
    MARGIN_CONTROL_FIELDS,
    MARGIN_HYPOTHESIS_AXES,
    MIN_AXIS_SAMPLE,
    MIN_QUALITY_CONTROL_GROUP,
    PROFIT_NORMALIZATION_CONTROL_FIELDS,
    RETURN_CHANGE_COMPONENT_FIELDS,
    RETURN_CHANGE_CONTROL_FIELDS,
    REVERSION_AXES,
    SECTOR_MEDIAN_AXES,
    TRAP_EXCESS_THRESHOLD,
    AxisSpec,
)
from baibai_engine.screening.calibration.evaluation.statistics import (
    _annualize_return,
    _decile_values,
    _group_stats,
    _rounded_delta,
    _spearman,
    _weighted_mean,
)
from baibai_engine.screening.calibration.forward import (
    TOTAL_RETURN_BASIS,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.calibration.panel import PanelRow


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


def _evaluate_margin_supply_demand_hypotheses(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    result: dict[str, object] = {}
    for axis_name in MARGIN_HYPOTHESIS_AXES:
        spec = next(spec for spec in AXES if spec.name == axis_name)
        controls: dict[str, object] = {}
        for control_name in MARGIN_CONTROL_FIELDS:
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


def _evaluate_profit_normalization_hypotheses(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    controls = {
        control_name: _stratified_axis_control(
            population,
            excess,
            axis_name="normalized_per_3fy",
            direction=-1,
            control_name=control_name,
        )
        for control_name in PROFIT_NORMALIZATION_CONTROL_FIELDS
    }

    population_n = len(population)

    def coverage(field_name: str) -> float | None:
        if not population_n:
            return None
        return round(
            sum(getattr(row, field_name) is not None for row in population) / population_n,
            4,
        )

    return {
        "population_n": population_n,
        "normalized_per_3fy_coverage": coverage("normalized_per_3fy"),
        "normalized_per_5fy_coverage": coverage("normalized_per_5fy"),
        "normalized_per_3fy_controls": controls,
        "self_range_coverage": {
            f"at_least_{sessions}": (
                round(
                    sum(row.self_range_observed_sessions >= sessions for row in population)
                    / population_n,
                    4,
                )
                if population_n
                else None
            )
            for sessions in (750,)
        },
    }


def _evaluate_asset_backed_hypotheses(
    population: Sequence[PanelRow], excess: Mapping[str, float]
) -> dict[str, object]:
    population_n = len(population)
    eligible = [
        row
        for row in population
        if row.asset_backed_ratio is not None and row.shareholder_return_change is not None
    ]
    groups = {
        "A_thick_change": [
            row
            for row in eligible
            if (row.asset_backed_ratio or 0.0) >= ASSET_BACKED_THRESHOLD
            and row.shareholder_return_change is True
        ],
        "B_thick_no_change": [
            row
            for row in eligible
            if (row.asset_backed_ratio or 0.0) >= ASSET_BACKED_THRESHOLD
            and row.shareholder_return_change is False
        ],
        "C_thin_change": [
            row
            for row in eligible
            if (row.asset_backed_ratio or 0.0) < ASSET_BACKED_THRESHOLD
            and row.shareholder_return_change is True
        ],
        "D_thin_no_change": [
            row
            for row in eligible
            if (row.asset_backed_ratio or 0.0) < ASSET_BACKED_THRESHOLD
            and row.shareholder_return_change is False
        ],
    }
    stats = {
        name: _group_stats([excess[row.ticker] for row in rows]) for name, rows in groups.items()
    }
    thick_median_delta = _rounded_delta(
        stats["A_thick_change"].get("median_excess"),
        stats["B_thick_no_change"].get("median_excess"),
    )
    thin_median_delta = _rounded_delta(
        stats["C_thin_change"].get("median_excess"),
        stats["D_thin_no_change"].get("median_excess"),
    )
    thick_trap_delta = _rounded_delta(
        stats["A_thick_change"].get("trap_rate"),
        stats["B_thick_no_change"].get("trap_rate"),
    )
    thin_trap_delta = _rounded_delta(
        stats["C_thin_change"].get("trap_rate"),
        stats["D_thin_no_change"].get("trap_rate"),
    )
    return {
        "population_n": population_n,
        "asset_backed_ratio_coverage": (
            round(sum(row.asset_backed_ratio is not None for row in population) / population_n, 4)
            if population_n
            else None
        ),
        "interaction_eligible_n": len(eligible),
        "threshold": ASSET_BACKED_THRESHOLD,
        "controls": {
            control_name: _stratified_axis_control(
                population,
                excess,
                axis_name="asset_backed_ratio",
                direction=1,
                control_name=control_name,
            )
            for control_name in ASSET_BACKED_CONTROL_FIELDS
        },
        "groups": stats,
        "thick_change_minus_no_change_median_excess": thick_median_delta,
        "thick_change_minus_no_change_trap_rate": thick_trap_delta,
        "thin_change_minus_no_change_median_excess": thin_median_delta,
        "thin_change_minus_no_change_trap_rate": thin_trap_delta,
        "difference_in_differences_median_excess": _rounded_delta(
            thick_median_delta, thin_median_delta
        ),
        "difference_in_differences_trap_rate": _rounded_delta(thick_trap_delta, thin_trap_delta),
    }


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


def _evaluate_sector_median_basis(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
) -> dict[str, object]:
    """Split each sector-gap axis by which population produced its median.

    A sector below the head-count floor is compared against the whole market instead,
    and the two answers are not the same quantity: the sectors that fall through sit
    below the market on every valuation axis, so their names carry a negative gap that
    sector composition alone can explain. The axis result is reported for each basis so
    that a reader can tell an axis that works from an axis that works on one basis.

    **What the group did is not what the axis is worth.** Falling back is decided per
    sector, so the market-basis group is a union of whole sectors and its median outcome
    is that sector mix -- subtracting each row's own sector median drives it to zero on
    every axis and cohort. `group_median_excess` is therefore reported as the mix it is,
    and the axis is measured inside each group by splitting on the axis value itself:
    the cheap half against the expensive half is a comparison the sector mix cannot
    produce, because both halves carry the same sectors.

    The two sides are not the same size. A cohort holds a few thousand names on their own
    sector and roughly fifty on the market, because only nine sectors sit below the floor.
    A decile spread over fifty rows puts five names in a bucket, so the spread is reported
    only where the sample supports it while the half-split, which needs far less, carries
    the comparison on both sides.
    """
    result: dict[str, object] = {}
    for axis_name in SECTOR_MEDIAN_AXES:
        spec = next(spec for spec in AXES if spec.name == axis_name)
        metric = axis_name.removeprefix(_SECTOR_MEDIAN_AXIS_PREFIX)
        groups: dict[str, list[tuple[float, float]]] = {"own_sector": [], "market_fallback": []}
        screened: dict[str, int] = {"own_sector": 0, "market_fallback": 0}
        for row in population:
            value = getattr(row, axis_name)
            if value is None:
                continue
            basis = (
                "market_fallback" if metric in row.smg_market_fallback.split("|") else "own_sector"
            )
            groups[basis].append((value * spec.direction, excess[row.ticker]))
            screened[basis] += row.in_review_set
        entry: dict[str, object] = {}
        for basis, pairs in groups.items():
            decile_values = _decile_values(pairs) if len(pairs) >= MIN_AXIS_SAMPLE else None
            best = decile_values[-1] if decile_values else []
            worst = decile_values[0] if decile_values else []
            stats = _group_stats([outcome for _, outcome in pairs])
            # Named field by field rather than splatted: on this coordinate a group
            # level and an axis effect are different quantities, and `median_excess`
            # would read as the second while being the first.
            entry[basis] = {
                "n": stats["n"],
                "trap_rate": stats["trap_rate"],
                "group_median_excess": stats["median_excess"],
                "group_mean_excess": stats["mean_excess"],
                "axis_effect": _half_split_effect(pairs),
                "passed_screen": screened[basis],
                "best_decile_median_excess": round(median(best), 6) if best else None,
                "decile_spread_median": (
                    round(median(best) - median(worst), 6) if best and worst else None
                ),
            }
        result[axis_name] = entry
    return result


# 群内を軸値で 2 分割して効きを測るのに要る最小標本。市場 fallback 側は 1 cohort
# あたり 50〜80 行なので decile は組めないが、半分ずつなら分位あたり 15 行以上を保てる。
MIN_HALF_SPLIT_SAMPLE = 30


def _half_split_effect(pairs: Sequence[tuple[float, float]]) -> dict[str, object]:
    """群の中で「割安側」と「割高側」の実現超過を比べる。

    群そのものの中央値は、群の決まり方 (業種が薄いかどうか) が持ち込む構成をそのまま
    映す。同じ群を軸値で割れば両側が同じ構成を持つので、差は軸の効きだけを表す。
    """
    if len(pairs) < MIN_HALF_SPLIT_SAMPLE:
        return {
            "n": len(pairs),
            "cheap_median_excess": None,
            "expensive_median_excess": None,
            "median_excess_delta": None,
        }
    ordered = sorted(pairs, key=lambda pair: pair[0])
    half = len(ordered) // 2
    expensive = [outcome for _, outcome in ordered[:half]]
    cheap = [outcome for _, outcome in ordered[len(ordered) - half :]]
    return {
        "n": len(ordered),
        "cheap_median_excess": round(median(cheap), 6),
        "expensive_median_excess": round(median(expensive), 6),
        "median_excess_delta": round(median(cheap) - median(expensive), 6),
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
        and row.resolved
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
                # The display context maps a current estimate back to the historical
                # calibration band. Keep the observed band edge with the cohort;
                # reconstructing it later from the median would only be an approximation.
                "max_predicted_er_annual": round(
                    max(float(row.er_annual or 0.0) for row, _, _ in chunk), 6
                ),
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
