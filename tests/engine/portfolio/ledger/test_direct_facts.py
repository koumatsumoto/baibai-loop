from datetime import UTC, timedelta
from decimal import Decimal

import pytest
import yaml
from tests.helpers.manual_facts import NOW, _binding_count, _service

from baibai_engine.cli.portfolio import main
from baibai_engine.portfolio.ledger.broker_facts import BrokerFactRecordingError
from baibai_engine.portfolio.ledger.decision_references import resolve_buy_reference
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_broker_fact_draft,
    build_event_draft,
    load_draft,
)
from baibai_engine.portfolio.ledger.events import (
    ContributionEvent,
    ExecutionEvent,
    PortfolioLedgerError,
    ReservationEvent,
    replay_events_through,
)


def unavailable(_reference):
    pytest.fail("a direct fact or known broker identity must not resolve a proposal")


def open_direct(service, **overrides):
    values = dict(
        decision_reference=None,
        direct_fact=True,
        status="open",
        occurred_at=NOW,
        ticker="1234",
        quantity=200,
        sector="情報・通信業",
        price_guard_yen=Decimal("1000"),
        expires_at=NOW + timedelta(days=1),
        order_id="synthetic-direct-order",
        now=NOW,
    )
    return build_broker_fact_draft(service, unavailable, **(values | overrides))


def fill_direct(service, **overrides):
    values = dict(
        decision_reference=None,
        direct_fact=True,
        status="filled",
        occurred_at=NOW + timedelta(minutes=10),
        ticker="1234",
        quantity=100,
        price_yen=Decimal("1000"),
        order_id="synthetic-direct-order",
        execution_id="synthetic-direct-fill-1",
        now=NOW + timedelta(minutes=20),
    )
    return build_broker_fact_draft(service, unavailable, **(values | overrides))


def test_initial_direct_draft_fixes_uuid_without_binding_or_ledger_write(tmp_path):
    service, path = _service(tmp_path)
    draft, ids = open_direct(service)
    assert draft is not None
    assert ids
    binding = draft.binding_addition
    assert binding is not None
    assert binding.reference.startswith("manual-fact-")
    assert binding.reference == binding.source_id == draft.replacement.events[-1].decision_reference
    assert service.load_with_head()[1] == 1
    assert _binding_count(path) == 0
    apply_draft(service, draft, human_confirmed=True)
    assert resolve_buy_reference(binding.reference, db_path=path) == "1234"
    assert _binding_count(path) == 1


def test_same_order_partial_fills_keep_reference_and_both_executions(tmp_path):
    service, path = _service(tmp_path)
    opened, _ = open_direct(service)
    apply_draft(service, opened, human_confirmed=True)
    reference = opened.binding_addition.reference
    first, _ = fill_direct(service)
    assert first is not None
    assert first.binding_addition is None
    apply_draft(service, first, human_confirmed=True)
    second, ids = fill_direct(
        service,
        occurred_at=NOW + timedelta(minutes=15),
        execution_id="synthetic-direct-fill-2",
    )
    assert second is not None
    assert ids
    assert second.binding_addition is None
    apply_draft(service, second, human_confirmed=True)
    fills = [event for event in service.load().events if isinstance(event, ExecutionEvent)]
    assert len(fills) == 2
    assert {event.decision_reference for event in fills} == {reference}
    assert {event.execution_id for event in fills} == {
        "synthetic-direct-fill-1",
        "synthetic-direct-fill-2",
    }
    state = replay_events_through(service.load().events, service.load().as_of)
    assert sum(lot.quantity for lot in state.lots["1234"]) == 200
    assert not state.active_reservations
    before = path.read_bytes()
    assert fill_direct(
        service,
        occurred_at=NOW + timedelta(minutes=15),
        execution_id="synthetic-direct-fill-2",
    ) == (None, ())
    assert path.read_bytes() == before
    with pytest.raises(BrokerFactRecordingError, match="conflicting human report"):
        fill_direct(
            service,
            occurred_at=NOW + timedelta(minutes=15),
            execution_id="synthetic-direct-fill-2",
            price_yen=Decimal("999"),
        )


def test_existing_order_open_retry_keeps_reference_before_generating_uuid(tmp_path):
    service, _ = _service(tmp_path)
    draft, _ = open_direct(service)
    apply_draft(service, draft, human_confirmed=True)
    assert open_direct(service) == (None, ())
    with pytest.raises(ValueError, match="conflicts"):
        open_direct(service, decision_reference="manual-fact-00000000-0000-4000-8000-000000000009")


