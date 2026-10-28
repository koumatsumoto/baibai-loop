"""資本確認工程でreplayと市場quote・権利単位を組み合わせ、現在の資本と未評価を見せる。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from baibai_engine.portfolio.holdings.market_observations import (
    UnadjustedCloseObservation,
    market_basis_is_confirmed,
    read_unadjusted_close,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    MarketPrice,
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.time import JST


def holding_episode(
    ledger: PortfolioLedgerDocument, ticker: str, now: datetime
) -> tuple[ExecutionEvent | None, datetime | None]:
    """Return the first buy of the open holding episode and its latest trade."""
    balance = 0
    first_buy: ExecutionEvent | None = None
    last_trade: datetime | None = None
    for event in ledger.events:
        if (
            not isinstance(event, ExecutionEvent)
            or event.ticker != ticker
            or event.occurred_at > now
        ):
            continue
        last_trade = event.occurred_at
        if event.side == "buy":
            if balance == 0:
                first_buy = event
            balance += event.quantity
        else:
            balance -= event.quantity
            if balance == 0:
                first_buy = None
    return first_buy, last_trade


def holding_quote(
    ledger: PortfolioLedgerDocument,
    *,
    ticker: str,
    sqlite_path: Path,
    now: datetime,
) -> tuple[UnadjustedCloseObservation | None, bool]:
    """Check the current holding episode's quantity units without requiring old closes."""
    quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=now)
    first_buy, last_trade = holding_episode(ledger, ticker, now)
    confirmed = (
        first_buy is not None
        and last_trade is not None
        and quote is not None
        and market_basis_is_confirmed(
            basis="shares",
            sqlite_path=sqlite_path,
            ticker=ticker,
            from_date=min(first_buy.occurred_at.astimezone(JST).date(), quote.price_as_of),
            through_date=max(last_trade.astimezone(JST).date(), quote.price_as_of),
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
                price_as_of=quote.price_as_of,
                source_kind="market_api",
                price_basis="unadjusted_close",
                source_ref="jquants_daily_bars",
            )
    return summarize_portfolio(ledger, state, prices)
