from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from tests.helpers.assessment_history import seed_history
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_triage import (
    published_review_set,
    research_entry,
    research_triage_payload,
    skip_entry,
)

from baibai_engine.market_context.assessment.models import MarketContextAssessment
from baibai_engine.position.ledger import ContributionEvent
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research import workspace as workspace_module
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.triage.contracts import ResearchTriage
from baibai_engine.research.triage.service import ResearchTriageService
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    compute_status,
    prepare_holding_workspace,
    prepare_workspace,
)
from baibai_engine.research.workspace import prepare as workspace_prepare
from baibai_engine.research.workspace import validation as workspace_validation
from baibai_engine.research.workspace_cli import main as research_main
from baibai_engine.screening.discovery.contracts import PublishedReviewSet

CASE_NOW = datetime.fromisoformat("2026-07-19T18:00:00+09:00")


@pytest.mark.parametrize("scope", ["new", "holding", "combined", "empty", "empty-no-triage"])
def test_research_read_input_quotes_only_selected_triage_entries(tmp_path, scope, monkeypatch):
    from baibai_engine.research.triage.query import research_triage_payload as read_triage

    db = tmp_path / "app.sqlite"
    triage = _publish_triage(
        db, second_research=True, with_holdings=scope in {"holding", "combined"}
    )
    canonical_before = read_triage(db, triage.research_triage_id)
    calibration = {
        "status": "historical_context_only",
        "interpretation": "historical distribution; not an individual security forecast",
        "candidates": {"0001": {"horizons": ["synthetic band"]}, "2331": {"horizons": []}},
    }
    monkeypatch.setattr(
        workspace_prepare, "_load_er_distribution_context", lambda **kwargs: calibration
    )
    selected = ("0001",) if scope in {"new", "combined"} else ()
    holdings = ("2331",) if scope in {"holding", "combined"} else ()
    bound_triage = None if scope in {"holding", "empty-no-triage"} else triage.research_triage_id
    workspace = tmp_path / "workspace"
    prepared = prepare_workspace(
        research_triage_id=bound_triage,
        asof=CASE_NOW.date(),
        db_path=db,
        workspace=workspace,
        request="synthetic explicit selected scope",
        research_set=selected,
        holding_tickers=holdings,
        started_at=CASE_NOW,
        sqlite_path=tmp_path / "absent-market",
        runs_path=tmp_path / "absent-runs",
        indicators_path=tmp_path / "absent-indicators",
    )
    inputs = yaml.safe_load((workspace / "domain-inputs.yaml").read_text())
    assert set(inputs["securities"]) == set(selected + holdings)
    assert inputs["research_targets"] == [
        *({"ticker": ticker, "purpose": "new"} for ticker in selected),
        *({"ticker": ticker, "purpose": "holding"} for ticker in holdings),
    ]
    assert prepared.actionable == bool(selected + holdings)
    assert read_triage(db, triage.research_triage_id) == canonical_before
    assert [
        case["ticker"] for case in compute_status(workspace, db_path=db, now=CASE_NOW)["cases"]
    ] == list(selected)
    if bound_triage is None:
        assert "research_triage" not in inputs
    else:
        manifest = yaml.safe_load((workspace / "manifest.yaml").read_text())
        quoted = inputs["research_triage"]
        assert {key: quoted[key] for key in ("research_triage_id", "payload_sha256")} == manifest[
            "inputs"
        ]["research_triage"]
        assert [row["ticker"] for row in quoted["entries"]] == list(selected)
        assert inputs["er_realized_distribution_context"] == {
            **calibration,
            "candidates": {ticker: calibration["candidates"][ticker] for ticker in selected},
        }
        for row in quoted["entries"]:
            source = next(entry for entry in triage.entries if entry.ticker == row["ticker"])
            assert row["analysis"] == source.candidate_snapshot.analysis.model_dump(mode="json")
            assert row["nominations"] == [
                item.model_dump(mode="json") for item in source.candidate_snapshot.nominations
            ]
            assert row["research_priority"] == source.priority == 2
            assert row["rationale"] == source.rationale
            assert row["research_question"] == source.research_question
            assert row["key_risk"] == source.key_risk
        # The full canonical comparison remains internal; the AI reads the scoped input above.
        internal = yaml.safe_load((workspace / "research-workspace.yaml").read_text())
        assert len(internal["review_set_entries"]) == prepared.review_set_size == 2
        assert internal["er_realized_distribution_context"] == calibration
    for ticker in holdings:
        child = workspace / "holdings" / ticker
        manifest = yaml.safe_load((child / "manifest.yaml").read_text())
        assert manifest["operation_id"] == prepared.operation_id
        assert manifest["domain_inputs_ref"] == "../../domain-inputs.yaml"
        assert (child / manifest["domain_inputs_ref"]).resolve() == (
            workspace / "domain-inputs.yaml"
        ).resolve()
        assert not (child / "domain-inputs.yaml").exists()
        assert compute_status(child, db_path=db, now=CASE_NOW)["cases"][0]["ticker"] == ticker


