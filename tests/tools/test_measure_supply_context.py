from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.helpers.calibration_store import publish_panel
from tools.experiments.measure_supply_context import SupplyContextError, build_supply_context, main

from baibai_engine.screening.calibration.store import CalibrationCacheError

PANEL_COLUMNS = (
    "asof",
    "ticker",
    "sector_33",
    "in_population",
    "market_cap_oku",
    "avg_turnover_oku",
    "listing_span_days",
    "er_annual",
    "er_reversion_annual",
    "er_carry_annual",
    "selection_rank",
    "pass_screen",
)


def _panel(
    directory: Path, asof: str, rows: list[dict[str, Any]], *, rules_hash: str = "abc123"
) -> None:
    publish_panel(directory, asof, rows, rules_hash=rules_hash)


def _liquid(ticker: str, asof: str, *, er: float, rank: int | str = "") -> dict[str, Any]:
    return {
        "asof": asof,
        "ticker": ticker,
        "sector_33": f"sector-{int(ticker) % 4}",
        "in_population": "True",
        "market_cap_oku": 500,
        "avg_turnover_oku": 5,
        "listing_span_days": 900,
        "er_annual": er,
        "er_reversion_annual": er * 0.4,
        "er_carry_annual": er * 0.6,
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
            [
                *[_liquid(f"{1000 + slot}", asof, er=level, rank=slot + 1) for slot in range(20)],
                *[
                    {
                        **_liquid(f"{3000 + slot}", asof, er=0.01),
                        "pass_screen": "False",
                    }
                    for slot in range(80)
                ],
            ],
        )


def _monthly_history(directory: Path, *, months: list[str]) -> None:
    for index, month in enumerate(months):
        asof = f"{month}-28"
        _panel(
            directory,
            asof,
            [
                *[
                    _liquid(f"{1000 + slot}", asof, er=0.05 + index / 1000, rank=slot + 1)
                    for slot in range(20)
                ],
                *[
                    {
                        **_liquid(f"{3000 + slot}", asof, er=0.01),
                        "pass_screen": "False",
                    }
                    for slot in range(80)
                ],
            ],
        )


