"""資本確認工程でreplayと市場quote・権利単位を組み合わせ、現在の資本と未評価を見せる。"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from pathlib import Path

from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import (
    ExecutionEvent,
    MarketPrice,
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.position.market_source import (
    UnadjustedCloseObservation,
    quantity_basis_is_confirmed,
    read_unadjusted_close,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY


def holding_quote(
    ledger: PortfolioLedgerDocument,
    *,
    ticker: str,
    sqlite_path: Path,
    now: datetime,
) -> tuple[UnadjustedCloseObservation | None, bool]:
    """Check the current holding episode's quantity units without requiring old closes."""
    quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=now)
    balance = 0
    acquired_on = now.astimezone(JST).date()
    quantity_as_of = acquired_on
    for event in ledger.events:
        if (
            isinstance(event, ExecutionEvent)
            and event.ticker == ticker
            and event.occurred_at <= now
        ):
            quantity_as_of = event.occurred_at.astimezone(JST).date()
            if event.side == "buy":
                if balance == 0:
                    acquired_on = event.occurred_at.astimezone(JST).date()
                balance += event.quantity
            else:
                balance -= event.quantity
    confirmed = (
        balance > 0
        and quote is not None
        and quantity_basis_is_confirmed(
            sqlite_path=sqlite_path,
            ticker=ticker,
            from_date=min(acquired_on, quote.price_as_of),
            through_date=max(quantity_as_of, quote.price_as_of),
        )
    )
    return quote, confirmed


def current_portfolio(
    ledger: PortfolioLedgerDocument,
    *,
    sqlite_path: Path,
    now: datetime,
) -> PortfolioSnapshot:
    """Compose one read-only valuation; unresolved quotes never erase capital facts."""
    state = replay_events_through(ledger.events, now)
    prices = {}
    for ticker, lots in state.lots.items():
        if not lots:
            continue
        quote, confirmed = holding_quote(ledger, ticker=ticker, sqlite_path=sqlite_path, now=now)
        if (
            quote is not None
            and confirmed
            and (now.astimezone(JST).date() - quote.price_as_of).days
            <= PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"]
        ):
            prices[ticker] = MarketPrice(
                ticker=ticker,
                price_yen=Decimal(str(quote.close_yen)),
                observed_at=datetime.combine(quote.price_as_of, time(15, 30), tzinfo=JST),
                source_kind="market_api",
                price_basis="unadjusted_close",
                source_ref="jquants_daily_bars",
            )
    return summarize_portfolio(ledger, state, prices)