def test_holding_resume_retains_parent_input_and_removes_old_duplicate(tmp_path):
    from datetime import timedelta

    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db, second_research=True, with_holdings=True)
    workspace = tmp_path / "workspace"
    kwargs = dict(
        db_path=db,
        started_at=CASE_NOW,
        sqlite_path=tmp_path / "absent-market",
        runs_path=tmp_path / "absent-runs",
        indicators_path=tmp_path / "absent-indicators",
    )
    prepared = prepare_workspace(
        research_triage_id=triage.research_triage_id,
        workspace=workspace,
        request="synthetic explicit mixed scope",
        research_set=("0001",),
        holding_tickers=("2331",),
        **kwargs,
    )
    child = workspace / "holdings/2331"
    parent_before = (workspace / "domain-inputs.yaml").read_bytes()
    db_before = db.read_bytes()
    # A force rebuild replaces a prior generated copy, without touching authored drafts.
    (child / "domain-inputs.yaml").write_text("old generated duplicate")
    _authored_case(child, "2331", "reject")
    authored_before = {path: path.read_bytes() for path in (child / "2331").glob("*.yaml")}
    resumed = prepare_holding_workspace(
        workspace=child, asof=CASE_NOW.date(), ticker="2331", force=True, **kwargs
    )
    assert resumed.operation_id == prepared.operation_id
    assert yaml.safe_load((child / "manifest.yaml").read_text())["domain_inputs_ref"] == (
        "../../domain-inputs.yaml"
    )
    assert not (child / "domain-inputs.yaml").exists()
    assert (workspace / "domain-inputs.yaml").read_bytes() == parent_before
    assert {path: path.read_bytes() for path in (child / "2331").glob("*.yaml")} == authored_before
    assert db.read_bytes() == db_before
    service = OperationService(db)
    for ticker in ("0001", "2331"):
        ended = service.record_unfinished(
            prepared.operation_id,
            ticker=ticker,
            reason="synthetic finite investigation ended",
            references=("https://example.invalid/source",),
            now=CASE_NOW,
        )
    service.complete(
        prepared.operation_id,
        ended.payload.model_copy(update={"result": "unfinished", "next": "new explicit request"}),
        completed_at=CASE_NOW,
    )
    kwargs["started_at"] = CASE_NOW + timedelta(hours=1)
    fresh = prepare_holding_workspace(
        workspace=child,
        asof=CASE_NOW.date(),
        ticker="2331",
        force=True,
        request="synthetic new standalone holding request",
        **kwargs,
    )
    assert fresh.operation_id != prepared.operation_id
    assert "domain_inputs_ref" not in yaml.safe_load((child / "manifest.yaml").read_text())
    standalone = yaml.safe_load((child / "domain-inputs.yaml").read_text())
    assert set(standalone["securities"]) == {"2331"}
    assert "research_triage" not in standalone
    assert standalone["known_at"] == kwargs["started_at"].isoformat()
    assert (workspace / "domain-inputs.yaml").read_bytes() == parent_before


@pytest.mark.parametrize("resume", [False, True])
def test_missing_exact_run_does_not_start_or_change_research(tmp_path, resume):
    from datetime import timedelta

    from tests.helpers.screening_run import screening_run_payload, security_analysis

    from baibai_engine.screening.run_store import ScreeningRunStore

    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    kwargs = dict(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        request="synthetic explicit start",
        research_set=("2331",),
        started_at=CASE_NOW,
        sqlite_path=tmp_path / "absent-market",
        indicators_path=tmp_path / "absent-indicators",
    )
    if resume:
        prepare_workspace(runs_path=tmp_path / "absent-runs", **kwargs)
    service = OperationService(db)
    operation_before = service.active()
    db_before = db.read_bytes()
    files_before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    runs = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(runs)
    payload = screening_run_payload(
        as_of=CASE_NOW.date().isoformat(), security_analyses=[security_analysis("2331")]
    )
    store.publish_run(
        screening_run_payload(
            as_of=(CASE_NOW.date() - timedelta(days=1)).isoformat(),
            security_analyses=[security_analysis("2331")],
        ),
        run_revision_id="retained-other-run",
    )
    runs_before = runs.read_bytes()
    kwargs["started_at"] = CASE_NOW + timedelta(hours=1)
    with pytest.raises(ValueError, match="unknown run_revision_id"):
        prepare_workspace(runs_path=runs, force=resume, **kwargs)
    assert service.active() == operation_before
    assert db.read_bytes() == db_before
    assert {
        path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()
    } == files_before
    assert runs.read_bytes() == runs_before
    if not resume:
        assert not workspace.exists()
    store.publish_run(payload, run_revision_id=triage.run_revision_id)
    prepared = prepare_workspace(runs_path=runs, force=resume, **kwargs)
    operation = service.get(prepared.operation_id)
    expected_cutoff = CASE_NOW if resume else kwargs["started_at"]
    assert operation.started_at == expected_cutoff
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text())["known_at"] == (
        expected_cutoff.isoformat()
    )


def test_unreadable_holding_input_leaves_no_operation_or_workspace(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite"
    _publish_triage(db, with_holdings=True)
    workspace = tmp_path / "holding"
    service = OperationService(db)
    before = db.read_bytes()

    def unavailable(**kwargs):
        assert service.active() is None
        assert not workspace.exists()
        raise ValueError("synthetic unreadable input")

    monkeypatch.setattr(workspace_prepare, "research_inputs", unavailable)
    with pytest.raises(ValueError, match="synthetic unreadable input"):
        prepare_holding_workspace(
            asof=CASE_NOW.date(),
            db_path=db,
            ticker="2331",
            workspace=workspace,
            request="synthetic explicit holding start",
            started_at=CASE_NOW,
        )
    assert service.active() is None
    assert db.read_bytes() == before
    assert not workspace.exists()


@pytest.mark.parametrize("mutation", ["extend", "shrink", "other-triage", "inactive"])
def test_research_writes_recheck_operation_binding(
    tmp_path: Path, mutation: str, subtests: pytest.Subtests
) -> None:
    import json

    from baibai_engine.storage.application.write import connect_rw

    db, workspace = tmp_path / "app.sqlite", tmp_path / "workspace"
    triage = _publish_triage(db, second_research=True)
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",) if mutation == "extend" else ("2331", "0001"),
    )
    _authored_case(workspace, "2331", "reject")
    if mutation in {"extend", "shrink"}:
        for name in ("manifest.yaml", "research-workspace.yaml"):
            path = workspace / name
            payload = yaml.safe_load(path.read_text())
            payload["research_set"] = ["2331", "0001"] if mutation == "extend" else ["2331"]
            path.write_text(yaml.safe_dump(payload))
    else:
        with connect_rw(db) as connection:
            if mutation == "inactive":
                connection.execute(
                    "UPDATE operation_session SET status='completed', completed_at=started_at"
                )
            else:
                row = connection.execute("SELECT payload FROM operation_session").fetchone()
                payload = json.loads(row[0])
                payload["artifacts"][0]["research_triage_id"] = "different-triage"
                connection.execute("UPDATE operation_session SET payload=?", (json.dumps(payload),))
    # Historical inspection is separate from permission to write.
    if mutation == "other-triage":
        with pytest.raises(ResearchWorkspaceConflictError, match="Operation"):
            compute_status(workspace, db_path=db, now=CASE_NOW)
    else:
        assert compute_status(workspace, db_path=db, now=CASE_NOW)["cases"]
    files_before = {path: path.read_bytes() for path in workspace.rglob("*.yaml")}
    db_before = db.read_bytes()
    for action, invoke in (
        (
            "thesis-scaffold",
            lambda: workspace_module.scaffold_thesis(
                workspace=workspace,
                ticker="2331",
                sqlite_path=tmp_path / "absent.sqlite",
                target_session=CASE_NOW.date(),
                retrieved_at=CASE_NOW,
                db_path=db,
                force=True,
            ),
        ),
        (
            "review-scaffold",
            lambda: workspace_module.scaffold_review(
                workspace=workspace, ticker="2331", db_path=db, force=True
            ),
        ),
        (
            "promote",
            lambda: workspace_module.promote(
                workspace=workspace,
                ticker="2331",
                db_path=db,
                thesis_id=None,
                supersedes_id=None,
                now=CASE_NOW,
            ),
        ),
    ):
        with subtests.test(action=action):
            with pytest.raises(ResearchWorkspaceConflictError, match="Operation"):
                invoke()
            assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == files_before
            assert db.read_bytes() == db_before