def test_apply_rejects_edited_buy_fill_reference_without_changing_ledger(tmp_path):
    service, _ = _service(tmp_path)
    opened, _ = open_direct(service)
    apply_draft(service, opened, human_confirmed=True)
    draft, _ = fill_direct(service)
    assert draft is not None
    fill = draft.replacement.events[-1]
    assert isinstance(fill, ExecutionEvent)
    other_reference = "manual-fact-00000000-0000-4000-8000-000000000009"
    edited = draft.model_copy(
        update={
            "replacement": draft.replacement.model_copy(
                update={
                    "events": (
                        *draft.replacement.events[:-1],
                        fill.model_copy(update={"decision_reference": other_reference}),
                    )
                }
            )
        }
    )
    before, head = service.load_with_head()

    with pytest.raises(PortfolioLedgerError, match="buy execution decision reference must match"):
        apply_draft(service, edited, human_confirmed=True)

    assert service.load_with_head() == (before, head)


def test_reserved_buy_replays_legacy_execution_without_a_recorded_reference(tmp_path):
    service, _ = _service(tmp_path)
    opened, _ = open_direct(service)
    apply_draft(service, opened, human_confirmed=True)
    draft, _ = fill_direct(service)
    assert draft is not None
    fill = draft.replacement.events[-1]
    assert isinstance(fill, ExecutionEvent)
    legacy = draft.replacement.model_copy(
        update={
            "events": (
                *draft.replacement.events[:-1],
                fill.model_copy(update={"decision_reference": None}),
            )
        }
    )

    state = replay_events_through(legacy.events, legacy.as_of)

    assert sum(lot.quantity for lot in state.lots["1234"]) == 100
    assert state.reserved_cash_yen == 100_000


def test_existing_fill_with_wrong_order_is_conflict(tmp_path):
    service, _ = _service(tmp_path)
    opened, _ = open_direct(service)
    apply_draft(service, opened, human_confirmed=True)
    fill, _ = fill_direct(service)
    apply_draft(service, fill, human_confirmed=True)
    with pytest.raises(ValueError, match="order_id conflicts"):
        fill_direct(service, order_id="synthetic-wrong-order")


def test_unknown_old_reference_is_not_relabelled_as_manual_fact(tmp_path):
    service, path = _service(tmp_path)
    with pytest.raises(ValueError, match="manual-fact"):
        open_direct(service, decision_reference="capital-allocation-assessment-unknown")
    assert service.load_with_head()[1] == 1
    assert _binding_count(path) == 0


def test_normal_report_requires_reference_and_direct_report_requires_buy_ticker(tmp_path):
    service, _ = _service(tmp_path)
    with pytest.raises(ValueError, match="decision_reference is required"):
        open_direct(service, direct_fact=False)
    with pytest.raises(ValueError, match="explicit buy ticker"):
        open_direct(service, ticker=None)
    with pytest.raises(ValueError, match="explicit buy ticker"):
        open_direct(service, side="sell")


def test_anonymous_equal_facts_are_not_inferred_to_be_the_same_or_a_new_fact(tmp_path):
    service, path = _service(tmp_path)
    report = dict(
        decision_reference=None,
        direct_fact=True,
        status="filled",
        ticker="1234",
        quantity=100,
        price_yen=Decimal("1000"),
        sector="情報・通信業",
        occurred_at=NOW + timedelta(minutes=2),
        price_guard_yen=Decimal("1000"),
        expires_at=NOW + timedelta(days=1),
        now=NOW + timedelta(minutes=3),
    )
    first, _ = build_broker_fact_draft(service, unavailable, **report)
    apply_draft(service, first, human_confirmed=True)
    with pytest.raises(ValueError, match="saved manual-fact reference"):
        build_broker_fact_draft(service, unavailable, **report)
    # A human explicitly identifies a different fact; equal amounts are not deduplicated.
    second, _ = build_broker_fact_draft(
        service,
        unavailable,
        **(report | {"decision_reference": "manual-fact-00000000-0000-4000-8000-000000000009"}),
    )
    apply_draft(service, second, human_confirmed=True)
    assert _binding_count(path) == 2
    assert sum(isinstance(event, ExecutionEvent) for event in service.load().events) == 2


