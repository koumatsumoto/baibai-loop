from __future__ import annotations

from pathlib import Path

from baibai_loop.position.ledger import load_portfolio_ledger, reconcile_portfolio

ROOT = Path(__file__).parents[1]
LEDGER_PATH = ROOT / "records/04-position/portfolio-ledger.yaml"


def test_canonical_ledger_reconstructs_the_active_portfolio() -> None:
    snapshot = reconcile_portfolio(load_portfolio_ledger(LEDGER_PATH))

    assert (
        snapshot.available_cash_yen,
        snapshot.reserved_cash_yen,
        snapshot.deployed_cost_yen,
        snapshot.book_capital_yen,
    ) == (3_000_000, 224_000, 1_647_100, 4_871_100)
    assert {
        holding.ticker: (holding.quantity, holding.deployed_cost_yen)
        for holding in snapshot.holdings
    } == {
        "9682": (200, 202_000),
        "9692": (100, 195_300),
        "9470": (200, 196_400),
        "8255": (200, 203_000),
        "3539": (100, 127_500),
        "2749": (100, 59_000),
        "9534": (100, 74_200),
        "4432": (100, 234_000),
        "9715": (100, 355_700),
    }
    assert {
        reservation.ticker: (reservation.remaining_quantity, reservation.reserved_yen)
        for reservation in snapshot.active_reservations
    } == {"2331": (100, 105_000), "8929": (100, 119_000)}