def test_research_resumes_reordered_set_and_keeps_prepare_context(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db, second_research=True)
    workspace = tmp_path / "workspace"
    context_file = tmp_path / "calibration.yaml"
    context_file.write_text("original calibration")
    context = {"generated_at": "2026-07-19T09:00:00+09:00", "summary": "prepare context"}
    monkeypatch.setattr(
        workspace_prepare,
        "_load_er_distribution_context",
        lambda **kwargs: context,
    )
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
    )
    operation = OperationService(db).active()
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("0001", "2331"),
        force=True,
    )
    assert OperationService(db).active() == operation
    _authored_case(workspace, "2331", "reject")
    before = (workspace / "research-workspace.yaml").read_bytes()
    ledger = LedgerStoreService(db)
    original, head = ledger.load_with_head()
    event = ContributionEvent(
        type="contribution",
        event_id="unrelated-deposit",
        occurred_at=original.as_of,
        amount_yen=1000,
    )
    ledger.apply_document(
        expected_head=head,
        expected_document=original,
        replacement=original.model_copy(update={"events": (*original.events, event)}),
    )
    assert ledger.append_head() > head
    for change in (lambda: context_file.write_text("new calibration"), context_file.unlink):
        change()
        status = compute_status(workspace, db_path=db, now=CASE_NOW)
        assert status["cases"][1]["status"] == "ready_for_promotion"
        assert (workspace / "research-workspace.yaml").read_bytes() == before
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id=None,
        supersedes_id=None,
        now=CASE_NOW,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "proposal",
        "arithmetic",
        "review-missing",
        "stale-core",
        "arithmetic-missing",
        "arithmetic-stale",
    ],
)
def test_status_and_promote_share_case_eligibility(tmp_path: Path, mutation: str, mocker) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    _authored_case(workspace, "2331", "reject")
    thesis_path = workspace / "2331/thesis-draft.yaml"
    review_path = workspace / "2331/2026-07-19-2331-decision-review.yaml"
    review = yaml.safe_load(review_path.read_text())
    if mutation == "proposal":
        review.update(proposal_changed=True, change_rationale="revise the valuation")
    elif mutation == "stale-core":
        review["reviewed_thesis_sha256"] = "0" * 64
    elif mutation.startswith("arithmetic"):
        from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash

        raw = yaml.safe_load(thesis_path.read_text())
        raw["valuation"]["base"]["terminal_value_per_share_yen"] = 999
        thesis_path.write_text(yaml.safe_dump(raw))
        review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(raw))
        if mutation == "arithmetic-stale":
            review["reviewed_thesis_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review))
    if mutation in {"review-missing", "arithmetic-missing"}:
        review_path.unlink()
    read = mocker.spy(workspace_validation, "_load_mapping")
    status = compute_status(workspace, db_path=db, now=CASE_NOW)
    case = status["cases"][0]
    assert case["status"] != "ready_for_promotion"
    errors = case["thesis_validation_errors"] + case["review_validation_errors"]
    assert errors
    assert sum(call.args[0] == thesis_path for call in read.call_args_list) == 1
    with pytest.raises(ResearchWorkspaceDataError) as error:
        workspace_module.promote(
            workspace=workspace,
            ticker="2331",
            db_path=db,
            thesis_id=None,
            supersedes_id=None,
            now=CASE_NOW,
        )
    assert errors[0] in str(error.value)


def _publish_triage(
    db_path: Path,
    *,
    research: bool = True,
    second_research: bool = False,
    with_holdings: bool = False,
    as_of: str = "2026-07-19",
) -> ResearchTriage:
    ledger = load_portfolio_ledger(
        Path(__file__).resolve().parents[1] / "fixtures/portfolio-ledger/representative.yaml"
    )
    seed_ledger(
        db_path,
        ledger.model_copy(
            update={
                "events": ledger.events
                if with_holdings
                else tuple(
                    event
                    for event in ledger.events
                    if event.type in {"opening_balance", "contribution", "withdrawal"}
                ),
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                ),
            }
        ),
    )
    if not research:
        entries = [skip_entry("2331"), skip_entry("0001")]
    elif second_research:
        entries = [research_entry("2331", rank=1), research_entry("0001", rank=2)]
    else:
        entries = [research_entry("2331", rank=1), skip_entry("0001")]
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id="research-triage-workspace-v2",
            review_set_id="review-set-workspace-v2",
            run_revision_id="runrev-workspace-v2",
            as_of=as_of,
            entries=entries,
        )
    )
    review_set = PublishedReviewSet.model_validate(
        published_review_set(
            as_of=as_of,
            review_set_id=triage.review_set_id,
            run_revision_id=triage.run_revision_id,
            tickers=("2331", "0001"),
        )
    )
    return ResearchTriageService(db_path).publish(triage, review_set=review_set)