def test_cli_returns_initial_and_retry_reference_without_preapply_writes(tmp_path, capsys):
    service, path = _service(tmp_path)
    args = [
        "broker-fact-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(path),
        "--direct-fact",
        "--status",
        "open",
        "--ticker",
        "1234",
        "--quantity",
        "200",
        "--occurred-at",
        NOW.isoformat(),
        "--sector",
        "情報・通信業",
        "--price-guard-yen",
        "1000",
        "--expires-at",
        (NOW + timedelta(days=1)).isoformat(),
        "--order-id",
        "synthetic-direct-order",
    ]
    assert main([*args, "--out", "direct.yaml"], now=NOW) == 0
    first = yaml.safe_load(capsys.readouterr().out)
    assert first["decision_reference"].startswith("manual-fact-")
    assert service.load_with_head()[1] == 1
    assert _binding_count(path) == 0
    draft = load_draft(tmp_path / "direct.yaml")
    assert draft.binding_addition.reference == first["decision_reference"]
    assert isinstance(draft.replacement.events[-1], ReservationEvent)
    apply_draft(service, draft, human_confirmed=True)
    assert main([*args, "--out", "retry.yaml"], now=NOW) == 0
    retry = yaml.safe_load(capsys.readouterr().out)
    assert retry["status"] == "no_change"
    assert retry["decision_reference"] == first["decision_reference"]
    assert not (tmp_path / "retry.yaml").exists()


def test_date_only_direct_fill_needs_no_order_time_or_expiry(tmp_path):
    service, _ = _service(tmp_path)
    before = service.load()
    report = dict(
        decision_reference=None,
        direct_fact=True,
        status="filled",
        occurred_on=NOW.date(),
        ticker="1234",
        quantity=100,
        price_yen=Decimal("2020"),
        sector="食料品",
        now=NOW,
    )
    draft, ids = build_broker_fact_draft(service, unavailable, **report)
    assert draft is not None
    assert len(ids) == 1
    event = draft.replacement.events[-1]
    assert isinstance(event, ExecutionEvent)
    assert event.occurred_on == NOW.date()
    assert event.reservation_id is None
    assert not any(isinstance(row, ReservationEvent) for row in draft.replacement.events)
    apply_draft(service, draft, human_confirmed=True)
    stored = service.load()
    state = replay_events_through(stored.events, stored.as_of)
    previous = replay_events_through(before.events, before.as_of)
    assert state.available_cash_yen == previous.available_cash_yen - 202_000
    assert state.reserved_cash_yen == 0
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100
    reference = draft.binding_addition.reference
    assert build_broker_fact_draft(
        service, unavailable, **(report | {"decision_reference": reference})
    ) == (None, ())


def test_date_only_order_without_expiry_can_be_cancelled_and_retried(tmp_path):
    service, _ = _service(tmp_path)
    opened, _ = open_direct(service, occurred_at=None, occurred_on=NOW.date(), expires_at=None)
    assert opened is not None
    order = opened.replacement.events[-1]
    assert order.occurred_on == NOW.date()
    assert order.expires_at is None
    apply_draft(service, opened, human_confirmed=True)
    assert open_direct(service, occurred_at=None, occurred_on=NOW.date(), expires_at=None) == (
        None,
        (),
    )
    report = dict(
        decision_reference=order.decision_reference,
        status="cancelled",
        occurred_on=NOW.date(),
        reservation_id=order.reservation_id,
        now=NOW,
    )
    cancelled, _ = build_broker_fact_draft(service, unavailable, **report)
    assert cancelled is not None
    apply_draft(service, cancelled, human_confirmed=True)
    assert build_broker_fact_draft(service, unavailable, **report) == (None, ())
    stored = service.load()
    assert not replay_events_through(stored.events, stored.as_of).active_reservations


