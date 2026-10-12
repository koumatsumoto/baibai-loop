from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.position.cli import main
from baibai_engine.position.drafts import apply_draft, build_broker_fact_draft, load_draft
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.capital_allocation_query import historical_allocated_ticker
from baibai_engine.storage.application.write import initialize_database

ROOT = Path(__file__).parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
ORDERED_AT = datetime.fromisoformat("2026-07-30T09:00:00+09:00")
EXPIRES_AT = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
ASSESSMENT_ID = "capital-allocation-assessment-20260730-2331"


def _services(
    tmp_path: Path, *, result: str = "allocate"
) -> tuple[LedgerStoreService, Callable[[str], str]]:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    payload = {
        "schema_version": 1,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": ASSESSMENT_ID,
        "as_of": "2026-07-30",
        "published_at": "2026-07-30T08:30:00+09:00",
        "result": result,
        "headline": "2331 へ配分" if result == "allocate" else "配分しない",
        "research_triage_id": "research_triage-20260730-test",
        "macro_context_id": None,
        "comparison": "要求利回りと永久損失を比較した",
        "forgone": "次回決算で再評価する",
        "alternatives": [
            {
                "ticker": "2331",
                "thesis_id": "thesis-20260730-2331-r1",
                "thesis_core_sha256": "a" * 64,
                "thesis_review_id": ("review-20260730-2331-r1" if result == "allocate" else None),
                "disposition": "allocate" if result == "allocate" else "decline",
                "rationale": "要求利回りと永久損失を比較した",
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "independent-reviewer",
            "reviewed_at": "2026-07-30T08:20:00+09:00",
            "conclusion": "pass",
            "draft_sha256": "b" * 64,
            "open_findings": [],
        },
    }
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO research_triage VALUES (?, ?, ?, ?, ?, ?)",
            (
                "research_triage-20260730-test",
                "review-set-test",
                "run-test",
                "2026-07-30",
                "2026-07-30T08:00:00+09:00",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO capital_allocation_assessment VALUES (?, ?, ?, ?, ?, ?)",
            (
                ASSESSMENT_ID,
                "2026-07-30",
                "2026-07-30T08:30:00+09:00",
                result,
                "research_triage-20260730-test",
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    original = load_portfolio_ledger(LEDGER)
    source = original.model_copy(
        update={
            "as_of": ORDERED_AT,
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in original.market_prices
            ),
        }
    )
    seed_ledger(db, source)
    return LedgerStoreService(db), partial(historical_allocated_ticker, db_path=db)


def _open_draft(
    ledger: LedgerStoreService,
    assessments: Callable[[str], str],
    *,
    expires_at: datetime = EXPIRES_AT,
):
    return build_broker_fact_draft(
        ledger,
        assessments,
        decision_reference=ASSESSMENT_ID,
        status="open",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1050"),
        expires_at=expires_at,
        now=ORDERED_AT + timedelta(minutes=1),
    )


def test_allocated_assessment_builds_and_persists_bound_open_result(tmp_path: Path) -> None:
    ledger, assessments = _services(tmp_path)
    draft, event_ids = _open_draft(ledger, assessments)

    assert draft is not None
    apply_draft(ledger, draft, human_confirmed=True)
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        row = connection.execute(
            "SELECT decision_reference FROM ledger_event WHERE event_id = ?", event_ids
        ).fetchone()
    assert row == (ASSESSMENT_ID,)


def test_non_allocation_assessment_and_ticker_mismatch_are_rejected(tmp_path: Path) -> None:
    ledger, assessments = _services(tmp_path, result="no_allocation")
    with pytest.raises(ValueError, match="not an allocate decision"):
        _open_draft(ledger, assessments)

    ledger, assessments = _services(tmp_path / "other")
    with pytest.raises(ValueError, match="ticker does not match"):
        build_broker_fact_draft(
            ledger,
            assessments,
            decision_reference=ASSESSMENT_ID,
            status="open",
            occurred_at=ORDERED_AT,
            ticker="9999",
            quantity=100,
            sector="サービス業",
            price_guard_yen=Decimal("1050"),
            expires_at=EXPIRES_AT,
            now=ORDERED_AT + timedelta(minutes=1),
        )


def test_open_fill_cancel_and_idempotency_follow_active_reservation(tmp_path: Path) -> None:
    ledger, assessments = _services(tmp_path)
    open_draft, _ = _open_draft(ledger, assessments)
    assert open_draft is not None
    apply_draft(ledger, open_draft, human_confirmed=True)
    reservation = open_draft.replacement.events[-1]

    fill, fill_ids = build_broker_fact_draft(
        ledger,
        assessments,
        decision_reference=ASSESSMENT_ID,
        status="filled",
        occurred_at=ORDERED_AT + timedelta(hours=1),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1000"),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=2),
    )
    assert fill is not None
    apply_draft(ledger, fill, human_confirmed=True)
    repeated, repeated_ids = build_broker_fact_draft(
        ledger,
        assessments,
        decision_reference=ASSESSMENT_ID,
        status="filled",
        occurred_at=ORDERED_AT + timedelta(hours=1),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1000"),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=2),
    )
    assert repeated is None
    assert repeated_ids == ()
    assert fill_ids

    cancelled, _ = build_broker_fact_draft(
        ledger,
        assessments,
        decision_reference=ASSESSMENT_ID,
        status="cancelled",
        occurred_at=ORDERED_AT + timedelta(hours=3),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=4),
    )
    assert cancelled is not None


def test_record_broker_fact_cli_uses_decision_reference_without_writing_before_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _services(tmp_path)
    args = [
        "broker-fact-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(tmp_path / "app.sqlite"),
        "--decision-reference",
        ASSESSMENT_ID,
        "--status",
        "open",
        "--occurred-at",
        ORDERED_AT.isoformat(),
        "--ticker",
        "2331",
        "--quantity",
        "100",
        "--sector",
        "サービス業",
        "--price-guard-yen",
        "1050",
        "--expires-at",
        EXPIRES_AT.isoformat(),
        "--out",
        "draft.yaml",
    ]

    assert main(args, now=ORDERED_AT + timedelta(minutes=1)) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["decision_reference"] == ASSESSMENT_ID
    assert ledger.append_head() == load_draft(tmp_path / "draft.yaml").expected_head


def test_unbound_reservation_is_not_a_runtime_compatibility_path(tmp_path: Path) -> None:
    ledger, assessments = _services(tmp_path)
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    with pytest.raises(ValueError, match="no canonical decision binding"):
        build_broker_fact_draft(
            ledger,
            assessments,
            decision_reference=ASSESSMENT_ID,
            status="expired",
            occurred_at=expiry,
            reservation_id="reservation-8929-pending",
            now=expiry + timedelta(days=1),
        )


def test_broker_draft_keeps_the_snapshot_head_when_another_write_intervenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, assessments = _services(tmp_path)
    intervening, _ = _open_draft(ledger, assessments)
    assert intervening is not None
    load_snapshot = ledger.load_with_head

    def load_then_write():
        snapshot = load_snapshot()
        apply_draft(ledger, intervening, human_confirmed=True)
        return snapshot

    monkeypatch.setattr(ledger, "load_with_head", load_then_write)
    draft, _ = _open_draft(ledger, assessments)
    monkeypatch.setattr(ledger, "load_with_head", load_snapshot)
    assert draft is not None
    assert draft.expected_head == intervening.expected_head
    assert draft.source == intervening.source
    with pytest.raises(ValueError, match="stale ledger draft"):
        apply_draft(ledger, draft, human_confirmed=True)


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_active_and_known_terminal_retries_do_not_resolve_historical_caa(
    tmp_path: Path, monkeypatch, capsys, terminal_status: str
) -> None:
    ledger, resolve = _services(tmp_path)
    references: list[str] = []

    def resolve_initial(reference: str) -> str:
        references.append(reference)
        return resolve(reference)

    draft, _ = _open_draft(ledger, resolve_initial, expires_at=ORDERED_AT + timedelta(hours=2))
    assert draft is not None
    assert references == [ASSESSMENT_ID]
    apply_draft(ledger, draft, human_confirmed=True)
    reservation = draft.replacement.events[-1]

    def unavailable_history(_reference: str, **_kwargs) -> str:
        pytest.fail("active or known terminal report resolved historical CAA")

    db = tmp_path / "app.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM capital_allocation_assessment")
    before = db.read_bytes()
    fill_args = dict(
        decision_reference=ASSESSMENT_ID,
        status="filled",
        occurred_at=ORDERED_AT + timedelta(minutes=30),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1000"),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=1),
    )
    fill, _ = build_broker_fact_draft(ledger, unavailable_history, **fill_args)
    assert fill is not None
    assert db.read_bytes() == before
    apply_draft(ledger, fill, human_confirmed=True)
    assert build_broker_fact_draft(ledger, unavailable_history, **fill_args) == (None, ())
    terminal_args = dict(
        decision_reference=ASSESSMENT_ID,
        status=terminal_status,
        occurred_at=ORDERED_AT + timedelta(hours=1 if terminal_status == "cancelled" else 3),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=4),
    )
    terminal, _ = build_broker_fact_draft(ledger, unavailable_history, **terminal_args)
    assert terminal is not None
    apply_draft(ledger, terminal, human_confirmed=True)
    assert build_broker_fact_draft(ledger, unavailable_history, **terminal_args) == (None, ())

    # CLIもcallableを渡すだけにし、予約済み再試行の前にCAAを先読みしない。
    monkeypatch.setattr(
        "baibai_engine.position.cli.historical_allocated_ticker", unavailable_history
    )
    before = db.read_bytes()
    assert (
        main(
            [
                "broker-fact-draft",
                "--root",
                str(tmp_path),
                "--db",
                str(db),
                "--decision-reference",
                ASSESSMENT_ID,
                "--status",
                terminal_status,
                "--occurred-at",
                terminal_args["occurred_at"].isoformat(),
                "--reservation-id",
                reservation.reservation_id,
                "--out",
                "retry.yaml",
            ],
            now=ORDERED_AT + timedelta(hours=4),
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"
    assert not (tmp_path / "retry.yaml").exists()
    assert db.read_bytes() == before


def test_historical_allocation_query_does_not_write_or_replay_current_eligibility(
    tmp_path: Path, monkeypatch
) -> None:
    _services(tmp_path)
    db = tmp_path / "app.sqlite"
    before = db.read_bytes()

    def reject_current_check(*_args, **_kwargs):
        pytest.fail("historical identity entered current allocation publication check")

    monkeypatch.setattr(
        "baibai_engine.research.capital_allocation_service.CapitalAllocationAssessmentService.check",
        reject_current_check,
    )
    assert historical_allocated_ticker(ASSESSMENT_ID, db_path=db) == "2331"
    assert db.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="application database is unavailable"):
        historical_allocated_ticker(ASSESSMENT_ID, db_path=missing)
    assert not missing.exists()