@pytest.mark.parametrize("purpose", ["new", "holding", "combined"])
def test_weekend_research_keeps_start_cutoff_on_resume_and_refreshes_for_new_operation(
    tmp_path, purpose
):
    from datetime import timedelta

    from baibai_engine.external_assessment.observations.models import (
        ModelValuationObservation,
        QuantsScoreObservation,
    )
    from baibai_engine.external_assessment.store import save_observations

    db = tmp_path / "app.sqlite"
    friday = date(2026, 7, 17)
    now = CASE_NOW
    triage = _publish_triage(
        db,
        with_holdings=purpose != "new",
        second_research=purpose == "combined",
        as_of=friday.isoformat(),
    )
    for name, hour in (("available", 12), ("future", 19)):
        body = MarketContextAssessment.model_validate(
            {
                "context_id": f"market-context-assessment-2026-07-17-{name}",
                "as_of": friday.isoformat(),
                "known_at": "2026-07-19T11:00:00+09:00",
                "published_at": f"2026-07-19T{hour}:00:00+09:00",
                "summary": name,
                "inputs": [],
            }
        ).payload()
        seed_history(db, body)
    market = tmp_path / "market.sqlite"
    for received, target, growth in (
        (now - timedelta(hours=1), 1200, 6),
        (now + timedelta(hours=1), 1800, 9),
    ):
        save_observations(
            market,
            [
                ModelValuationObservation(
                    ticker="2331",
                    fetched_at_utc=received,
                    distributor="monex",
                    source_url="https://example.invalid/monex/2331",
                    parser_version="synthetic-1",
                    consensus_target=target,
                ),
                QuantsScoreObservation(
                    ticker="2331",
                    fetched_at_utc=received,
                    score_as_of_date=received.date(),
                    source_url="https://example.invalid/kabumap/2331",
                    parser_version="synthetic-1",
                    growth_score=growth,
                ),
            ],
        )
    before = market.read_bytes()
    workspace = tmp_path / "workspace"
    kwargs = {
        "db_path": db,
        "request": "日曜に金曜対象の調査を開始",
        "sqlite_path": market,
        "runs_path": tmp_path / "absent-runs.sqlite",
        "indicators_path": tmp_path / "absent-indicators.sqlite",
    }
    if purpose == "new":
        prepare = prepare_workspace
        kwargs.update(research_triage_id=triage.research_triage_id, research_set=("2331",))
    elif purpose == "combined":
        prepare = prepare_workspace
        kwargs.update(
            research_triage_id=triage.research_triage_id,
            research_set=("0001",),
            holding_tickers=("2331",),
        )
    else:
        prepare = prepare_holding_workspace
        kwargs.update(asof=friday, ticker="2331")

    def assert_inputs(root, *, cutoff, target, growth, received, context_name):
        paths = [root / "domain-inputs.yaml"]
        if purpose == "combined":
            child = root / "holdings/2331"
            manifest = yaml.safe_load((child / "manifest.yaml").read_text())
            assert manifest["domain_inputs_ref"] == "../../domain-inputs.yaml"
            assert not (child / "domain-inputs.yaml").exists()
            assert (child / manifest["domain_inputs_ref"]).resolve() == paths[0].resolve()
        for path in paths:
            result = yaml.safe_load(path.read_text())
            assert result["known_at"] == cutoff.isoformat()
            assert result["as_of"] == friday.isoformat()
            assert result["received_cutoff"] == cutoff.isoformat()
            external = result["securities"]["2331"]["external_assessment"]
            assert datetime.fromisoformat(external["cutoff"]) == cutoff
            assert datetime.fromisoformat(external["evaluated_at"]) == cutoff
            latest = external["model_valuations"][0]["inputs"]["latest"]
            assert latest["consensus_target"] == target
            assert datetime.fromisoformat(latest["fetched_at_utc"]) == received
            quants = external["quants_scores"][0]["inputs"]["latest"]
            assert quants["growth_score"] == growth
            assert quants["score_as_of_date"] == now.date().isoformat()
            assert datetime.fromisoformat(quants["fetched_at_utc"]) == received
            context = result["market_context"]["saved_market_context_assessment"]
            assert context["context_id"] == f"market-context-assessment-2026-07-17-{context_name}"
            assert context["summary"] == context_name
        assert market.read_bytes() == before

    first = prepare(workspace=workspace, started_at=now, **kwargs)
    service = OperationService(db, sqlite_path=market)
    original = service.get(first.operation_id)
    db_before_resume = db.read_bytes()
    for root, force, at in (
        (workspace, False, now),
        (workspace, True, now + timedelta(hours=2)),
        (tmp_path / "same-operation", False, now + timedelta(hours=2)),
    ):
        if at != now:
            resumed = prepare(workspace=root, started_at=at, force=force, **kwargs)
            assert resumed.operation_id == first.operation_id
        assert service.get(first.operation_id) == original
        assert db.read_bytes() == db_before_resume
        assert_inputs(
            root,
            cutoff=now,
            target=1200,
            growth=6,
            received=now - timedelta(hours=1),
            context_name="available",
        )

    tickers = ("0001", "2331") if purpose == "combined" else ("2331",)
    for ticker in tickers:
        ended = service.record_unfinished(
            first.operation_id,
            ticker=ticker,
            reason="synthetic finite investigation ended",
            references=("https://example.invalid/source",),
            now=now + timedelta(hours=2),
        )
    completed = service.complete(
        first.operation_id,
        ended.payload.model_copy(update={"result": "unfinished", "next": "new explicit request"}),
        completed_at=now + timedelta(hours=2),
    )
    later = now + timedelta(hours=3)
    fresh_workspace = tmp_path / "new-operation"
    fresh = prepare(workspace=fresh_workspace, started_at=later, **kwargs)
    assert fresh.operation_id != first.operation_id
    assert service.get(first.operation_id) == completed
    assert service.get(fresh.operation_id).started_at == later
    assert_inputs(
        fresh_workspace,
        cutoff=later,
        target=1800,
        growth=9,
        received=now + timedelta(hours=1),
        context_name="future",
    )


