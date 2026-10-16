from datetime import datetime

import pytest

from baibai_web.readmodel.builders import build_dashboard


@pytest.mark.parametrize(
    ("expires", "label", "expected"),
    [
        (None, None, None),
        ("2026-10-17T00:00:00+09:00", None, "2026-10-16"),
        ("2026-10-16T15:00:00+00:00", None, "2026-10-16"),
        ("2026-10-17T00:00:00.000001+09:00", None, "2026-10-17"),
        ("2026-10-16T15:30:00+09:00", None, "2026-10-16"),
        ("2026-10-17T00:00:00+09:00", "今週中", "今週中"),
    ],
)
def test_dashboard_labels_the_same_last_valid_order_day_as_the_calendar(mocker, expires, label, expected):
    expires_at = datetime.fromisoformat(expires) if expires is not None else None
    now = datetime.fromisoformat("2026-10-16T09:00:00+09:00")
    reservation = mocker.Mock(
        reservation_id="r", side="buy", ticker="2222", sector="test",
        remaining_quantity=100, price_guard_yen=1000, reserved_yen=100000,
        expires_at=expires_at, expiry_label=label, route_label=None,
    )
    ledger, research, screening, market = [mocker.Mock() for _ in range(4)]
    ledger.exists.return_value = True
    ledger.snapshot.return_value = mocker.Mock(
        holdings=[], active_reservations=[reservation], warnings=[],
        as_of=now, ledger_as_of=now, holdings_market_value_yen=0,
        available_cash_yen=900000, reserved_cash_yen=100000,
        total_capital_yen=1000000, deployed_cost_yen=0, realized_gross_pnl_yen=0,
    )
    research.revisions.return_value = []
    screening.latest_run.return_value = None
    market.next_earnings_dates.return_value = {}
    view = build_dashboard(ledger, research, screening, market)
    assert view.reservations[0].expiry_label == expected
    assert view.reservations[0].expires_at == expires_at
    assert reservation.expiry_label == label
