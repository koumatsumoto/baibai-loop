"""cohort評価を集約し、較正の要約とbias比較を作る。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, stdev

from baibai_engine.screening.calibration.evaluation.policy import (
    ASSET_BACKED_CONTROL_FIELDS,
    AXES,
    GATE_BASE_AXES,
    MARGIN_CONTROL_FIELDS,
    MARGIN_HYPOTHESIS_AXES,
    MIN_THRESHOLD_REMOVED_SAMPLE,
    PROFIT_NORMALIZATION_CONTROL_FIELDS,
    RETURN_CHANGE_COMPONENT_FIELDS,
    RETURN_CHANGE_CONTROL_FIELDS,
    SECTOR_MEDIAN_AXES,
)
from baibai_engine.screening.rule_config import CandidateDiscoveryRules


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


def _aggregate(
    cohorts: Sequence[dict[str, object]],
    *,
    candidate_discovery_rules: CandidateDiscoveryRules,
) -> dict[str, object]:
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

    candidate_discovery_summary: dict[str, object] = {}
    candidate_discovery_key_set: set[str] = set()
    for cohort in cohorts:
        candidate_discovery = cohort.get("candidate_discovery")
        if isinstance(candidate_discovery, dict):
            candidate_discovery_key_set.update(candidate_discovery)
    candidate_discovery_keys = sorted(candidate_discovery_key_set)
    for key in candidate_discovery_keys:
        if key.startswith("nomination_union_vs_er_top"):
            rows = [
                candidate_discovery[key]
                for cohort in cohorts
                if isinstance((candidate_discovery := cohort.get("candidate_discovery")), dict)
                and isinstance(candidate_discovery.get(key), dict)
            ]
            candidate_discovery_summary[key] = {
                "cohorts": len(rows),
                **{
                    f"mean_{field}": (
                        round(fmean(float(row[field]) for row in rows), 1) if rows else 0
                    )
                    for field in (
                        "review_set_n",
                        "pure_er_n",
                        "overlap_n",
                        "displaced_from_er_n",
                    )
                },
                "mean_overlap_share": (
                    round(
                        fmean(
                            float(row["overlap_n"]) / float(row["pure_er_n"])
                            for row in rows
                            if row.get("pure_er_n")
                        ),
                        4,
                    )
                    if any(row.get("pure_er_n") for row in rows)
                    else None
                ),
            }
            continue
        medians: list[float] = []
        means: list[float] = []
        traps: list[float] = []
        ns: list[int] = []
        for cohort in cohorts:
            candidate_discovery = cohort.get("candidate_discovery")
            if not isinstance(candidate_discovery, dict):
                continue
            stats = candidate_discovery.get(key)
            if not isinstance(stats, dict) or not stats.get("n"):
                continue
            ns.append(int(stats["n"]))
            if isinstance(stats.get("median_excess"), int | float):
                medians.append(float(stats["median_excess"]))
            if isinstance(stats.get("mean_excess"), int | float):
                means.append(float(stats["mean_excess"]))
            if isinstance(stats.get("trap_rate"), int | float):
                traps.append(float(stats["trap_rate"]))
        candidate_discovery_summary[key] = {
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
        "candidate_discovery": candidate_discovery_summary,
        "gates": _aggregate_gates(cohorts),
        "sector_median_basis": _aggregate_sector_median_basis(cohorts),
        "shareholder_return_change": _aggregate_shareholder_return_change(cohorts),
        "margin_supply_demand_hypotheses": _aggregate_margin_hypotheses(cohorts),
        "profit_normalization_hypotheses": _aggregate_profit_normalization(cohorts),
        "asset_backed_hypotheses": _aggregate_asset_backed_hypotheses(cohorts),
        "er_calibration": _aggregate_er_calibration(cohorts),
    }


def _aggregate_gates(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    """Cross-cohort verdict on the deterioration gate, per valuation axis.

    A per-cohort pass/blocked pair answers one as-of. Whether the gate earns its place is
    a question about the cohorts together: the sign of the difference and how often it
    holds. Reported as the gate's own effect — what the names it removes gave up — so a
    negative number means the gate cost return on that axis.

    The blocked side is the small one: it is the deteriorating names inside a value axis's
    best decile, which can be a handful. A cohort speaks only when that side reaches the
    same floor the threshold coordinate uses, and the rest are counted apart, so a gate
    nobody can measure reports no effect rather than one drawn from a few names.
    """
    result: dict[str, object] = {}
    for axis_name in GATE_BASE_AXES:
        deltas: list[float] = []
        thin_cohorts = 0
        passed_n = blocked_n = 0
        for cohort in cohorts:
            gates = cohort.get("gates")
            if not isinstance(gates, dict):
                continue
            entry = gates.get(axis_name)
            if not isinstance(entry, dict):
                continue
            passed = entry.get("gate_pass")
            blocked = entry.get("gate_blocked")
            if not isinstance(passed, dict) or not isinstance(blocked, dict):
                continue
            blocked_count = int(blocked.get("n") or 0)
            passed_n += int(passed.get("n") or 0)
            blocked_n += blocked_count
            passed_median = passed.get("median_excess")
            blocked_median = blocked.get("median_excess")
            if not isinstance(passed_median, int | float) or not isinstance(
                blocked_median, int | float
            ):
                continue
            if blocked_count < MIN_THRESHOLD_REMOVED_SAMPLE:
                thin_cohorts += 1
                continue
            deltas.append(float(passed_median) - float(blocked_median))
        if not deltas and not thin_cohorts:
            continue
        result[axis_name] = {
            "cohorts": len(deltas) + thin_cohorts,
            "eligible_cohorts": len(deltas),
            "passed_n": passed_n,
            "blocked_n": blocked_n,
            "mean_gate_median_excess_delta": round(fmean(deltas), 6) if deltas else None,
            "stdev_gate_median_excess_delta": (
                round(stdev(deltas), 6) if len(deltas) > 1 else None
            ),
            "gate_positive_share": (
                round(sum(1 for value in deltas if value > 0) / len(deltas), 4) if deltas else None
            ),
        }
    return result


def _aggregate_sector_median_basis(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    """Cross-cohort effect of each sector-gap axis, kept apart by baseline.

    `axis_effect` は群の中を軸値で割った差なので、両側とも同じ構成を持つ。これが軸の
    効きを表す量で、`group_median_excess` の側は群の決まり方が持ち込む業種構成である。
    2 つを別の名前で並べ、構成を効きとして読めないようにする。
    """
    result: dict[str, object] = {}
    for axis_name in SECTOR_MEDIAN_AXES:
        entry: dict[str, object] = {}
        for basis in ("own_sector", "market_fallback"):
            spreads: list[float] = []
            group_medians: list[float] = []
            effects: list[float] = []
            traps: list[float] = []
            cohort_count = total_n = passed_screen = 0
            for cohort in cohorts:
                node = cohort.get("sector_median_basis")
                if not isinstance(node, dict):
                    continue
                axis = node.get(axis_name)
                if not isinstance(axis, dict):
                    continue
                group = axis.get(basis)
                if not isinstance(group, dict) or not group.get("n"):
                    continue
                cohort_count += 1
                total_n += int(group.get("n") or 0)
                passed_screen += int(group.get("passed_screen") or 0)
                group_median = group.get("group_median_excess")
                if isinstance(group_median, int | float):
                    group_medians.append(float(group_median))
                effect = group.get("axis_effect")
                if isinstance(effect, dict):
                    delta = effect.get("median_excess_delta")
                    if isinstance(delta, int | float):
                        effects.append(float(delta))
                trap = group.get("trap_rate")
                if isinstance(trap, int | float):
                    traps.append(float(trap))
                # The market side never fills a decile, so the spread joins the report
                # only where a cohort had the sample; the half split carries both sides.
                spread = group.get("decile_spread_median")
                if isinstance(spread, int | float):
                    spreads.append(float(spread))
            entry[basis] = {
                "cohorts": cohort_count,
                "total_n": total_n,
                "passed_screen": passed_screen,
                # 群そのものの水準。fallback 側では「薄い業種の集合が何をしたか」であり
                # 軸の効きではない。
                "mean_group_median_excess": (
                    round(fmean(group_medians), 6) if group_medians else None
                ),
                # 軸の効き。群内を割安 / 割高で割った差。ばらつきを平均と並べるのは、
                # 月末 as-of の窓が大きく重なり、cohort 数だけ独立観測があるように
                # 見えるため。市場側は 1 cohort 50 行前後なので特に効く。
                "effect_cohorts": len(effects),
                "mean_axis_effect": round(fmean(effects), 6) if effects else None,
                "stdev_axis_effect": round(stdev(effects), 6) if len(effects) > 1 else None,
                "axis_effect_positive_share": (
                    round(sum(1 for value in effects if value > 0) / len(effects), 4)
                    if effects
                    else None
                ),
                "mean_trap_rate": round(fmean(traps), 4) if traps else None,
                "spread_cohorts": len(spreads),
                "mean_decile_spread_median": round(fmean(spreads), 6) if spreads else None,
                "decile_spread_positive_share": (
                    round(sum(1 for value in spreads if value > 0) / len(spreads), 4)
                    if spreads
                    else None
                ),
            }
        result[axis_name] = entry
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
        result[axis_name] = {
            "eligible_n": eligible_n,
            "controls": controls_summary,
        }
    return result


def _aggregate_profit_normalization(
    cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    controls = {
        name: _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
        for name in PROFIT_NORMALIZATION_CONTROL_FIELDS
    }
    coverage_3fy: list[float] = []
    coverage_5fy: list[float] = []
    self_coverage: dict[int, list[float]] = {750: []}
    for cohort in cohorts:
        hypotheses = cohort.get("profit_normalization_hypotheses")
        if not isinstance(hypotheses, dict):
            continue
        _append_numeric(hypotheses.get("normalized_per_3fy_coverage"), coverage_3fy)
        _append_numeric(hypotheses.get("normalized_per_5fy_coverage"), coverage_5fy)
        range_coverage = hypotheses.get("self_range_coverage")
        if isinstance(range_coverage, dict):
            for sessions, values in self_coverage.items():
                _append_numeric(range_coverage.get(f"at_least_{sessions}"), values)
        cohort_controls = hypotheses.get("normalized_per_3fy_controls")
        if isinstance(cohort_controls, dict):
            for name, accumulator in controls.items():
                control = cohort_controls.get(name)
                if not isinstance(control, dict):
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

    return {
        "mean_normalized_per_3fy_coverage": (
            round(fmean(coverage_3fy), 4) if coverage_3fy else None
        ),
        "mean_normalized_per_5fy_coverage": (
            round(fmean(coverage_5fy), 4) if coverage_5fy else None
        ),
        "normalized_per_3fy_controls": _control_summaries(controls),
        "mean_self_range_coverage": {
            f"at_least_{sessions}": round(fmean(values), 4) if values else None
            for sessions, values in self_coverage.items()
        },
    }


def _aggregate_asset_backed_hypotheses(
    cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    coverage: list[float] = []
    thick_medians: list[float] = []
    thick_traps: list[float] = []
    did_medians: list[float] = []
    did_traps: list[float] = []
    group_ns = dict.fromkeys(
        (
            "A_thick_change",
            "B_thick_no_change",
            "C_thin_change",
            "D_thin_no_change",
        ),
        0,
    )
    controls = {
        name: _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
        for name in ASSET_BACKED_CONTROL_FIELDS
    }
    for cohort in cohorts:
        hypotheses = cohort.get("asset_backed_hypotheses")
        if not isinstance(hypotheses, dict):
            continue
        _append_numeric(hypotheses.get("asset_backed_ratio_coverage"), coverage)
        cohort_groups = hypotheses.get("groups")
        if not isinstance(cohort_groups, dict):
            continue
        cohort_ns: dict[str, int] = {}
        for name in group_ns:
            stats = cohort_groups.get(name)
            count = stats.get("n") if isinstance(stats, dict) else None
            if isinstance(count, int):
                group_ns[name] += count
                cohort_ns[name] = count
        if cohort_ns.get("A_thick_change", 0) >= 5 and cohort_ns.get("B_thick_no_change", 0) >= 5:
            _append_numeric(
                hypotheses.get("thick_change_minus_no_change_median_excess"), thick_medians
            )
            _append_numeric(hypotheses.get("thick_change_minus_no_change_trap_rate"), thick_traps)
            if cohort_ns.get("C_thin_change", 0) >= 5 and cohort_ns.get("D_thin_no_change", 0) >= 5:
                _append_numeric(
                    hypotheses.get("difference_in_differences_median_excess"), did_medians
                )
                _append_numeric(hypotheses.get("difference_in_differences_trap_rate"), did_traps)
        cohort_controls = hypotheses.get("controls")
        if not isinstance(cohort_controls, dict):
            continue
        for name, accumulator in controls.items():
            control = cohort_controls.get(name)
            if not isinstance(control, dict):
                continue
            median_value = control.get("stratified_median_excess_spread")
            trap_value = control.get("stratified_trap_rate_delta")
            if not isinstance(median_value, int | float) or not isinstance(trap_value, int | float):
                continue
            accumulator.median_deltas.append(float(median_value))
            accumulator.trap_deltas.append(float(trap_value))
            accumulator.cohorts += 1
            matched_weight = control.get("matched_weight")
            if isinstance(matched_weight, int):
                accumulator.matched_weight += matched_weight

    return {
        "mean_asset_backed_ratio_coverage": round(fmean(coverage), 4) if coverage else None,
        "group_n": group_ns,
        "comparable_cohorts": len(thick_medians),
        "mean_thick_change_minus_no_change_median_excess": (
            round(fmean(thick_medians), 6) if thick_medians else None
        ),
        "thick_median_delta_positive_share": (
            round(sum(value > 0 for value in thick_medians) / len(thick_medians), 4)
            if thick_medians
            else None
        ),
        "mean_thick_change_minus_no_change_trap_rate": (
            round(fmean(thick_traps), 6) if thick_traps else None
        ),
        "difference_in_differences_cohorts": len(did_medians),
        "mean_difference_in_differences_median_excess": (
            round(fmean(did_medians), 6) if did_medians else None
        ),
        "mean_difference_in_differences_trap_rate": (
            round(fmean(did_traps), 6) if did_traps else None
        ),
        "controls": _control_summaries(controls),
    }


def _aggregate_er_calibration(cohorts: Sequence[dict[str, object]]) -> dict[str, object]:
    absolute_errors: list[float] = []
    realized_spreads: list[float] = []
    total_n = 0
    for cohort in cohorts:
        calibration = cohort.get("er_calibration")
        if not isinstance(calibration, dict):
            continue
        if isinstance(calibration.get("n"), int):
            total_n += int(calibration["n"])
        quintiles = calibration.get("er_quintiles")
        if not isinstance(quintiles, list) or len(quintiles) < 2:
            continue
        errors = [
            abs(float(item["calibration_error"]))
            for item in quintiles
            if isinstance(item, dict) and isinstance(item.get("calibration_error"), int | float)
        ]
        if errors:
            absolute_errors.append(fmean(errors))
        first = quintiles[0]
        last = quintiles[-1]
        if isinstance(first, dict) and isinstance(last, dict):
            bottom = first.get("median_realized_price_excess")
            top = last.get("median_realized_price_excess")
            if isinstance(bottom, int | float) and isinstance(top, int | float):
                realized_spreads.append(float(top) - float(bottom))
    return {
        "cohorts": len(absolute_errors),
        "total_n": total_n,
        "mean_absolute_calibration_error": (
            round(fmean(absolute_errors), 6) if absolute_errors else None
        ),
        "mean_realized_top_bottom_spread": (
            round(fmean(realized_spreads), 6) if realized_spreads else None
        ),
        "spread_positive_share": (
            round(sum(value > 0 for value in realized_spreads) / len(realized_spreads), 4)
            if realized_spreads
            else None
        ),
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