def test_prepare_and_status_need_only_published_triage_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workspace_prepare, "ER_LEVEL_CALIBRATION_CONTEXT_PATH", tmp_path / "missing-context.yaml"
    )
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    workspace = tmp_path / "workspace"

    assert (
        research_main(
            [
                "prepare",
                "--request",
                "synthetic explicit research request",
                "--research-triage-id",
                triage.research_triage_id,
                "--db",
                str(db_path),
                "--workspace",
                str(workspace),
                "--ticker",
                "2331",
            ]
        )
        == 0
    )
    prepared_output = yaml.safe_load(capsys.readouterr().out)

    assert prepared_output["actionable"] is True
    assert prepared_output["admissible_research_tickers"] == ["2331"]
    assert not (workspace / "review-set.yaml").exists()
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text())
    assert set(manifest) == {"as_of", "inputs", "research_set", "operation_id", "selection_sha256"}
    assert set(manifest["inputs"]) == {"research_triage"}
    assert set(manifest["inputs"]["research_triage"]) == {"research_triage_id", "payload_sha256"}
    assert not (workspace / "research-comparison.yaml").exists()
    comparison = yaml.safe_load((workspace / "research-workspace.yaml").read_text())
    assert "admissible_count" not in comparison
    assert "admissible_research_tickers" not in comparison
    assert comparison["er_realized_distribution_context"] == {
        "status": "unavailable",
        "reason": "missing_artifact",
    }
    assert compute_status(
        workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )["research_triage"] == {
        "purpose": "fundamental_research",
        "research_triage_id": triage.research_triage_id,
        "admissible_research_tickers": ["2331"],
    }
    operation = OperationService(db_path).active()
    assert operation is not None
    assert operation.payload.canonical_refs == (triage.research_triage_id,)
    assert operation.payload.artifacts[0]["targets"] == [{"ticker": "2331", "purpose": "new"}]


def test_workspace_rejects_skip_admission_and_payload_hash_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
        research_set=("2331",),
    )
    workspace_path = workspace / "research-workspace.yaml"
    payload = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    payload["research_set"] = ["0001"]
    workspace_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResearchWorkspaceConflictError, match="fixed selection"):
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )

    manifest_path = workspace / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["research_triage"]["payload_sha256"] = "0" * 64
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ResearchWorkspaceConflictError, match="does not match the canonical"):
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )


def test_zero_research_is_a_normal_prepared_workspace(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path, research=False)

    workspace = tmp_path / "workspace"
    prepared = prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
    )

    assert not prepared.actionable
    assert prepared.admissible_count == 0
    assert prepared.admissible_research_tickers == ()
    assert (
        compute_status(
            workspace, db_path=db_path, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "no_research"
    )
    assert OperationService(db_path).active() is not None


def test_explicit_empty_selection_is_recorded_without_fake_proposal(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)

    prepared = prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=tmp_path / "workspace",
    )

    assert not prepared.actionable
    assert prepared.admissible_research_tickers == ("2331",)
    assert OperationService(db_path).active() is not None


def test_prepare_rejects_skip_ticker_before_starting_operation(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)

    with pytest.raises(ResearchWorkspaceDataError, match="did not mark research"):
        prepare_workspace(
            started_at=CASE_NOW,
            request="synthetic explicit research request",
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=tmp_path / "workspace",
            research_set=("0001",),
        )

    assert OperationService(db_path).active() is None


def test_active_operation_blocks_only_new_research_start(tmp_path: Path) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path)
    service = OperationService(db_path)
    existing = service.start(
        session_kind="capital-allocation",
        as_of=date(2026, 8, 31),
        ticker="2331",
        started_at=datetime.fromisoformat("2026-08-31T18:00:00+09:00"),
        payload=OperationPayload(
            checkpoint="research",
            artifacts=(
                {
                    "kind": "research_selection",
                    "request": "synthetic competing request",
                    "targets": [],
                },
            ),
        ),
    )

    with pytest.raises(ResearchWorkspaceConflictError, match="active operation already exists"):
        prepare_workspace(
            started_at=CASE_NOW,
            request="synthetic explicit research request",
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=tmp_path / "workspace",
            research_set=("2331",),
        )

    assert service.active() == existing


def test_force_cannot_overwrite_workspace_with_a_different_active_research_set(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.sqlite"
    triage = _publish_triage(db_path, second_research=True)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db_path,
        workspace=workspace,
        research_set=("2331",),
    )
    manifest_before = (workspace / "manifest.yaml").read_bytes()

    with pytest.raises(ResearchWorkspaceConflictError, match="active operation already exists"):
        prepare_workspace(
            started_at=CASE_NOW,
            request="synthetic explicit research request",
            research_triage_id=triage.research_triage_id,
            db_path=db_path,
            workspace=workspace,
            research_set=("0001",),
            force=True,
        )

    assert (workspace / "manifest.yaml").read_bytes() == manifest_before


