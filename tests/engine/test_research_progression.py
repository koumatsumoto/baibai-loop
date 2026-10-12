"""固定対象、部分完了、L3完了と確認済み公開の境界を通す。"""

import socket
import sqlite3
from datetime import timedelta

import pytest
from tests.engine.test_position_review_v3 import NOW
from tests.helpers.research_triage import research_entry, research_triage_payload
from tests.helpers.research_v4 import pair_payload

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.inputs import research_inputs
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.progress import (
    ResearchSelection,
    ResearchTarget,
    progress_binding,
    selection_binding,
)
from baibai_engine.research.operation.service import (
    OperationCompletionError,
    OperationConflictError,
    OperationService,
)
from baibai_engine.research.position_review import RemainingReward
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_engine.research.thesis_store import ThesisStoreService
from baibai_engine.research.triage.contracts import ResearchTriage
from baibai_engine.research.workspace import ResearchWorkspaceConflictError, prepare_workspace

pytest_plugins = ["tests.engine.test_position_review_v3"]


def ledger_rows(db):
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT payload FROM ledger_event ORDER BY append_seq").fetchall()


def seed_research(db):
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id="research-triage-finite",
            as_of=NOW.date().isoformat(),
            published_at=(NOW - timedelta(hours=2)).isoformat(),
            entries=[
                research_entry(ticker, rank=index)
                for index, ticker in enumerate(("1234", "5678"), 1)
            ],
        )
    )
    with connect_rw(db) as connection:
        connection.execute(
            "INSERT INTO research_triage VALUES (?,?,?,?,?,?)",
            (
                triage.research_triage_id,
                triage.review_set_id,
                triage.run_revision_id,
                triage.as_of.isoformat(),
                triage.published_at.isoformat(),
                canonical_json(triage.model_dump(mode="json")),
            ),
        )
    for ticker in ("1234", "5678"):
        thesis, review = pair_payload(ticker=ticker)
        review["review_id"] = "review-" + ticker
        ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
            "thesis-" + ticker, thesis, review
        )
    return triage


def start(db, market, targets):
    selection = ResearchSelection(
        request="synthetic explicit user start",
        research_triage_id="research-triage-finite"
        if any(purpose == "new" for _, purpose in targets)
        else None,
        targets=tuple(
            ResearchTarget(ticker=ticker, purpose=purpose) for ticker, purpose in targets
        ),
    )
    service = OperationService(db, sqlite_path=market)
    operation = service.start(
        session_kind="capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        payload=OperationPayload(
            checkpoint="explicit start", artifacts=(selection.model_dump(mode="json"),)
        ),
    )
    return service, operation


def assessment(db, thesis_ids):
    raw = scaffold_capital_allocation(
        db_path=db,
        capital_allocation_assessment_id="capital-allocation-assessment-20260907-finite",
        as_of=NOW.date(),
        research_triage_id="research-triage-finite",
        thesis_ids=thesis_ids,
        published_at=NOW,
    )
    raw.update(
        headline="synthetic reviewed comparison",
        comparison="complete scope only",
        forgone="unfinished work is not economic rejection",
    )
    for item in raw["alternatives"]:
        item["rationale"] = "synthetic economic no-allocation"
    raw["review"]["reviewer_identity"] = "synthetic independent reviewer"
    draft = CapitalAllocationAssessment.model_validate(raw)
    return draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(draft)}
            )
        }
    )


def final_payload(operation, *, assessment_id=None):
    artifacts = operation.payload.artifacts
    if assessment_id is not None:
        artifacts = (*artifacts, {"kind": "capital_allocation_assessment", "ref": assessment_id})
    return operation.payload.model_copy(
        update={
            "artifacts": artifacts,
            "result": "L3 proposal completed or investigation ended",
            "next": "external human decision or new explicit request",
        }
    )


