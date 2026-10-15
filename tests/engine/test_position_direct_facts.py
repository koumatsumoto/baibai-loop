from datetime import timedelta
from decimal import Decimal

import pytest
import yaml
from tests.engine.test_position_decision_references import NOW, _binding_count, _service

from baibai_engine.position.broker_fact_recording import BrokerFactRecordingError
from baibai_engine.position.cli import main
from baibai_engine.position.decision_references import resolve_buy_reference
from baibai_engine.position.drafts import apply_draft, build_broker_fact_draft, load_draft
from baibai_engine.position.ledger import ExecutionEvent, ReservationEvent, replay_events_through


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
    assert service.append_head() == 1
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
    assert service.append_head() == 1
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
    assert service.append_head() == 1
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
    assert open_direct(
        service, occurred_at=None, occurred_on=NOW.date(), expires_at=None
    ) == (None, ())
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