def test_thesis_scaffold_without_quote_is_unresolved_v4(tmp_path):
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "research"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    workspace_module.scaffold_thesis(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        sqlite_path=tmp_path / "absent.sqlite",
        target_session=date(2026, 7, 19),
        retrieved_at=CASE_NOW,
    )
    raw = yaml.safe_load((workspace / "2331/thesis-draft.yaml").read_text())
    assert raw["schema_version"] == 4
    assert raw["valuation"]["status"] == "unresolved"
    assert raw["valuation"]["market_price_fact_id"] is None
    assert not (workspace / "2331/research-checklist.yaml").exists()


def _authored_case(workspace: Path, ticker: str, recommendation: str) -> None:
    from tests.helpers.research_v4 import pair_payload

    from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash

    raw, review = pair_payload(ticker=ticker, as_of="2026-07-19", quote_as_of="2026-07-17")
    raw["input_snapshot"]["sources"][0]["retrieved_at"] = "2026-07-19T09:00:00+09:00"
    raw["judgment"]["proposed_at"] = "2026-07-19T10:00:00+09:00"
    raw["judgment"]["disposition"] = recommendation
    review["review_id"] = "review-" + ticker
    review["reviewed_at"] = "2026-07-19T11:00:00+09:00"
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(raw))
    directory = workspace / ticker
    directory.mkdir(exist_ok=True)
    (directory / "thesis-draft.yaml").write_text(yaml.safe_dump(raw))
    (directory / f"2026-07-19-{ticker}-decision-review.yaml").write_text(yaml.safe_dump(review))
    # Stray old local files have no authority over publish.
    (directory / "research-checklist.yaml").write_text(
        yaml.safe_dump({"checks": [{"status": "complete"}]})
    )


@pytest.mark.parametrize("recommendations", [("candidate", "reject"), ("defer", "reject")])
def test_all_cases_promote_without_a_winner_and_status_tracks_exact_publication(
    tmp_path: Path, recommendations: tuple[str, str]
) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db, second_research=True)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["ticker"] for case in status["cases"]] == ["2331", "0001"]
    assert all(case["status"] == "incomplete" for case in status["cases"])
    assert "selected_ticker" not in status
    assert not (workspace / "research-comparison.yaml").exists()
    for ticker, recommendation in zip(("2331", "0001"), recommendations, strict=True):
        _authored_case(workspace, ticker, recommendation)
    assert all(
        case["status"] == "ready_for_promotion"
        for case in compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"]
    )
    now = datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    workspace_module.promote(
        workspace=workspace,
        ticker="0001",
        db_path=db,
        thesis_id="thesis-second",
        supersedes_id=None,
        now=now,
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["status"] for case in status["cases"]] == ["ready_for_promotion", "published"]
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="thesis-first",
        supersedes_id=None,
        now=now,
    )
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert status["workspace_status"] == "published"
    assert status["next_action"] is None
    assert [case["thesis_id"] for case in status["cases"]] == ["thesis-first", "thesis-second"]
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2027-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    # Re-prepare keeps authored drafts; status derives publication without a local journal.
    authored = (workspace / "2331/thesis-draft.yaml").read_bytes()
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331", "0001"),
        force=True,
    )
    assert (workspace / "2331/thesis-draft.yaml").read_bytes() == authored
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    review_path = workspace / "0001/2026-07-19-0001-decision-review.yaml"
    review = yaml.safe_load(review_path.read_text())
    review["reviewed_thesis_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review))
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"][1]["status"]
        == "ready_for_review"
    )
    with pytest.raises(ResearchWorkspaceDataError, match="stale"):
        workspace_module.promote(
            workspace=workspace,
            ticker="0001",
            db_path=db,
            thesis_id=None,
            supersedes_id=None,
            now=now,
        )


def test_holding_workspace_has_one_ledger_subject_without_comparison(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _publish_triage(db, with_holdings=True)
    workspace = tmp_path / "holding"
    workspace_module.prepare_holding_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit holding request",
        db_path=db,
        workspace=workspace,
        ticker="2331",
        asof=date(2026, 7, 11),
    )
    assert not (workspace / "research-comparison.yaml").exists()
    status = compute_status(
        workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
    )
    assert [case["ticker"] for case in status["cases"]] == ["2331"]
    assert status["research_triage"]["research_triage_id"] is None
    path = workspace / "research-workspace.yaml"
    document = yaml.safe_load(path.read_text())
    document["research_set"] = ["0001"]
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ResearchWorkspaceDataError, match="holding subject fixed"):
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )


@pytest.mark.parametrize("check_status", ["pending", "blocked"])
@pytest.mark.parametrize("edit", ["thesis", "review"])
def test_publication_ignores_checklist_but_requires_exact_documents(
    tmp_path: Path, check_status: str, edit: str
) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    _authored_case(workspace, "2331", "reject")
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="published-case",
        supersedes_id=None,
        now=CASE_NOW,
    )
    checklist = workspace / "2331/research-checklist.yaml"
    raw = yaml.safe_load(checklist.read_text())
    raw["checks"][0]["status"] = check_status
    checklist.write_text(yaml.safe_dump(raw))
    before = db.read_bytes()
    status = compute_status(workspace, db_path=db, now=CASE_NOW)
    assert status["workspace_status"] == "published"
    assert status["next_action"] is None
    assert status["cases"][0]["thesis_id"] == "published-case"
    if edit == "thesis":
        path = workspace / "2331/thesis-draft.yaml"
        raw = yaml.safe_load(path.read_text())
        raw["judgment"]["strongest_countercase"] += " Changed local thesis."
    else:
        path = workspace / "2331/2026-07-19-2331-decision-review.yaml"
        raw = yaml.safe_load(path.read_text())
        raw["reviewer_identity"] += "-changed"
    path.write_text(yaml.safe_dump(raw))
    status = compute_status(workspace, db_path=db, now=CASE_NOW)
    assert status["workspace_status"] != "published"
    assert status["cases"][0]["thesis_id"] is None
    assert db.read_bytes() == before