@pytest.mark.parametrize(
    "targets",
    [
        tuple((str(1000 + i), "new") for i in range(4)),
        (("1234", "new"), ("5678", "new"), ("9012", "new"), ("2331", "holding")),
        (("1234", "new"), ("1234", "holding")),
    ],
)
def test_selection_caps_combined_subjects_and_refuses_duplicate_purposes(targets):
    with pytest.raises(ValueError, match=r"targets|unique"):
        ResearchSelection(
            request="explicit",
            research_triage_id="triage",
            targets=tuple(ResearchTarget(ticker=t, purpose=p) for t, p in targets),
        )


def test_empty_selection_completes_without_fake_caa_or_confirmation(holding_case):
    db, market, _ = holding_case
    before = ledger_rows(db)
    service, operation = start(db, market, ())
    completed = service.complete(operation.operation_id, final_payload(operation), completed_at=NOW)
    assert completed.status == "completed"
    assert completed.payload.human_confirmation is None
    assert ledger_rows(db) == before
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("SELECT count(*) FROM capital_allocation_assessment").fetchone()[0]
            == 0
        )


def test_completed_a_and_unfinished_b_compare_only_a_with_fixed_coverage(holding_case):
    db, market, _ = holding_case
    seed_research(db)
    before = ledger_rows(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    with pytest.raises(ValueError, match="covered"):
        assessment(db, ["thesis-1234"])
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="source unavailable within this investigation",
        references=("https://example.invalid/disclosure",),
        now=NOW,
    )
    draft = assessment(db, ["thesis-1234"])
    assert [item.ticker for item in draft.alternatives] == ["1234"]
    assert len(draft.research_scope.targets) == 2
    assert [item.status for item in draft.research_scope.outcomes] == ["reviewed", "unfinished"]
    with pytest.raises(ValueError, match="completed new research"):
        assessment(db, ["thesis-1234", "thesis-5678"])
    published = CapitalAllocationAssessmentService(db, clock=lambda: NOW).publish(draft)
    operation = service.get(operation.operation_id)
    completed = service.complete(
        operation.operation_id,
        final_payload(operation, assessment_id=published.capital_allocation_assessment_id),
        completed_at=NOW,
    )
    assert completed.payload.human_confirmation is None
    assert ledger_rows(db) == before
    with pytest.raises(OperationConflictError, match="immutable"):
        service.record_thesis(operation.operation_id, "thesis-5678", now=NOW)


def test_all_unfinished_end_without_economic_rejection_or_fake_assessment(holding_case):
    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    for ticker in ("1234", "5678"):
        service.record_unfinished(
            operation.operation_id,
            ticker=ticker,
            reason="investigation stopped with insufficient evidence",
            references=("workspace:" + ticker,),
            now=NOW,
        )
    operation = service.get(operation.operation_id)
    with pytest.raises(ValueError, match="completed new research"):
        assessment(db, ["thesis-1234"])
    with pytest.raises(OperationCompletionError, match="fabricate"):
        service.complete(
            operation.operation_id,
            final_payload(operation, assessment_id="absent"),
            completed_at=NOW,
        )
    completed = service.complete(operation.operation_id, final_payload(operation), completed_at=NOW)
    assert all(item.status == "unfinished" for item in progress_binding(completed.payload).outcomes)
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("SELECT count(*) FROM capital_allocation_assessment").fetchone()[0]
            == 0
        )


def test_mixed_operation_retains_checked_holding_draft_and_later_adoption_rechecks(holding_case):
    db, market, _ = holding_case
    seed_research(db)
    before = ledger_rows(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new"), ("2331", "holding")))
    position = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = position.build(thesis_id="current", position_id="2331")
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(
                status="sufficient", reason="synthetic verified residual return"
            )
        }
    )
    captured = service.record_position_proposal(operation.operation_id, draft, now=NOW)
    assert (
        service.record_position_proposal(operation.operation_id, draft, now=NOW + timedelta(days=1))
        == captured
    )
    proposal = progress_binding(captured.payload).outcomes[0]
    assert proposal.draft.action == "hold"
    assert proposal.checked_at == NOW
    assert proposal.draft.quote == draft.quote
    assert proposal.draft.holding == draft.holding
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="limited evidence",
        references=("workspace:5678",),
        now=NOW,
    )
    published = CapitalAllocationAssessmentService(db, clock=lambda: NOW).publish(
        assessment(db, ["thesis-1234"])
    )
    assert {item.ticker for item in published.research_scope.targets} == {"1234", "5678"}
    operation = service.get(operation.operation_id)
    completed = service.complete(
        operation.operation_id,
        final_payload(operation, assessment_id=published.capital_allocation_assessment_id),
        completed_at=NOW + timedelta(days=1),
    )
    assert completed.payload.human_confirmation is None
    assert ledger_rows(db) == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM position_review").fetchone()[0] == 0
    with pytest.raises(ValueError, match="human confirmation"):
        position.publish(draft, confirmed=False)
    with pytest.raises(ValueError, match="formal judgment basis"):
        PositionReviewService(
            db, sqlite_path=market, clock=lambda: NOW + timedelta(days=1)
        ).publish(draft, confirmed=True)


