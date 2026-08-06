from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
import yaml
from tools.measure_signal_cohorts import (
    SignalCohortMeasurementError,
    build_measurement,
    main,
)

PANEL_COLUMNS = (
    "asof",
    "ticker",
    "market_cap_oku",
    "avg_turnover_oku",
    "listing_span_days",
    "per_forward",
    "per_trailing",
    "dividend_yield",
    "net_share_change_yoy",
    "er_annual",
    "er_reversion_annual",
    "er_carry_annual",
    "er_upside_capped",
)
FORWARD_COLUMNS = ("asof", "ticker", "horizon", "price_return", "status")


def _write_panel(
    directory: Path, asof: str, rows: list[dict[str, Any]], *, rules_hash: str = "abc123"
) -> None:
    (directory / f"panel-{asof}.meta.yaml").write_text(
        f"asof: '{asof}'\nrules_hash: {rules_hash}\n", encoding="utf-8"
    )
    path = directory / f"panel-{asof}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PANEL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in PANEL_COLUMNS})


def _write_forward(directory: Path, asof: str, rows: list[dict[str, Any]]) -> None:
    path = directory / f"forward-{asof}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORWARD_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in FORWARD_COLUMNS})


def _liquid(ticker: str, asof: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "asof": asof,
        "ticker": ticker,
        "market_cap_oku": 500,
        "avg_turnover_oku": 5,
        "listing_span_days": 900,
        "per_forward": 10,
        "per_trailing": 11,
        "dividend_yield": 0.03,
        "net_share_change_yoy": 0.0,
        "er_annual": 0.05,
        "er_reversion_annual": 0.02,
        "er_carry_annual": 0.03,
        "er_upside_capped": 0.2,
    }
    row.update(overrides)
    return row


def _store(tmp_path: Path) -> Path:
    directory = tmp_path / "calibration"
    directory.mkdir()
    return directory


def test_illiquid_rows_are_excluded_from_every_group(tmp_path: Path) -> None:
    directory = _store(tmp_path)
    _write_panel(
        directory,
        "2024-01-31",
        [
            _liquid("1111", "2024-01-31"),
            _liquid("2222", "2024-01-31", market_cap_oku=50),
            _liquid("3333", "2024-01-31", avg_turnover_oku=0.2),
            _liquid("4444", "2024-01-31", listing_span_days=30),
        ],
    )
    _write_forward(
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
    directory = _store(tmp_path)
    _write_panel(
        directory,
        "2024-01-31",
        [_liquid("1111", "2024-01-31"), _liquid("2222", "2024-01-31")],
    )
    _write_forward(
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


def test_buyback_group_uses_the_clip_and_reports_the_cohort_difference(tmp_path: Path) -> None:
    directory = _store(tmp_path)
    rows = []
    forward = []
    for index in range(24):
        clipped = index < 12
        ticker = f"{1000 + index}"
        rows.append(
            _liquid(
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
    _write_panel(directory, "2024-01-31", rows)
    _write_forward(directory, "2024-01-31", forward)

    payload = build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)

    comparisons = payload["comparisons"]
    assert isinstance(comparisons, dict)
    buyback = comparisons["buyback_component"][0]
    at_clip, partial, non_positive = buyback["groups"]
    assert at_clip["resolved_rows"] == 12
    assert partial["resolved_rows"] == 0
    assert non_positive["resolved_rows"] == 12
    assert buyback["cohort_agreement"]["cohorts_compared"] == 1
    assert buyback["cohort_agreement"]["cohorts_treatment_ahead"] == 1
    assert buyback["cohort_agreement"]["median_difference_pct_points"] == pytest.approx(20.0)


def test_missing_store_fails_instead_of_reporting_an_empty_comparison(tmp_path: Path) -> None:
    directory = _store(tmp_path)

    with pytest.raises(SignalCohortMeasurementError):
        build_measurement(calibration_dir=directory, horizons=("1y",), er_threshold=0.085)


def test_cli_writes_yaml_and_reports_a_missing_store(tmp_path: Path) -> None:
    directory = _store(tmp_path)
    _write_panel(directory, "2024-01-31", [_liquid("1111", "2024-01-31")])
    _write_forward(
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