def test_obsolete_override_cannot_bypass_exact_publication(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "workspace"
    prepare_workspace(
        started_at=CASE_NOW,
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    _authored_case(workspace, "2331", "candidate")
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="thesis-original",
        supersedes_id=None,
        now=datetime.fromisoformat("2026-07-19T12:00:00+09:00"),
    )
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["workspace_status"]
        == "published"
    )
    path = workspace / "2331/thesis-draft.yaml"
    draft = yaml.safe_load(path.read_text())
    draft["human_evidence_override"] = {"reason": "different human acknowledgement"}
    path.write_text(yaml.safe_dump(draft))
    assert (
        compute_status(
            workspace, db_path=db, now=datetime.fromisoformat("2026-07-19T12:00:00+09:00")
        )["cases"][0]["status"]
        == "incomplete"
    )


def test_risk_only_case_completes_review_promotion_and_no_allocation(tmp_path):
    from baibai_engine.research.capital_allocation import (
        CapitalAllocationAssessment,
        capital_allocation_draft_sha256,
    )
    from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
    from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
    from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash

    db = tmp_path / "app.sqlite"
    triage = _publish_triage(db)
    workspace = tmp_path / "research"
    prepare_workspace(
        request="synthetic explicit research request",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
        started_at=CASE_NOW,
    )
    workspace_module.scaffold_thesis(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        sqlite_path=tmp_path / "no-price.sqlite",
        target_session=CASE_NOW.date(),
        retrieved_at=CASE_NOW,
    )
    _authored_case(workspace, "2331", "defer")
    path = workspace / "2331/thesis-draft.yaml"
    thesis = yaml.safe_load(path.read_text())
    thesis["input_snapshot"]["facts"] = []
    thesis["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="企業の資金繰りは確認したが回収価値と価格は未確定",
    )
    path.write_text(yaml.safe_dump(thesis))
    review_path = workspace / "2331/2026-07-19-2331-decision-review.yaml"
    review = yaml.safe_load(review_path.read_text())
    review["recalculated_projections"] = []
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    review_path.write_text(yaml.safe_dump(review))
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="risk-only",
        supersedes_id=None,
        now=CASE_NOW,
    )
    raw = scaffold_capital_allocation(
        db_path=db,
        capital_allocation_assessment_id="capital-allocation-assessment-20260719-risk-only",
        as_of=CASE_NOW.date(),
        research_triage_id=triage.research_triage_id,
        thesis_ids=["risk-only"],
        published_at=CASE_NOW,
    )
    raw.update(
        headline="評価未解決のため現金維持",
        comparison="不確かな価値より現金",
        forgone="資料入手後に再評価",
    )
    raw["alternatives"][0]["rationale"] = "企業評価未解決"
    assessment = CapitalAllocationAssessment.model_validate(raw)
    assessment = assessment.model_copy(
        update={
            "review": assessment.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(assessment)}
            )
        }
    )
    assert (
        CapitalAllocationAssessmentService(db, clock=lambda: CASE_NOW).publish(assessment).result
        == "no_allocation"
    )
    # Refresh preserves original sources and forecasts; a new independent review is required.
    refresh_at = datetime.fromisoformat("2026-07-20T12:00:00+09:00")
    workspace_module.scaffold_thesis(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        sqlite_path=tmp_path / "no-price.sqlite",
        target_session=refresh_at.date(),
        retrieved_at=refresh_at,
        from_thesis_id="risk-only",
        force=True,
    )
    refreshed = yaml.safe_load(path.read_text())
    assert refreshed["input_snapshot"]["as_of"] == "2026-07-20"
    assert (
        refreshed["input_snapshot"]["sources"]
        == ThesisDocument.model_validate(thesis).model_dump(mode="json")["input_snapshot"][
            "sources"
        ]
    )
    assert refreshed["valuation"] == thesis["valuation"]
    assert (
        compute_status(workspace, db_path=db, now=refresh_at)["cases"][0]["status"]
        == "ready_for_review"
    )


