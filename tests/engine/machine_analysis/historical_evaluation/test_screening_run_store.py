from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from tests.helpers.legacy_candidates import _analysis, _publish, _run
from tests.helpers.screening_run import saved_candidate

import baibai_engine.storage.sqlite.machine_analysis as machine_store
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.contracts import (
    APPROACH_IDS,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.projection import (
    candidate_rows,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import (
    ScreeningRunReader,
    initialize_run_store,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.screening_run_serializer import (
    published_run_payload,
    serialize_published_run_yaml,
)
from baibai_engine.storage.sqlite.machine_analysis import RUN_STORE_SCHEMA_VERSION


def test_schema8_keeps_generation_and_selection_separate_from_legacy_runs(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    assert initialize_run_store(path) == RUN_STORE_SCHEMA_VERSION == 8
    assert initialize_run_store(path) == 8
    result = _publish(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert tables == {
            "screening_run",
            "stock_analysis",
            "model_generation",
            "generated_model",
            "screening_selection",
        }
        header = json.loads(connection.execute("SELECT payload FROM screening_run").fetchone()[0])
        assert "stock_analyses" not in header
        assert "candidate_discovery" in header
    run = ScreeningRunReader(path).get_run(result)
    assert run is not None
    assert run.stock_analyses[0]["candidate"]["common_eligible"] is True
    assert len(run.stock_analyses[0]["candidate"]["reasons"]) == 4
    assert not hasattr(result, "review_set_id")
    assert not hasattr(ScreeningRunReader(path), "get_review_set")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                **_analysis("1111"),
                "market_cap_oku": None,
                "candidate": saved_candidate(eligible=False),
            }
        ],
    ],
)
def test_zero_analyses_and_zero_candidates_are_valid_completed_runs(tmp_path: Path, rows) -> None:
    path = tmp_path / "runs.sqlite"
    result = _publish(path, _run(rows=rows))
    run = ScreeningRunReader(path).get_run(result)
    assert run is not None
    assert len(run.stock_analyses) == len(rows)
    assert candidate_rows(run.stock_analyses) == ()
    assert run.payload["candidate_discovery"]["diagnostics"] == {
        "common_eligible_count": 0,
        "candidate_count": 0,
        "candidate_counts_by_approach": dict.fromkeys(APPROACH_IDS, 0),
    }


def test_read_export_preserves_saved_raw_values_candidate_and_nulls(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    raw = _run()
    retired_rules = tmp_path / "retired-rules.yaml"
    assert not retired_rules.exists()
    raw["rules_ref"] = str(retired_rules)
    raw["stock_analyses"][0]["metrics"].update(custom_known_value=0.123456789, missing=None)
    result = _publish(path, raw)
    run = ScreeningRunReader(path).get_run(result)
    exported = published_run_payload(run)
    assert run.rules_ref == exported["rules_ref"] == str(retired_rules)
    assert yaml.safe_load(serialize_published_run_yaml(run)) == exported
    assert exported["run_revision_id"] == result
    assert exported["stock_analyses"][0]["metrics"]["custom_known_value"] == 0.123456789
    assert exported["stock_analyses"] == list(run.stock_analyses)
    assert "security_analyses" not in exported


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("asof_date", "canonical identity"),
        ("run_at", "canonical identity"),
        ("naive_run_at", "timezone-aware"),
    ],
)
def test_reader_refuses_mismatched_header_and_naive_clock(
    tmp_path: Path, damage: str, message: str
) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-a")
    with closing(sqlite3.connect(path)) as connection, connection:
        if damage == "asof_date":
            connection.execute("UPDATE screening_run SET asof_date='2026-10-13'")
        elif damage == "run_at":
            connection.execute("UPDATE screening_run SET run_at='2026-10-14T18:00:00.000001+09:00'")
        else:
            naive = "2026-10-14T18:00:00"
            connection.execute(
                "UPDATE screening_run SET run_at=?, payload=json_set(payload,'$.run_at',?)",
                (naive, naive),
            )
    before = path.read_bytes()

    with pytest.raises(ValueError, match=message):
        ScreeningRunReader(path).get_run("run-a")

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "candidate",
    [None, {}, {"common_eligible": False, "reasons": [], "normalized_per_3fy_sector_gap": "0"}],
)
@pytest.mark.parametrize("damaged_ticker", ["1111", "2222"])
def test_reader_checks_every_analysis(tmp_path: Path, candidate, damaged_ticker) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(
        path,
        _run(rows=[_analysis("1111"), {**_analysis("2222"), "market_cap_oku": None}]),
        revision="run-a",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE stock_analysis SET payload=json_set(payload,'$.candidate',json(?)) WHERE ticker=?",
            (json.dumps(candidate), damaged_ticker),
        )
    with pytest.raises(
        ValueError,
        match=r"invalid|StockCandidateContext|CandidateDiscoverySummary",
    ):
        ScreeningRunReader(path).get_run("run-a")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "boolean",
        "reasons",
        "gap",
        "ineligible",
        "reason_approach",
        "reason_method",
        "reason_rank",
        "header",
    ],
)
def test_saved_candidate_shape_drift_is_invalid_not_empty(tmp_path: Path, mutation) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-a")
    with closing(sqlite3.connect(path)) as connection, connection:
        raw = json.loads(connection.execute("SELECT payload FROM stock_analysis").fetchone()[0])
        candidate = raw["candidate"]
        if mutation == "missing":
            raw.pop("candidate")
        elif mutation == "boolean":
            candidate["common_eligible"] = "true"
        elif mutation == "reasons":
            candidate["reasons"] = {}
        elif mutation == "gap":
            candidate.pop("normalized_per_3fy_sector_gap")
        elif mutation == "ineligible":
            candidate["common_eligible"] = False
        elif mutation == "reason_approach":
            candidate["reasons"][0]["valuation_approach_id"] = "unknown"
        elif mutation == "reason_method":
            candidate["reasons"][0]["valuation_method_id"] = ""
        elif mutation == "reason_rank":
            candidate["reasons"][0]["rank"] = False
        else:
            connection.execute(
                "UPDATE screening_run SET payload=json_remove(payload,'$.candidate_discovery')"
            )
        connection.execute("UPDATE stock_analysis SET payload=?", (json.dumps(raw),))
    with pytest.raises(
        ValueError,
        match=r"invalid|StockCandidateContext|CandidateDiscoverySummary",
    ):
        ScreeningRunReader(path).get_run("run-a")


