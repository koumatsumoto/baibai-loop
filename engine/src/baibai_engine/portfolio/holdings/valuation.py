"""資本確認工程でreplayと市場quote・権利単位を組み合わせ、現在の資本と未評価を見せる。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
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
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


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
    connection: sqlite3.Connection | None = None,
) -> tuple[UnadjustedCloseObservation | None, bool]:
    """Check current holding units without requiring old closes.

    A supplied connection and its read transaction remain owned by the caller.
    """
    owns_connection = connection is None
    if connection is None:
        if not sqlite_path.exists():
            return None, False
        connection = connect_read_only(sqlite_path)
    try:
        if owns_connection:
            connection.execute("BEGIN")
        quote = read_unadjusted_close(
            sqlite_path=sqlite_path, ticker=ticker, at=now, connection=connection
        )
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
                known_actions_through=now.astimezone(JST).date(),
                connection=connection,
            )
        )
        return quote, confirmed
    finally:
        if owns_connection:
            connection.close()


def holding_market_price(
    *,
    ticker: str,
    quote: UnadjustedCloseObservation | None,
    basis_confirmed: bool,
    now: datetime,
) -> MarketPrice | None:
    """Only a fresh close on confirmed share units can value current holdings."""
    if (
        quote is None
        or not basis_confirmed
        or not 0
        <= (now.astimezone(JST).date() - quote.price_as_of).days
        <= PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"]
    ):
        return None
    return MarketPrice(
        ticker=ticker,
        price_yen=Decimal(str(quote.close_yen)),
        price_as_of=quote.price_as_of,
        source_kind="market_api",
        price_basis="unadjusted_close",
        source_ref="jquants_daily_bars",
    )


def current_portfolio(
    ledger: PortfolioLedgerDocument,
    *,
    sqlite_path: Path,
    now: datetime,
) -> PortfolioSnapshot:
    """Value all holdings from one market snapshot without erasing unresolved capital facts."""
    state = replay_events_through(ledger.events, now)
    prices: dict[str, MarketPrice] = {}
    if any(state.lots.values()) and sqlite_path.exists():
        with closing(connect_read_only(sqlite_path)) as connection:
            connection.execute("BEGIN")
            for ticker, lots in state.lots.items():
                if not lots:
                    continue
                quote, confirmed = holding_quote(
                    ledger, ticker=ticker, sqlite_path=sqlite_path, now=now, connection=connection
                )
                price = holding_market_price(
                    ticker=ticker, quote=quote, basis_confirmed=confirmed, now=now
                )
                if price is not None:
                    prices[ticker] = price
    return summarize_portfolio(ledger, state, prices)
