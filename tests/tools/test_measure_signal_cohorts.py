from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.helpers.calibration_store import (
    calibration_root,
    liquid_panel_row,
    publish_forward,
    publish_panel,
)
from tools.experiments import measure_signal_cohorts
from tools.experiments.measure_signal_cohorts import (
    SignalCohortMeasurementError,
    build_measurement,
    main,
)

FORWARD_COLUMNS = (
    "asof",
    "ticker",
    "horizon",
    "price_return",
    "status",
    "total_return",
    "total_return_status",
)


def test_illiquid_rows_are_excluded_from_every_group(tmp_path: Path) -> None:
    directory = calibration_root(tmp_path)
    publish_panel(
        directory,
        "2024-01-31",
        [
            liquid_panel_row("1111", "2024-01-31"),
            liquid_panel_row("2222", "2024-01-31", market_cap_oku=50),
            liquid_panel_row("3333", "2024-01-31", avg_turnover_oku=0.2),
            liquid_panel_row("4444", "2024-01-31", listing_span_days=30),
        ],
    )
    publish_forward(
        directory,
        "2024-01-31",
        [
            {
                "asof": "2024-01-31",
                "ticker": ticker,
                "horizon": "1y",
                "price_return": "0.1",
                "status": "resolved",
            }
            for ticker in ("1111", "2222", "3333", "4444")
        ],
    )

    payload = build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)

    assert payload["panel_rows"] == 1


def test_unresolved_forward_rows_do_not_enter_the_median(tmp_path: Path) -> None:
    directory = calibration_root(tmp_path)
    publish_panel(
        directory,
        "2024-01-31",
        [liquid_panel_row("1111", "2024-01-31"), liquid_panel_row("2222", "2024-01-31")],
    )
    publish_forward(
        directory,
        "2024-01-31",
        [
            {
                "asof": "2024-01-31",
                "ticker": "1111",
                "horizon": "1y",
                "price_return": "0.2",
                "status": "resolved",
            },
            {
                "asof": "2024-01-31",
                "ticker": "2222",
                "horizon": "1y",
                "price_return": "9.9",
                "status": "unresolved_future_horizon",
            },
        ],
    )

    payload = build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)

    comparisons = payload["comparisons"]
    assert isinstance(comparisons, dict)
    population = comparisons["high_estimate"][0]["groups"][1]
    assert population["resolved_rows"] == 1
    assert population["median_annualized_pct"] == pytest.approx(20.0)


def test_share_count_group_uses_the_clip_and_reports_the_cohort_difference(
    tmp_path: Path,
) -> None:
    directory = calibration_root(tmp_path)
    rows = []
    forward = []
    for index in range(24):
        clipped = index < 12
        ticker = f"{1000 + index}"
        rows.append(
            liquid_panel_row(
                ticker,
                "2024-01-31",
                net_share_change_yoy=-0.20 if clipped else 0.0,
            )
        )
        forward.append(
            {
                "asof": "2024-01-31",
                "ticker": ticker,
                "horizon": "1y",
                "price_return": "0.30" if clipped else "0.10",
                "status": "resolved",
            }
        )
    publish_panel(directory, "2024-01-31", rows)
    publish_forward(directory, "2024-01-31", forward)

    payload = build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)

    comparisons = payload["comparisons"]
    assert isinstance(comparisons, dict)
    share_count = comparisons["share_count_component"][0]
    at_clip, partial, non_positive, unknown = share_count["groups"]
    assert at_clip["resolved_rows"] == 12
    assert partial["resolved_rows"] == 0
    assert non_positive["resolved_rows"] == 12
    assert unknown["group"] == "share_change_unobserved"
    assert unknown["resolved_rows"] == 0
    assert share_count["cohort_agreement"]["cohorts_compared"] == 1
    assert share_count["cohort_agreement"]["cohorts_treatment_ahead"] == 1
    assert share_count["cohort_agreement"]["median_difference_pct_points"] == pytest.approx(20.0)


def test_missing_store_fails_instead_of_reporting_an_empty_comparison(tmp_path: Path) -> None:
    directory = calibration_root(tmp_path)

    with pytest.raises(SignalCohortMeasurementError):
        build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)


def test_cli_writes_yaml_and_reports_a_missingcalibration_root(tmp_path: Path) -> None:
    directory = calibration_root(tmp_path)
    publish_panel(directory, "2024-01-31", [liquid_panel_row("1111", "2024-01-31")])
    publish_forward(
        directory,
        "2024-01-31",
        [
            {
                "asof": "2024-01-31",
                "ticker": "1111",
                "horizon": "1y",
                "price_return": "0.1",
                "status": "resolved",
            }
        ],
    )
    out = tmp_path / "measurement.yaml"

    assert main(["--calibration-dir", str(directory), "--horizon", "1y", "--out", str(out)]) == 0
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "signal-cohort-measurement"
    assert payload["metric_basis"] == "price_return_only"

    assert main(["--calibration-dir", str(tmp_path / "absent"), "--horizon", "1y"]) == 1


