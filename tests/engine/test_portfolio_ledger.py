from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    reconcile_portfolio,
    snapshot_to_payload,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
TOKYO = ZoneInfo("Asia/Tokyo")


def _raw() -> dict[str, object]:
    raw = safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _document(raw: dict[str, object] | None = None) -> PortfolioLedgerDocument:
    return PortfolioLedgerDocument.model_validate(raw or _raw())


def test_representative_ledger_reconciles_every_required_event_to_one_yen() -> None:
    snapshot = reconcile_portfolio(load_portfolio_ledger(FIXTURE))

    assert snapshot.available_cash_yen == 10_080_500
    assert snapshot.reserved_cash_yen == 119_000
    assert snapshot.deployed_cost_yen == 203_000
    assert snapshot.holdings_market_value_yen == 220_000
    assert snapshot.confirmed_income_yen == 3_300
    assert snapshot.confirmed_cost_yen == 500
    assert snapshot.confirmed_tax_yen == 300
    assert snapshot.confirmed_cost_tax_yen == 800
    assert snapshot.book_capital_yen == 10_402_500
    assert snapshot.total_capital_yen == 10_419_500
    assert snapshot.estimated_exit_tax_rate_bps is None
    assert snapshot.estimated_exit_tax_yen is None
    assert snapshot.portfolio_scope == "repository_only"
    assert snapshot.warnings == ()

    assert len(snapshot.holdings) == 1
    holding = snapshot.holdings[0]
    assert holding.ticker == "2331"
    assert holding.quantity == 200
    assert holding.deployed_cost_yen == 203_000
    assert holding.market_price_source_kind == "test_fixture"
    assert holding.market_price_basis == "close"
    assert holding.market_price_source_ref == "offline-fixture:2331"
    assert len(snapshot.active_reservations) == 1
    reservation = snapshot.active_reservations[0]
    assert reservation.reservation_id == "reservation-8929-pending"
    assert reservation.order_id == "order-8929-pending"
    assert reservation.reserved_yen == 119_000


def test_pending_order_cannot_be_reserved_twice() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    duplicate = copy.deepcopy(events[2])
    duplicate["event_id"] = "reserve-2331-duplicate"
    duplicate["occurred_at"] = "2026-06-06T09:00:00+09:00"
    duplicate["expires_at"] = "2026-06-10T15:30:00+09:00"
    events.insert(5, duplicate)

    with pytest.raises(PortfolioLedgerError, match="reservation_id already used"):
        reconcile_portfolio(_document(raw))


def test_broker_order_cannot_be_reserved_under_a_second_reservation_id() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    duplicate = copy.deepcopy(events[2])
    duplicate["event_id"] = "reserve-2331-same-order"
    duplicate["reservation_id"] = "reservation-2331-same-order"
    duplicate["occurred_at"] = "2026-06-06T09:00:00+09:00"
    duplicate["expires_at"] = "2026-06-10T15:30:00+09:00"
    events.insert(5, duplicate)

    with pytest.raises(PortfolioLedgerError, match="order_id already reserved"):
        reconcile_portfolio(_document(raw))


def test_available_cash_shortage_is_a_hard_error() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    opening = events[0]
    contribution = events[1]
    assert isinstance(opening, dict)
    assert isinstance(contribution, dict)
    opening["amount_yen"] = 100_000
    contribution["amount_yen"] = 1

    with pytest.raises(PortfolioLedgerError, match="insufficient available cash"):
        reconcile_portfolio(_document(raw))


def test_withdrawal_reduces_available_cash_without_touching_reservations() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events.insert(
        2,
        {
            "event_id": "withdrawal-202606",
            "type": "withdrawal",
            "occurred_at": "2026-06-01T09:00:00+09:00",
            "amount_yen": 100_000,
        },
    )

    snapshot = reconcile_portfolio(_document(raw))

    assert snapshot.available_cash_yen == 9_980_500
    assert snapshot.reserved_cash_yen == 119_000


def test_withdrawal_cannot_exceed_available_cash() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events.insert(
        1,
        {
            "event_id": "withdrawal-too-large",
            "type": "withdrawal",
            "occurred_at": "2026-05-01T09:00:01+09:00",
            "amount_yen": 10_000_001,
        },
    )

    with pytest.raises(PortfolioLedgerError, match="insufficient available cash"):
        reconcile_portfolio(_document(raw))