def test_date_only_expiry_checks_report_clock_not_replay_key(tmp_path):
    service, path = _service(tmp_path)
    expiry = NOW + timedelta(hours=5)
    opened, _ = open_direct(service, expires_at=expiry)
    apply_draft(service, opened, human_confirmed=True)
    order = opened.replacement.events[-1]
    report = dict(
        decision_reference=order.decision_reference,
        status="expired",
        occurred_on=NOW.date(),
        reservation_id=order.reservation_id,
    )
    boundary = expiry.astimezone(UTC)
    just_before = boundary - timedelta(microseconds=1)
    reserved = path.read_bytes()
    with pytest.raises(BrokerFactRecordingError, match="date-only expired report"):
        build_broker_fact_draft(service, unavailable, **report, now=just_before)
    assert path.read_bytes() == reserved

    expired, ids = build_broker_fact_draft(service, unavailable, **report, now=boundary)
    assert expired is not None
    assert build_broker_fact_draft(
        service, unavailable, **report, now=expiry + timedelta(hours=1)
    ) == (
        expired,
        ids,
    )
    release = expired.replacement.events[-1]
    assert release.occurred_on == NOW.date()
    assert release.occurred_at == order.occurred_at < expiry
    apply_draft(service, expired, human_confirmed=True)
    stored = service.load()
    assert not replay_events_through(stored.events, stored.as_of).active_reservations
    saved = path.read_bytes()
    assert build_broker_fact_draft(service, unavailable, **report, now=just_before) == (None, ())
    assert path.read_bytes() == saved
    # A later-arriving cash report does not retest the clocks of retained releases.
    contribution = ContributionEvent(
        type="contribution",
        event_id="late-cash",
        occurred_at=NOW - timedelta(minutes=1),
        amount_yen=1000,
    )
    draft = build_event_draft(service, contribution, now=just_before)
    apply_draft(service, draft, human_confirmed=True, now=just_before)
    assert tuple(e for e in service.load().events if e.event_id != "late-cash") == stored.events


@pytest.mark.parametrize("date_only", [False, True])
def test_confirmed_apply_checks_known_expiry_not_a_prepared_files_claim(
    tmp_path, capsys, date_only
):
    service, path = _service(tmp_path)
    expiry = NOW + timedelta(hours=5)
    opened, _ = open_direct(service, expires_at=expiry)
    assert opened is not None
    apply_draft(service, opened, human_confirmed=True, now=NOW)
    order = service.load().events[-1]
    source, head = service.load_with_head()
    reserve = path.read_bytes()
    out = tmp_path / "terminal.yaml"
    just_before = expiry.astimezone(UTC) - timedelta(microseconds=1)
    boundary = expiry.astimezone(UTC)
    base = [
        "broker-fact-draft",
        "--db",
        str(path),
        "--decision-reference",
        order.decision_reference,
        "--reservation-id",
        order.reservation_id,
    ]
    date_args = ["--occurred-on", NOW.date().isoformat()]
    assert main([*base, *date_args, "--status", "expired", "--out", str(out)], now=just_before) == 2
    assert "date-only expired report predates expires_at" in capsys.readouterr().err
    assert not out.exists()
    assert (
        main([*base, *date_args, "--status", "cancelled", "--out", str(out)], now=just_before) == 0
    )
    capsys.readouterr()
    authored = yaml.safe_load(out.read_text(encoding="utf-8"))
    release = authored["replacement"]["events"][-1]
    release["reason"] = "expired"
    if not date_only:
        release["occurred_at"] = expiry.isoformat()
        release["occurred_on"] = None
        authored["replacement"]["as_of"] = expiry.isoformat()
    out.write_text(yaml.safe_dump(authored), encoding="utf-8")
    edited = out.read_bytes()
    args = ["apply-draft", str(out), "--db", str(path), "--confirmed"]
    assert main(args, now=just_before) == 2
    bad = capsys.readouterr()
    expected = (
        "date-only expired report predates expires_at"
        if date_only
        else "occurred_at must not be in the future"
    )
    assert expected in bad.err
    assert bad.out == ""
    assert path.read_bytes() == reserve
    assert out.read_bytes() == edited
    assert service.load_with_head() == (source, head)
    assert replay_events_through(source.events, just_before).reserved_cash_yen == 200_000
    if not date_only:
        # A later confirmation still cannot make a known pre-deadline exact expiry true.
        release["occurred_at"] = NOW.isoformat()
        out.write_text(yaml.safe_dump(authored), encoding="utf-8")
        assert main(args, now=boundary) == 2
        assert "expired release predates expires_at" in capsys.readouterr().err
        assert path.read_bytes() == reserve
        out.write_bytes(edited)

    assert main(args, now=boundary) == 0
    assert "status: applied" in capsys.readouterr().out
    saved, saved_head = service.load_with_head()
    assert saved.events[:-1] == source.events
    assert saved_head == head + 1
    assert replay_events_through(saved.events, boundary).reserved_cash_yen == 0
    old = path.read_bytes()
    occurrence = date_args if date_only else ["--occurred-at", boundary.isoformat()]
    retry = [*base, *occurrence, "--status", "expired", "--out", str(tmp_path / "retry.yaml")]
    assert main(retry, now=boundary) == 0
    assert "status: no_change" in capsys.readouterr().out
    assert not (tmp_path / "retry.yaml").exists()
    assert path.read_bytes() == old
    assert main(args, now=boundary) == 2
    assert "stale ledger draft" in capsys.readouterr().err
    assert path.read_bytes() == old


