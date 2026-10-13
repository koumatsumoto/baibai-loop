"""保存分析からのユーザー指定開始を、既存Review・CAA・表示まで通す。"""

import socket
import sqlite3
from datetime import timedelta
from hashlib import sha256

import pytest
import yaml
from fastapi.testclient import TestClient
from tests.engine.test_position_review_v3 import NOW
from tests.helpers.research_v4 import pair_payload
from tests.helpers.screening_run import screening_run_payload, security_analysis
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.explicit_selection import load_explicit_research_run
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.progress import (
    ResearchSelection,
    ResearchTarget,
    selection_binding,
    selection_sha256,
)
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.planning import plan_limit
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    compute_status,
    prepare_workspace,
)
from baibai_engine.research.workspace.publication import promote
from baibai_engine.research.workspace.scaffold import scaffold_review, scaffold_thesis
from baibai_engine.research.workspace_cli import main
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.observations.jquants import store_jquants_market_calendar
from baibai_web.api.server import create_app

RUN = "run-synthetic-user-request"
REQUEST = "synthetic user explicitly requests these named companies"


def seed_run(path, *, run_at=NOW):
    analyses = [
        security_analysis(
            "1234",
            name="Synthetic requested company",
            metrics={
                "er_annual": 0.13,
                "fv_sector_median_yen": 1400.0,
                "fcf_ttm": -100.0,
            },
        ),
        security_analysis("5678", name="Synthetic missing estimate", metrics={}),
    ]
    rules = load_screening_rules()
    store = ScreeningRunStore(path)
    store.publish_run(
        screening_run_payload(
            as_of=NOW.date().isoformat(), run_at=run_at.isoformat(), security_analyses=analyses
        ),
        run_revision_id=RUN,
        review_set_rules=rules.candidate_discovery,
        required_jpx_flags=rules.universe.required_jpx_flags,
    )
    return store


def prepare(db, runs, workspace, market, **changes):
    kwargs = dict(
        research_triage_id=None,
        db_path=db,
        runs_path=runs,
        workspace=workspace,
        sqlite_path=market,
        indicators_path=workspace.parent / "absent-indicators",
        request=REQUEST,
        research_set=("1234", "5678"),
        started_at=NOW,
        run_revision_id=RUN,
    )
    kwargs.update(changes)
    return prepare_workspace(**kwargs)


def reviewed_assessment(db, *, thesis_id):
    raw = scaffold_capital_allocation(
        db_path=db,
        as_of=NOW.date(),
        capital_allocation_assessment_id="capital-allocation-assessment-20260907-user-request",
        research_triage_id=None,
        thesis_ids=[thesis_id],
        published_at=NOW,
    )
    raw.update(
        headline="synthetic completed comparison",
        comparison="synthetic source comparison",
        forgone="unfinished is not an economic reject",
    )
    raw["alternatives"][0]["rationale"] = "synthetic no-allocation judgment"
    raw["review"]["reviewer_identity"] = "synthetic independent content reviewer"
    draft = CapitalAllocationAssessment.model_validate(raw)
    CapitalAllocationAssessmentService(db, clock=lambda: NOW).check(draft)
    return draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(draft)}
            )
        }
    )


