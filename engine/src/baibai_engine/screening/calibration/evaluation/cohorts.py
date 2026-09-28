"""cohorts for evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY
from baibai_engine.screening.calibration.evaluation.aggregate import _aggregate
from baibai_engine.screening.calibration.evaluation.axes import (
    _adjustment_factor_status,
    _evaluate_asset_backed_hypotheses,
    _evaluate_axis,
    _evaluate_er_calibration,
    _evaluate_er_level_calibration,
    _evaluate_gates,
    _evaluate_margin_supply_demand_hypotheses,
    _evaluate_profit_normalization_hypotheses,
    _evaluate_reversion,
    _evaluate_sector_median_basis,
    _evaluate_shareholder_return_change,
)
from baibai_engine.screening.calibration.evaluation.discovery import (
    _candidate_discovery_fidelity,
    _evaluate_candidate_discovery,
    _observation_dependence,
)
from baibai_engine.screening.calibration.evaluation.policy import AXES
from baibai_engine.screening.calibration.evaluation.sensitivity import (
    delisting_exclusion_sensitivity,
    priced_master_without_universe_sensitivity,
)
from baibai_engine.screening.calibration.evaluation.statistics import (
    _cohort_excess_context,
    _status_counts,
)
from baibai_engine.screening.calibration.evidence import (
    CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    candidate_discovery_approach_fidelity_metric,
)
from baibai_engine.screening.calibration.forward import (
    CONTROL_EVENT_EXIT_STATUS,
    FAILURE_EXIT_STATUS,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.discovery.review_set import APPROACH_IDS
from baibai_engine.screening.rule_config import CandidateDiscoveryRules


def evaluate_cohorts(
    panels: Mapping[str, Sequence[PanelRow]],
    forwards: Mapping[str, Sequence[ForwardReturnRow]],
    *,
    horizons: Sequence[str],
    candidate_discovery_rules: CandidateDiscoveryRules,
    candidate_discovery_input_statuses: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Evaluate all cohorts and aggregate per horizon.

    ``panels`` / ``forwards`` は asof (ISO 文字列) を key にする。
    """
    per_horizon: dict[str, object] = {}
    input_statuses = candidate_discovery_input_statuses or {}
    for horizon in horizons:
        require_horizon(horizon)
        cohort_results: list[dict[str, object]] = []
        for asof in sorted(panels):
            cohort_results.append(
                _evaluate_cohort(
                    panels[asof],
                    forwards.get(asof, ()),
                    asof=asof,
                    horizon=horizon,
                    candidate_discovery_rules=candidate_discovery_rules,
                    candidate_discovery_jpx_input_status=input_statuses.get(asof, "unavailable"),
                )
            )
        per_horizon[horizon] = {
            "evidence_role": require_horizon(horizon).evidence_role,
            "cohorts": cohort_results,
            "observation_dependence": _observation_dependence(cohort_results, horizon=horizon),
            "aggregate": _aggregate(
                cohort_results, candidate_discovery_rules=candidate_discovery_rules
            ),
        }
    return per_horizon