def test_immutable_selection_and_missing_outcomes_prevent_silent_subject_drop(holding_case):
    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    with pytest.raises(OperationCompletionError, match="covered"):
        service.complete(operation.operation_id, final_payload(operation), completed_at=NOW)
    selection = selection_binding(operation.payload)
    changed = selection.model_copy(update={"targets": selection.targets[:1]})
    with pytest.raises(OperationConflictError, match="immutable"):
        service.checkpoint(
            operation.operation_id,
            operation.payload.model_copy(update={"artifacts": (changed.model_dump(mode="json"),)}),
        )
    assert service.get(operation.operation_id) == operation


def test_domain_inputs_are_read_only_and_do_not_require_independent_macro_report(
    holding_case, tmp_path, monkeypatch
):
    db, market, _ = holding_case
    before = db.read_bytes(), market.read_bytes()

    def no_network(*args, **kwargs):
        pytest.fail("research inputs attempted network I/O")

    monkeypatch.setattr(socket, "socket", no_network)
    snapshot = research_inputs(
        tickers=("1234", "2331"),
        as_of=NOW.date(),
        cutoff=NOW,
        market_path=market,
        runs_path=tmp_path / "absent-runs.sqlite",
        macro_path=tmp_path / "absent-macro.sqlite",
        app_path=db,
    )
    assert set(snapshot["securities"]) == {"1234", "2331"}
    assert snapshot["market_context"]["saved_macro_context"] is None
    assert snapshot["market_context"]["macro_reading"] is None
    assert (db.read_bytes(), market.read_bytes()) == before
    assert not (tmp_path / "absent-runs.sqlite").exists()
    assert not (tmp_path / "absent-macro.sqlite").exists()


def test_legacy_active_handoff_preserves_exact_subjects_and_recorded_confirmation(
    holding_case, tmp_path
):
    db, market, _ = holding_case
    triage = seed_research(db)
    identifier = "op-20260907-capital-allocation-1"
    legacy = OperationPayload(
        checkpoint="legacy active",
        artifacts=(
            {"kind": "research_triage", "ref": triage.research_triage_id, "research_set": ["1234"]},
        ),
        human_confirmation={"request": "confirm original set", "result": "1234"},
    )
    with connect_rw(db) as connection:
        connection.execute(
            "INSERT INTO operation_session VALUES (?,?,?,?,?,?,?,?)",
            (
                identifier,
                "capital-allocation",
                "active",
                NOW.date().isoformat(),
                None,
                (NOW - timedelta(hours=1)).isoformat(),
                None,
                legacy.model_dump_json(),
            ),
        )
    prepared = prepare_workspace(
        research_triage_id=triage.research_triage_id,
        db_path=db,
        workspace=tmp_path / "workspace",
        request="synthetic explicit bounded continuation",
        research_set=("1234",),
        started_at=NOW,
        sqlite_path=market,
        runs_path=tmp_path / "absent-runs",
        macro_path=tmp_path / "absent-macro",
    )
    assert prepared.operation_id == identifier
    operation = OperationService(db).get(identifier)
    assert operation.payload.human_confirmation is None
    assert any(
        item.get("kind") == "legacy_human_confirmation" for item in operation.payload.artifacts
    )
    assert [item.ticker for item in selection_binding(operation.payload).targets] == ["1234"]
    with pytest.raises(ResearchWorkspaceConflictError, match=r"different|exists"):
        prepare_workspace(
            research_triage_id=triage.research_triage_id,
            db_path=db,
            workspace=tmp_path / "other",
            request="synthetic continuation",
            research_set=("1234", "5678"),
            started_at=NOW,
        )


