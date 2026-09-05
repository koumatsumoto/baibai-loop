"""Human Research Set → canonical Assessment → planning → Operation completion."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.fixed_now import FIXED_NOW

from baibai_engine.appdb.write import connect_rw
from baibai_engine.operation.models import OperationPayload, OperationSession
from baibai_engine.operation.service import (
    OperationCompletionError,
    OperationConflictError,
    OperationService,
)
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    CapitalAllocationConflictError,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    thesis_core_hash,
    thesis_review_hash,
)
from baibai_engine.research.workspace import plan_limit
from baibai_engine.research.workspace_cli import main as research_main
from baibai_engine.screening.sqlite_cache import open_connection

NOW = FIXED_NOW
TRIAGE = "research-triage-20260714-boundary"
ASSESSMENT = "capital-allocation-assessment-20260714-boundary"
THESIS = "thesis-20260714-2331-r1"
FIXTURES = Path(__file__).parents[1] / "fixtures/thesis"


@pytest.fixture
def db(app_method_root: Path) -> Path:
    path = app_method_root / "stores/application/baibai.sqlite"
    with connect_rw(path) as connection:
        connection.execute(
            "INSERT INTO research_triage VALUES (?, ?, ?, ?, ?, ?)",
            (
                TRIAGE,
                "review-set-boundary",
                "run-boundary",
                NOW.date().isoformat(),
                NOW.isoformat(),
                json.dumps(
                    {
                        "as_of": NOW.date().isoformat(),
                        "entries": [
                            {"ticker": ticker, "decision": "research"}
                            for ticker in ("2331", "4444", "5555")
                        ],
                    }
                ),
            ),
        )
    for ticker in ("4444", "5555"):
        thesis = yaml.safe_load(
            (FIXTURES / "2331-decision.yaml").read_text().replace("2331", ticker)
        )
        review = yaml.safe_load(
            (FIXTURES / "2331-decision-review.yaml").read_text().replace("2331", ticker)
        )
        core = thesis_core_hash(ThesisDocument.model_validate(thesis))
        thesis["human_evidence_override"]["thesis_sha256"] = core
        review["reviewed_thesis_sha256"] = core
        thesis["human_evidence_override"]["review_sha256"] = thesis_review_hash(
            ThesisReview.model_validate(review)
        )
        ResearchStoreService(path, clock=lambda: NOW).publish_thesis_with_review(
            f"thesis-20260714-{ticker}-r1", thesis, review
        )
    return path


def _start(db: Path, tickers: tuple[str, ...] = ("2331", "4444")) -> OperationSession:
    return OperationService(db).start(
        session_kind="capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        payload=OperationPayload(
            checkpoint="human confirmed",
            artifacts=(
                {
                    "kind": "research_triage",
                    "ref": TRIAGE,
                    "research_set": list(tickers),
                },
            ),
        ),
    )


def _draft(db: Path, tickers: tuple[str, ...] = ("2331", "4444")) -> dict:
    return scaffold_capital_allocation(
        db_path=db,
        capital_allocation_assessment_id=ASSESSMENT,
        as_of=NOW.date(),
        research_triage_id=TRIAGE,
        thesis_ids=[f"thesis-20260714-{ticker}-r1" for ticker in tickers],
        published_at=NOW,
    )


def _assessment(
    draft: dict, allocated: str | None = None, result: str = "no_allocation"
) -> CapitalAllocationAssessment:
    draft["result"] = "allocate" if allocated else result
    draft["headline"] = "比較した結果を確定する"
    draft["comparison"] = "候補を現金維持と比較した"
    draft["forgone"] = "非配分候補は次回決算で再評価する"
    draft["review"]["reviewer_identity"] = "independent-reviewer"
    for alternative in draft["alternatives"]:
        alternative["rationale"] = "要求利回りと永久損失を比較した"
        if alternative["ticker"] == allocated:
            alternative["disposition"] = "allocate"
            alternative["thesis_review_id"] = f"review-{allocated}-20260703"
    assessment = CapitalAllocationAssessment.model_validate(draft)
    return assessment.model_copy(
        update={
            "review": assessment.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(assessment)}
            )
        }
    )


def _final(operation: OperationSession, reference: str = ASSESSMENT) -> OperationPayload:
    return OperationPayload(
        checkpoint="final",
        artifacts=(
            *operation.payload.artifacts,
            {
                "kind": "capital_allocation_assessment",
                "ref": reference,
            },
        ),
        canonical_refs=(TRIAGE, reference),
        result="human deferred",
        next="next trigger",
        human_confirmation={"request": "confirm result", "result": "defer"},
    )


@pytest.mark.parametrize("tickers", [("2331",), ("2331", "5555"), ("2331", "2331", "4444")])
def test_scaffold_and_publish_reject_missing_extra_and_duplicate_cases(
    db: Path, tickers: tuple[str, ...]
) -> None:
    operation = _start(db)
    with pytest.raises(CapitalAllocationConflictError, match="exactly match"):
        _draft(db, tickers)
    draft = _draft(db)
    with connect_rw(db) as connection:
        rows = connection.execute("SELECT thesis_id, ticker, core_sha256 FROM thesis").fetchall()
    sources = {row["ticker"]: row for row in rows}
    draft["alternatives"] = [
        {
            "ticker": ticker,
            "thesis_id": sources[ticker]["thesis_id"],
            "thesis_core_sha256": sources[ticker]["core_sha256"],
            "thesis_review_id": None,
            "disposition": "decline",
            "rationale": "比較した結果見送る",
        }
        for ticker in tickers
    ]
    if len(tickers) != len(set(tickers)):
        with pytest.raises(ValueError, match="unique tickers"):
            _assessment(draft)
        return
    assessment = _assessment(draft)
    service = CapitalAllocationAssessmentService(db)
    for invoke in (service.check, service.publish):
        with pytest.raises(CapitalAllocationConflictError, match="exactly match"):
            invoke(assessment)
    assert OperationService(db).get(operation.operation_id) == operation
    with connect_rw(db) as connection:
        assert (
            connection.execute("SELECT count(*) FROM capital_allocation_assessment").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("mutation", ["triage", "position-review", "no-active", "older-cycle"])
def test_assessment_requires_the_active_research_operation(db: Path, mutation: str) -> None:
    operation = _start(db)
    draft = _draft(db)
    with connect_rw(db) as connection:
        if mutation == "triage":
            changed = operation.payload.model_dump(mode="json")
            changed["artifacts"][0]["ref"] = "another-triage"
            connection.execute("UPDATE operation_session SET payload=?", (json.dumps(changed),))
        elif mutation == "position-review":
            connection.execute("UPDATE operation_session SET session_kind='position-review'")
        elif mutation == "no-active":
            connection.execute(
                "UPDATE operation_session SET status='completed', completed_at=?",
                (NOW.isoformat(),),
            )
        else:
            connection.execute(
                "UPDATE operation_session SET started_at=?",
                ((NOW + timedelta(seconds=1)).isoformat(),),
            )
    service = CapitalAllocationAssessmentService(db)
    for invoke in (service.check, service.publish):
        with pytest.raises(CapitalAllocationConflictError):
            invoke(_assessment(draft))
    with pytest.raises(CapitalAllocationConflictError):
        _draft(db)


@pytest.mark.parametrize("result", ["allocate", "no_allocation", "defer"])
def test_full_set_publishes_completes_and_allows_next_cycle(db: Path, result: str) -> None:
    operation = _start(db)
    service = CapitalAllocationAssessmentService(db)
    assessment = _assessment(
        _draft(db, ("4444", "2331")), "2331" if result == "allocate" else None, result
    )
    service.check(assessment)
    service.publish(assessment)
    checkpoint = operation.payload.model_copy(update={"checkpoint": "assessment published"})
    OperationService(db).checkpoint(operation.operation_id, checkpoint)
    completed = OperationService(db).complete(
        operation.operation_id, _final(operation), completed_at=NOW
    )
    assert completed.status == "completed"
    assert service.publish(assessment) == assessment  # immutable idempotency after completion
    with pytest.raises(OperationConflictError, match="immutable"):
        OperationService(db).complete(operation.operation_id, _final(operation), completed_at=NOW)
    second = _start(db)
    assert second.operation_id != operation.operation_id


@pytest.mark.parametrize("replacement", [None, ["2331"], ["2331", "5555"], []])
def test_checkpoint_and_complete_cannot_replace_human_selection(
    db: Path, replacement: list[str] | None
) -> None:
    operation = _start(db)
    payload = _final(operation).model_dump(mode="json")
    if replacement is None:
        payload["artifacts"] = payload["artifacts"][1:]
    else:
        payload["artifacts"][0]["research_set"] = replacement
    changed = OperationPayload.model_validate(payload)
    service = OperationService(db)
    for invoke in (
        lambda: service.checkpoint(operation.operation_id, changed),
        lambda: service.complete(operation.operation_id, changed, completed_at=NOW),
    ):
        with pytest.raises(OperationConflictError):
            invoke()
        assert service.get(operation.operation_id) == operation


@pytest.mark.parametrize("reference", [None, "missing-assessment"])
def test_arbitrary_artifact_cannot_complete_research(db: Path, reference: str | None) -> None:
    operation = _start(db)
    final = _final(operation, reference or ASSESSMENT)
    if reference is None:
        final = final.model_copy(
            update={"artifacts": (*operation.payload.artifacts, {"kind": "note", "ref": "done"})}
        )
    with pytest.raises(OperationCompletionError):
        OperationService(db).complete(operation.operation_id, final, completed_at=NOW)
    assert OperationService(db).get(operation.operation_id) == operation


@pytest.mark.parametrize("mutation", ["triage", "subset", "older-cycle"])
def test_completion_rejects_canonical_assessment_from_another_research(
    db: Path, mutation: str
) -> None:
    operation = _start(db)
    assessment = _assessment(_draft(db))
    CapitalAllocationAssessmentService(db).publish(assessment)
    # Seed a different active cycle to test completion independently of publish validation.
    payload = operation.payload.model_dump(mode="json")
    if mutation == "triage":
        payload["artifacts"][0]["ref"] = "other-triage"
    elif mutation == "subset":
        payload["artifacts"][0]["research_set"] = ["2331"]
    with connect_rw(db) as connection:
        connection.execute(
            "UPDATE operation_session SET payload=?, started_at=?",
            (
                json.dumps(payload),
                (NOW + timedelta(seconds=1) if mutation == "older-cycle" else NOW).isoformat(),
            ),
        )
    current = OperationService(db).get(operation.operation_id)
    with pytest.raises(OperationCompletionError):
        OperationService(db).complete(
            operation.operation_id, _final(current), completed_at=NOW + timedelta(seconds=2)
        )
    assert OperationService(db).get(operation.operation_id) == current


def _plan(db: Path, market: Path, identifier: str = ASSESSMENT) -> dict:
    return plan_limit(
        capital_allocation_assessment_id=identifier,
        db_path=db,
        sqlite_path=market,
        target_session=NOW.date(),
        budget_min_yen=200000,
        budget_max_yen=300000,
        now=NOW,
    )


@pytest.mark.parametrize("result", ["no_allocation", "defer", "missing"])
def test_planning_rejects_non_allocated_or_unknown_assessment_before_market_read(
    db: Path, tmp_path: Path, result: str
) -> None:
    _start(db)
    if result != "missing":
        CapitalAllocationAssessmentService(db).publish(_assessment(_draft(db), result=result))
    with pytest.raises(CapitalAllocationConflictError, match=r"not an allocate|unavailable"):
        _plan(db, tmp_path / "absent-market.sqlite")


@pytest.mark.parametrize("allocated", ["2331", "4444"])
def test_planning_uses_only_canonical_allocated_thesis(
    db: Path, tmp_path: Path, allocated: str
) -> None:
    _start(db)
    CapitalAllocationAssessmentService(db).publish(_assessment(_draft(db), allocated))
    market = tmp_path / "market.sqlite"
    with open_connection(market) as connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, 1000, 1)",
            (allocated, (NOW.date() - timedelta(days=1)).isoformat()),
        )
    first = _plan(db, market)
    assert first["status"] == "planned_limit"
    assert first["ticker"] == allocated
    assert first["thesis_id"] == f"thesis-20260714-{allocated}-r1"
    assert first["decision_reference"] == ASSESSMENT
    # Editing a local thesis cannot choose a different ticker or change the calculation.
    local = tmp_path / "thesis.yaml"
    local.write_text("input_snapshot: {ticker: '5555'}\n")
    assert _plan(db, market) == first
    with pytest.raises(SystemExit) as error:
        research_main(
            [
                "plan-limit",
                "--capital-allocation-assessment-id",
                ASSESSMENT,
                "--db",
                str(db),
                "--sqlite-path",
                str(market),
                "--target-session",
                str(NOW.date()),
                "--thesis",
                str(local),
            ],
            now=NOW,
        )
    assert error.value.code == 2


def test_planning_cli_resolves_assessment_id_and_defers_missing_price(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(db)
    CapitalAllocationAssessmentService(db).publish(_assessment(_draft(db), "2331"))
    assert (
        research_main(
            [
                "plan-limit",
                "--capital-allocation-assessment-id",
                ASSESSMENT,
                "--db",
                str(db),
                "--sqlite-path",
                str(tmp_path / "missing.sqlite"),
                "--target-session",
                str(NOW.date()),
            ],
            now=NOW,
        )
        == 0
    )
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["decision_reference"] == ASSESSMENT
    assert output["defer_reasons"] == ["latest_business_day_close_missing"]


@pytest.mark.parametrize("broken", ["thesis-id", "core", "review-id", "other-review"])
def test_planning_rejects_broken_canonical_binding(db: Path, tmp_path: Path, broken: str) -> None:
    _start(db)
    draft = _assessment(_draft(db), "2331").payload()
    alternative = draft["alternatives"][0]
    if broken == "thesis-id":
        alternative["thesis_id"] = "missing-thesis"
    elif broken == "core":
        alternative["thesis_core_sha256"] = "f" * 64
    elif broken == "review-id":
        alternative["thesis_review_id"] = "missing-review"
    else:
        alternative["thesis_review_id"] = "review-4444-20260703"
    # A canonical row from another writer must not turn an unresolved binding into an order.
    with connect_rw(db) as connection:
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?, ?, ?, ?, ?, ?)",
            (
                ASSESSMENT,
                NOW.date().isoformat(),
                NOW.isoformat(),
                "allocate",
                TRIAGE,
                json.dumps(draft),
            ),
        )
    with pytest.raises(CapitalAllocationConflictError):
        _plan(db, tmp_path / "missing-market.sqlite")


def test_planning_defers_when_human_evidence_override_has_expired(db: Path, tmp_path: Path) -> None:
    _start(db)
    CapitalAllocationAssessmentService(db).publish(_assessment(_draft(db), "2331"))
    output = plan_limit(
        capital_allocation_assessment_id=ASSESSMENT,
        db_path=db,
        sqlite_path=tmp_path / "missing-market.sqlite",
        target_session=NOW.date(),
        budget_min_yen=200000,
        budget_max_yen=300000,
        now=NOW + timedelta(days=30),
    )
    assert output["status"] == "defer"
    assert "thesis_not_decision_ready" in output["defer_reasons"]
