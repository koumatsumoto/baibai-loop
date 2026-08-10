from __future__ import annotations

import pytest
from tools.experiments.measure_capacity_native_universe import Candidate, CapacityFact
from tools.experiments.replay_capacity_native_universe import (
    ExcessObservation,
    _cohort_weighted_stats,
    _quintile_members,
    _stats,
)


def _candidate(ticker: str, er: float) -> Candidate:
    return Candidate(
        ticker=ticker,
        sector="A",
        market="PRIME",
        er_annual=er,
        er_carry_annual=0.01,
        er_reversion_annual=0.02,
        market_cap_oku=100,
        avg_turnover_oku=1,
        listing_span_days=365,
        fact=CapacityFact(
            minimum_lot_yen=100_000,
            trading_value_median_60d=10_000_000,
            trading_value_p20_60d=10_000_000,
            usable_sessions=60,
            expected_sessions=60,
            nonzero_volume_share_60d=1,
            no_trade_share_60d=0,
            zero_return_share_60d=0,
            return_pair_count=59,
            amihud_illiquidity_median_60d=0,
            amihud_illiquidity_p90_60d=0,
            capacity_days_by_notional={"starter": 1, "standard": 3, "stress": 5},
            core_fact_available=True,
        ),
    )


def test_quintiles_are_deterministic_at_ties() -> None:
    candidates = [_candidate(str(index), 0.1) for index in range(10)]
    assert _quintile_members(candidates) == {
        "0": 1,
        "1": 1,
        "2": 2,
        "3": 2,
        "4": 3,
        "5": 3,
        "6": 4,
        "7": 4,
        "8": 5,
        "9": 5,
    }


def test_stats_keep_trap_boundary_inclusive() -> None:
    observations = [
        ExcessObservation(asof="2020-01-31", ticker="A", value=-0.20),
        ExcessObservation(asof="2020-01-31", ticker="B", value=0.10),
    ]
    assert _stats(observations) == {"n": 2, "median_excess": -0.05, "trap_rate": 0.5}


def test_cohort_weighting_does_not_weight_large_cohort_more() -> None:
    observations = [
        ExcessObservation(asof="2020-01-31", ticker="A", value=-0.5),
        ExcessObservation(asof="2020-02-28", ticker="B", value=0.2),
        ExcessObservation(asof="2020-02-28", ticker="C", value=0.4),
        ExcessObservation(asof="2020-02-28", ticker="D", value=0.6),
    ]
    result = _cohort_weighted_stats(observations)
    assert result["cohorts"] == 2
    assert result["median_of_cohort_medians"] == pytest.approx(-0.05)
    assert result["mean_cohort_trap_rate"] == 0.5
