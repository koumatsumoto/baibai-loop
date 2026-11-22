"""資本確認工程でreplayと市場quote・権利単位を組み合わせ、現在の資本と未評価を見せる。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from baibai_engine.portfolio.holdings.market_observations import (
    UnadjustedCloseObservation,
    has_received_share_action,
    market_basis_is_confirmed,
    read_unadjusted_close,
    read_unadjusted_closes,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    HoldingEpisode,
    MarketPrice,
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    ShareConversionEvent,
    holding_episodes,
    replay_events_through,
    share_conversions,
    summarize_portfolio,
)
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def holding_episode(
    ledger: PortfolioLedgerDocument, ticker: str, now: datetime
) -> tuple[ExecutionEvent | None, datetime | None]:
    """Return the first buy of the open holding episode and its latest trade."""
    episodes = tuple(
        item
        for item in holding_episodes(
            tuple(event for event in ledger.events if event.occurred_at <= now)
        )
        if item.first_buy.ticker == ticker
    )
    episode = next((item for item in episodes if not item.closed), None)
    if episode is not None:
        return episode.first_buy, episode.last_trade.occurred_at
    return None, None if not episodes else episodes[-1].last_trade.occurred_at


class UnresolvedHoldingQuantityError(ValueError):
    """A received share action contradicts an apparently closed ledger holding."""


def require_closed_holding_basis(
    ledger: PortfolioLedgerDocument, *, now: datetime, connection: sqlite3.Connection
) -> None:
    """Do not let a reported full sale erase a known unresolved share-unit change."""
    events = tuple(event for event in ledger.events if event.occurred_at <= now)
    _require_closed_holding_basis(
        holding_episodes(events), share_conversions(events), connection=connection
    )


def _require_closed_holding_basis(
    episodes: tuple[HoldingEpisode, ...],
    conversions: tuple[ShareConversionEvent, ...],
    *,
    connection: sqlite3.Connection,
) -> None:
    for episode in episodes:
        if episode.closed and has_received_share_action(
            connection,
            ticker=episode.first_buy.ticker,
            after=episode.first_buy.occurred_at.astimezone(JST).date(),
            through=episode.last_trade.occurred_at.astimezone(JST).date(),
            conversions=conversions,
        ):
            raise UnresolvedHoldingQuantityError(
                f"{episode.first_buy.ticker}: 保有中の株数変更を照合できず、"
                "全売却後の数量を確認できません。"
                "分割・併合後の確認済み数量の記帳が必要です。"
            )


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
        return quote, holding_quote_basis_is_confirmed(
            ledger,
            quote=quote,
            ticker=ticker,
            sqlite_path=sqlite_path,
            now=now,
            connection=connection,
        )
    finally:
        if owns_connection:
            connection.close()


def holding_quote_basis_is_confirmed(
    ledger: PortfolioLedgerDocument,
    *,
    quote: UnadjustedCloseObservation | None,
    ticker: str,
    sqlite_path: Path,
    now: datetime,
    connection: sqlite3.Connection,
) -> bool:
    """Check held share units using a quote from the caller's read snapshot."""
    first_buy, last_trade = holding_episode(ledger, ticker, now)
    conversions = share_conversions(
        tuple(event for event in ledger.events if event.occurred_at <= now)
    )
    return _quote_matches_holding_basis(
        quote,
        ticker=ticker,
        sqlite_path=sqlite_path,
        now=now,
        connection=connection,
        first_buy=first_buy,
        last_trade=last_trade,
        conversions=conversions,
    )


def _quote_matches_holding_basis(
    quote: UnadjustedCloseObservation | None,
    *,
    ticker: str,
    sqlite_path: Path,
    now: datetime,
    connection: sqlite3.Connection,
    first_buy: ExecutionEvent | None,
    last_trade: datetime | None,
    conversions: tuple[ShareConversionEvent, ...],
) -> bool:
    return (
        first_buy is not None
        and last_trade is not None
        and quote is not None
        and not any(
            event.ticker == ticker and event.occurred_on > quote.price_as_of
            for event in conversions
        )
        and market_basis_is_confirmed(
            basis="shares",
            sqlite_path=sqlite_path,
            ticker=ticker,
            from_date=min(first_buy.occurred_at.astimezone(JST).date(), quote.price_as_of),
            through_date=max(last_trade.astimezone(JST).date(), quote.price_as_of),
            known_actions_through=now.astimezone(JST).date(),
            conversions=conversions,
            connection=connection,
        )
    )


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
    has_trades = any(
        isinstance(event, ExecutionEvent) and event.occurred_at <= now for event in ledger.events
    )
    if has_trades and sqlite_path.exists():
        with closing(connect_read_only(sqlite_path)) as connection:
            connection.execute("BEGIN")
            events = tuple(event for event in ledger.events if event.occurred_at <= now)
            episodes = holding_episodes(events)
            conversions = share_conversions(events)
            _require_closed_holding_basis(episodes, conversions, connection=connection)
            open_episodes = {item.first_buy.ticker: item for item in episodes if not item.closed}
            tickers = tuple(ticker for ticker, lots in state.lots.items() if lots)
            quotes = read_unadjusted_closes(
                sqlite_path=sqlite_path, tickers=tickers, at=now, connection=connection
            )
            for ticker in tickers:
                episode = open_episodes.get(ticker)
                quote = quotes.get(ticker)
                confirmed = _quote_matches_holding_basis(
                    quote,
                    ticker=ticker,
                    sqlite_path=sqlite_path,
                    now=now,
                    connection=connection,
                    first_buy=None if episode is None else episode.first_buy,
                    last_trade=None if episode is None else episode.last_trade.occurred_at,
                    conversions=conversions,
                )
                price = holding_market_price(
                    ticker=ticker, quote=quote, basis_confirmed=confirmed, now=now
                )
                if price is not None:
                    prices[ticker] = price
    return summarize_portfolio(ledger, state, prices)
