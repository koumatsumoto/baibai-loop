"""固定対象、部分完了、L3完了と確認済み公開の境界を通す。"""

import socket
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from tests.engine.test_position_review_v3 import NOW
from tests.helpers.research_selection import selection as make_selection
from tests.helpers.research_v4 import pair_payload

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.inputs import collect_research_inputs, project_research_inputs
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.progress import (
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

pytest_plugins = ["tests.engine.test_position_review_v3"]


def ledger_rows(db):
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT payload FROM ledger_event ORDER BY append_seq").fetchall()


def seed_research(db):
    for ticker in ("1234", "5678"):
        thesis, review = pair_payload(ticker=ticker)
        review["review_id"] = "review-" + ticker
        ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
            "thesis-" + ticker, thesis, review
        )


def start(db, market, targets):
    scope = "review_positions" if targets and targets[0][1] == "holding" else "discover_new"
    value = make_selection(now=NOW, tickers=tuple(t for t, _ in targets), scope=scope)
    service = OperationService(db, sqlite_path=market)
    operation = service.start(
        session_kind="position-review" if scope == "review_positions" else "capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        ticker=targets[0][0] if scope == "review_positions" and len(targets) == 1 else None,
        payload=OperationPayload(
            checkpoint="explicit start", artifacts=(value.model_dump(mode="json"),)
        ),
    )
    return service, operation


def assessment(db, thesis_ids):
    raw = scaffold_capital_allocation(
        db_path=db,
        capital_allocation_assessment_id="capital-allocation-assessment-20260907-finite",
        as_of=NOW.date(),
        operation_id=OperationService(db).active().operation_id,
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


@pytest.mark.parametrize(
    "clock_time",
    [
        datetime.fromisoformat("2026-09-08T08:30:00+09:00"),
        datetime.fromisoformat("2026-09-07T23:30:00+00:00"),
    ],
)
def test_caa_today_uses_japanese_business_day_for_equivalent_instants(holding_case, clock_time):
    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"),))
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    draft = assessment(db, ["thesis-1234"]).model_copy(
        update={
            "as_of": date(2026, 9, 8),
            "published_at": datetime.fromisoformat("2026-09-08T08:15:00+09:00"),
        }
    )
    draft = draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(draft)}
            )
        }
    )
    publisher = CapitalAllocationAssessmentService(db, clock=lambda: clock_time)
    publisher.check(draft)
    assert publisher.publish(draft).published_at == clock_time


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
    assert [item.thesis_id for item in assessment(db, []).alternatives] == ["thesis-1234"]
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


def test_holding_operation_retains_checked_draft_and_later_adoption_rechecks(holding_case):
    db, market, _ = holding_case
    seed_research(db)
    before = ledger_rows(db)
    service, operation = start(db, market, (("2331", "holding"),))
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
    operation = service.get(operation.operation_id)
    completed = service.complete(
        operation.operation_id, final_payload(operation), completed_at=NOW + timedelta(days=1)
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
    changed = selection.model_copy(update={"selection_summary": "a different fixed selection"})
    with pytest.raises(OperationConflictError, match="immutable"):
        service.checkpoint(
            operation.operation_id,
            operation.payload.model_copy(update={"artifacts": (changed.model_dump(mode="json"),)}),
        )
    assert service.get(operation.operation_id) == operation


def test_domain_inputs_are_read_only_and_do_not_require_independent_historical_report(
    holding_case, tmp_path, monkeypatch
):
    db, market, _ = holding_case
    before = db.read_bytes(), market.read_bytes()

    def no_network(*args, **kwargs):
        pytest.fail("research inputs attempted network I/O")

    monkeypatch.setattr(socket, "socket", no_network)
    selected = make_selection(now=NOW, tickers=("1234", "2331"))
    inputs = collect_research_inputs(
        snapshot=selected.inputs.analysis,
        input_gaps=selected.inputs.input_gaps,
        tickers=("1234", "2331"),
        research_as_of=NOW.date(),
        input_known_at=NOW,
        market_path=market,
        indicators_path=tmp_path / "absent-indicators.sqlite",
        app_path=db,
    )
    snapshot = project_research_inputs(selected.model_copy(update={"inputs": inputs}))
    assert set(snapshot["securities"]) == {"1234", "2331"}
    assert snapshot["market_context"]["saved_market_context_assessment"] is None
    assert snapshot["market_context"]["reading"] is None
    assert (db.read_bytes(), market.read_bytes()) == before
    assert not (tmp_path / "absent-runs.sqlite").exists()
    assert not (tmp_path / "absent-indicators.sqlite").exists()


@pytest.mark.parametrize("mutation", ["old_namespace", "hash", "review", "reason"])
def test_planning_scope_matches_current_operation_and_exact_outcomes(holding_case, mutation):
    from baibai_engine.research.operation.progress import (
        comparison_scope,
    )
    from baibai_engine.research.operation.research_binding import require_planning_research_scope
    from baibai_engine.storage.application.read import connect_read_only

    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="missing primary fact",
        references=("synthetic-source",),
        now=NOW,
    )
    canonical = service.get(operation.operation_id)
    scope = comparison_scope(canonical.operation_id, canonical.payload)
    alternatives = (
        (
            "1234",
            "thesis-1234",
            scope.outcomes[0].thesis_core_sha256,
            scope.outcomes[0].thesis_review_id,
        ),
    )
    with connect_read_only(db) as reader:
        assert (
            require_planning_research_scope(
                reader, scope=scope, alternatives=alternatives
            ).request.scope
            == "discover_new"
        )
        if mutation == "old_namespace":
            scope = scope.model_copy(update={"operation_id": "op-20260907-capital-allocation-1"})
        if mutation in {"hash", "review"}:
            values = list(alternatives[0])
            values[2 if mutation == "hash" else 3] = "0" * 64
            alternatives = (tuple(values),)
        if mutation == "reason":
            scope = scope.model_copy(
                update={
                    "outcomes": (
                        scope.outcomes[0],
                        scope.outcomes[1].model_copy(update={"reason": "changed reason"}),
                    )
                }
            )
        with pytest.raises(ValueError, match=r"planning unavailable|alternatives differ"):
            require_planning_research_scope(reader, scope=scope, alternatives=alternatives)


def test_saved_caa_scope_does_not_change_when_unfinished_target_completes(holding_case):
    from baibai_engine.research.operation.research_binding import require_planning_research_scope
    from baibai_engine.storage.application.read import connect_read_only

    db, market, _ = holding_case
    seed_research(db)
    service, operation = start(db, market, (("1234", "new"), ("5678", "new")))
    service.record_thesis(operation.operation_id, "thesis-1234", now=NOW)
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="missing primary fact",
        references=("synthetic",),
        now=NOW,
    )
    published = CapitalAllocationAssessmentService(db, clock=lambda: NOW).publish(
        assessment(db, [])
    )
    with sqlite3.connect(db) as reader:
        before = reader.execute("SELECT payload FROM capital_allocation_assessment").fetchone()[0]
    service.record_thesis(operation.operation_id, "thesis-5678", now=NOW)
    with connect_read_only(db) as reader:
        assert (
            reader.execute("SELECT payload FROM capital_allocation_assessment").fetchone()[0]
            == before
        )
        with pytest.raises(ValueError, match="scope changed"):
            require_planning_research_scope(
                reader,
                scope=published.research_scope,
                alternatives=tuple(
                    (item.ticker, item.thesis_id, item.thesis_core_sha256, item.thesis_review_id)
                    for item in published.alternatives
                ),
            )