def test_analysis_asof_cannot_drift_on_combined_or_holding_workspace_reuse(holding_case, tmp_path):
    from baibai_engine.research.workspace import prepare_holding_workspace

    db, market, _ = holding_case
    workspace = tmp_path / "workspace"
    kwargs = dict(
        research_triage_id=None,
        db_path=db,
        workspace=workspace,
        request="synthetic explicit holding selection",
        holding_tickers=("2331",),
        started_at=NOW,
        sqlite_path=market,
        runs_path=tmp_path / "absent-runs",
        macro_path=tmp_path / "absent-macro",
    )
    prepared = prepare_workspace(asof=NOW.date(), **kwargs)
    operation = OperationService(db).get(prepared.operation_id)
    before = {path: path.read_bytes() for path in workspace.rglob("*.yaml")}
    with pytest.raises(ResearchWorkspaceConflictError, match="as_of"):
        prepare_workspace(asof=NOW.date() + timedelta(days=1), force=True, **kwargs)
    assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == before
    with pytest.raises(ResearchWorkspaceConflictError, match="as_of"):
        prepare_holding_workspace(
            asof=NOW.date() + timedelta(days=1),
            ticker="2331",
            db_path=db,
            workspace=workspace / "holdings" / "2331",
            started_at=NOW,
            force=True,
            sqlite_path=market,
        )
    assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == before
    resumed = prepare_workspace(asof=NOW.date(), force=True, **kwargs)
    assert resumed.operation_id == operation.operation_id
    assert OperationService(db).get(operation.operation_id) == operation
    # Hand editing both workspace dates is not another path to shifting the Operation date.
    import yaml

    from baibai_engine.research.workspace import compute_status

    for name in ("manifest.yaml", "research-workspace.yaml"):
        path = workspace / name
        value = yaml.safe_load(path.read_text())
        value["as_of"] = (NOW.date() + timedelta(days=1)).isoformat()
        path.write_text(yaml.safe_dump(value))
    with pytest.raises(ResearchWorkspaceConflictError, match="as_of"):
        compute_status(workspace, db_path=db, now=NOW)


@pytest.mark.parametrize("active_selection", [False, True])
def test_legacy_holding_manifest_is_readable_but_cannot_mutate_outside_operation(
    holding_case, tmp_path, active_selection
):
    import yaml

    from baibai_engine.research.triage.contracts import ResearchTriageEntry
    from baibai_engine.research.workspace import (
        compute_status,
        promote,
        scaffold_review,
        scaffold_thesis,
    )

    db, market, _ = holding_case
    if active_selection:
        triage = seed_research(db)
        triage = triage.model_copy(
            update={
                "entries": (
                    *triage.entries,
                    ResearchTriageEntry.model_validate(research_entry("9012", rank=3)),
                )
            }
        )
        with connect_rw(db) as connection:
            connection.execute(
                "UPDATE research_triage SET payload=? WHERE research_triage_id=?",
                (triage.model_dump_json(), triage.research_triage_id),
            )
        start(db, market, (("1234", "new"), ("5678", "new"), ("9012", "new")))
    workspace = tmp_path / "legacy-holding"
    (workspace / "2331").mkdir(parents=True)
    (workspace / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "purpose": "position_review",
                "holding_ticker": "2331",
                "as_of": NOW.date().isoformat(),
            }
        )
    )
    (workspace / "research-workspace.yaml").write_text(
        yaml.safe_dump(
            {
                "as_of": NOW.date().isoformat(),
                "research_set": ["2331"],
                "position_review_subject": [{"ticker": "2331"}],
            }
        )
    )
    thesis, review = pair_payload(ticker="2331")
    (workspace / "2331/thesis-draft.yaml").write_text(yaml.safe_dump(thesis))
    (workspace / "2331/2026-09-07-2331-decision-review.yaml").write_text(yaml.safe_dump(review))
    # Saved work remains inspectable; inspection is not permission to extend the fixed selection.
    assert compute_status(workspace, db_path=db, now=NOW)["cases"][0]["ticker"] == "2331"
    before = {path: path.read_bytes() for path in workspace.rglob("*.yaml")}
    db_before = db.read_bytes()
    for mutate in (
        lambda: scaffold_thesis(
            workspace=workspace,
            ticker="2331",
            sqlite_path=market,
            target_session=NOW.date(),
            retrieved_at=NOW,
            db_path=db,
            force=True,
        ),
        lambda: scaffold_review(workspace=workspace, ticker="2331", db_path=db, force=True),
        lambda: promote(
            workspace=workspace,
            ticker="2331",
            db_path=db,
            thesis_id="outside-selection",
            supersedes_id=None,
            now=NOW,
        ),
    ):
        with pytest.raises(ResearchWorkspaceConflictError, match="Operation ID"):
            mutate()
        assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == before
        assert db.read_bytes() == db_before


