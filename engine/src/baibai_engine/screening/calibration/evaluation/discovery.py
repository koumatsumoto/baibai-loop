"""Candidate Discoveryの選抜をcohort上で再現する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.foundation.date_utils import add_months_clamped
from baibai_engine.screening.calibration.evaluation.policy import COMPARISON_TOP_NS
from baibai_engine.screening.calibration.evaluation.statistics import _group_stats
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.calibration.panel import PanelRow
from baibai_engine.screening.discovery.review_set import APPROACH_IDS
from baibai_engine.screening.rule_config import CandidateDiscoveryRules


def _evaluate_candidate_discovery(
    population: Sequence[PanelRow],
    excess: Mapping[str, float],
    *,
    candidate_discovery_rules: CandidateDiscoveryRules,
) -> dict[str, object]:
    result: dict[str, object] = {}
    review_set_rows = sorted(
        (row for row in population if row.in_review_set), key=lambda row: row.ticker
    )
    result["review_set_all"] = _group_stats([excess[row.ticker] for row in review_set_rows])

    approach_ranks = {
        row.ticker: _valuation_approach_rank_map(
            row, nomination_depth=candidate_discovery_rules.nomination_depth
        )
        for row in population
    }
    for approach in APPROACH_IDS:
        slug = approach.replace("-", "_")
        for top_n in COMPARISON_TOP_NS:
            result[f"approach_{slug}_top{top_n}"] = _group_stats(
                [
                    excess[row.ticker]
                    for row in population
                    if (rank := approach_ranks[row.ticker].get(approach)) is not None
                    and rank <= top_n
                ]
            )

    # Pure E[r] is a benchmark only; it neither gates nor fills the Review Set.
    population_by_er = sorted(
        (row for row in population if row.er_annual is not None),
        key=lambda row: (-(row.er_annual or 0.0), row.ticker),
    )
    for top_n in COMPARISON_TOP_NS:
        er_rows = population_by_er[:top_n]
        result[f"pure_er_top{top_n}"] = _group_stats(
            [excess[row.ticker] for row in population_by_er[:top_n]]
        )
        review_tickers = {row.ticker for row in review_set_rows}
        er_tickers = {row.ticker for row in er_rows}
        result[f"nomination_union_vs_er_top{top_n}"] = {
            "review_set_n": len(review_tickers),
            "pure_er_n": len(er_tickers),
            "overlap_n": len(review_tickers & er_tickers),
            "displaced_from_er_n": len(er_tickers - review_tickers),
        }
    return result


def _candidate_discovery_fidelity(
    panel: Sequence[PanelRow],
    excess: Mapping[str, float],
    candidate_discovery: Mapping[str, object],
    *,
    candidate_discovery_rules: CandidateDiscoveryRules,
    jpx_input_status: str,
) -> dict[str, object]:
    """Prove which production method roles one cohort actually replayed."""
    input_complete = jpx_input_status == "complete"
    approach_ranks = {
        row.ticker: _valuation_approach_rank_map(
            row, nomination_depth=candidate_discovery_rules.nomination_depth
        )
        for row in panel
        if row.in_population
    }
    approaches: dict[str, dict[str, object]] = {}
    for approach in APPROACH_IDS:
        nomination_count = sum(approach in ranks for ranks in approach_ranks.values())
        resolved_nomination_count = sum(
            approach in ranks and ticker in excess for ticker, ranks in approach_ranks.items()
        )
        approaches[approach] = {
            "nomination_depth": candidate_discovery_rules.nomination_depth,
            "nomination_count": nomination_count,
            "resolved_nomination_count": resolved_nomination_count,
            "eligible": (
                input_complete
                and nomination_count == candidate_discovery_rules.nomination_depth
                and resolved_nomination_count == candidate_discovery_rules.nomination_depth
            ),
        }

    review_rows = [row for row in panel if row.in_population and row.in_review_set]
    resolved_review_count = sum(row.ticker in excess for row in review_rows)
    nominated_tickers = {ticker for ticker, ranks in approach_ranks.items() if ranks}
    review_tickers = {row.ticker for row in review_rows}
    nomination_union_eligible = (
        input_complete
        and all(approaches[approach]["eligible"] is True for approach in APPROACH_IDS)
        and review_tickers == nominated_tickers
        and resolved_review_count == len(review_rows)
    )
    return {
        "jpx_regulation_input_status": jpx_input_status,
        "approaches": approaches,
        "review_count": len(review_rows),
        "resolved_review_count": resolved_review_count,
        "nomination_union_eligible": nomination_union_eligible,
    }


def _observation_dependence(
    cohorts: Sequence[Mapping[str, object]], *, horizon: str
) -> dict[str, object]:
    """Disclose overlap without pretending monthly forward windows are independent."""
    resolved_asofs = sorted(
        date.fromisoformat(str(cohort["asof"]))
        for cohort in cohorts
        if cohort.get("metric_calculation_status") == "resolved"
    )
    entry_year_counts: dict[str, int] = {}
    for asof in resolved_asofs:
        year = str(asof.year)
        entry_year_counts[year] = entry_year_counts.get(year, 0) + 1
    non_overlapping = 0
    next_available: date | None = None
    for asof in resolved_asofs:
        if next_available is not None and asof < next_available:
            continue
        non_overlapping += 1
        next_available = add_months_clamped(asof, require_horizon(horizon).months)
    return {
        "resolved_cohort_count": len(resolved_asofs),
        "forward_months": require_horizon(horizon).months,
        "entry_year_counts": entry_year_counts,
        "greedy_non_overlapping_window_count": non_overlapping,
    }


def _valuation_approach_rank_map(row: PanelRow, *, nomination_depth: int) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for token in row.valuation_approach_ranks.split("|"):
        if not token:
            continue
        approach, separator, raw_rank = token.rpartition(":")
        if separator and approach in APPROACH_IDS:
            try:
                rank = int(raw_rank)
            except ValueError:
                continue
            if 1 <= rank <= nomination_depth:
                ranks[approach] = rank
    return ranks
