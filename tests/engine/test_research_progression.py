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
from baibai_engine.foundation.research_triage import ResearchTriage
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