def test_expired_reservation_requires_an_explicit_release() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[:] = [event for event in events if event["event_id"] != "release-2331-expired"]

    with pytest.raises(PortfolioLedgerError, match="expired reservations require"):
        reconcile_portfolio(_document(raw))


def test_buy_execution_cannot_happen_at_expiry() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[3]["occurred_at"] = "2026-06-05T15:30:00+09:00"

    with pytest.raises(PortfolioLedgerError, match="cannot occur at or after expires_at"):
        reconcile_portfolio(_document(raw))


def test_release_after_expiry_requires_expired_reason() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[4]["reason"] = "cancelled"

    with pytest.raises(PortfolioLedgerError, match="must use expired reason"):
        reconcile_portfolio(_document(raw))


def test_reservation_quantity_must_match_board_lot() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[2]["quantity"] = 150

    with pytest.raises(PortfolioLedgerError, match="multiple of board_lot"):
        reconcile_portfolio(_document(raw))


def test_partial_execution_quantity_must_match_board_lot() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[3]["quantity"] = 50

    with pytest.raises(PortfolioLedgerError, match="execution quantity must be a multiple"):
        reconcile_portfolio(_document(raw))


def test_sell_cannot_exceed_repository_holding() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events.append(
        {
            "event_id": "sell-too-many",
            "type": "execution",
            "occurred_at": "2026-07-10T09:00:00+09:00",
            "execution_id": "execution-sell-too-many",
            "reservation_id": None,
            "ticker": "2331",
            "side": "sell",
            "quantity": 300,
            "price_yen": 1100,
        }
    )

    with pytest.raises(PortfolioLedgerError, match="sell quantity exceeds repository holding"):
        reconcile_portfolio(_document(raw))


def test_future_exit_tax_is_separate_and_configurable() -> None:
    raw = _raw()
    raw["estimated_exit_tax_rate_bps"] = 2031
    raw["estimated_exit_tax_basis"] = "ledger_fifo_gross_unrealized_gain"

    snapshot = reconcile_portfolio(_document(raw))

    assert snapshot.confirmed_cost_tax_yen == 800
    assert snapshot.estimated_exit_tax_rate_bps == 2031
    assert snapshot.estimated_exit_tax_yen == 3_452


def test_current_and_reserved_exposure_emit_expiring_override_warnings() -> None:
    raw = _raw()
    raw["overrides"] = [
        {
            "override_id": "override-2331-concentration",
            "scope": "ticker",
            "key": "2331",
            "reason": "Small-lot accumulation remains within permanent-loss budget.",
            "decision_reference": "decision-20260711-2331",
            "approved_at": "2026-07-01T09:00:00+09:00",
            "expires_at": "2026-07-20T15:30:00+09:00",
        }
    ]
    policy = copy.deepcopy(PORTFOLIO_POLICY)
    risk = policy["risk_budget"]
    assert isinstance(risk, dict)
    risk["max_ticker_concentration_pct"] = 1.0

    snapshot = reconcile_portfolio(_document(raw), policy=policy)

    warnings = {warning.key: warning for warning in snapshot.warnings}
    assert set(warnings) == {"2331", "8929"}
    assert warnings["2331"].overridden is True
    assert warnings["2331"].override_id == "override-2331-concentration"
    assert warnings["8929"].overridden is False
    assert warnings["8929"].actual_pct == 1.14


def test_ticker_sector_and_common_factor_include_active_reservation() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    pending = events[-1]
    assert isinstance(pending, dict)
    pending["ticker"] = "2331"
    pending["sector"] = "サービス業"
    pending["common_factors"] = ["labor-automation"]
    policy = copy.deepcopy(PORTFOLIO_POLICY)
    risk = policy["risk_budget"]
    assert isinstance(risk, dict)
    risk["max_ticker_concentration_pct"] = 3.0
    risk["max_sector_concentration_pct"] = 3.0
    risk["max_common_factor_concentration_pct"] = 3.0

    snapshot = reconcile_portfolio(_document(raw), policy=policy)

    warnings = {(warning.scope, warning.key): warning for warning in snapshot.warnings}
    assert warnings[("ticker", "2331")].actual_pct == 3.25
    assert warnings[("sector", "サービス業")].actual_pct == 3.25
    assert warnings[("common_factor", "labor-automation")].actual_pct == 3.25


