from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger, portfolio_snapshot

from baibai_engine.position.broker_fact_recording import BrokerFactRecordingError
from baibai_engine.position.cli import main
from baibai_engine.position.decision_references import resolve_buy_reference
from baibai_engine.position.drafts import (
    apply_draft,
    build_broker_fact_draft,
    build_sell_execution_draft,
    load_draft,
)
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.storage.application.write import initialize_database

ROOT = Path(__file__).parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
ORDERED_AT = datetime.fromisoformat("2026-07-30T09:00:00+09:00")
EXPIRES_AT = datetime.fromisoformat("2026-07-31T15:30:00+09:00")
RETAINED_BUY_REFERENCE = "capital-allocation-assessment-20260730-2331"


def _services(
    tmp_path: Path, *, retain_buy_reference: bool = True
) -> tuple[LedgerStoreService, Callable[[str], str]]:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    # 切替で保全された旧IDを現行reference表へseedする。旧CAA writerは使わない。
    with closing(sqlite3.connect(db)) as connection, connection:
        if retain_buy_reference:
            connection.execute(
                "INSERT INTO position_decision_reference VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    RETAINED_BUY_REFERENCE,
                    "2331",
                    "buy",
                    "retained_reference",
                    None,
                    RETAINED_BUY_REFERENCE,
                    "a" * 64,
                ),
            )
    original = load_portfolio_ledger(LEDGER)
    source = original.model_copy(
        update={
            "as_of": ORDERED_AT,
        }
    )
    seed_ledger(db, source)
    return LedgerStoreService(db), partial(resolve_buy_reference, db_path=db)


def _open_draft(
    ledger: LedgerStoreService,
    resolve_reference: Callable[[str], str],
    *,
    expires_at: datetime = EXPIRES_AT,
):
    return build_broker_fact_draft(
        ledger,
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
        status="open",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1050"),
        expires_at=expires_at,
        now=ORDERED_AT + timedelta(minutes=1),
    )


def test_retained_buy_reference_builds_and_persists_bound_open_result(tmp_path: Path) -> None:
    ledger, resolve_reference = _services(tmp_path)
    draft, event_ids = _open_draft(ledger, resolve_reference)

    assert draft is not None
    apply_draft(ledger, draft, human_confirmed=True)
    with closing(sqlite3.connect(tmp_path / "app.sqlite")) as connection, connection:
        row = connection.execute(
            "SELECT decision_reference FROM ledger_event WHERE event_id = ?", event_ids
        ).fetchone()
    assert row == (RETAINED_BUY_REFERENCE,)