def _both_basescalibration_root(tmp_path: Path) -> Path:
    """One as-of where the two bases resolve different rows and different values."""

    directory = calibration_root(tmp_path)
    publish_panel(
        directory,
        "2020-01-31",
        [liquid_panel_row(f"{1000 + index}", "2020-01-31") for index in range(3)],
    )
    publish_forward(
        directory,
        "2020-01-31",
        [
            # 配当を受け取った行: total は price より高い。
            {
                "asof": "2020-01-31",
                "ticker": "1000",
                "horizon": "1y",
                "price_return": "0.10",
                "status": "resolved",
                "total_return": "0.16",
                "total_return_status": "resolved",
            },
            {
                "asof": "2020-01-31",
                "ticker": "1001",
                "horizon": "1y",
                "price_return": "0.20",
                "status": "resolved",
                "total_return": "0.26",
                "total_return_status": "resolved",
            },
            # 価格は解決したが配当を観測できなかった行: total basis では落ちる。
            {
                "asof": "2020-01-31",
                "ticker": "1002",
                "horizon": "1y",
                "price_return": "0.90",
                "status": "resolved",
                "total_return": "",
                "total_return_status": "unresolved_missing_dividend",
            },
        ],
    )
    return directory


def _population_group(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    comparison = payload["comparisons"]["high_estimate"][0]
    return next(
        group for group in comparison["groups"] if group["group"] == "liquidity_passing_population"
    )


def test_total_basis_uses_the_dividend_inclusive_return(tmp_path: Path) -> None:
    directory = _both_basescalibration_root(tmp_path)

    price = build_measurement(
        calibration_dir=directory, horizons=("1y",), er_threshold=0.085, basis="price"
    )
    total = build_measurement(
        calibration_dir=directory, horizons=("1y",), er_threshold=0.085, basis="total"
    )

    # price は 3 行の中央値 20%、total は配当を観測できた 2 行の中央値 21%。
    assert _population_group(price)["resolved_rows"] == 3
    assert _population_group(price)["median_annualized_pct"] == 20.0
    assert _population_group(total)["resolved_rows"] == 2
    assert _population_group(total)["median_annualized_pct"] == 21.0
    assert price["metric_basis"] == "price_return_only"
    assert total["metric_basis"] == "total_return_realized_dividends"


def test_basis_coverage_reports_both_denominators(tmp_path: Path) -> None:
    """total の中央値を「同じ群の別 basis」として読ませないための母数表明。"""

    directory = _both_basescalibration_root(tmp_path)

    payload = build_measurement(
        calibration_dir=directory, horizons=("1y",), er_threshold=0.085, basis="total"
    )

    coverage = payload["basis_coverage"]["1y"]
    assert coverage["price_resolved_rows"] == 3
    assert coverage["total_resolved_rows"] == 2
    assert coverage["total_to_price_ratio"] == 0.667
    # 2/3 は 0.75 を割るので、両 basis を並べて読める horizon ではない。
    assert coverage["bases_comparable"] is False


def test_measurement_reads_the_atomic_current_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One measurement cannot mix a replacement into its fixed current snapshot."""

    directory = calibration_root(tmp_path)
    for index in range(24):
        ticker = f"{1000 + index}"
        publish_panel(directory, "2024-01-31", [liquid_panel_row(ticker, "2024-01-31")])
        publish_forward(
            directory,
            "2024-01-31",
            [
                {
                    "asof": "2024-01-31",
                    "ticker": ticker,
                    "horizon": "1y",
                    "price_return": "0.10",
                    "status": "resolved",
                }
            ],
        )

    real = measure_signal_cohorts.require_single_rules_hash

    def publish_then_continue(bundle: Any) -> str:
        # Between fixing the generation and the first row read, which is where the
        # window is widest: the panel and forward passes each walk every cohort.
        publish_panel(directory, "2024-02-29", [liquid_panel_row("9999", "2024-02-29")])
        publish_forward(
            directory,
            "2024-02-29",
            [
                {
                    "asof": "2024-02-29",
                    "ticker": "9999",
                    "horizon": "1y",
                    "price_return": "5.00",
                    "status": "resolved",
                }
            ],
        )
        return real(bundle)

    monkeypatch.setattr(measure_signal_cohorts, "require_single_rules_hash", publish_then_continue)
    payload = build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)

    assert payload["panel_asof_start"] == "2024-01-31"
    assert payload["panel_asof_end"] == "2024-01-31"
    assert payload["calibration_snapshot"] == "current"