def test_user_selected_outside_candidates_completes_review_caa_and_read_api(
    app_method_root, tmp_path, monkeypatch
):
    root = app_method_root
    db, runs, market = (
        root / "stores/application/baibai.sqlite",
        root / "stores/screening/runs.sqlite",
        root / "stores/market/market.sqlite",
    )
    store = seed_run(runs)
    reader = ScreeningRunReader(runs)
    saved_run = reader.get_run(RUN)
    assert reader.list_review_sets(run_revision_id=RUN)[0].payload["entries"] == []
    with sqlite3.connect(db) as connection:
        before_ledger = connection.execute(
            "SELECT payload FROM ledger_event ORDER BY append_seq"
        ).fetchall()
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("unexpected network")
    )
    workspace = tmp_path / "research"
    prepared = prepare(db, runs, workspace, market)
    assert prepared.admissible_research_tickers == ("1234", "5678")
    service = OperationService(db, sqlite_path=market, runs_path=runs)
    operation = service.get(prepared.operation_id)
    selection = selection_binding(operation.payload)
    assert selection.research_triage_id is None
    assert selection.explicit_run.run_revision_id == RUN
    inputs = yaml.safe_load((workspace / "domain-inputs.yaml").read_text())
    assert "research_triage" not in inputs
    assert inputs["explicit_research_request"]["request"] == REQUEST
    assert inputs["securities"]["1234"]["machine_valuation"]["as_of"] == NOW.date().isoformat()
    assert inputs["securities"]["5678"]["machine_valuation"]["estimate"]["er_annual"] is None
    assert reader.get_run(RUN) == saved_run
    # Normal pruning must not erase the selected source evidence.
    for number in range(3):
        store.publish_run(
            screening_run_payload(
                as_of=NOW.date().isoformat(), run_at=(NOW + timedelta(hours=number + 1)).isoformat()
            ),
            run_revision_id=f"later-{number}",
        )
    store.prune(keep=3)
    assert reader.get_run(RUN) is None
    resumed = prepare(db, runs, workspace, market, force=True, started_at=NOW + timedelta(hours=4))
    assert resumed.operation_id == operation.operation_id
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == inputs
    scaffold_thesis(
        workspace=workspace,
        ticker="1234",
        sqlite_path=market,
        target_session=NOW.date(),
        retrieved_at=NOW,
        db_path=db,
    )
    thesis, review = pair_payload()
    (workspace / "1234/thesis-draft.yaml").write_text(yaml.safe_dump(thesis, sort_keys=False))
    scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    # Independent expected literals come from the shared synthetic reviewed pair.
    review["review_id"] = "review-synthetic-explicit"
    (workspace / "1234/2026-09-07-1234-decision-review.yaml").write_text(
        yaml.safe_dump(review, sort_keys=False)
    )
    result = promote(
        workspace=workspace,
        ticker="1234",
        db_path=db,
        thesis_id="thesis-synthetic-explicit",
        supersedes_id=None,
        now=NOW,
    )
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="synthetic source missing",
        references=(RUN,),
        now=NOW,
    )
    assessed = reviewed_assessment(db, thesis_id=result.thesis_id)
    published = CapitalAllocationAssessmentService(db, clock=lambda: NOW).publish(assessed)
    assert published.research_triage_id is None
    current = service.get(operation.operation_id)
    payload = current.payload.model_copy(
        update={
            "result": "synthetic completed",
            "next": "human decision",
            "artifacts": (
                *current.payload.artifacts,
                {
                    "kind": "capital_allocation_assessment",
                    "ref": published.capital_allocation_assessment_id,
                },
            ),
        }
    )
    assert service.complete(operation.operation_id, payload, completed_at=NOW).status == "completed"
    with TestClient(create_app(root), base_url="http://127.0.0.1") as client:
        detail = client.get(
            "/api/capital-allocation-assessments/" + published.capital_allocation_assessment_id
        )
        assert detail.status_code == 200
        view = detail.json()
        assert view["research_triage_id"] is None
        assert view["alternatives"][0]["thesis_id"] == result.thesis_id
        assert view["alternatives"][0]["thesis_review_id"] == result.review_id
        assert view["unfinished_research"][0]["ticker"] == "5678"
        assert published.capital_allocation_assessment_id in {
            row["capital_allocation_assessment_id"]
            for row in client.get("/api/capital-allocation-assessments").json()["assessments"]
        }
        shown = next(
            o
            for o in client.get("/api/operations").json()["operations"]
            if o["operation_id"] == operation.operation_id
        )
        assert shown["research"]["source_run_revision_id"] == RUN
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("SELECT payload FROM ledger_event ORDER BY append_seq").fetchall()
            == before_ledger
        )
        assert connection.execute("SELECT count(*) FROM research_triage").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["run", "request", "ticker", "manifest"])
def test_explicit_selection_cannot_be_rebound(tmp_path, change, app_method_root):
    db, runs, workspace, market = (
        tmp_path / n for n in ["app.sqlite", "runs.sqlite", "workspace", "missing-market"]
    )
    db = app_method_root / "stores/application/baibai.sqlite"
    seed_run(runs)
    prepared = prepare(db, runs, workspace, market)
    before = OperationService(db).get(prepared.operation_id)
    if change == "manifest":
        path = workspace / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        manifest["inputs"]["explicit_run"]["snapshot_sha256"] = "0" * 64
        path.write_text(yaml.safe_dump(manifest))
        with pytest.raises(ResearchWorkspaceConflictError, match="explicit run"):
            compute_status(workspace, db_path=db, now=NOW)
    else:
        changes = {
            "run": {"run_revision_id": "other-run"},
            "request": {"request": "different request"},
            "ticker": {"research_set": ("1234",)},
        }[change]
        with pytest.raises(
            (ValueError, ResearchWorkspaceConflictError), match=r"fixed|cover|active"
        ):
            prepare(db, runs, workspace, market, force=True, **changes)
    assert OperationService(db).get(prepared.operation_id) == before


@pytest.mark.parametrize("failure", ["unknown-run", "missing-ticker", "future-run", "asof"])
def test_bad_explicit_source_stops_before_application_or_workspace_write(tmp_path, failure):
    db, runs, workspace = (tmp_path / n for n in ["absent-app.sqlite", "runs.sqlite", "workspace"])
    seed_run(runs, run_at=NOW + timedelta(hours=1) if failure == "future-run" else NOW)
    before = runs.read_bytes()
    changes = {
        "unknown-run": {"run_revision_id": "unknown"},
        "missing-ticker": {"research_set": ("9012",)},
        "future-run": {},
        "asof": {"asof": NOW.date() - timedelta(days=1)},
    }[failure]
    with pytest.raises((ValueError, ResearchWorkspaceDataError)):
        prepare(db, runs, workspace, tmp_path / "absent-market", **changes)
    assert not db.exists()
    assert not workspace.exists()
    assert runs.read_bytes() == before


