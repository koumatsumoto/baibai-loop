from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from tools.experiments.measure_signal_cohorts import SignalCohortMeasurementError
from tools.experiments.measure_supply_context import SupplyContextError, build_supply_context, main

PANEL_COLUMNS = (
    "asof",
    "ticker",
    "market_cap_oku",
    "avg_turnover_oku",
    "listing_span_days",
    "er_annual",
    "selection_rank",
    "pass_screen",
)


def _panel(
    directory: Path, asof: str, rows: list[dict[str, Any]], *, rules_hash: str = "abc123"
) -> None:
    (directory / f"panel-{asof}.meta.yaml").write_text(
        f"asof: '{asof}'\nrules_hash: {rules_hash}\n", encoding="utf-8"
    )
    with (directory / f"panel-{asof}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PANEL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in PANEL_COLUMNS})


def _liquid(ticker: str, asof: str, *, er: float, rank: int | str = "") -> dict[str, Any]:
    return {
        "asof": asof,
        "ticker": ticker,
        "market_cap_oku": 500,
        "avg_turnover_oku": 5,
        "listing_span_days": 900,
        "er_annual": er,
        "selection_rank": rank,
        "pass_screen": "True",
    }


def _history(directory: Path, *, levels: list[float]) -> None:
    """月ごとに水準の違う panel を作る。上位 5 の平均がその月の座標になる。"""

    for index, level in enumerate(levels):
        asof = f"2024-{index + 1:02d}-28"
        _panel(
            directory,
            asof,
            [_liquid(f"{1000 + slot}", asof, er=level, rank=slot + 1) for slot in range(5)],
        )


def _runs_db(path: Path, *, estimates: list[float]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE screening_run (run_revision_id TEXT, asof_date TEXT);
        CREATE TABLE screening_selection (
            selection_id TEXT, run_revision_id TEXT, created_at TEXT
        );
        CREATE TABLE selection_entry (selection_id TEXT, ordinal INTEGER, ticker TEXT);
        CREATE TABLE screening_candidate (run_revision_id TEXT, ticker TEXT, payload TEXT);
        """
    )
    connection.execute("INSERT INTO screening_run VALUES ('run-1', '2024-06-28')")
    connection.execute(
        "INSERT INTO screening_selection VALUES ('sel-1', 'run-1', '2024-06-28T00:00:00+09:00')"
    )
    for ordinal, estimate in enumerate(estimates):
        ticker = f"{2000 + ordinal}"
        connection.execute("INSERT INTO selection_entry VALUES ('sel-1', ?, ?)", (ordinal, ticker))
        connection.execute(
            "INSERT INTO screening_candidate VALUES ('run-1', ?, ?)",
            (ticker, json.dumps({"metrics": {"er_annual": estimate}})),
        )
    connection.commit()
    connection.close()


def test_a_supply_level_above_every_past_month_reports_the_top_percentile(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06, 0.07, 0.08])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.20] * 5)

    payload = build_supply_context(
        calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
    )

    top5 = payload["selection_top5_mean_er"]
    assert isinstance(top5, dict)
    assert top5["value"] == pytest.approx(0.20)
    assert top5["percentile_rank"] == 100.0


def test_a_supply_level_below_every_past_month_reports_the_bottom_percentile(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06, 0.07, 0.08])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.01] * 5)

    payload = build_supply_context(
        calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
    )

    top5 = payload["selection_top5_mean_er"]
    assert isinstance(top5, dict)
    assert top5["percentile_rank"] == 0.0


def test_the_hurdle_count_uses_the_latest_month_end_panel_and_says_so(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06, 0.07])
    # 最新月だけ hurdle を超える行を足す。件数座標は月末 panel から取る。
    _panel(
        calibration,
        "2024-04-28",
        [
            *(_liquid(f"{1000 + slot}", "2024-04-28", er=0.05, rank=slot + 1) for slot in range(5)),
            _liquid("9001", "2024-04-28", er=0.12),
            _liquid("9002", "2024-04-28", er=0.09),
        ],
    )
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.05] * 5)

    payload = build_supply_context(
        calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
    )

    count = payload["hurdle_clearing_count"]
    assert isinstance(count, dict)
    assert count["basis"] == "latest_month_end_panel"
    assert count["panel_asof"] == "2024-04-28"
    assert count["value"] == 2


def test_illiquid_rows_never_enter_the_hurdle_count(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    # 件数座標は最新月を過去と比べるので、比較対象の月が 1 つ以上要る。
    _history(calibration, levels=[0.05])
    asof = "2024-02-28"
    rows = [_liquid(f"{1000 + slot}", asof, er=0.05, rank=slot + 1) for slot in range(5)]
    rows.append({**_liquid("9001", asof, er=0.12), "avg_turnover_oku": 0.1})
    _panel(calibration, asof, rows)
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.05] * 5)

    payload = build_supply_context(
        calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
    )

    count = payload["hurdle_clearing_count"]
    assert isinstance(count, dict)
    assert count["value"] == 0


def test_a_selection_without_enough_ranked_estimates_fails_instead_of_averaging_a_partial_top(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.05, 0.06])

    with pytest.raises(SupplyContextError, match="ranked candidates"):
        build_supply_context(
            calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
        )


def test_cli_writes_yaml_and_reports_a_missing_store(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.07] * 5)
    out = tmp_path / "supply.yaml"

    assert (
        main(
            [
                "--calibration-dir",
                str(calibration),
                "--runs-db",
                str(runs),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "supply-context"

    assert main(["--calibration-dir", str(tmp_path / "absent"), "--runs-db", str(runs)]) == 1


def test_a_lone_panel_cannot_place_its_own_count_in_history(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.05] * 5)

    with pytest.raises(SupplyContextError, match="single panel"):
        build_supply_context(
            calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
        )


def test_panels_built_with_different_screening_rules_are_refused(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    _panel(
        calibration,
        "2024-03-28",
        [_liquid(f"{1000 + slot}", "2024-03-28", er=0.05, rank=slot + 1) for slot in range(5)],
        rules_hash="different",
    )
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.05] * 5)

    with pytest.raises(SignalCohortMeasurementError, match="mix screening rules"):
        build_supply_context(
            calibration_dir=calibration, runs_db=runs, selection_id=None, hurdle=0.085
        )
