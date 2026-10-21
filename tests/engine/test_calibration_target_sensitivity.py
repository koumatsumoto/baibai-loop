"""Decision-target sensitivity cannot borrow another estimand's robustness."""

from __future__ import annotations

import pytest
from tests.helpers.calibration_store import forward_row, panel_row

from baibai_engine.screening.calibration.evaluation.cohorts import evaluate_cohorts
from baibai_engine.screening.calibration.evaluation.sensitivity import _signs_for_returns
from baibai_engine.screening.calibration.evidence import (
    CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    required_sensitivity_blockers,
)
from baibai_engine.screening.rule_config import load_screening_rules


def _coverage(**stability: bool) -> dict[str, object]:
    return {
        key: {"direction_stable": False, "metric_direction_stable": dict(stability)}
        for key in ("delisting_exclusion", "priced_master_without_universe")
    }


def test_unrequested_union_flip_does_not_block_er() -> None:
    coverage = _coverage(er_calibration=True, nomination_union_all=False)
    assert required_sensitivity_blockers(coverage, ("er_calibration",)) == ()
    assert required_sensitivity_blockers(coverage, ("nomination_union_all",)) == (
        "unpriced_exit_flips:nomination_union_all",
        "priced_master_without_universe_flips:nomination_union_all",
    )


def test_requested_er_flip_is_not_overridden_by_union() -> None:
    coverage = _coverage(er_calibration=False, nomination_union_all=True)
    assert required_sensitivity_blockers(coverage, ("er_calibration",)) == (
        "unpriced_exit_flips:er_calibration",
        "priced_master_without_universe_flips:er_calibration",
    )


def test_declared_guardrail_cannot_be_omitted() -> None:
    coverage = _coverage(er_calibration=True, normalized_per_3fy=False)
    assert required_sensitivity_blockers(coverage, ("er_calibration", "normalized_per_3fy")) == (
        "unpriced_exit_flips:normalized_per_3fy",
        "priced_master_without_universe_flips:normalized_per_3fy",
    )


def test_missing_sensitivity_is_not_legacy_global_success() -> None:
    coverage = {key: {"direction_stable": True} for key in _coverage()}
    assert required_sensitivity_blockers(coverage, ("pure_er_top5",)) == (
        "unpriced_exit_sensitivity_unmeasured:pure_er_top5",
        "priced_master_without_universe_sensitivity_unmeasured:pure_er_top5",
    )


def test_fidelity_is_not_an_outcome_sign() -> None:
    # Its mandatory metric status is still checked by evaluate_evidence_readiness.
    assert required_sensitivity_blockers({}, (CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,)) == ()


@pytest.mark.parametrize("missing_count", [None, 1, 20])
def test_relative_price_stability_cannot_certify_absolute_total_return(missing_count) -> None:
    coverage = _coverage(er_calibration=True)
    coverage["er_level_outcomes"] = {"missing_count": missing_count}
    assert required_sensitivity_blockers(coverage, ("er_calibration", "er_level_calibration")) == (
        "total_return_missingness_unmeasured:er_level_calibration",
    )


def test_complete_absolute_outcomes_need_no_missingness_imputation() -> None:
    coverage = _coverage(er_calibration=True)
    coverage["er_level_outcomes"] = {"missing_count": 0}
    assert required_sensitivity_blockers(coverage, ("er_calibration", "er_level_calibration")) == ()


def test_missing_top_er_outcomes_do_not_promote_lower_ranked_names() -> None:
    panel = [
        panel_row("2020-01-31", str(index), er_annual=1 - index / 1000) for index in range(120)
    ]
    # Top 5 are all missing; the rest meet the statistical sample floor.
    returns = {str(index): index / 100 for index in range(5, 120)}
    signs = _signs_for_returns(
        panel,
        returns,
        horizon="3y",
        stale_count=0,
        candidate_discovery_rules=load_screening_rules().candidate_discovery,
    )
    assert signs["pure_er_top5"] is None
    assert signs["pure_er_top10"] is not None
    assert "approach_current_earnings_power_nominations" in signs


def test_absolute_coverage_includes_missing_price_and_missing_dividend_outcomes() -> None:
    asof = "2020-01-31"
    panel = [
        panel_row(
            asof,
            str(index),
            close=100,
            er_annual=0.1,
            er_reversion_annual=0.08,
            er_carry_annual=0.02,
        )
        for index in range(120)
    ]
    forwards = [
        forward_row(
            asof,
            str(index),
            "3y",
            price_return=0.2,
            total_return=0.3,
            total_return_status="resolved",
        )
        for index in range(2, 120)
    ]
    forwards.extend(
        (
            forward_row(asof, "0", "3y", status="unresolved_missing_exit"),
            forward_row(asof, "1", "3y", price_return=0.2),
        )
    )
    results = evaluate_cohorts(
        {asof: panel},
        {asof: forwards},
        horizons=("3y",),
        candidate_discovery_rules=load_screening_rules().candidate_discovery,
    )
    outcome = results["3y"]["cohorts"][0]["coverage"]["er_level_outcomes"]
    assert outcome == {
        "prediction_count": 120,
        "observed_count": 118,
        "missing_count": 2,
        "missingness_sensitivity": "not_measured",
    }