def _runs_db(path: Path, *, estimates: list[float]) -> None:
    breadth_estimates = (
        estimates
        if len(estimates) < 5
        else [*estimates, *([estimates[-1]] * (20 - len(estimates)))]
    )
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE screening_run (run_revision_id TEXT, asof_date TEXT);
        CREATE TABLE screening_selection (
            selection_id TEXT, run_revision_id TEXT, created_at TEXT, payload TEXT
        );
        CREATE TABLE selection_entry (selection_id TEXT, ordinal INTEGER, ticker TEXT);
        CREATE TABLE screening_candidate (
            run_revision_id TEXT, ticker TEXT, sector_33 TEXT, er_annual REAL, payload TEXT
        );
        """
    )
    connection.execute("INSERT INTO screening_run VALUES ('run-1', '2024-06-28')")
    connection.execute(
        "INSERT INTO screening_selection VALUES (?, ?, ?, ?)",
        (
            "sel-1",
            "run-1",
            "2024-06-28T00:00:00+09:00",
            json.dumps(
                {
                    "longlist": [
                        {
                            "ticker": f"{2000 + ordinal}",
                            "rank": ordinal + 1,
                            "expected_return_pct": estimate * 100,
                        }
                        for ordinal, estimate in enumerate(breadth_estimates)
                    ]
                }
            ),
        ),
    )
    for ordinal, estimate in enumerate(breadth_estimates):
        ticker = f"{2000 + ordinal}"
        connection.execute("INSERT INTO selection_entry VALUES ('sel-1', ?, ?)", (ordinal, ticker))
        connection.execute(
            "INSERT INTO screening_candidate VALUES ('run-1', ?, ?, ?, ?)",
            (
                ticker,
                f"sector-{ordinal % 4}",
                estimate,
                json.dumps(
                    {
                        "metrics": {
                            "er_annual": estimate,
                            "er_reversion_annual": estimate * 0.4,
                            "er_carry_annual": estimate * 0.6,
                        }
                    }
                ),
            ),
        )
    connection.commit()
    connection.close()


def _longlist_history(directory: Path, *, asof: str, tickers: list[str]) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{asof}.json").write_text(
        json.dumps(
            {
                "kind": "daily-longlist-membership",
                "schema_version": 1,
                "as_of": asof,
                "selection_status": "available" if tickers else "selection_missing",
                "selection_id": f"selection-{asof}" if tickers else None,
                "members": [
                    {"ticker": ticker, "rank": rank, "er_annual": 0.1}
                    for rank, ticker in enumerate(tickers, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )


def _application_db(path: Path, *, rows: list[tuple[str, str, list[str]]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE shortlist (shortlist_id TEXT, as_of TEXT, published_at TEXT, payload TEXT)"
    )
    for shortlist_id, asof, reject_classes in rows:
        connection.execute(
            "INSERT INTO shortlist VALUES (?, ?, ?, ?)",
            (
                shortlist_id,
                asof,
                f"{asof}T18:00:00+09:00",
                json.dumps(
                    {
                        "entries": (
                            [
                                {"decision": "rejected", "reject_class": reject_class}
                                for reject_class in reject_classes
                            ]
                            if reject_classes
                            else [{"decision": "selected"}]
                        )
                    }
                ),
            ),
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


def test_panels_built_with_different_screening_rules_cannot_be_published(
    tmp_path: Path,
) -> None:
    """A mixture is declined where it would be created, not where it would be read.

    Cohorts screened under different rules answer different questions, so averaging
    them reports a change in the rules as a change in the market. Leaving that to the
    consumer means the store holds a mixture until someone happens to look.
    """

    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])

    with pytest.raises(CalibrationCacheError, match="mixes measurement policies"):
        _panel(
            calibration,
            "2024-03-28",
            [_liquid(f"{1000 + slot}", "2024-03-28", er=0.05, rank=slot + 1) for slot in range(5)],
            rules_hash="different",
        )


def test_breadth_coordinates_report_current_values_and_panel_percentiles(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _monthly_history(
        calibration,
        months=[
            "2023-01",
            "2023-02",
            "2023-03",
            "2023-04",
            "2023-05",
            "2023-06",
            "2023-07",
            "2023-08",
            "2023-09",
            "2023-10",
            "2023-11",
            "2023-12",
            "2024-01",
        ],
    )
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    assert breadth["trailing_12m_unique_top20"]["value"] == 20
    assert breadth["carry_dominant_share"]["value"] == 1.0
    assert breadth["carry_dominant_share"]["percentile_rank"] == 0.0
    assert breadth["sector_hhi"]["value"] == 0.25
    assert breadth["max_cluster_share"]["value"] == 0.25


def test_missing_panel_month_does_not_claim_zero_trailing_breadth(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _monthly_history(
        calibration,
        months=[
            "2023-01",
            "2023-02",
            "2023-03",
            "2023-04",
            "2023-05",
            "2023-06",
            "2023-07",
            "2023-08",
            "2023-09",
            "2023-10",
            "2023-11",
            "2024-01",
        ],
    )
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    trailing = breadth["trailing_12m_unique_top20"]
    assert isinstance(trailing, dict)
    assert trailing["status"] == "unmeasured"
    assert trailing["value"] is None
    assert "consecutive" in trailing["reason"]


def test_incomplete_latest_panel_does_not_fall_back_to_prior_trailing_breadth(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    months = [
        "2023-01",
        "2023-02",
        "2023-03",
        "2023-04",
        "2023-05",
        "2023-06",
        "2023-07",
        "2023-08",
        "2023-09",
        "2023-10",
        "2023-11",
        "2023-12",
        "2024-01",
    ]
    _monthly_history(calibration, months=months)
    latest_asof = "2024-01-28"
    _panel(
        calibration,
        latest_asof,
        [_liquid(f"{1000 + slot}", latest_asof, er=0.06, rank=slot + 1) for slot in range(20)],
    )
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    trailing = breadth["trailing_12m_unique_top20"]
    assert isinstance(trailing, dict)
    assert trailing["status"] == "unmeasured"
    assert trailing["value"] is None
    assert trailing["panel_asof"] is None


def test_pruned_previous_run_does_not_claim_zero_temporal_overlap(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    temporal = breadth["temporal_jaccard"]
    assert isinstance(temporal, dict)
    assert temporal["status"] == "unmeasured"
    assert temporal["value"] is None
    assert "retention" in temporal["reason"]


def test_longlist_history_recovers_previous_top20_after_run_pruning(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)
    history = tmp_path / "longlists"
    _longlist_history(
        history,
        asof="2024-06-27",
        tickers=[f"{2000 + index}" for index in range(20)],
    )

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        longlist_history_dir=history,
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    temporal = breadth["temporal_jaccard"]
    assert isinstance(temporal, dict)
    assert temporal["status"] == "measured"
    assert temporal["value"] == 1.0
    assert temporal["previous_source"] == "longlist_history"


def test_longlist_history_skips_selection_missing_day_to_last_available_top20(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)
    history = tmp_path / "longlists"
    _longlist_history(
        history,
        asof="2024-06-26",
        tickers=[f"{2000 + index}" for index in range(20)],
    )
    _longlist_history(history, asof="2024-06-27", tickers=[])

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        longlist_history_dir=history,
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    temporal = breadth["temporal_jaccard"]
    assert isinstance(temporal, dict)
    assert temporal["status"] == "measured"
    assert temporal["value"] == 1.0
    assert temporal["previous_asof"] == "2024-06-26"


def test_missing_shortlist_does_not_claim_zero_event_wait_share(tmp_path: Path) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=tmp_path / "absent.sqlite",
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    event_wait = breadth["event_wait_share"]
    assert isinstance(event_wait, dict)
    assert event_wait["status"] == "unmeasured"
    assert event_wait["value"] is None


def test_event_wait_share_uses_latest_shortlist_and_prior_cycles_for_percentile(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)
    application = tmp_path / "application.sqlite"
    _application_db(
        application,
        rows=[
            ("shortlist-1", "2024-06-26", ["event_wait", "other", "other", "other"]),
            ("shortlist-2", "2024-06-27", ["event_wait", "event_wait", "other", "other"]),
        ],
    )

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=application,
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    event_wait = breadth["event_wait_share"]
    assert isinstance(event_wait, dict)
    assert event_wait["status"] == "measured"
    assert event_wait["value"] == 0.5
    assert event_wait["percentile_rank"] == 100.0
    assert event_wait["event_wait_count"] == 2


def test_latest_shortlist_without_rejections_does_not_reuse_prior_event_wait_share(
    tmp_path: Path,
) -> None:
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    _history(calibration, levels=[0.05, 0.06])
    runs = tmp_path / "runs.sqlite"
    _runs_db(runs, estimates=[0.1] * 20)
    application = tmp_path / "application.sqlite"
    _application_db(
        application,
        rows=[
            ("shortlist-1", "2024-06-26", ["event_wait", "other"]),
            ("shortlist-2", "2024-06-27", []),
        ],
    )

    payload = build_supply_context(
        calibration_dir=calibration,
        runs_db=runs,
        application_db=application,
        selection_id=None,
        hurdle=0.085,
    )

    breadth = payload["breadth"]
    assert isinstance(breadth, dict)
    event_wait = breadth["event_wait_share"]
    assert isinstance(event_wait, dict)
    assert event_wait["status"] == "unmeasured"
    assert event_wait["value"] is None
    assert event_wait["reason"] == "latest_shortlist_has_no_rejected_entries"
    assert event_wait["shortlist_id"] == "shortlist-2"
    assert event_wait["history_observations"] == 1
