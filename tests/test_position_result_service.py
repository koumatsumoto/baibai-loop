from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.position.cli import main
from baibai_engine.position.drafts import apply_draft, load_draft
from baibai_engine.position.ledger import load_portfolio_ledger, reconcile_portfolio
from baibai_engine.position.result_service import build_result_draft
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.store import PlannedLimitInput, ProposalStoreService
from baibai_engine.research.opportunity import plan_limit
from baibai_engine.research.store import ResearchStoreService
from tests.helpers.db_seed import seed_ledger
from tests.helpers.fixed_now import FIXED_NOW

ROOT = Path(__file__).parents[1]
THESIS = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
THESIS_ID = "thesis-20260711-2331-r1"
CREATED_AT = datetime.fromisoformat("2026-07-11T10:02:00+09:00")
LEGACY_REPORT = "https://github.com/koumatsumoto/baibai-loop/issues/761"
NATIVE_PROPOSAL = "prop-20260731-8929-1"


def _raw(path: Path) -> dict[str, object]:
    value = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _approved(tmp_path: Path) -> tuple[LedgerStoreService, ProposalStoreService, str]:
    db = tmp_path / "app.sqlite"
    ResearchStoreService(db, clock=lambda: FIXED_NOW).publish_thesis_with_review(
        THESIS_ID, _raw(THESIS), _raw(REVIEW)
    )
    ledger = LedgerStoreService(db)
    source = load_portfolio_ledger(LEDGER)
    seed_ledger(
        db,
        source.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in source.market_prices
                )
            }
        ),
    )
    proposals = ProposalStoreService(
        db,
        market_db_path=tmp_path / "market.sqlite",
        clock=lambda: CREATED_AT + timedelta(minutes=1),
    )
    document, append_head = ledger.load_with_head()
    snapshot = reconcile_portfolio(document)
    market = tmp_path / "market.sqlite"
    with sqlite3.connect(market) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL)"
        )
        connection.execute("INSERT INTO jquants_daily_bars VALUES ('2331', '2026-07-10', 1000, 1)")
    planned = PlannedLimitInput.model_validate(
        plan_limit(
            thesis=THESIS,
            db_path=db,
            sqlite_path=market,
            target_session=date(2026, 7, 13),
            budget_min_yen=200_000,
            budget_max_yen=300_000,
            now=CREATED_AT,
        )
    )
    proposal = proposals.create(
        THESIS_ID,
        planned,
        snapshot,
        snapshot_append_head=append_head,
        created_at=CREATED_AT,
    )
    proposals.decide(
        proposal.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=snapshot,
        snapshot_append_head=append_head,
    )
    return ledger, proposals, proposal.proposal_id


def _legacy_services(
    tmp_path: Path, *, simultaneous_expiry: bool = False
) -> tuple[LedgerStoreService, ProposalStoreService]:
    db = tmp_path / "app.sqlite"
    source = load_portfolio_ledger(LEDGER)
    if simultaneous_expiry:
        raw = source.model_dump(mode="json")
        raw["events"].append(
            {
                "event_id": "migration-reserve-2331",
                "occurred_at": "2026-07-03T09:00:00+09:00",
                "type": "reservation",
                "reservation_id": "migration-reservation-2331",
                "order_id": "migration-order-2331",
                "ticker": "2331",
                "sector": "サービス業",
                "common_factors": ["labor-automation"],
                "decision_reference": None,
                "quantity": 100,
                "price_guard_yen": "1050",
                "expires_at": "2026-07-31T15:30:00+09:00",
            }
        )
        source = type(source).model_validate(raw)
    refreshed_at = datetime.fromisoformat("2026-07-30T15:30:00+09:00")
    seed_ledger(
        db,
        source.model_copy(
            update={
                "as_of": refreshed_at,
                "market_prices": tuple(
                    price.model_copy(
                        update={
                            "observed_at": refreshed_at,
                            "source_kind": "licensed_dataset",
                        }
                    )
                    for price in source.market_prices
                ),
            }
        ),
    )
    return LedgerStoreService(db), ProposalStoreService(db)


