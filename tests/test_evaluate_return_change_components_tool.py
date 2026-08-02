from __future__ import annotations

from dataclasses import dataclass

from tools.evaluate_return_change_components import aggregate_cohorts, evaluate_cohort


@dataclass(frozen=True)
class _PanelRow:
    ticker: str
    in_population: bool
    per_trailing: float | None
    dividend_yield: float | None
    pbr: float | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    price_change_60d: float | None
    share_count_reduction_streak: int | None
    dps_guidance_up: bool | None


@dataclass(frozen=True)
class _ForwardRow:
    ticker: str
    horizon: str
    status: str
    price_return: float | None


def _cohort() -> dict[str, object]:
    panel: list[_PanelRow] = []
    forwards: list[_ForwardRow] = []
    for index in range(100):
        in_low_band = index < 20
        positive = in_low_band and index % 2 == 0
        control = float(index // 2)
        panel.append(
            _PanelRow(
                ticker=f"{index:04d}",
                in_population=True,
                per_trailing=float(index + 1),
                dividend_yield=control / 100,
                pbr=control,
                market_cap_oku=control * 100,
                avg_turnover_oku=control,
                price_change_60d=control / 10,
                share_count_reduction_streak=(1 if positive else 0) if in_low_band else None,
                dps_guidance_up=positive if in_low_band else None,
            )
        )
        forwards.append(
            _ForwardRow(
                ticker=f"{index:04d}",
                horizon="3y",
                status="resolved",
                price_return=0.1 if positive else (-0.3 if in_low_band else 0.0),
            )
        )
    return evaluate_cohort(panel, forwards, asof="2023-06-30", horizon="3y")


def test_components_use_the_same_low_per_band_and_controls() -> None:
    cohort = _cohort()

    assert cohort["low_valuation_n"] == 20
    components = cohort["components"]
    assert isinstance(components, dict)
    for component_name in ("share_count_reduction", "dps_guidance_up"):
        component = components[component_name]
        assert isinstance(component, dict)
        assert component["eligible_n"] == 20
        assert component["true"]["n"] == 10
        assert component["false"]["n"] == 10
        assert component["median_excess_delta"] == 0.4
        assert component["trap_rate_delta"] == -1.0
        for control in component["controls"].values():
            assert control["strata_used"] == 2
            assert control["matched_weight"] == 10
            assert control["stratified_median_excess_delta"] == 0.4
            assert control["stratified_trap_rate_delta"] == -1.0


def test_aggregate_keeps_component_samples_separate() -> None:
    cohort = _cohort()

    aggregate = aggregate_cohorts([cohort, cohort])

    for component_name in ("share_count_reduction", "dps_guidance_up"):
        component = aggregate[component_name]
        assert component["cohorts"] == 2
        assert component["true_n"] == 20
        assert component["false_n"] == 20
        assert component["mean_median_excess_delta"] == 0.4
        assert component["median_delta_positive_share"] == 1.0
        control = component["controls"]["price_change_60d"]
        assert control == {
            "available_cohorts": 2,
            "matched_weight": 20,
            "mean_stratified_median_excess_delta": 0.4,
            "mean_stratified_trap_rate_delta": -1.0,
        }
