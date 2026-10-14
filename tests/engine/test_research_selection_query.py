"""contextは候補を読み、保存snapshotはselected行だけを保持する。"""

import sqlite3
from datetime import datetime, timedelta

import pytest
from tests.helpers.screening_run import screening_run_payload, security_analysis

from baibai_engine.research.selection import InputGap, ResearchRequest
from baibai_engine.research.selection_query import (
    ResearchInputsRequiredError,
    load_analysis_snapshot,
    load_selection_context,
    verify_analysis_snapshot,
)
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def publish(path, *, candidates=True, rows=None):
    rules = load_screening_rules()
    raw = screening_run_payload(
        as_of="2026-10-13",
        security_analyses=rows
        or [security_analysis("1234", name="合成会社"), security_analysis("5678", name="合成会社")],
        filters={"diagnostic": True},
    )
    ScreeningRunStore(path).publish_run(
        raw,
        run_revision_id="fixed",
        review_set_rules=rules.candidate_discovery if candidates else None,
        required_jpx_flags=rules.universe.required_jpx_flags if candidates else (),
    )
    return raw


def context(path, scope="discover_new", tickers=(), revision=None):
    return load_selection_context(
        request=ResearchRequest(text="request", scope=scope, requested_tickers=tickers),
        research_as_of=None,
        run_revision_id=revision,
        known_at=NOW,
        runs_path=path,
        db_path=path.parent / "absent-app",
    )


def test_context_and_selected_snapshot_preserve_original_missing_and_nulls(tmp_path):
    path = tmp_path / "runs"
    raw = publish(
        path,
        rows=[
            security_analysis("1234", metrics=None),
            security_analysis("5678", avg_turnover_oku=None),
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
    assert snap.analyses == {"1234": raw["security_analyses"][0]}
    assert "metrics" not in snap.analyses["1234"]
    assert "filters" not in snap.model_dump()
    verify_analysis_snapshot(snap, tickers=("1234",), known_at=NOW, runs_path=path)
    assert path.read_bytes() == before


def test_missing_context_input_is_structured_candidate_zero_is_ready(tmp_path):
    path = tmp_path / "runs"
    assert context(path)["missing_inputs"][0]["code"] == "no_screening_run"
    publish(path, rows=[security_analysis("1234", market_cap_oku=0)])
    result = context(path)
    assert result["status"] == "ready"
    assert result["candidates"] == []
    assert not (tmp_path / "absent-app").exists()


def test_specified_candidate_outside_and_input_gap(tmp_path):
    path = tmp_path / "runs"
    publish(path, candidates=False)
    assert context(path, "specified_stocks", ("1234",))["status"] == "ready"
    assert context(path)["status"] == "inputs_required"
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


def test_future_run_and_candidate_and_canonical_mismatch_are_invalid(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM review_set").fetchone()[0]
        import json

        candidate = json.loads(raw)
        candidate["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
        db.execute(
            "UPDATE review_set SET payload=?,created_at=?",
            (json.dumps(candidate), candidate["created_at"]),
        )
    with pytest.raises(ValueError, match="candidate projection"):
        context(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE screening_run SET run_at=?", ((NOW + timedelta(seconds=1)).isoformat(),))
    with pytest.raises(ValueError, match="cutoff"):
        context(path, "specified_stocks", ("1234",))


def test_gap_does_not_bypass_broken_source(tmp_path):
    path = tmp_path / "runs"
    publish(path)
    with sqlite3.connect(path) as db:
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
    import json

    with sqlite3.connect(path) as db:
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
    with sqlite3.connect(path) as db:
        raw = json.loads(db.execute("SELECT payload FROM review_set").fetchone()[0])
        raw["run_revision_id"] = "different-revision"
        db.execute("UPDATE review_set SET payload=?", (json.dumps(raw),))
    with pytest.raises(ValueError, match="candidate projection"):
        context(path)