@pytest.mark.parametrize("complete_at_night", [False, True])
def test_night_research_reaches_next_morning_planning_through_delta_refresh(
    tmp_path, complete_at_night
):
    from contextlib import closing
    from datetime import timedelta

    from tests.helpers.research_v4 import pair_payload

    from baibai_engine.research.capital_allocation import (
        CapitalAllocationAssessment,
        capital_allocation_draft_sha256,
    )
    from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
    from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
    from baibai_engine.research.planning import plan_limit
    from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash
    from baibai_engine.research.thesis_store import load_reviewed_thesis
    from baibai_engine.storage.application.read import connect_read_only
    from baibai_engine.storage.observations.schema import open_connection

    night = datetime.fromisoformat("2026-09-07T18:00:00+09:00")
    morning = night.replace(hour=9) + timedelta(days=1)
    db, market, workspace = (
        tmp_path / "app.sqlite",
        tmp_path / "market.sqlite",
        tmp_path / "research",
    )
    triage = _publish_triage(db, as_of=night.date().isoformat())
    with closing(open_connection(market)) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) VALUES ('2331','2026-09-07',1000,1)"
        )
        connection.execute("INSERT INTO jquants_market_calendar VALUES ('2026-09-08',1)")
        connection.commit()
    prepared = prepare_workspace(
        request="synthetic explicit overnight research",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
        started_at=night,
        sqlite_path=market,
        runs_path=tmp_path / "absent-runs",
        indicators_path=tmp_path / "absent-indicators",
    )
    path = workspace / "2331/thesis-draft.yaml"
    path.parent.mkdir(exist_ok=True)
    raw, review = pair_payload(ticker="2331", quote_as_of=night.date().isoformat())
    raw["input_snapshot"]["sources"][0]["retrieved_at"] = night.replace(hour=16).isoformat()
    raw["judgment"]["proposed_at"] = night.replace(hour=16).isoformat()
    raw["valuation"]["required_annual_return_pct"] = 6
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(raw))
    path.write_text(yaml.safe_dump(raw))
    review_path = path.parent / "2026-09-07-2331-decision-review.yaml"
    review_path.write_text(yaml.safe_dump(review))
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="night",
        supersedes_id=None,
        now=night,
    )

    def publish_allocate(thesis_id, assessment_id, now):
        payload = scaffold_capital_allocation(
            db_path=db,
            capital_allocation_assessment_id=assessment_id,
            as_of=now.date(),
            research_triage_id=triage.research_triage_id,
            thesis_ids=[thesis_id],
            published_at=now,
        )
        payload.update(
            result="allocate",
            headline="synthetic allocation",
            comparison="same horizon and future cash checked",
            forgone="cash alternative checked",
        )
        payload["alternatives"][0].update(
            disposition="allocate", rationale="synthetic value comparison"
        )
        assessment = CapitalAllocationAssessment.model_validate(payload)
        assessment = assessment.model_copy(
            update={
                "review": assessment.review.model_copy(
                    update={"draft_sha256": capital_allocation_draft_sha256(assessment)}
                )
            }
        )
        CapitalAllocationAssessmentService(db, clock=lambda: now, sqlite_path=market).publish(
            assessment
        )

    def plan(assessment_id, now):
        return plan_limit(
            capital_allocation_assessment_id=assessment_id,
            db_path=db,
            sqlite_path=market,
            target_session=morning.date(),
            budget_min_yen=200000,
            budget_max_yen=300000,
            now=now,
        )

    night_assessment = "capital-allocation-assessment-20260907-night"
    publish_allocate("night", night_assessment, night)
    assert plan(night_assessment, night)["status"] == "planned_limit"
    before = plan(night_assessment, morning)
    assert before["status"] == "defer"
    assert before["defer_reasons"] == ["valuation_basis_requires_refresh"]

    original = path.read_bytes()
    original_document = ThesisDocument.model_validate(yaml.safe_load(original))
    if complete_at_night:
        service = OperationService(db, sqlite_path=market)
        operation = service.get(prepared.operation_id)
        service.complete(
            operation.operation_id,
            operation.payload.model_copy(
                update={
                    "artifacts": (
                        *operation.payload.artifacts,
                        {"kind": "capital_allocation_assessment", "ref": night_assessment},
                    ),
                    "result": "proposal completed",
                    "next": "human decision remains external",
                }
            ),
            completed_at=night,
        )
        workspace = tmp_path / "morning-research"
        prepared = prepare_workspace(
            request="synthetic explicit morning delta refresh",
            research_triage_id=triage.research_triage_id,
            db_path=db,
            workspace=workspace,
            research_set=("2331",),
            started_at=morning,
            sqlite_path=market,
            runs_path=tmp_path / "absent-runs",
            indicators_path=tmp_path / "absent-indicators",
        )
        path = workspace / "2331/thesis-draft.yaml"
        review_path = path.parent / "2026-09-07-2331-decision-review.yaml"
    workspace_module.scaffold_thesis(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        sqlite_path=market,
        target_session=morning.date(),
        retrieved_at=morning,
        force=True,
        from_thesis_id="night",
    )
    refreshed = yaml.safe_load(path.read_text())
    assert (
        refreshed["input_snapshot"]["sources"]
        == original_document.model_dump(mode="json")["input_snapshot"]["sources"]
    )
    assert refreshed["valuation"] == original_document.model_dump(mode="json")["valuation"]
    assert (
        compute_status(workspace, db_path=db, now=morning)["cases"][0]["status"]
        == "ready_for_review"
    )
    # An author and independent reviewer confirm the new basis; the scaffold alone is insufficient.
    refreshed["valuation"]["base"]["calculation"] += "。翌朝の価値・12か月・未権利分配を再確認"
    path.write_text(yaml.safe_dump(refreshed))
    review.update(
        review_id="review-morning",
        reviewed_at=morning.isoformat(),
        reviewed_thesis_sha256=thesis_core_hash(ThesisDocument.model_validate(refreshed)),
    )
    review_path.write_text(yaml.safe_dump(review))
    workspace_module.promote(
        workspace=workspace,
        ticker="2331",
        db_path=db,
        thesis_id="morning",
        supersedes_id="night",
        now=morning,
    )
    morning_assessment = "capital-allocation-assessment-20260908-morning"
    publish_allocate("morning", morning_assessment, morning)
    after = plan(morning_assessment, morning)
    assert after["status"] == "planned_limit"
    assert after["quantity"] == 300
    assert after["price_as_of"] == "2026-09-07"
    assert after["valuation_as_of"] == "2026-09-08"
    assert after["available_cash_yen"] == before["available_cash_yen"]
    assert OperationService(db).active().operation_id == prepared.operation_id
    with closing(connect_read_only(db)) as connection:
        stored = load_reviewed_thesis(connection, "night")
    assert stored.document == original_document


def test_saved_v3_triage_workspace_resumes_with_original_payload_hash(tmp_path):
    import sqlite3
    from hashlib import sha256

    from baibai_engine.storage.application.json import canonical_json

    db, workspace = tmp_path / "app.sqlite", tmp_path / "workspace"
    triage = _publish_triage(db)
    old = triage.model_dump(mode="json")
    old["schema_version"] = 3
    old["triage_contract_id"] = "research-triage-v3"
    old["macro_context_id"] = old.pop("market_context_assessment_id")
    raw = canonical_json(old)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE research_triage SET payload=?", (raw,))
    args = dict(
        started_at=CASE_NOW,
        request="resume saved v3 admission",
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=workspace,
        research_set=("2331",),
    )
    prepare_workspace(**args)
    manifest = yaml.safe_load((workspace / "manifest.yaml").read_text())
    assert (
        manifest["inputs"]["research_triage"]["payload_sha256"] == sha256(raw.encode()).hexdigest()
    )
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    status = compute_status(workspace, db_path=db, now=CASE_NOW)
    assert status["research_triage"]["research_triage_id"] == triage.research_triage_id
    after = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    assert after == before
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT payload FROM research_triage").fetchone()[0] == raw