def test_alphanumeric_holding_prepares_thesis_and_verified_proposal_end_to_end(tmp_path):
    import yaml
    from tests.helpers.db_seed import seed_ledger
    from tests.helpers.ledger import load_portfolio_ledger
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.research.workspace import (
        prepare_holding_workspace,
        promote,
        scaffold_review,
        scaffold_thesis,
    )

    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    from pathlib import Path

    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    events = []
    for event in ledger.events:
        payload = event.model_dump(mode="json")
        if payload.get("ticker") == "2331":
            payload["ticker"] = "285A"
        events.append(type(event).model_validate(payload))
    ledger = ledger.model_copy(update={"events": tuple(events), "market_prices": ()})
    seed_ledger(db, ledger)
    seed_daily_bars(
        market, [("285A", "2026-06-03", 1000.0, 1.0), ("285A", "2026-09-04", 1000.0, 1.0)]
    )
    workspace = tmp_path / "holding"
    prepared = prepare_holding_workspace(
        asof=NOW.date(),
        db_path=db,
        ticker="285A",
        workspace=workspace,
        request="synthetic explicit alpha-code holding research",
        started_at=NOW,
        sqlite_path=market,
        runs_path=tmp_path / "absent-runs",
        macro_path=tmp_path / "absent-macro",
    )
    operation = OperationService(db).get(prepared.operation_id)
    assert operation.ticker == "285A"
    assert selection_binding(operation.payload).targets[0].ticker == "285A"
    scaffold_thesis(
        workspace=workspace,
        ticker="285A",
        sqlite_path=market,
        target_session=NOW.date(),
        retrieved_at=NOW,
        db_path=db,
    )
    thesis, review = pair_payload(ticker="285A")
    (workspace / "285A/thesis-draft.yaml").write_text(yaml.safe_dump(thesis))
    scaffold_review(workspace=workspace, ticker="285A", db_path=db)
    (workspace / "285A/2026-09-07-285A-decision-review.yaml").write_text(yaml.safe_dump(review))
    promoted = promote(
        workspace=workspace,
        ticker="285A",
        db_path=db,
        thesis_id="alpha-current",
        supersedes_id=None,
        now=NOW,
    )
    position = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = position.build(thesis_id=promoted.thesis_id, position_id="285A")
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(
                status="uncertain", reason="synthetic uncertain future return"
            )
        }
    )
    before = ledger_rows(db)
    captured = OperationService(db, sqlite_path=market).record_position_proposal(
        operation.operation_id, draft, now=NOW
    )
    assert progress_binding(captured.payload).outcomes[0].ticker == "285A"
    assert progress_binding(captured.payload).outcomes[0].draft.ticker == "285A"
    assert ledger_rows(db) == before
    completed = OperationService(db, sqlite_path=market).complete(
        operation.operation_id, final_payload(captured), completed_at=NOW
    )
    assert completed.status == "completed"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM position_review").fetchone()[0] == 0
