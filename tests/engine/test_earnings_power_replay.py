from __future__ import annotations

from collections.abc import Mapping

import pytest
from tests.helpers.calibration_store import forward_row, panel_row

from baibai_engine.screening.selection.earnings_replay import (
    FrozenReplayPolicy,
    evaluate_frozen_replay,
)


def _meta() -> Mapping[str, object]:
    return {
        "panel_variant": "production",
        "production_authority": True,
        "master_snapshot_status": "exact_date",
        "asof_population_mismatch_count": 0,
        "priced_master_without_universe_count": 0,
        "bars_window_clamped": False,
        "fin_window_clamped": False,
    }


def _inputs(*, earnings_return: float, cohort_count: int = 2):
    panels = {}
    forwards = {}
    metas = {}
    for index in range(cohort_count):
        asof = f"2020-{index + 1:02d}-28"
        panels[asof] = [
            panel_row(
                asof,
                "E2",
                close=100.0,
                normalized_per_3fy=5.0,
                er_annual=0.10,
                sector_33="情報・通信業",
            ),
            panel_row(
                asof,
                "E1",
                close=100.0,
                normalized_per_3fy=5.0,
                er_annual=0.20,
                sector_33="サービス業",
            ),
            panel_row(
                asof,
                "V1",
                close=100.0,
                normalized_per_3fy=20.0,
                selection_rank=1,
                sector_33="卸売業",
            ),
            panel_row(
                asof,
                "V2",
                close=100.0,
                normalized_per_3fy=20.0,
                selection_rank=2,
                sector_33="小売業",
            ),
        ]
        forwards[asof] = [
            forward_row(
                asof,
                ticker,
                horizon,
                price_return=(earnings_return if ticker.startswith("E") else 0.1),
                total_return_status="resolved",
                total_return=(earnings_return if ticker.startswith("E") else 0.1),
                adjustment_factor_coverage="complete",
                exit_date="2026-01-01",
            )
            for horizon in ("3y", "5y")
            for ticker in ("E1", "E2", "V1", "V2")
        ]
        metas[asof] = _meta()
    return panels, forwards, metas


def _policy(*, minimum_cohorts: int = 2) -> FrozenReplayPolicy:
    return FrozenReplayPolicy(
        depth=2,
        minimum_cohorts_per_horizon=minimum_cohorts,
        minimum_median_alt_only_count=2,
        maximum_median_sector_concentration=0.5,
    )


def test_frozen_replay_is_eligible_when_both_bases_and_horizons_win() -> None:
    panels, forwards, metas = _inputs(earnings_return=0.3)

    result = evaluate_frozen_replay(
        panels,
        forwards,
        metas,
        bundle_id="bundle",
        bundle_manifest_sha256="a" * 64,
        policy=_policy(),
    )

    assert result["verdict"] == "eligible_for_shadow"
    assert result["geometry"][0] == {
        "asof": "2020-01-28",
        "earnings_count": 2,
        "value_carry_count": 2,
        "overlap_count": 0,
        "alt_only_count": 2,
        "earnings_sector_concentration": 0.5,
    }
    price = result["horizons"]["3y"]["bases"]["price_return"]
    assert price["cohort_equal_median_difference"]["as_reported"] == pytest.approx(0.2)


def test_frozen_replay_negative_is_not_rescued_by_complete_sensitivities() -> None:
    panels, forwards, metas = _inputs(earnings_return=-0.3)

    result = evaluate_frozen_replay(
        panels,
        forwards,
        metas,
        bundle_id="bundle",
        bundle_manifest_sha256="b" * 64,
        policy=_policy(),
    )

    assert result["verdict"] == "negative"
    assert result["verdict_inputs"]["negative_comparison"] is True


def test_frozen_replay_requires_the_preregistered_cohort_floor() -> None:
    panels, forwards, metas = _inputs(earnings_return=0.3, cohort_count=1)

    result = evaluate_frozen_replay(
        panels,
        forwards,
        metas,
        bundle_id="bundle",
        bundle_manifest_sha256="c" * 64,
        policy=_policy(minimum_cohorts=2),
    )

    assert result["verdict"] == "insufficient"
