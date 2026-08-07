from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import baibai_engine.proposals.store as proposal_store_module
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    reconcile_portfolio,
)
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.cli import main as proposal_main
from baibai_engine.proposals.store import (
    PlannedLimitInput,
    ProposalConflictError,
    ProposalRecord,
    ProposalStoreService,
    ProposalValidationError,
)
from baibai_engine.research.opportunity import plan_limit
from baibai_engine.research.opportunity_cli import main as research_main
from baibai_engine.research.store import ResearchStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    ThesisIdentity,
    ThesisResult,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW

ROOT = Path(__file__).parents[1]
THESIS = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
THESIS_ID = "thesis-20260711-2331-r1"
CREATED_AT = datetime.fromisoformat("2026-07-11T10:02:00+09:00")


def _raw(path: Path) -> dict[str, object]:
    parsed = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return copy.deepcopy(parsed)


def _database(path: Path, *, with_review: bool = True) -> None:
    service = ResearchStoreService(path, clock=lambda: FIXED_NOW)
    if with_review:
        service.publish_thesis_with_review(THESIS_ID, _raw(THESIS), _raw(REVIEW))
    else:
        thesis = _raw(THESIS)
        thesis["judgment"]["recommendation"] = "defer"  # type: ignore[index]
        thesis["judgment"]["sizing_action"] = "none"  # type: ignore[index]
        service.publish_thesis(THESIS_ID, thesis)
    ledger = PortfolioLedgerDocument.model_validate(_raw(LEDGER))
    seed_ledger(
        path,
        ledger.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in ledger.market_prices
                )
            }
        ),
    )


def _planned(path: Path) -> PlannedLimitInput:
    market = path.with_name("market.sqlite")
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL)"
        )
        connection.execute("DELETE FROM jquants_daily_bars")
        connection.execute("INSERT INTO jquants_daily_bars VALUES ('2331', '2026-07-10', 1000, 1)")
    return PlannedLimitInput.model_validate(
        plan_limit(
            thesis=THESIS,
            db_path=path,
            sqlite_path=market,
            target_session=date(2026, 7, 13),
            budget_min_yen=200_000,
            budget_max_yen=300_000,
            now=CREATED_AT,
        )
    )


def _snapshot() -> PortfolioSnapshot:
    return reconcile_portfolio(PortfolioLedgerDocument.model_validate(_raw(LEDGER)))


def _current_snapshot(path: Path) -> PortfolioSnapshot:
    return reconcile_portfolio(LedgerStoreService(path).load())


def _service(path: Path) -> ProposalStoreService:
    return ProposalStoreService(
        path,
        market_db_path=path.with_name("market.sqlite"),
        clock=lambda: CREATED_AT + timedelta(hours=1),
    )


def _create(service: ProposalStoreService, path: Path) -> ProposalRecord:
    return service.create(
        THESIS_ID,
        _planned(path),
        _current_snapshot(path),
        snapshot_append_head=LedgerStoreService(path).append_head(),
        created_at=CREATED_AT,
    )