def test_approved_proposal_builds_bound_open_draft(tmp_path: Path) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    proposal = proposals.get(proposal_id)
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    orders = generated["orders"]
    assert isinstance(orders, list)
    assert orders
    order = orders[0]
    assert isinstance(order, dict)

    draft, event_ids = build_result_draft(
        ledger,
        proposals,
        proposal_id=proposal_id,
        status="open",
        occurred_at=CREATED_AT + timedelta(minutes=2),
        ticker="2331",
        quantity=int(str(order["quantity"])),
        sector="サービス業",
        price_guard_yen=Decimal(str(order["limit_price_yen"])),
        expires_at=datetime.fromisoformat(str(order["expires_at"])),
        now=CREATED_AT + timedelta(minutes=3),
    )

    assert draft is not None
    assert event_ids
    applied = apply_draft(ledger, draft, human_confirmed=True)
    assert applied.event_ids == event_ids
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        rows = connection.execute(
            "SELECT proposal_id FROM ledger_event WHERE proposal_id = ?", (proposal_id,)
        ).fetchall()
    assert rows == [(proposal_id,)]


def test_unapproved_and_order_drift_do_not_build_result(tmp_path: Path) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    with pytest.raises(ValueError, match="does not match approved proposal"):
        build_result_draft(
            ledger,
            proposals,
            proposal_id=proposal_id,
            status="open",
            occurred_at=CREATED_AT + timedelta(minutes=2),
            ticker="2331",
            quantity=999,
            sector="サービス業",
            price_guard_yen=Decimal("1"),
            expires_at=CREATED_AT + timedelta(days=1),
            now=CREATED_AT + timedelta(minutes=3),
        )


def test_record_result_cli_builds_db_bound_draft_and_retries_as_no_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    proposal = proposals.get(proposal_id)
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    orders = generated["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    before = ledger.load()

    args = [
        "record-result",
        "--root",
        str(tmp_path),
        "--db",
        str(tmp_path / "app.sqlite"),
        "--proposal-ref",
        proposal_id,
        "--status",
        "open",
        "--occurred-at",
        (CREATED_AT + timedelta(minutes=2)).isoformat(),
        "--ticker",
        "2331",
        "--quantity",
        str(order["quantity"]),
        "--sector",
        "サービス業",
        "--price-guard-yen",
        str(order["limit_price_yen"]),
        "--expires-at",
        str(order["expires_at"]),
        "--out",
        "draft.yaml",
    ]
    assert main(args, now=CREATED_AT + timedelta(minutes=3)) == 0

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["proposal_id"] == proposal_id
    assert output["status"] == "draft_created"
    draft = load_draft(tmp_path / "draft.yaml")
    assert draft.expected_head == ledger.append_head()
    assert ledger.load() == before

    apply_draft(ledger, draft, human_confirmed=True)
    repeat_args = [*args[:-1], "repeat-open.yaml"]
    assert main(repeat_args, now=CREATED_AT + timedelta(minutes=3)) == 0
    repeated = yaml.safe_load(capsys.readouterr().out)
    assert repeated["status"] == "no_change"
    assert repeated["event_ids"] == []
    assert repeated["output"] is None
    assert not (tmp_path / "repeat-open.yaml").exists()


def test_expired_legacy_reservation_builds_release_without_proposal_row(tmp_path: Path) -> None:
    ledger, proposals = _legacy_services(tmp_path)
    source = ledger.load()
    before = reconcile_portfolio(source)
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    draft, event_ids = build_result_draft(
        ledger,
        proposals,
        proposal_id=LEGACY_REPORT,
        status="expired",
        occurred_at=expiry,
        reservation_id="reservation-8929-pending",
        now=expiry + timedelta(days=1),
    )

    assert draft is not None
    assert len(event_ids) == 1
    release = draft.replacement.events[-1]
    assert release.type == "release"
    assert release.reason == "expired"
    assert release.decision_reference == LEGACY_REPORT
    after = reconcile_portfolio(draft.replacement)
    assert after.available_cash_yen == before.available_cash_yen + 119_000
    assert after.reserved_cash_yen == before.reserved_cash_yen - 119_000
    assert after.available_cash_yen + after.reserved_cash_yen == (
        before.available_cash_yen + before.reserved_cash_yen
    )

    applied = apply_draft(ledger, draft, human_confirmed=True)
    assert applied.event_ids == event_ids
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM proposal").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT proposal_id FROM ledger_event WHERE event_id = ?", event_ids
            ).fetchone()[0]
            is None
        )


def test_expired_unknown_reservation_is_rejected_without_write(tmp_path: Path) -> None:
    ledger, proposals = _legacy_services(tmp_path)
    before = ledger.load()
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    with pytest.raises(ValueError, match="expired requires an active reservation"):
        build_result_draft(
            ledger,
            proposals,
            proposal_id=LEGACY_REPORT,
            status="expired",
            occurred_at=expiry,
            reservation_id="reservation-missing",
            now=expiry + timedelta(days=1),
        )

    assert ledger.load() == before