def _evaluate_cohort(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    asof: str,
    horizon: str,
    candidate_discovery_rules: CandidateDiscoveryRules,
    candidate_discovery_jpx_input_status: str,
) -> dict[str, object]:
    context = _cohort_excess_context(panel, forward_rows, horizon=horizon)
    all_rows = [row for row in forward_rows if row.horizon == horizon]
    candidate_rows = [row for row in all_rows if row.ticker != TOPIX_ETF_PROXY]
    unresolved = [row for row in candidate_rows if not row.resolved]
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
        "resolved_count": sum(1 for row in candidate_rows if row.resolved),
        # Windows a completed cash tender offer priced instead of a market close.
        # Counted apart so a reader can see how much of the resolved population is
        # settled takeover consideration rather than an observed quote.
        "control_event_exit_count": sum(
            1 for row in candidate_rows if row.status == CONTROL_EVENT_EXIT_STATUS
        ),
        # Windows the exchange closed by removing a failing company, priced at the last
        # close it printed. Counted apart for the same reason: these carry the left tail,
        # so a reader has to be able to see how much of a cohort's loss comes from them.
        "failure_exit_count": sum(1 for row in candidate_rows if row.status == FAILURE_EXIT_STATUS),
        "data_unresolved_count": len(unresolved),
        "data_unresolved_reason_counts": unresolved_reasons,
        # Unresolved rows are not one kind of defect. A name that was not listed
        # at asof is a correct exclusion; a name priced earlier but absent at
        # asof would be a silently dropped tradeable name; a name whose series
        # ends inside the window is the survivorship exposure that needs an exit
        # value. Only the last two can bias a cohort, so the evidence readiness
        # reads these counts rather than the undivided total.
        "entry_not_listed_count": len(entry_not_listed),
        "entry_price_gap_count": len(entry_price_gap),
        "unpriced_exit_count": len(unpriced_exit),
        # Whether those exclusions could have produced the cohort's conclusions.
        "delisting_exclusion": delisting_exclusion_sensitivity(
            panel,
            forward_rows,
            horizon=horizon,
            candidate_discovery_rules=candidate_discovery_rules,
        ),
        "priced_master_without_universe": priced_master_without_universe_sensitivity(
            panel,
            forward_rows,
            horizon=horizon,
            candidate_discovery_rules=candidate_discovery_rules,
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
        fidelity = _candidate_discovery_fidelity(
            panel,
            {},
            {},
            candidate_discovery_rules=candidate_discovery_rules,
            jpx_input_status=candidate_discovery_jpx_input_status,
        )
        return {
            "asof": asof,
            "horizon": horizon,
            "metric_basis": "price_return_only",
            "metric_bases": ["price_return_only", "fy_actual_dividend_total_return"],
            "coverage": coverage,
            "metric_calculation_status": "unresolved",
            "axes": {},
            "candidate_discovery": {},
            "candidate_discovery_fidelity": fidelity,
            "gates": {},
            "sector_median_basis": {},
            "reversion": {},
            "shareholder_return_change": {},
            "margin_supply_demand_hypotheses": {},
            "profit_normalization_hypotheses": {},
            "asset_backed_hypotheses": {},
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
    candidate_discovery = _evaluate_candidate_discovery(
        population, excess, candidate_discovery_rules=candidate_discovery_rules
    )
    candidate_discovery_fidelity = _candidate_discovery_fidelity(
        panel,
        excess,
        candidate_discovery,
        candidate_discovery_rules=candidate_discovery_rules,
        jpx_input_status=candidate_discovery_jpx_input_status,
    )
    margin_hypotheses = _evaluate_margin_supply_demand_hypotheses(population, excess)
    profit_hypotheses = _evaluate_profit_normalization_hypotheses(population, excess)
    asset_backed_hypotheses = _evaluate_asset_backed_hypotheses(population, excess)
    return_change = _evaluate_shareholder_return_change(population, excess)
    er_calibration = _evaluate_er_calibration(
        population, excess, years=require_horizon(horizon).months / 12
    )
    er_level_calibration = _evaluate_er_level_calibration(population, forward_rows, horizon=horizon)
    metric_statuses = {
        key: ("eligible" if isinstance(value, dict) and value.get("n", 0) else "unresolved")
        for key, value in candidate_discovery.items()
    }
    metric_statuses["er_calibration"] = "eligible" if er_calibration else "unresolved"
    metric_statuses["er_level_calibration"] = "eligible" if er_level_calibration else "unresolved"
    metric_statuses["shareholder_return_change"] = (
        "eligible" if return_change.get("eligible_n", 0) else "unresolved"
    )
    metric_statuses["margin_short_to_adv"] = (
        "eligible" if "margin_short_to_adv" in axes else "unresolved"
    )
    metric_statuses["normalized_per_3fy"] = (
        "eligible" if "normalized_per_3fy" in axes else "unresolved"
    )
    approaches = candidate_discovery_fidelity["approaches"]
    assert isinstance(approaches, dict)
    for approach in APPROACH_IDS:
        approach_fidelity = approaches[approach]
        assert isinstance(approach_fidelity, dict)
        metric_statuses[candidate_discovery_approach_fidelity_metric(approach)] = (
            "eligible" if approach_fidelity["eligible"] is True else "unresolved"
        )
    metric_statuses[CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC] = (
        "eligible"
        if candidate_discovery_fidelity["nomination_union_eligible"] is True
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
        "candidate_discovery": candidate_discovery,
        "candidate_discovery_fidelity": candidate_discovery_fidelity,
        "gates": _evaluate_gates(population, excess),
        "sector_median_basis": _evaluate_sector_median_basis(population, excess),
        "reversion": _evaluate_reversion(population, excess),
        "shareholder_return_change": return_change,
        "margin_supply_demand_hypotheses": margin_hypotheses,
        "profit_normalization_hypotheses": profit_hypotheses,
        "asset_backed_hypotheses": asset_backed_hypotheses,
        "er_calibration": er_calibration,
        "er_level_calibration": er_level_calibration,
    }