def test_ticker_concentration_at_ten_percent_does_not_warn() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    opening = events[0]
    pending = events[-1]
    assert isinstance(opening, dict)
    assert isinstance(pending, dict)
    opening["amount_yen"] = 10_000_500
    pending.update(
        {
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": ["labor-automation"],
            "price_guard_yen": 8_220,
        }
    )

    snapshot = reconcile_portfolio(_document(raw))

    assert snapshot.total_capital_yen == 10_420_000
    assert snapshot.holdings[0].market_value_yen == 220_000
    assert snapshot.active_reservations[0].reserved_yen == 822_000
    assert not any(
        warning.code == "portfolio.ticker-concentration" for warning in snapshot.warnings
    )


def test_ticker_concentration_above_ten_percent_warns_with_active_reservation() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    opening = events[0]
    pending = events[-1]
    assert isinstance(opening, dict)
    assert isinstance(pending, dict)
    opening["amount_yen"] = 10_000_500
    pending.update(
        {
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": ["labor-automation"],
            "price_guard_yen": 8_221,
        }
    )

    snapshot = reconcile_portfolio(_document(raw))

    warning = next(
        item for item in snapshot.warnings if item.code == "portfolio.ticker-concentration"
    )
    assert warning.key == "2331"
    assert warning.actual_pct == 10.0
    assert warning.warning_pct == 10.0
    assert warning.overridden is False


def test_dry_powder_is_a_warning_instead_of_a_cash_error() -> None:
    policy = copy.deepcopy(PORTFOLIO_POLICY)
    cash = policy["cash_management"]
    assert isinstance(cash, dict)
    cash["dry_powder_warning_pct"] = 99.0

    snapshot = reconcile_portfolio(_document(), policy=policy)

    assert [warning.code for warning in snapshot.warnings] == ["portfolio.dry-powder"]
    assert snapshot.warnings[0].overridden is False


def test_snapshot_payload_is_deterministic_and_yaml_safe() -> None:
    payload = snapshot_to_payload(reconcile_portfolio(_document()))
    dumped = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    assert yaml.safe_load(dumped) == payload
    assert payload["portfolio_scope"] == "repository_only"


def test_fractional_unit_price_is_exact_when_notional_is_whole_yen() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[2]["price_guard_yen"] = 1052.5
    events[3]["price_yen"] = 1052.5

    snapshot = reconcile_portfolio(_document(raw))

    assert snapshot.deployed_cost_yen == 204_250


def test_fractional_unit_price_never_rounds_a_sub_yen_notional() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[2]["quantity"] = 1
    events[2]["price_guard_yen"] = 1052.5
    policy = copy.deepcopy(PORTFOLIO_POLICY)
    order_constraints = policy["order_constraints"]
    assert isinstance(order_constraints, dict)
    order_constraints["board_lot"] = 1

    with pytest.raises(PortfolioLedgerError, match="must reconcile to whole yen"):
        reconcile_portfolio(_document(raw), policy=policy)


def test_stale_market_price_is_a_hard_error() -> None:
    raw = _raw()
    prices = raw["market_prices"]
    assert isinstance(prices, list)
    prices[0]["observed_at"] = "2026-07-01T15:00:00+09:00"

    with pytest.raises(PortfolioLedgerError, match="market price for 2331 is stale"):
        reconcile_portfolio(_document(raw))