def test_migration_reservation_does_not_bypass_proposal_binding_for_fill(tmp_path: Path) -> None:
    ledger, proposals = _legacy_services(tmp_path)
    before = ledger.load()
    fill_time = datetime.fromisoformat("2026-07-31T10:00:00+09:00")

    with pytest.raises(ValueError, match="only supports terminal result"):
        build_result_draft(
            ledger,
            proposals,
            proposal_id=LEGACY_REPORT,
            status="filled",
            occurred_at=fill_time,
            ticker="8929",
            quantity=100,
            price_yen=Decimal("1180"),
            reservation_id="reservation-8929-pending",
            now=fill_time + timedelta(days=1),
        )

    assert ledger.load() == before


def test_migration_terminal_result_requires_issue_reference(tmp_path: Path) -> None:
    ledger, proposals = _legacy_services(tmp_path)
    before = ledger.load()
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    with pytest.raises(ValueError, match="requires an HTTPS GitHub Issue URL"):
        build_result_draft(
            ledger,
            proposals,
            proposal_id=NATIVE_PROPOSAL,
            status="expired",
            occurred_at=expiry,
            reservation_id="reservation-8929-pending",
            now=expiry + timedelta(days=1),
        )

    assert ledger.load() == before


def test_record_result_cli_builds_simultaneous_expiry_as_one_atomic_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _legacy_services(tmp_path, simultaneous_expiry=True)
    before = reconcile_portfolio(ledger.load())
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    assert (
        main(
            [
                "record-result",
                "--root",
                str(tmp_path),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--proposal-ref",
                LEGACY_REPORT,
                "--status",
                "expired",
                "--occurred-at",
                expiry.isoformat(),
                "--reservation-id",
                "migration-reservation-2331",
                "--reservation-id",
                "reservation-8929-pending",
                "--out",
                "simultaneous-expiry.yaml",
            ],
            now=expiry + timedelta(days=1),
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    assert len(output["event_ids"]) == 2
    draft = load_draft(tmp_path / "simultaneous-expiry.yaml")
    releases = [event for event in draft.replacement.events if event.type == "release"]
    assert {event.reservation_id for event in releases[-2:]} == {
        "migration-reservation-2331",
        "reservation-8929-pending",
    }
    after = reconcile_portfolio(draft.replacement)
    assert after.available_cash_yen == before.available_cash_yen + 224_000
    assert after.reserved_cash_yen == before.reserved_cash_yen - 224_000
    assert after.available_cash_yen + after.reserved_cash_yen == (
        before.available_cash_yen + before.reserved_cash_yen
    )

    applied = apply_draft(ledger, draft, human_confirmed=True)
    assert applied.event_ids == tuple(output["event_ids"])
    assert reconcile_portfolio(ledger.load()).active_reservations == ()

    assert (
        main(
            [
                "record-result",
                "--root",
                str(tmp_path),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--proposal-ref",
                LEGACY_REPORT,
                "--status",
                "expired",
                "--occurred-at",
                expiry.isoformat(),
                "--reservation-id",
                "migration-reservation-2331",
                "--reservation-id",
                "reservation-8929-pending",
                "--out",
                "repeat.yaml",
            ],
            now=expiry + timedelta(days=1),
        )
        == 0
    )
    repeated = yaml.safe_load(capsys.readouterr().out)
    assert repeated["status"] == "no_change"
    assert repeated["event_ids"] == []
    assert not (tmp_path / "repeat.yaml").exists()


def test_record_result_cli_rejects_entire_batch_when_one_reservation_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _legacy_services(tmp_path, simultaneous_expiry=True)
    before = ledger.load()
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
    output = tmp_path / "invalid-expiry.yaml"

    assert (
        main(
            [
                "record-result",
                "--root",
                str(tmp_path),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--proposal-ref",
                LEGACY_REPORT,
                "--status",
                "expired",
                "--occurred-at",
                expiry.isoformat(),
                "--reservation-id",
                "migration-reservation-2331",
                "--reservation-id",
                "reservation-missing",
                "--out",
                output.name,
            ],
            now=expiry + timedelta(days=1),
        )
        == 2
    )

    assert "expired requires an active reservation" in capsys.readouterr().err
    assert not output.exists()
    assert ledger.load() == before
