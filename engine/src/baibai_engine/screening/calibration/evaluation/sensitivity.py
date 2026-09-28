"""tail除去とregime分割による評価の感度を測る。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import median

from baibai_engine.screening.calibration.evaluation.axes import (
    _evaluate_axis,
    _evaluate_er_calibration,
)
from baibai_engine.screening.calibration.evaluation.discovery import _evaluate_candidate_discovery
from baibai_engine.screening.calibration.evaluation.policy import AxisSpec
from baibai_engine.screening.calibration.evaluation.statistics import (
    _CohortExcessContext,
    _context_from_returns,
    _resolved_price_returns,
)
from baibai_engine.screening.calibration.forward import (
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.rule_config import CandidateDiscoveryRules

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
    "review_set_all",
    "er_calibration",
)

OPTIONAL_SENSITIVITY_METRICS: tuple[str, ...] = (
    "margin_short_to_adv",
    "normalized_per_3fy",
)

_ALL_SENSITIVITY_METRICS = (*_SENSITIVITY_METRICS, *OPTIONAL_SENSITIVITY_METRICS)


def _margin_short_to_adv_adoption_sign(value: object) -> float | None:
    """Encode the preregistered raw-annotation direction and trap conclusion."""
    if not isinstance(value, dict):
        return None
    spread = value.get("decile_spread_median")
    best_trap = value.get("best_decile_trap_rate")
    deciles = value.get("deciles")
    if (
        not isinstance(spread, int | float)
        or not isinstance(best_trap, int | float)
        or not isinstance(deciles, list)
        or not deciles
        or not isinstance(deciles[0], dict)
    ):
        return None
    worst_trap = deciles[0].get("trap_rate")
    if not isinstance(worst_trap, int | float):
        return None
    return float(spread > 0 and best_trap <= worst_trap)


def _direction_signs(
    context: _CohortExcessContext,
    panel: Sequence[PanelRow],
    *,
    horizon: str,
    candidate_discovery_rules: CandidateDiscoveryRules,
) -> dict[str, float | None]:
    """The sign-bearing quantity of each conclusion the evidence readiness reads."""
    candidate_discovery = _evaluate_candidate_discovery(
        context.population,
        context.excess,
        candidate_discovery_rules=candidate_discovery_rules,
    )
    signs: dict[str, float | None] = {}
    for key in ("review_set_all",):
        group = candidate_discovery.get(key)
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
    signs["margin_short_to_adv"] = _margin_short_to_adv_adoption_sign(
        _evaluate_axis(
            AxisSpec(name="margin_short_to_adv", direction=-1),
            context.population,
            context.excess,
        )
    )
    normalized_axis = _evaluate_axis(
        AxisSpec(name="normalized_per_3fy", direction=-1),
        context.population,
        context.excess,
    )
    normalized_spread = (
        normalized_axis.get("decile_spread_median") if isinstance(normalized_axis, dict) else None
    )
    signs["normalized_per_3fy"] = (
        float(normalized_spread) if isinstance(normalized_spread, int | float) else None
    )
    return signs


def _signs_for_returns(
    panel: Sequence[PanelRow],
    price_returns: Mapping[str, float],
    *,
    horizon: str,
    stale_count: int,
    candidate_discovery_rules: CandidateDiscoveryRules,
) -> dict[str, float | None]:
    context = _context_from_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)
    if context is None:
        return dict.fromkeys(_ALL_SENSITIVITY_METRICS)
    return _direction_signs(
        context,
        panel,
        horizon=horizon,
        candidate_discovery_rules=candidate_discovery_rules,
    )


def delisting_exclusion_sensitivity(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
    candidate_discovery_rules: CandidateDiscoveryRules,
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
    as_reported = _signs_for_returns(
        panel,
        price_returns,
        horizon=horizon,
        stale_count=stale_count,
        candidate_discovery_rules=candidate_discovery_rules,
    )
    imputed: dict[str, dict[str, float | None]] = {}
    for name in _DELISTING_IMPUTATIONS:
        value = _TOTAL_LOSS_RETURN if name == "total_loss" else neutral
        augmented = dict(price_returns)
        for ticker in excluded:
            augmented[ticker] = value
        imputed[name] = _signs_for_returns(
            panel,
            augmented,
            horizon=horizon,
            stale_count=stale_count,
            candidate_discovery_rules=candidate_discovery_rules,
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
    candidate_discovery_rules: CandidateDiscoveryRules,
) -> dict[str, object]:
    """Bound the conclusions' sensitivity to priced rows the screen could not evaluate.

    These rows have no valuation metrics or rank, and some also have no observed
    forward return. The reported case uses only observations, while both imputations
    assign every target a bounded value without inventing rank or E[r].
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
    as_reported = _signs_for_returns(
        panel,
        price_returns,
        horizon=horizon,
        stale_count=stale_count,
        candidate_discovery_rules=candidate_discovery_rules,
    )
    imputed: dict[str, dict[str, float | None]] = {}
    for name, value in (("total_loss", _TOTAL_LOSS_RETURN), ("neutral", neutral)):
        augmented = dict(price_returns)
        for ticker in targets:
            augmented[ticker] = value
        imputed[name] = _signs_for_returns(
            panel,
            augmented,
            horizon=horizon,
            stale_count=stale_count,
            candidate_discovery_rules=candidate_discovery_rules,
        )

    metric_stability = _metric_direction_stability(as_reported, imputed)
    stable = all(metric_stability[metric] for metric in _SENSITIVITY_METRICS)
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
            as_reported.get(metric),
            *(imputed[name].get(metric) for name in _DELISTING_IMPUTATIONS),
        ]
        present = [value for value in values if value is not None]
        # No conclusion under any case cannot have been produced by the exclusion;
        # whether the metric is reportable is handled by metric_statuses.
        stability[metric] = not present or (
            len(present) == len(values) and len({value > 0 for value in present}) == 1
        )
    return stability
