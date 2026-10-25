"""contextは候補を読み、保存snapshotはselected行だけを保持する。"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.screening_run import screening_run_payload, stock_analysis

from baibai_engine.investment_judgment.selection import InputGap, ResearchRequest
from baibai_engine.investment_judgment.selection_query import (
    ResearchInputsRequiredError,
    load_analysis_snapshot,
    load_selection_context,
    verify_analysis_snapshot,
)
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunStore

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def publish(path, *, rows=None):
    raw = screening_run_payload(
        as_of="2026-10-13",
        stock_analyses=(
            [stock_analysis("1234", name="合成会社"), stock_analysis("5678", name="合成会社")]
            if rows is None
            else rows
        ),
        filters={"diagnostic": True},
    )
    ScreeningRunStore(path).publish_run(
        raw,
        run_revision_id="fixed",
        rules=load_screening_rules(),
    )
    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader

    saved = ScreeningRunReader(path).get_run("fixed")
    return {**raw, "stock_analyses": list(saved.stock_analyses)}


def context(path, scope="discover_new", tickers=(), revision=None, comparisons=()):
    return load_selection_context(
        request=ResearchRequest(text="request", scope=scope, requested_tickers=tickers),
        research_as_of=None,
        run_revision_id=revision,
        known_at=NOW,
        runs_path=path,
        db_path=path.parent / "absent-app",
        comparison_tickers=comparisons,
    )


def test_context_and_selected_snapshot_preserve_original_missing_and_nulls(tmp_path):
    path = tmp_path / "runs"
    raw = publish(
        path,
        rows=[
            stock_analysis("1234", metrics=None),
            stock_analysis("5678", avg_turnover_oku=None),
        ],
    )
    before = path.read_bytes()
    assert context(path)["status"] == "ready"
    snap = load_analysis_snapshot(
        run_revision_id="fixed",
        research_as_of=NOW.date(),
        tickers=("1234",),
        gaps=(),
        known_at=NOW,
        runs_path=path,
    )
    assert snap.analysis_as_of.isoformat() == "2026-10-13"
    assert snap.analyses == {"1234": raw["stock_analyses"][0]}
    assert "metrics" not in snap.analyses["1234"]
    assert "filters" not in snap.model_dump()
    verify_analysis_snapshot(snap, tickers=("1234",), known_at=NOW, runs_path=path)
    assert path.read_bytes() == before


def test_missing_context_input_is_structured_candidate_zero_is_ready(tmp_path):
    path = tmp_path / "runs"
    assert context(path)["missing_inputs"][0]["code"] == "no_screening_run"
    publish(path, rows=[stock_analysis("1234", market_cap_oku=0)])
    result = context(path)
    assert result["status"] == "ready"
    assert result["candidates"] == []
    assert not (tmp_path / "absent-app").exists()


def test_specified_candidate_outside_and_input_gap(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    assert context(path, "specified_stocks", ("1234",))["status"] == "ready"
    assert context(path)["status"] == "ready"
    with pytest.raises(ResearchInputsRequiredError) as error:
        load_analysis_snapshot(
            run_revision_id="fixed",
            research_as_of=NOW.date(),
            tickers=("9999",),
            gaps=(),
            known_at=NOW,
            runs_path=path,
        )
    assert error.value.code == "selected_analysis_missing"
    assert error.value.tickers == ("9999",)
    gap = InputGap(
        ticker="9999",
        reason="attempted unavailable",
        attempted_sources=("synthetic primary source",),
    )
    snap = load_analysis_snapshot(
        run_revision_id="fixed",
        research_as_of=NOW.date(),
        tickers=("9999",),
        gaps=(gap,),
        known_at=NOW,
        runs_path=path,
    )
    assert snap.unavailable == {"9999": gap.reason}
    with pytest.raises(ValueError, match="unknown"):
        context(path, "specified_stocks", ("1234",), "missing")


def test_future_run_and_missing_candidate_header_are_invalid(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with closing(sqlite3.connect(path)) as db, db:
        original = db.execute("SELECT payload FROM screening_run").fetchone()[0]
        db.execute("UPDATE screening_run SET payload=json_remove(payload,'$.candidate_discovery')")
    with pytest.raises(ValueError, match="CandidateDiscoverySummary"):
        context(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE screening_run SET payload=?", (original,))
        future = (NOW + timedelta(seconds=1)).isoformat()
        db.execute(
            "UPDATE screening_run SET run_at=?, payload=json_set(payload,'$.run_at',?)",
            (future, future),
        )
    with pytest.raises(ValueError, match="cutoff"):
        context(path, "specified_stocks", ("1234",), "fixed")


def test_research_rejects_a_run_with_mismatched_saved_identity(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE screening_run SET payload=json_set(payload,'$.asof_date','2026-10-12')")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="canonical identity"):
        context(path, "specified_stocks", ("1234",))

    assert path.read_bytes() == before


def test_gap_does_not_bypass_broken_source(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="schema"):
        load_analysis_snapshot(
            run_revision_id="fixed",
            research_as_of=NOW.date(),
            tickers=("9999",),
            gaps=(InputGap(ticker="9999", reason="missing", attempted_sources=("source",)),),
            known_at=NOW,
            runs_path=path,
        )


def test_timezone_equivalent_cutoffs_use_the_same_jst_research_date(tmp_path):
    request = ResearchRequest(text="request", scope="discover_new")
    arguments = dict(
        request=request,
        research_as_of=None,
        run_revision_id=None,
        runs_path=tmp_path / "absent",
        db_path=tmp_path / "absent-app",
    )
    jst = datetime.fromisoformat("2026-10-14T00:30:00+09:00")
    utc = datetime.fromisoformat("2026-10-13T15:30:00+00:00")
    assert load_selection_context(known_at=jst, **arguments)["research_as_of"] == "2026-10-14"
    assert load_selection_context(known_at=utc, **arguments)["research_as_of"] == "2026-10-14"


def test_candidate_run_revision_mismatch_and_header_null_are_not_filled(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with closing(sqlite3.connect(path)) as db, db:
        raw = json.loads(db.execute("SELECT payload FROM screening_run").fetchone()[0])
        raw.pop("er_model_version")
        raw["screening_rules_hash"] = None
        db.execute("UPDATE screening_run SET payload=?", (json.dumps(raw),))
    snap = load_analysis_snapshot(
        run_revision_id="fixed",
        research_as_of=NOW.date(),
        tickers=("1234",),
        gaps=(),
        known_at=NOW,
        runs_path=path,
    )
    assert snap.er_model_version is None
    assert snap.screening_rules_hash is None
    assert context(path)["status"] == "ready"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE screening_run SET payload=json_set(payload,'$.candidate_discovery.diagnostics.candidate_count',999)"
        )
    with pytest.raises(ValueError, match="diagnostics"):
        context(path)


def test_empty_published_analysis_is_not_replaced_by_fixture_defaults(tmp_path):
    path = tmp_path / "runs"
    raw = publish(path, rows=[])
    assert raw["stock_analyses"] == []
    assert context(path)["status"] == "ready"
    assert context(path)["candidates"] == []


def test_comparisons_preserve_scope_nomination_and_saved_values(tmp_path):
    path = tmp_path / "runs"
    saved = publish(
        path,
        rows=[
            stock_analysis("1234", per_forward=10, metrics={"per_forward_sector_gap": -0.2}),
            stock_analysis("5678", metrics={"sales_yoy": 0, "fcf_yield": None}),
            stock_analysis("9012", market_cap_oku=0, metrics=None),
        ],
    )
    before = path.read_bytes()
    baseline = context(path, revision="fixed")
    result = context(path, revision="fixed", comparisons=("5678", "9012", "9999", "5678"))
    assert result["status"] == baseline["status"] == "ready"
    assert result["request"] == baseline["request"]
    assert result["request"]["requested_tickers"] == []
    assert result["candidates"] == baseline["candidates"]
    assert [row["ticker"] for row in result["candidates"]] == ["1234"]
    assert result["capacity"] == baseline["capacity"]
    assert result["run_revision_id"] == "fixed"
    assert result["comparison_analyses"] == {
        row["ticker"]: row for row in saved["stock_analyses"] if row["ticker"] != "1234"
    }
    assert result["comparison_analyses"]["5678"]["metrics"] == {
        "sales_yoy": 0,
        "fcf_yield": None,
    }
    assert "metrics" not in result["comparison_analyses"]["9012"]
    assert result["missing_comparison_tickers"] == ["9999"]
    assert result["missing_inputs"] == []
    selected = load_analysis_snapshot(
        run_revision_id="fixed",
        research_as_of=NOW.date(),
        tickers=("1234",),
        gaps=(),
        known_at=NOW,
        runs_path=path,
    )
    assert set(selected.analyses) == {"1234"}
    assert path.read_bytes() == before


def test_comparison_does_not_fill_missing_or_unknown_run(tmp_path):
    path = tmp_path / "runs"
    missing = context(path, comparisons=("5678",))
    assert missing["status"] == "inputs_required"
    assert missing["comparison_analyses"] == {}
    assert missing["missing_comparison_tickers"] == ["5678"]
    publish(path)
    with pytest.raises(ValueError, match="unknown"):
        context(path, revision="unknown", comparisons=("5678",))