def test_reader_preserves_saved_ordinal_before_candidate_projection(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(
        path,
        _run(
            rows=[
                {**_analysis("2222"), "candidate": saved_candidate(rank=1)},
                {
                    **_analysis("1111"),
                    "market_cap_oku": None,
                    "candidate": saved_candidate(eligible=False),
                },
                {**_analysis("3333"), "candidate": saved_candidate(rank=2)},
            ]
        ),
        revision="run-a",
    )
    run = ScreeningRunReader(path).get_run("run-a")
    assert run is not None
    assert [row["ticker"] for row in run.stock_analyses] == ["2222", "1111", "3333"]
    assert [row["ticker"] for row in candidate_rows(run.stock_analyses)] == ["2222", "3333"]


def test_old_schema_requires_explicit_cutover(tmp_path: Path) -> None:
    legacy = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(legacy)) as connection, connection:
        connection.execute("CREATE TABLE old_cache(value TEXT)")
        connection.execute("PRAGMA user_version=5")
    with pytest.raises(RuntimeError, match="explicit migration"):
        initialize_run_store(legacy)
    with pytest.raises(RuntimeError, match="explicit migration"):
        ScreeningRunReader(legacy).get_run("absent")


@pytest.fixture
def precise_clock_runs(tmp_path):
    path = tmp_path / "runs.sqlite"
    revisions = [
        "run-revision-ffffffff-ffff-4fff-bfff-ffffffffffff",
        "run-revision-00000000-0000-4000-8000-000000000001",
        "run-revision-00000000-0000-4000-8000-000000000000",
    ]
    clocks = [
        "2026-10-14T12:00:00.000400+09:00",
        "2026-10-14T03:00:00.000401+00:00",
        "2026-10-14T12:00:00.000402+09:00",
    ]
    for revision, clock in zip(revisions, clocks, strict=True):
        _publish(path, _run(at=clock), revision=revision)
    return path, revisions, clocks


def test_reader_preserves_exact_saved_ids_and_clocks_without_writing(precise_clock_runs):
    path, revisions, clocks = precise_clock_runs
    before = path.read_bytes()
    for revision, clock in zip(revisions, clocks, strict=True):
        saved = ScreeningRunReader(path).get_run(revision)
        assert saved is not None
        assert saved.run_revision_id == revision
        assert saved.run_at == saved.payload["run_at"] == clock
    assert path.read_bytes() == before


def test_initialization_failure_rolls_back_run_schema_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(machine_store, "SCHEMA_SQL", machine_store.SCHEMA_SQL + "\nINVALID SQL;")
        with pytest.raises(sqlite3.OperationalError):
            initialize_run_store(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert initialize_run_store(path) == RUN_STORE_SCHEMA_VERSION


@pytest.mark.parametrize("mutation", ["rank", "count"])
def test_reader_rejects_inconsistent_candidate_union(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, revision="run-a")
    with closing(sqlite3.connect(path)) as connection, connection:
        if mutation == "rank":
            connection.execute(
                "UPDATE stock_analysis SET payload="
                "json_set(payload, '$.candidate.reasons[0].rank', 2)"
            )
        else:
            connection.execute(
                "UPDATE screening_run SET payload="
                "json_set(payload, '$.candidate_discovery.diagnostics.candidate_count', 0)"
            )
    message = "ranks must be contiguous" if mutation == "rank" else "diagnostics differ"
    with pytest.raises(ValueError, match=message):
        ScreeningRunReader(path).get_run("run-a")


def test_retired_annotations_remain_exact_in_saved_historical_export(tmp_path):
    path = tmp_path / "runs.sqlite"
    row = {
        **_analysis("1111"),
        "candidate": saved_candidate(),
        "next_earnings_date": "2026-10-20",
        "price_change_1d": 0.012345,
        "price_change_5d": 0.0,
        "price_change_20d": None,
        "sector_relative_strength_percentile": 0.625,
        "freshness_warnings": [{"original": "retained claim, not recomputed"}],
    }
    row["metrics"] = {
        **row["metrics"],
        "next_earnings_status": "announced",
        "next_earnings_estimated_date": None,
        "stale_fin_flag": False,
        "tse_capital_policy_status": "disclosed",
        "operating_profit_ttm": 100,
        "sales_ttm": 200,
        "same_state_equity_yen": 300,
        "debt": 50,
        "cash": 20,
        "operating_return_on_capital_proxy": 0.987654,
        "operating_margin": 0.123456,
    }
    identity = _publish(path, _run(rows=[row]))
    saved = ScreeningRunReader(path).get_run(identity)
    assert saved is not None
    exported = published_run_payload(saved)["stock_analyses"][0]
    assert exported == row
    assert "turnover_spike_5d" not in exported
    assert yaml.safe_load(serialize_published_run_yaml(saved))["stock_analyses"][0] == exported
