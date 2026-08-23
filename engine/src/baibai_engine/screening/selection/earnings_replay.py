"""Frozen historical replay for the Earnings Power v1 selection policy.

The functions in this module are deliberately outcome-input driven.  Policy
thresholds and ordering come from the already frozen selection policy; this module
only compares the resulting cohorts with the existing Value / Carry comparator.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from ..calibration.evaluation import evaluate_cohorts
from ..calibration.forward import ForwardReturnRow
from ..calibration.panel import PanelRow

_HORIZONS = ("3y", "5y")
_BASES = ("price_return", "total_return")
_SENSITIVITIES = ("as_reported", "neutral", "failure")


@dataclass(frozen=True, slots=True)
class FrozenReplayPolicy:
    maximum_normalized_per_3fy: float = 12.0
    depth: int = 20
    minimum_cohorts_per_horizon: int = 12
    minimum_total_return_coverage: float = 0.75
    minimum_median_alt_only_count: float = 5.0
    maximum_median_sector_concentration: float = 0.50


FROZEN_REPLAY_POLICY = FrozenReplayPolicy()


@dataclass(frozen=True, slots=True)
class ReplayGroup:
    tickers: tuple[str, ...]
    sector_concentration: float | None


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _earnings_group(panel: Sequence[PanelRow], policy: FrozenReplayPolicy) -> ReplayGroup:
    eligible = [
        row
        for row in panel
        if row.in_population
        and _finite(row.normalized_per_3fy)
        and cast(float, row.normalized_per_3fy) > 0
        and cast(float, row.normalized_per_3fy) <= policy.maximum_normalized_per_3fy
    ]
    ordered = sorted(
        eligible,
        key=lambda row: (
            cast(float, row.normalized_per_3fy),
            1 if not _finite(row.er_annual) else 0,
            -(cast(float, row.er_annual) if _finite(row.er_annual) else 0.0),
            row.ticker,
        ),
    )[: policy.depth]
    sectors: dict[str, int] = {}
    for row in ordered:
        sectors[row.sector_33] = sectors.get(row.sector_33, 0) + 1
    concentration = max(sectors.values()) / len(ordered) if ordered else None
    return ReplayGroup(tuple(row.ticker for row in ordered), concentration)


def _value_carry_group(panel: Sequence[PanelRow], policy: FrozenReplayPolicy) -> ReplayGroup:
    ordered = sorted(
        (
            row
            for row in panel
            if row.in_population
            and row.selection_rank is not None
            and 1 <= row.selection_rank <= policy.depth
        ),
        key=lambda row: (cast(int, row.selection_rank), row.ticker),
    )
    sectors: dict[str, int] = {}
    for row in ordered:
        sectors[row.sector_33] = sectors.get(row.sector_33, 0) + 1
    concentration = max(sectors.values()) / len(ordered) if ordered else None
    return ReplayGroup(tuple(row.ticker for row in ordered), concentration)


def _integrity_reasons(
    meta: Mapping[str, object], panel: Sequence[PanelRow], coverage: Mapping[str, object]
) -> tuple[str, ...]:
    """Mirror the canonical long-horizon coverage blockers used by calibration CLI."""
    reasons: list[str] = []
    if meta.get("panel_variant") != "production" or meta.get("production_authority") is not True:
        reasons.append("production_authority")
    if meta.get("master_snapshot_status") != "exact_date":
        reasons.append("master_snapshot")
    if meta.get("asof_population_mismatch_count") != 0:
        reasons.append("survivorship")
    if bool(meta.get("bars_window_clamped") or meta.get("fin_window_clamped")):
        reasons.append("input_range_clamped")
    if any(row.self_range_degraded for row in panel):
        reasons.append("self_range_degraded")
    if coverage.get("adjustment_factor_coverage") != "complete":
        reasons.append("adjustment_factor")
    if coverage.get("candidate_partition_complete") is not True:
        reasons.append("candidate_partition_incomplete")
    for field, label in (
        ("entry_price_gap_count", "entry_price_gap"),
        ("future_horizon_count", "horizon_not_matured"),
        ("unclassified_unresolved_count", "unclassified_unresolved"),
    ):
        value = coverage.get(field)
        if not isinstance(value, int) or value:
            reasons.append(label)
    unevaluated = meta.get("priced_master_without_universe_count")
    unevaluated_sensitivity = coverage.get("priced_master_without_universe")
    if (
        not isinstance(unevaluated, int)
        or not isinstance(unevaluated_sensitivity, dict)
        or unevaluated_sensitivity.get("excluded_count") != unevaluated
    ):
        reasons.append("priced_master_without_universe_unmeasured")
    elif unevaluated_sensitivity.get("direction_stable") is not True:
        reasons.append("priced_master_without_universe_flips_direction")
    delisting = coverage.get("delisting_exclusion")
    if isinstance(delisting, dict) and delisting.get("direction_stable") is not True:
        reasons.append("unpriced_exit_flips_direction")
    return tuple(dict.fromkeys(reasons))


def _basis_value(row: ForwardReturnRow, basis: str) -> float | None:
    if basis == "price_return":
        return cast(float, row.price_return) if row.resolved and _finite(row.price_return) else None
    return (
        cast(float, row.total_return)
        if row.total_return_status == "resolved" and _finite(row.total_return)
        else None
    )


def _group_observation(
    tickers: Sequence[str], rows: Mapping[str, ForwardReturnRow], basis: str
) -> dict[str, object]:
    reported: list[float] = []
    neutral: list[float] = []
    failure: list[float] = []
    for ticker in tickers:
        value = _basis_value(rows[ticker], basis) if ticker in rows else None
        if value is not None:
            reported.append(value)
            neutral.append(value)
            failure.append(value)
        else:
            neutral.append(0.0)
            failure.append(-1.0)
    return {
        "expected_count": len(tickers),
        "resolved_count": len(reported),
        "coverage": len(reported) / len(tickers) if tickers else 0.0,
        "median": {
            "as_reported": _median(reported),
            "neutral": _median(neutral),
            "failure": _median(failure),
        },
        "trap_rate": sum(value <= -0.20 for value in reported) / len(reported)
        if reported
        else None,
        "reported_values": tuple(reported),
    }


def _aggregate_group(cohorts: Sequence[Mapping[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for sensitivity in _SENSITIVITIES:
        cohort_medians = [
            cast(float, cast(Mapping[str, object], cohort["median"])[sensitivity])
            for cohort in cohorts
            if _finite(cast(Mapping[str, object], cohort["median"])[sensitivity])
        ]
        result[f"cohort_equal_median_{sensitivity}"] = _median(cohort_medians)
    reported_values = [
        cast(float, value)
        for cohort in cohorts
        for value in cast(Sequence[object], cohort["reported_values"])
        if _finite(value)
    ]
    expected = sum(cast(int, cohort["expected_count"]) for cohort in cohorts)
    resolved = sum(cast(int, cohort["resolved_count"]) for cohort in cohorts)
    result.update(
        {
            "ticker_equal_median_as_reported": _median(reported_values),
            "coverage": resolved / expected if expected else 0.0,
            "trap_rate": (
                sum(value <= -0.20 for value in reported_values) / len(reported_values)
                if reported_values
                else None
            ),
        }
    )
    return result


def evaluate_frozen_replay(
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    metas: Mapping[str, Mapping[str, object]],
    *,
    bundle_id: str,
    bundle_manifest_sha256: str,
    policy: FrozenReplayPolicy = FROZEN_REPLAY_POLICY,
) -> dict[str, object]:
    """Evaluate the frozen policy without changing thresholds from observed outcomes."""
    canonical = evaluate_cohorts(panels, forwards, horizons=_HORIZONS)
    geometry: list[dict[str, object]] = []
    groups: dict[str, tuple[ReplayGroup, ReplayGroup]] = {}
    for asof in sorted(panels):
        earnings = _earnings_group(panels[asof], policy)
        value_carry = _value_carry_group(panels[asof], policy)
        groups[asof] = (earnings, value_carry)
        overlap = set(earnings.tickers).intersection(value_carry.tickers)
        geometry.append(
            {
                "asof": asof,
                "earnings_count": len(earnings.tickers),
                "value_carry_count": len(value_carry.tickers),
                "overlap_count": len(overlap),
                "alt_only_count": len(set(earnings.tickers) - set(value_carry.tickers)),
                "earnings_sector_concentration": earnings.sector_concentration,
            }
        )

    horizon_results: dict[str, object] = {}
    all_differences: list[dict[str, object]] = []
    for horizon in _HORIZONS:
        raw = cast(Mapping[str, object], canonical[horizon])
        raw_cohorts = cast(Sequence[Mapping[str, object]], raw["cohorts"])
        eligible_asofs: list[str] = []
        excluded: list[dict[str, object]] = []
        coverage_by_asof: dict[str, Mapping[str, object]] = {}
        for raw_cohort in raw_cohorts:
            asof = cast(str, raw_cohort["asof"])
            coverage = cast(Mapping[str, object], raw_cohort["coverage"])
            coverage_by_asof[asof] = coverage
            reasons = _integrity_reasons(metas[asof], panels[asof], coverage)
            if reasons:
                excluded.append({"asof": asof, "reasons": list(reasons)})
            else:
                eligible_asofs.append(asof)

        bases: dict[str, object] = {}
        for basis in _BASES:
            earnings_cohorts: list[dict[str, object]] = []
            value_cohorts: list[dict[str, object]] = []
            alt_cohorts: list[dict[str, object]] = []
            for asof in eligible_asofs:
                rows = {row.ticker: row for row in forwards.get(asof, ()) if row.horizon == horizon}
                earnings, value_carry = groups[asof]
                alt_only = tuple(sorted(set(earnings.tickers) - set(value_carry.tickers)))
                earnings_cohorts.append(_group_observation(earnings.tickers, rows, basis))
                value_cohorts.append(_group_observation(value_carry.tickers, rows, basis))
                alt_cohorts.append(_group_observation(alt_only, rows, basis))
            earnings_aggregate = _aggregate_group(earnings_cohorts)
            value_aggregate = _aggregate_group(value_cohorts)
            alt_aggregate = _aggregate_group(alt_cohorts)
            differences = {
                sensitivity: (
                    cast(float, earnings_aggregate[f"cohort_equal_median_{sensitivity}"])
                    - cast(float, value_aggregate[f"cohort_equal_median_{sensitivity}"])
                    if _finite(earnings_aggregate[f"cohort_equal_median_{sensitivity}"])
                    and _finite(value_aggregate[f"cohort_equal_median_{sensitivity}"])
                    else None
                )
                for sensitivity in _SENSITIVITIES
            }
            all_differences.append({"horizon": horizon, "basis": basis, "differences": differences})
            bases[basis] = {
                "earnings_power": earnings_aggregate,
                "value_carry": value_aggregate,
                "alt_only": alt_aggregate,
                "cohort_equal_median_difference": differences,
            }
        horizon_results[horizon] = {
            "eligible_cohort_count": len(eligible_asofs),
            "eligible_asofs": eligible_asofs,
            "excluded_cohorts": excluded,
            "bases": bases,
        }

    eligible_counts = [
        cast(int, cast(Mapping[str, object], horizon_results[h])["eligible_cohort_count"])
        for h in _HORIZONS
    ]
    negative = any(
        all(
            _finite(cast(Mapping[str, object], item["differences"])[sensitivity])
            and cast(float, cast(Mapping[str, object], item["differences"])[sensitivity]) < 0
            for sensitivity in _SENSITIVITIES
        )
        for item in all_differences
    )
    sign_split = any(
        len(
            {
                cast(float, value) >= 0
                for value in cast(Mapping[str, object], item["differences"]).values()
                if _finite(value)
            }
        )
        > 1
        for item in all_differences
    )
    total_coverages = [
        cast(float, cast(Mapping[str, object], basis_result[group])["coverage"])
        for horizon in _HORIZONS
        for basis_name, basis_result in cast(
            Mapping[str, Mapping[str, object]],
            cast(Mapping[str, object], horizon_results[horizon])["bases"],
        ).items()
        if basis_name == "total_return"
        for group in ("earnings_power", "value_carry")
    ]
    total_return_coverage_low = any(
        value < policy.minimum_total_return_coverage for value in total_coverages
    )
    all_nonnegative = all(
        _finite(cast(Mapping[str, object], item["differences"])["as_reported"])
        and cast(float, cast(Mapping[str, object], item["differences"])["as_reported"]) >= 0
        for item in all_differences
    )
    alt_counts = [cast(int, row["alt_only_count"]) for row in geometry]
    concentrations = [
        cast(float, row["earnings_sector_concentration"])
        for row in geometry
        if _finite(row["earnings_sector_concentration"])
    ]
    median_alt_count = _median(alt_counts)
    median_concentration = _median(concentrations)
    if any(count < policy.minimum_cohorts_per_horizon for count in eligible_counts):
        verdict: Literal["insufficient", "negative", "inconclusive", "eligible_for_shadow"] = (
            "insufficient"
        )
    elif negative:
        verdict = "negative"
    elif sign_split or total_return_coverage_low:
        verdict = "inconclusive"
    elif (
        all_nonnegative
        and median_alt_count is not None
        and median_alt_count >= policy.minimum_median_alt_only_count
        and median_concentration is not None
        and median_concentration <= policy.maximum_median_sector_concentration
    ):
        verdict = "eligible_for_shadow"
    else:
        verdict = "negative"

    return {
        "kind": "earnings-power-v1-frozen-historical-replay",
        "bundle": {
            "bundle_id": bundle_id,
            "manifest_sha256": bundle_manifest_sha256,
        },
        "policy": asdict(policy),
        "source_rules_hashes": sorted(
            {
                cast(str, meta["rules_hash"])
                for meta in metas.values()
                if isinstance(meta.get("rules_hash"), str)
            }
        ),
        "geometry": geometry,
        "horizons": horizon_results,
        "verdict_inputs": {
            "negative_comparison": negative,
            "sensitivity_sign_split": sign_split,
            "total_return_coverage_below_floor": total_return_coverage_low,
            "median_alt_only_count": median_alt_count,
            "median_sector_concentration": median_concentration,
            "all_as_reported_differences_nonnegative": all_nonnegative,
        },
        "verdict": verdict,
    }