def test_human_sell_order_persists_without_allocation_or_cash_change(tmp_path: Path) -> None:
    ledger, _ = _services(tmp_path)
    before, _ = ledger.load_with_head()

    def unexpected_buy_reference_lookup(_reference: str) -> str:
        raise AssertionError("sell order must not resolve a buy reference")

    draft, event_ids = build_broker_fact_draft(
        ledger,
        unexpected_buy_reference_lookup,
        decision_reference="human-reported-sell-2331",
        status="open",
        side="sell",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=EXPIRES_AT,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert draft is not None
    assert draft.replacement.events[-1].side == "sell"
    assert len(event_ids) == 1
    apply_draft(ledger, draft, human_confirmed=True)
    stored, _ = ledger.load_with_head()
    assert stored.events[-1].event_id == event_ids[0]
    assert stored.events[-1].side == "sell"
    assert stored.events[-1].order_id.startswith("repository-order-")
    from baibai_engine.position.ledger import replay_events_through

    original_cash = replay_events_through(before.events, before.as_of)
    updated_cash = replay_events_through(stored.events, stored.as_of)
    assert updated_cash.available_cash_yen == original_cash.available_cash_yen
    assert updated_cash.reserved_cash_yen == original_cash.reserved_cash_yen

    reservation_id = stored.events[-1].reservation_id
    fill = build_sell_execution_draft(
        ledger,
        occurred_on=EXPIRES_AT.date(),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1110"),
        reservation_id=reservation_id,
    )
    assert fill.replacement.events[-1].reservation_id == reservation_id
    assert fill.replacement.events[-1].decision_reference == "human-reported-sell-2331"
    apply_draft(ledger, fill, human_confirmed=True)
    filled, _ = ledger.load_with_head()
    filled_state = replay_events_through(filled.events, filled.as_of)
    assert reservation_id not in filled_state.active_reservations
    assert sum(lot.quantity for lot in filled_state.lots["2331"]) == 100
    assert filled_state.available_cash_yen == original_cash.available_cash_yen + 111_000


def test_same_day_reserved_buy_fill_uses_reported_date_without_claiming_fill_time(
    tmp_path: Path,
) -> None:
    ledger, resolve_reference = _services(tmp_path)
    opened, _ = _open_draft(ledger, resolve_reference)
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    before, _ = ledger.load_with_head()
    reservation_id = before.events[-1].reservation_id
    from baibai_engine.position.ledger import replay_events_through

    before_state = replay_events_through(before.events, before.as_of)

    draft, event_ids = build_broker_fact_draft(
        ledger,
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
        status="filled",
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=200,
        price_yen=Decimal("1050"),
        reservation_id=reservation_id,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert draft is not None
    fill = draft.replacement.events[-1]
    assert fill.occurred_on == ORDERED_AT.date()
    assert fill.occurred_at == ORDERED_AT  # reservation instant is only a replay key
    apply_draft(ledger, draft, human_confirmed=True)
    stored, _ = ledger.load_with_head()
    state = replay_events_through(stored.events, stored.as_of)
    assert reservation_id not in state.active_reservations
    assert sum(lot.quantity for lot in state.lots["2331"]) == 400
    assert state.reserved_cash_yen == before_state.reserved_cash_yen - 210_000
    again, repeated_ids = build_broker_fact_draft(
        ledger,
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
        status="filled",
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=200,
        price_yen=Decimal("1050"),
        reservation_id=reservation_id,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert again is None
    assert repeated_ids == ()
    assert len(event_ids) == 1
    with pytest.raises(BrokerFactRecordingError, match="conflicting human report"):
        build_broker_fact_draft(
            ledger,
            resolve_reference,
            decision_reference=RETAINED_BUY_REFERENCE,
            status="filled",
            occurred_on=ORDERED_AT.date(),
            ticker="2331",
            quantity=200,
            price_yen=Decimal("1000"),
            reservation_id=reservation_id,
            now=ORDERED_AT + timedelta(minutes=1),
        )


def test_same_day_reserved_sell_date_only_replays_after_order(tmp_path: Path) -> None:
    ledger, _ = _services(tmp_path)
    opened, _ = build_broker_fact_draft(
        ledger,
        lambda _: "2331",
        decision_reference="human-reported-sell-2331",
        status="open",
        side="sell",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=100,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=EXPIRES_AT,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    before, _ = ledger.load_with_head()
    reservation_id = before.events[-1].reservation_id
    draft = build_sell_execution_draft(
        ledger,
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1110"),
        reservation_id=reservation_id,
    )
    fill = draft.replacement.events[-1]
    assert fill.occurred_on == ORDERED_AT.date()
    assert fill.occurred_at == ORDERED_AT
    apply_draft(ledger, draft, human_confirmed=True)


def test_distinct_date_only_buy_execution_ids_consume_one_order_and_retry_after_full(
    tmp_path: Path,
) -> None:
    ledger, resolve_reference = _services(tmp_path)
    opened, _ = _open_draft(ledger, resolve_reference)
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    reservation_id = opened.replacement.events[-1].reservation_id
    reported = dict(
        decision_reference=RETAINED_BUY_REFERENCE,
        status="filled",
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1000"),
        reservation_id=reservation_id,
        now=ORDERED_AT + timedelta(minutes=1),
    )

    first, _ = build_broker_fact_draft(
        ledger, resolve_reference, **reported, execution_id="broker-order-a/fill-1"
    )
    assert first is not None
    assert first.replacement.events[-1].execution_id == "broker-order-a/fill-1"
    apply_draft(ledger, first, human_confirmed=True)
    assert (
        next(
            item.remaining_quantity
            for item in portfolio_snapshot(ledger.load()).active_reservations
            if item.reservation_id == reservation_id
        )
        == 100
    )

    second, _ = build_broker_fact_draft(
        ledger, resolve_reference, **reported, execution_id="broker-order-a/fill-2"
    )
    assert second is not None
    apply_draft(ledger, second, human_confirmed=True)
    snapshot = portfolio_snapshot(ledger.load())
    assert all(item.reservation_id != reservation_id for item in snapshot.active_reservations)
    assert next(item.quantity for item in snapshot.holdings if item.ticker == "2331") == 400
    db_before_retry = (tmp_path / "app.sqlite").read_bytes()

    def unavailable_history(_reference: str) -> str:
        pytest.fail("known execution retry must not need the retained buy reference")

    assert build_broker_fact_draft(
        ledger, unavailable_history, **reported, execution_id="broker-order-a/fill-2"
    ) == (None, ())
    assert (tmp_path / "app.sqlite").read_bytes() == db_before_retry
    with pytest.raises(BrokerFactRecordingError, match="conflicting human report"):
        build_broker_fact_draft(
            ledger,
            unavailable_history,
            **{**reported, "price_yen": Decimal("990")},
            execution_id="broker-order-a/fill-2",
        )
    with pytest.raises(BrokerFactRecordingError, match="execution_id"):
        build_broker_fact_draft(ledger, resolve_reference, **reported)


def test_distinct_date_only_sell_execution_ids_and_costs_retry_after_full(tmp_path: Path) -> None:
    ledger, _ = _services(tmp_path)
    opened, _ = build_broker_fact_draft(
        ledger,
        lambda _: "2331",
        decision_reference="human-reported-sell-2331",
        status="open",
        side="sell",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=EXPIRES_AT,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    reservation_id = opened.replacement.events[-1].reservation_id
    reported = dict(
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1110"),
        reservation_id=reservation_id,
        fees_yen=100,
        tax_yen=200,
    )

    first = build_sell_execution_draft(ledger, **reported, execution_id="broker-order-s/fill-1")
    assert first is not None
    apply_draft(ledger, first, human_confirmed=True)
    assert (
        next(
            item.remaining_quantity
            for item in portfolio_snapshot(ledger.load()).active_reservations
            if item.reservation_id == reservation_id
        )
        == 100
    )
    with pytest.raises(ValueError, match="execution_id"):
        build_sell_execution_draft(ledger, **{**reported, "price_yen": Decimal("1120")})

    second = build_sell_execution_draft(ledger, **reported, execution_id="broker-order-s/fill-2")
    assert second is not None
    apply_draft(ledger, second, human_confirmed=True)
    snapshot = portfolio_snapshot(ledger.load())
    assert all(item.reservation_id != reservation_id for item in snapshot.active_reservations)
    assert all(item.ticker != "2331" for item in snapshot.holdings)
    db_before_retry = (tmp_path / "app.sqlite").read_bytes()
    assert (
        build_sell_execution_draft(ledger, **reported, execution_id="broker-order-s/fill-2") is None
    )
    assert (tmp_path / "app.sqlite").read_bytes() == db_before_retry
    with pytest.raises(ValueError, match="conflicting human report"):
        build_sell_execution_draft(
            ledger,
            **{**reported, "tax_yen": 300},
            execution_id="broker-order-s/fill-2",
        )


def test_date_only_sell_without_id_rejects_changed_second_fill(tmp_path: Path) -> None:
    ledger, _ = _services(tmp_path)
    opened, _ = build_broker_fact_draft(
        ledger,
        lambda _: "2331",
        decision_reference="human-reported-sell-2331",
        status="open",
        side="sell",
        occurred_at=ORDERED_AT,
        ticker="2331",
        quantity=200,
        sector="サービス業",
        price_guard_yen=Decimal("1100"),
        expires_at=EXPIRES_AT,
        now=ORDERED_AT + timedelta(minutes=1),
    )
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    reservation_id = opened.replacement.events[-1].reservation_id
    first = build_sell_execution_draft(
        ledger,
        occurred_on=ORDERED_AT.date(),
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1110"),
        reservation_id=reservation_id,
    )
    assert first is not None
    apply_draft(ledger, first, human_confirmed=True)
    with pytest.raises(ValueError, match="execution_id"):
        build_sell_execution_draft(
            ledger,
            occurred_on=ORDERED_AT.date(),
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1120"),
            reservation_id=reservation_id,
        )


def test_broker_fact_cli_passes_execution_id_and_reports_retry_as_no_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, resolve_reference = _services(tmp_path)
    opened, _ = _open_draft(ledger, resolve_reference)
    assert opened is not None
    apply_draft(ledger, opened, human_confirmed=True)
    reservation_id = opened.replacement.events[-1].reservation_id
    args = [
        "broker-fact-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(tmp_path / "app.sqlite"),
        "--decision-reference",
        RETAINED_BUY_REFERENCE,
        "--status",
        "filled",
        "--occurred-on",
        ORDERED_AT.date().isoformat(),
        "--reservation-id",
        reservation_id,
        "--ticker",
        "2331",
        "--quantity",
        "200",
        "--price-yen",
        "1000",
        "--execution-id",
        "broker-order-a/fill-1",
    ]
    assert main([*args, "--out", "fill.yaml"], now=ORDERED_AT + timedelta(minutes=1)) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "draft_created"
    fill = load_draft(tmp_path / "fill.yaml")
    assert fill.replacement.events[-1].execution_id == "broker-order-a/fill-1"
    apply_draft(ledger, fill, human_confirmed=True)
    assert main([*args, "--out", "retry.yaml"], now=ORDERED_AT + timedelta(minutes=1)) == 0
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "no_change"
    assert not (tmp_path / "retry.yaml").exists()


def test_unavailable_buy_reference_and_ticker_mismatch_are_rejected(tmp_path: Path) -> None:
    ledger, resolve_reference = _services(tmp_path, retain_buy_reference=False)
    with pytest.raises(ValueError, match="buy decision reference is unavailable"):
        _open_draft(ledger, resolve_reference)

    ledger, resolve_reference = _services(tmp_path / "other")
    with pytest.raises(ValueError, match="ticker does not match"):
        build_broker_fact_draft(
            ledger,
            resolve_reference,
            decision_reference=RETAINED_BUY_REFERENCE,
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
    ledger, resolve_reference = _services(tmp_path)
    open_draft, _ = _open_draft(ledger, resolve_reference)
    assert open_draft is not None
    apply_draft(ledger, open_draft, human_confirmed=True)
    reservation = open_draft.replacement.events[-1]

    fill, fill_ids = build_broker_fact_draft(
        ledger,
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
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
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
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
        resolve_reference,
        decision_reference=RETAINED_BUY_REFERENCE,
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
        RETAINED_BUY_REFERENCE,
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
    assert output["decision_reference"] == RETAINED_BUY_REFERENCE
    assert ledger.append_head() == load_draft(tmp_path / "draft.yaml").expected_head


def test_unbound_reservation_is_not_a_runtime_compatibility_path(tmp_path: Path) -> None:
    ledger, resolve_reference = _services(tmp_path)
    expiry = datetime.fromisoformat("2026-07-31T15:30:00+09:00")

    with pytest.raises(ValueError, match="no canonical decision binding"):
        build_broker_fact_draft(
            ledger,
            resolve_reference,
            decision_reference=RETAINED_BUY_REFERENCE,
            status="expired",
            occurred_at=expiry,
            reservation_id="reservation-8929-pending",
            now=expiry + timedelta(days=1),
        )


def test_broker_draft_keeps_the_snapshot_head_when_another_write_intervenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, resolve_reference = _services(tmp_path)
    intervening, _ = _open_draft(ledger, resolve_reference)
    assert intervening is not None
    load_snapshot = ledger.load_with_head

    def load_then_write():
        snapshot = load_snapshot()
        apply_draft(ledger, intervening, human_confirmed=True)
        return snapshot

    monkeypatch.setattr(ledger, "load_with_head", load_then_write)
    draft, _ = _open_draft(ledger, resolve_reference)
    monkeypatch.setattr(ledger, "load_with_head", load_snapshot)
    assert draft is not None
    assert draft.expected_head == intervening.expected_head
    assert draft.source == intervening.source
    with pytest.raises(ValueError, match="stale ledger draft"):
        apply_draft(ledger, draft, human_confirmed=True)


@pytest.mark.parametrize("terminal_status", ["cancelled", "expired"])
def test_active_and_known_terminal_retries_do_not_resolve_buy_binding(
    tmp_path: Path, monkeypatch, capsys, terminal_status: str
) -> None:
    ledger, resolve = _services(tmp_path)
    references: list[str] = []

    def resolve_initial(reference: str) -> str:
        references.append(reference)
        return resolve(reference)

    draft, _ = _open_draft(ledger, resolve_initial, expires_at=ORDERED_AT + timedelta(hours=2))
    assert draft is not None
    assert references == [RETAINED_BUY_REFERENCE]
    apply_draft(ledger, draft, human_confirmed=True)
    reservation = draft.replacement.events[-1]

    def unavailable_history(_reference: str, **_kwargs) -> str:
        pytest.fail("active or known terminal report resolved retained buy reference")

    db = tmp_path / "app.sqlite"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DELETE FROM position_decision_reference")
    before = db.read_bytes()
    fill_args = dict(
        decision_reference=RETAINED_BUY_REFERENCE,
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
        decision_reference=RETAINED_BUY_REFERENCE,
        status=terminal_status,
        occurred_at=ORDERED_AT + timedelta(hours=1 if terminal_status == "cancelled" else 3),
        reservation_id=reservation.reservation_id,
        now=ORDERED_AT + timedelta(hours=4),
    )
    terminal, _ = build_broker_fact_draft(ledger, unavailable_history, **terminal_args)
    assert terminal is not None
    apply_draft(ledger, terminal, human_confirmed=True)
    assert build_broker_fact_draft(ledger, unavailable_history, **terminal_args) == (None, ())

    # CLIもcallableを渡すだけにし、予約済み再試行の前にbindingを先読みしない。
    monkeypatch.setattr("baibai_engine.position.cli.resolve_buy_reference", unavailable_history)
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
                RETAINED_BUY_REFERENCE,
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


def test_retained_buy_reference_does_not_write_or_replay_current_eligibility(
    tmp_path: Path, monkeypatch
) -> None:
    _services(tmp_path)
    db = tmp_path / "app.sqlite"
    before = db.read_bytes()

    def reject_current_check(*_args, **_kwargs):
        pytest.fail("historical identity entered current Proposal publication check")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.entry_policy.evaluate_buy_terms",
        reject_current_check,
    )
    assert resolve_buy_reference(RETAINED_BUY_REFERENCE, db_path=db) == "2331"
    assert db.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="application database is unavailable"):
        resolve_buy_reference(RETAINED_BUY_REFERENCE, db_path=missing)
    assert not missing.exists()