def test_api_start_rejects_forged_analysis_snapshot_and_preserves_old_selection_hash(tmp_path):
    runs, db = tmp_path / "runs.sqlite", tmp_path / "absent-app.sqlite"
    seed_run(runs)
    source = load_explicit_research_run(runs, run_revision_id=RUN, tickers=("1234",), known_at=NOW)
    source.analyses["1234"]["avg_turnover_oku"] = 99999
    selection = ResearchSelection(
        request=REQUEST,
        explicit_run=source,
        targets=(ResearchTarget(ticker="1234", purpose="new"),),
    )
    with pytest.raises(ValueError, match="canonical"):
        OperationService(db, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(
                checkpoint="synthetic", artifacts=(selection.model_dump(mode="json"),)
            ),
        )
    assert not db.exists()
    old = {
        "kind": "research_selection",
        "request": "synthetic old request",
        "research_triage_id": "old-triage",
        "targets": [{"ticker": "1234", "purpose": "new"}],
    }
    parsed = ResearchSelection.model_validate(old)
    assert parsed.model_dump(mode="json") == old
    assert selection_sha256(parsed) == sha256(canonical_json(old).encode()).hexdigest()


def test_cli_accepts_explicit_run_and_rejects_two_sources(tmp_path, capsys, app_method_root):
    db, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    db = app_method_root / "stores/application/baibai.sqlite"
    seed_run(runs)
    args = [
        "prepare",
        "--db",
        str(db),
        "--workspace",
        str(tmp_path / "workspace"),
        "--runs-path",
        str(runs),
        "--sqlite-path",
        str(tmp_path / "missing-market"),
        "--indicators-path",
        str(tmp_path / "missing-indicators"),
        "--request",
        REQUEST,
        "--run-revision-id",
        RUN,
        "--ticker",
        "1234",
    ]
    assert main(args, now=NOW) == 0
    assert yaml.safe_load(capsys.readouterr().out)["actionable"]
    with pytest.raises(SystemExit):
        main([*args, "--research-triage-id", "not-a-source"], now=NOW)


def test_explicit_planning_uses_pinned_liquidity_and_keeps_current_order_guards(
    app_method_root, tmp_path
):
    root = app_method_root
    db, runs, market = (
        root / "stores/application/baibai.sqlite",
        root / "stores/screening/runs.sqlite",
        root / "stores/market/market.sqlite",
    )
    seed_run(runs)
    seed_daily_bars(market, [("1234", NOW.date().isoformat(), 1000.0, 1.0)])
    session = NOW.date() + timedelta(days=1)
    store_jquants_market_calendar(
        market,
        [{"Date": day.isoformat(), "HolidayDivision": "1"} for day in [NOW.date(), session]],
        requested_start=NOW.date(),
        requested_end=session,
    )
    prepared = prepare(db, runs, tmp_path / "work", market, research_set=("1234",))
    thesis, review = pair_payload()
    from baibai_engine.research.thesis_store import ThesisStoreService

    ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
        "synthetic-plan-thesis", thesis, review
    )
    service = OperationService(db, sqlite_path=market)
    service.record_thesis(prepared.operation_id, "synthetic-plan-thesis", now=NOW)
    draft = reviewed_assessment(db, thesis_id="synthetic-plan-thesis")
    draft = draft.model_copy(
        update={
            "result": "allocate",
            "alternatives": (draft.alternatives[0].model_copy(update={"disposition": "allocate"}),),
        }
    )
    draft = draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(draft)}
            )
        }
    )
    CapitalAllocationAssessmentService(db, clock=lambda: NOW, sqlite_path=market).publish(draft)
    before = db.read_bytes()
    args = dict(
        capital_allocation_assessment_id=draft.capital_allocation_assessment_id,
        db_path=db,
        sqlite_path=market,
        budget_min_yen=200000,
        budget_max_yen=300000,
        now=NOW,
    )
    planned = plan_limit(target_session=session, **args)
    assert planned["status"] == "planned_limit"
    assert planned["liquidity_context"]["research_triage_id"] is None
    assert planned["liquidity_context"]["run_revision_id"] == RUN
    assert planned["liquidity_context"]["adv_yen"] == 1000000000
    assert plan_limit(target_session=NOW.date(), **args)["status"] == "defer"
    stale_args = {**args, "now": NOW + timedelta(days=1)}
    assert plan_limit(target_session=session, **stale_args)["status"] == "defer"
    assert db.read_bytes() == before