def test_create_uses_db_thesis_review_and_current_planning_limit(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    proposal = _create(_service(path), path)

    assert proposal.proposal_id == "prop-20260711-2331-1"
    assert proposal.status == "pending"
    assert proposal.thesis_id == THESIS_ID
    assert proposal.review_id == "review-2331-20260703"
    stored_input = proposal.payload["planned_limit"]
    assert isinstance(stored_input, dict)
    assert stored_input["source_ledger_append_head"] == LedgerStoreService(path).append_head()
    assert "source_ref" not in stored_input
    assert "thesis_sha256" not in stored_input
    assert "independent_review_sha256" not in stored_input
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    assert generated["orders"]


def test_create_and_decide_use_operation_clock_not_record_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    operation_now = CREATED_AT + timedelta(minutes=5)
    clock_reads: list[datetime] = []
    validation_instants: list[datetime | None] = []
    real_evaluate = proposal_store_module.evaluate_thesis

    def clock() -> datetime:
        clock_reads.append(operation_now)
        return operation_now

    def recording_evaluate(
        document: ThesisDocument,
        *,
        review: IndependentReview | None = None,
        now: datetime | None = None,
        identity: ThesisIdentity,
    ) -> ThesisResult:
        validation_instants.append(now)
        return real_evaluate(document, review=review, now=now, identity=identity)

    monkeypatch.setattr(proposal_store_module, "evaluate_thesis", recording_evaluate)
    service = ProposalStoreService(
        path,
        market_db_path=path.with_name("market.sqlite"),
        clock=clock,
    )

    proposal = _create(service, path)
    service.decide(
        proposal.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_current_snapshot(path),
        snapshot_append_head=LedgerStoreService(path).append_head(),
    )

    assert clock_reads == [operation_now, operation_now]
    assert validation_instants
    assert set(validation_instants) == {operation_now}


def test_proposal_write_rejects_naive_operation_clock_before_insert(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(
        path,
        market_db_path=path.with_name("market.sqlite"),
        clock=lambda: CREATED_AT.replace(tzinfo=None),
    )

    with pytest.raises(ProposalValidationError, match="timezone"):
        _create(service, path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_proposal_cli_rejects_naive_clock_before_database_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "must-not-exist.sqlite"

    assert (
        proposal_main(
            [
                "--db",
                str(path),
                "create",
                "--thesis-id",
                THESIS_ID,
                "--input",
                str(tmp_path / "unused.yaml"),
            ],
            now=CREATED_AT.replace(tzinfo=None),
        )
        == 1
    )

    assert "timezone-aware" in capsys.readouterr().err
    assert not path.exists()


def test_proposal_write_rejects_future_event_timestamp_before_insert(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(
        path,
        market_db_path=path.with_name("market.sqlite"),
        clock=lambda: CREATED_AT,
    )

    with pytest.raises(ProposalValidationError, match="after the operation clock"):
        service.create(
            THESIS_ID,
            _planned(path),
            _current_snapshot(path),
            snapshot_append_head=LedgerStoreService(path).append_head(),
            created_at=CREATED_AT + timedelta(seconds=1),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("operation_now", "decided_at"),
    [
        (CREATED_AT + timedelta(minutes=5), CREATED_AT + timedelta(minutes=5, seconds=1)),
        (CREATED_AT.replace(tzinfo=None), CREATED_AT + timedelta(minutes=1)),
    ],
)
def test_proposal_decide_rejects_invalid_clock_without_updating_pending_row(
    tmp_path: Path,
    operation_now: datetime,
    decided_at: datetime,
) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    proposal = _create(_service(path), path)
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT status, decided_at, payload FROM proposal WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()

    service = ProposalStoreService(
        path,
        market_db_path=path.with_name("market.sqlite"),
        clock=lambda: operation_now,
    )
    with pytest.raises(ProposalValidationError, match=r"timezone|operation clock"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=decided_at,
            snapshot=_current_snapshot(path),
            snapshot_append_head=LedgerStoreService(path).append_head(),
        )

    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT status, decided_at, payload FROM proposal WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
    assert before == after


def test_internal_ids_are_allocated_without_collision_under_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    first = _create(service, path)
    second = _create(service, path)

    assert first.proposal_id == "prop-20260711-2331-1"
    assert second.proposal_id == "prop-20260711-2331-2"


def test_create_requires_one_matching_ready_buy_review_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path, with_review=False)

    with pytest.raises(ProposalValidationError, match="exactly one review"):
        _service(path).create(
            THESIS_ID,
            _planned(path),
            _snapshot(),
            snapshot_append_head=LedgerStoreService(path).append_head(),
            created_at=CREATED_AT,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_create_rejects_caller_controlled_market_database_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    planned = _planned(path).model_copy(
        update={"source_ref": f"{tmp_path / 'crafted.sqlite'}:jquants_daily_bars"}
    )

    with pytest.raises(ProposalConflictError, match="configured market DB"):
        _service(path).create(
            THESIS_ID,
            planned,
            _current_snapshot(path),
            snapshot_append_head=LedgerStoreService(path).append_head(),
            created_at=CREATED_AT,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_human_decision_can_move_between_non_approved_current_states(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)

    deferred = service.decide(
        proposal.proposal_id,
        "defer",
        decided_at=CREATED_AT + timedelta(minutes=1),
    )
    rejected = service.decide(
        proposal.proposal_id,
        "reject",
        decided_at=CREATED_AT + timedelta(minutes=2),
    )

    assert deferred.status == "deferred"
    assert rejected.status == "rejected"
    assert rejected.decided_at == CREATED_AT + timedelta(minutes=2)


def test_decision_time_cannot_move_backwards(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)
    service.decide(
        proposal.proposal_id,
        "defer",
        decided_at=CREATED_AT + timedelta(minutes=2),
    )

    with pytest.raises(ProposalValidationError, match="cannot predate"):
        service.decide(
            proposal.proposal_id,
            "reject",
            decided_at=CREATED_AT + timedelta(minutes=1),
        )

    current = service.get(proposal.proposal_id)
    assert current.status == "deferred"
    assert current.decided_at == CREATED_AT + timedelta(minutes=2)


def test_approve_recalculates_and_accepts_an_unchanged_current_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)

    approved = service.decide(
        proposal.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_current_snapshot(path),
        snapshot_append_head=LedgerStoreService(path).append_head(),
    )

    assert approved.status == "approved"


def test_approve_rejects_planning_limit_drift_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)
    current_snapshot = _current_snapshot(path)
    changed = replace(
        current_snapshot, available_cash_yen=current_snapshot.available_cash_yen - 100_000
    )

    with pytest.raises(ProposalConflictError, match="create a new proposal"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=1),
            snapshot=changed,
            snapshot_append_head=LedgerStoreService(path).append_head(),
        )

    assert service.get(proposal.proposal_id).status == "pending"


def test_approve_rejects_changed_market_close_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)
    with sqlite3.connect(tmp_path / "market.sqlite") as connection:
        connection.execute("UPDATE jquants_daily_bars SET close = 999 WHERE ticker = '2331'")

    with pytest.raises(ProposalConflictError, match="planning price differs"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=1),
            snapshot=_current_snapshot(path),
            snapshot_append_head=LedgerStoreService(path).append_head(),
        )

    assert service.get(proposal.proposal_id).status == "pending"


