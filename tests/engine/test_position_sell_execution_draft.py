from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger, portfolio_snapshot

from baibai_engine.position.cli import main
from baibai_engine.position.drafts import (
    apply_draft,
    build_sell_execution_draft,
    load_draft,
)
from baibai_engine.position.ledger import (
    PortfolioLedgerError,
)
from baibai_engine.position.store import LedgerStoreService

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
JST = ZoneInfo("Asia/Tokyo")
OCCURRED_AT = datetime(2026, 7, 12, 10, 0, tzinfo=JST)
DECISION_REF = "position-review-20260711-2331-position-2331"


def _seed(db: Path) -> LedgerStoreService:
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
    return LedgerStoreService(db)


def _holding(service: LedgerStoreService, ticker: str) -> object:
    return next(
        item for item in portfolio_snapshot(service.load()).holdings if item.ticker == ticker
    )


def test_sell_over_current_holding_is_rejected_fail_closed(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="exceeds repository holding for 2331"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=300,
            price_yen=Decimal("1100"),
        )


def test_sell_draft_apply_reduces_holding_and_realizes_fifo_pnl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    before = portfolio_snapshot(service.load())
    before_cash = before.available_cash_yen
    before_holding = _holding(service, "2331")
    assert before_holding.quantity == 200
    assert before_holding.deployed_cost_yen == 203_000

    assert (
        main(
            [
                "sell-execution-draft",
                "--root",
                str(tmp_path),
                "--db",
                str(db),
                "--ticker",
                "2331",
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--occurred-at",
                OCCURRED_AT.isoformat(),
                "--decision-reference",
                DECISION_REF,
                "--out",
                "sell-draft.yaml",
            ]
        )
        == 0
    )
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["status"] == "draft_created"
    assert output["ticker"] == "2331"

    draft_path = tmp_path / "sell-draft.yaml"
    draft = load_draft(draft_path)
    assert draft.kind == "sell-execution"
    # Draft generation never touches the canonical DB.
    assert draft.expected_head == service.append_head()
    assert _holding(service, "2331").quantity == 200

    assert main(["apply-draft", str(draft_path), "--db", str(db), "--confirmed"]) == 0

    after_document = service.load()
    after = portfolio_snapshot(after_document)
    after_holding = _holding(service, "2331")

    proceeds = 100 * 1100
    consumed_cost = before_holding.deployed_cost_yen - after_holding.deployed_cost_yen
    realized_gross_pnl = proceeds - consumed_cost

    assert after_holding.quantity == 100
    # FIFO consumes the oldest 100@1040 lot, leaving 100@990.
    assert after_holding.deployed_cost_yen == 99_000
    assert consumed_cost == 104_000
    assert realized_gross_pnl == 6_000
    assert after.available_cash_yen == before_cash + proceeds

    sell = next(
        event
        for event in after_document.events
        if event.type == "execution" and event.event_id.startswith("human-sell-")
    )
    assert sell.side == "sell"
    assert sell.reservation_id is None
    assert sell.decision_reference == DECISION_REF


def test_sell_quantity_not_multiple_of_board_lot_is_rejected(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="multiple of board_lot"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=150,
            price_yen=Decimal("1100"),
        )


def test_sell_fees_and_tax_over_proceeds_and_cash_are_rejected(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    with pytest.raises(PortfolioLedgerError, match="insufficient available cash"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1100"),
            fees_yen=11_000_000,
        )


def test_identical_sell_parameters_after_apply_are_rejected_as_duplicate(tmp_path: Path) -> None:
    service = _seed(tmp_path / "app.sqlite")
    first = build_sell_execution_draft(
        service,
        occurred_at=OCCURRED_AT,
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
    )
    apply_draft(service, first, human_confirmed=True)
    with pytest.raises(ValueError, match="event_id must be unique"):
        build_sell_execution_draft(
            service,
            occurred_at=OCCURRED_AT,
            ticker="2331",
            quantity=100,
            price_yen=Decimal("1100"),
        )


def test_cli_rejects_zero_quantity_zero_price_and_negative_fees(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    _seed(db)
    base = [
        "sell-execution-draft",
        "--root",
        str(tmp_path),
        "--db",
        str(db),
        "--ticker",
        "2331",
        "--occurred-at",
        OCCURRED_AT.isoformat(),
    ]
    assert main([*base, "--quantity", "0", "--price-yen", "1100", "--out", "q0.yaml"]) == 2
    with pytest.raises(SystemExit) as excinfo:
        main([*base, "--quantity", "100", "--price-yen", "0", "--out", "p0.yaml"])
    assert excinfo.value.code == 2
    assert (
        main(
            [
                *base,
                "--quantity",
                "100",
                "--price-yen",
                "1100",
                "--fees-yen",
                "-500",
                "--out",
                "f.yaml",
            ]
        )
        == 2
    )


def test_sell_draft_records_fees_and_tax_as_cost_and_tax_events(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    service = _seed(db)
    before = portfolio_snapshot(service.load())

    draft = build_sell_execution_draft(
        service,
        occurred_at=OCCURRED_AT,
        ticker="2331",
        quantity=100,
        price_yen=Decimal("1100"),
        fees_yen=500,
        tax_yen=900,
        decision_reference=DECISION_REF,
    )
    apply_draft(service, draft, human_confirmed=True)

    after_document = service.load()
    after = portfolio_snapshot(after_document)

    assert after.confirmed_cost_yen == before.confirmed_cost_yen + 500
    assert after.confirmed_tax_yen == before.confirmed_tax_yen + 900
    assert after.available_cash_yen == before.available_cash_yen + 100 * 1100 - 500 - 900

    fee = next(
        event for event in after_document.events if event.event_id.startswith("human-sell-fee-")
    )
    tax = next(
        event for event in after_document.events if event.event_id.startswith("human-sell-tax-")
    )
    assert fee.type == "cost"
    assert fee.cost_kind == "commission"
    assert tax.type == "tax_confirmed"
    assert tax.tax_kind == "capital_gain"