@pytest.mark.property
@given(
    contribution=st.integers(min_value=1, max_value=400_000),
    lots=st.integers(min_value=1, max_value=10),
    filled=st.integers(min_value=0, max_value=1_000),
    guard=st.integers(min_value=1, max_value=20_000),
    discount=st.integers(min_value=0, max_value=500),
)
def test_reservation_partial_fill_release_preserves_book_capital(
    contribution: int,
    lots: int,
    filled: int,
    guard: int,
    discount: int,
) -> None:
    quantity = lots * 100
    filled = min((filled // 100) * 100, quantity)
    execution_price = max(1, guard - min(discount, guard - 1))
    opening = quantity * guard + 1_000
    events: list[dict[str, object]] = [
        {
            "event_id": "opening",
            "type": "opening_balance",
            "occurred_at": "2026-01-01T09:00:00+09:00",
            "amount_yen": opening,
        },
        {
            "event_id": "contribution",
            "type": "contribution",
            "occurred_at": "2026-01-02T09:00:00+09:00",
            "amount_yen": contribution,
        },
        {
            "event_id": "reservation",
            "type": "reservation",
            "occurred_at": "2026-01-03T09:00:00+09:00",
            "reservation_id": "reservation",
            "order_id": "order",
            "ticker": "2331",
            "sector": "サービス業",
            "common_factors": [],
            "quantity": quantity,
            "price_guard_yen": guard,
            "expires_at": "2026-01-05T15:30:00+09:00",
        },
    ]
    if filled:
        events.append(
            {
                "event_id": "execution",
                "type": "execution",
                "occurred_at": "2026-01-04T09:00:00+09:00",
                "execution_id": "execution",
                "reservation_id": "reservation",
                "ticker": "2331",
                "side": "buy",
                "quantity": filled,
                "price_yen": execution_price,
            }
        )
    if filled < quantity:
        events.append(
            {
                "event_id": "release",
                "type": "release",
                "occurred_at": "2026-01-05T15:30:00+09:00",
                "reservation_id": "reservation",
                "reason": "expired",
            }
        )
    raw: dict[str, object] = {
        "schema_version": 2,
        "portfolio_scope": "repository_only",
        "as_of": "2026-01-06T15:30:00+09:00",
        "estimated_exit_tax_rate_bps": None,
        "estimated_exit_tax_basis": None,
        "events": events,
        "market_prices": (
            [
                {
                    "ticker": "2331",
                    "price_yen": execution_price,
                    "observed_at": "2026-01-06T15:00:00+09:00",
                    "source_kind": "test_fixture",
                    "price_basis": "close",
                    "source_ref": "property-fixture",
                }
            ]
            if filled
            else []
        ),
        "overrides": [],
    }

    snapshot = reconcile_portfolio(_document(raw))
    deployed = filled * execution_price

    assert snapshot.reserved_cash_yen == 0
    assert snapshot.deployed_cost_yen == deployed
    assert snapshot.available_cash_yen == opening + contribution - deployed
    assert snapshot.book_capital_yen == opening + contribution
    assert snapshot.holdings_market_value_yen == deployed


def test_loader_rejects_untyped_float_yen(tmp_path: Path) -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[0]["amount_yen"] = 10_000_000.0
    path = tmp_path / "portfolio-ledger.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(PortfolioLedgerError, match="valid integer"):
        load_portfolio_ledger(path)


def test_runtime_rejects_scientific_price_string_like_public_schema() -> None:
    raw = _raw()
    events = raw["events"]
    assert isinstance(events, list)
    events[2]["price_guard_yen"] = "1e3"

    with pytest.raises(ValueError, match="fixed-point decimal notation"):
        _document(raw)


def test_override_cannot_outlive_policy_window() -> None:
    raw = _raw()
    raw["overrides"] = [
        {
            "override_id": "too-long",
            "scope": "dry_powder",
            "key": "portfolio",
            "reason": "Temporary exception.",
            "decision_reference": "decision-1",
            "approved_at": datetime(2026, 1, 1, tzinfo=TOKYO).isoformat(),
            "expires_at": datetime(2026, 3, 1, tzinfo=TOKYO).isoformat(),
        }
    ]

    with pytest.raises(PortfolioLedgerError, match="exceeds 31 days"):
        reconcile_portfolio(_document(raw))


def test_override_approval_cannot_be_in_the_future() -> None:
    raw = _raw()
    raw["overrides"] = [
        {
            "override_id": "future-override",
            "scope": "ticker",
            "key": "2331",
            "reason": "Future information cannot affect the snapshot.",
            "decision_reference": "decision-future",
            "approved_at": "2026-07-12T09:00:00+09:00",
            "expires_at": "2026-07-20T09:00:00+09:00",
        }
    ]

    with pytest.raises(ValueError, match="override approval cannot occur after as_of"):
        _document(raw)