def test_approve_requires_current_input_and_snapshot_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    proposal = _create(service, path)

    with pytest.raises(ProposalValidationError, match="current DB-derived"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=1),
        )
    with pytest.raises(ProposalValidationError, match="current"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=10),
            snapshot=_current_snapshot(path),
        )

    assert service.get(proposal.proposal_id).status == "pending"


def test_ledger_reference_locks_decision_and_approved_is_not_redecidable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = _service(path)
    first = _create(service, path)
    second = _create(service, path)
    service.decide(
        first.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_current_snapshot(path),
        snapshot_append_head=LedgerStoreService(path).append_head(),
    )
    service.decide(
        second.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_current_snapshot(path),
        snapshot_append_head=LedgerStoreService(path).append_head(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO ledger_event(
                event_id, occurred_at, same_instant_order, event_type,
                proposal_id, payload
            ) VALUES ('event-1', ?, 0, 'contribution', ?, '{}')
            """,
            ((CREATED_AT + timedelta(minutes=1)).isoformat(), first.proposal_id),
        )

    with pytest.raises(ProposalConflictError, match="ledger event"):
        service.decide(
            first.proposal_id,
            "reject",
            decided_at=CREATED_AT + timedelta(minutes=2),
        )
    assert service.get(first.proposal_id).status == "approved"

    with pytest.raises(ProposalConflictError, match="cannot be re-decided"):
        service.decide(
            second.proposal_id,
            "defer",
            decided_at=CREATED_AT + timedelta(minutes=2),
        )


def test_plan_limit_output_creates_proposal_through_public_clis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    _database(db)
    market = tmp_path / "market.sqlite"
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL)"
        )
        connection.execute("INSERT INTO jquants_daily_bars VALUES ('2331', '2026-07-10', 1000, 1)")
    output = tmp_path / "planned-limit.yaml"

    assert (
        research_main(
            [
                "plan-limit",
                "--thesis",
                str(THESIS),
                "--db",
                str(db),
                "--sqlite-path",
                str(market),
                "--target-session",
                "2026-07-13",
                "--output",
                str(output),
            ],
            now=CREATED_AT,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        proposal_main(
            [
                "--db",
                str(db),
                "--market-db",
                str(market),
                "create",
                "--thesis-id",
                THESIS_ID,
                "--input",
                str(output),
            ],
            now=CREATED_AT,
        )
        == 0
    )
    created = safe_load(capsys.readouterr().out)
    assert isinstance(created, dict)
    assert created["status"] == "pending"
    assert created["payload"]["planned_limit"]["source_ledger_append_head"] == 11