@pytest.mark.parametrize(
    ("status", "expiry"), [("expired", None), ("cancelled", NOW + timedelta(hours=5))]
)
def test_date_only_terminal_report_does_not_infer_an_expiry(tmp_path, status, expiry):
    service, _ = _service(tmp_path)
    opened, _ = open_direct(service, expires_at=expiry)
    apply_draft(service, opened, human_confirmed=True)
    order = opened.replacement.events[-1]
    released, _ = build_broker_fact_draft(
        service,
        unavailable,
        decision_reference=order.decision_reference,
        status=status,
        occurred_on=NOW.date(),
        reservation_id=order.reservation_id,
        now=NOW,
    )
    assert released is not None
    release = released.replacement.events[-1]
    assert (release.reason, release.occurred_on) == (status, NOW.date())
    apply_draft(service, released, human_confirmed=True, now=NOW)
    assert not replay_events_through(service.load().events, NOW).active_reservations


def test_batch_date_only_expiry_waits_for_each_recorded_deadline(tmp_path):
    service, path = _service(tmp_path)
    reference = "manual-fact-00000000-0000-4000-8000-000000000009"
    reservation_ids = []
    for hours in (4, 5):
        opened, _ = open_direct(
            service,
            decision_reference=reference,
            quantity=100,
            order_id=f"order-{hours}",
            expires_at=NOW + timedelta(hours=hours),
        )
        apply_draft(service, opened, human_confirmed=True)
        reservation_ids.append(opened.replacement.events[-1].reservation_id)
    report = dict(
        decision_reference=reference,
        status="expired",
        occurred_on=NOW.date(),
        reservation_ids=tuple(reservation_ids),
    )
    boundary = NOW + timedelta(hours=5)
    reserved = path.read_bytes()
    with pytest.raises(BrokerFactRecordingError, match="date-only expired report"):
        build_broker_fact_draft(
            service, unavailable, **report, now=boundary - timedelta(microseconds=1)
        )
    assert path.read_bytes() == reserved
    released, ids = build_broker_fact_draft(
        service, unavailable, **report, now=boundary + timedelta(microseconds=1)
    )
    assert released is not None
    assert len(ids) == 2
    with pytest.raises(BrokerFactRecordingError, match="date-only expired report"):
        apply_draft(
            service, released, human_confirmed=True, now=boundary - timedelta(microseconds=1)
        )
    assert path.read_bytes() == reserved
    apply_draft(service, released, human_confirmed=True, now=boundary)
    assert service.load() == released.replacement
    state = replay_events_through(released.replacement.events, released.replacement.as_of)
    assert (state.reserved_cash_yen, state.available_cash_yen) == (0, 300_000)


@pytest.mark.parametrize("open_order_count", [1, 2])
def test_direct_fill_does_not_consume_unidentified_same_ticker_orders(tmp_path, open_order_count):
    service, _ = _service(tmp_path)
    for index in range(open_order_count):
        opened, _ = open_direct(
            service,
            quantity=100,
            price_guard_yen=Decimal("500"),
            order_id=f"unrelated-order-{index}",
        )
        assert opened is not None
        apply_draft(service, opened, human_confirmed=True)
    report = dict(
        decision_reference=None,
        direct_fact=True,
        status="filled",
        occurred_on=NOW.date(),
        ticker="1234",
        quantity=100,
        price_yen=Decimal("500"),
        sector="情報・通信業",
        execution_id="independent-confirmed-fill",
        now=NOW,
    )
    with pytest.raises(BrokerFactRecordingError, match="inconsistent sector/common_factors"):
        build_broker_fact_draft(service, unavailable, **(report | {"sector": "食料品"}))
    draft, _ = build_broker_fact_draft(service, unavailable, **report)
    assert draft is not None
    fill = draft.replacement.events[-1]
    assert isinstance(fill, ExecutionEvent)
    assert fill.reservation_id is None
    apply_draft(service, draft, human_confirmed=True)
    stored = service.load()
    state = replay_events_through(stored.events, stored.as_of)
    assert len(state.active_reservations) == open_order_count
    assert state.reserved_cash_yen == 50_000 * open_order_count
    assert state.available_cash_yen == 250_000 - 50_000 * open_order_count
    assert sum(lot.quantity for lot in state.lots["1234"]) == 100
    assert build_broker_fact_draft(service, unavailable, **report) == (None, ())
