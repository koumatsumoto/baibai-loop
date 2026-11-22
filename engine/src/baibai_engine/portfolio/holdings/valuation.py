"""資本確認工程でreplayと市場quote・権利単位を組み合わせ、現在の資本と未評価を見せる。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

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


@dataclass(frozen=True, slots=True)
class HoldingHistory:
    """Holding episodes and recorded share changes for one ledger cutoff."""

    as_of: datetime
    open_by_ticker: Mapping[str, HoldingEpisode]
    closed_episodes: tuple[HoldingEpisode, ...]
    conversions_by_ticker: Mapping[str, tuple[ShareConversionEvent, ...]]


def prepare_holding_history(ledger: PortfolioLedgerDocument, *, now: datetime) -> HoldingHistory:
    """Derive the same holding intervals once for all consumers of this read."""
    events = tuple(event for event in ledger.events if event.occurred_at <= now)
    opened: dict[str, HoldingEpisode] = {}
    closed: list[HoldingEpisode] = []
    conversions: dict[str, list[ShareConversionEvent]] = defaultdict(list)
    for episode in holding_episodes(events):
        if episode.closed:
            closed.append(episode)
        else:
            opened[episode.first_buy.ticker] = episode
    for conversion in share_conversions(events):
        conversions[conversion.ticker].append(conversion)
    return HoldingHistory(
        as_of=now,
        open_by_ticker=MappingProxyType(opened),
        closed_episodes=tuple(closed),
        conversions_by_ticker=MappingProxyType(
            {ticker: tuple(items) for ticker, items in conversions.items()}
        ),
    )


def holding_episode(
    ledger: PortfolioLedgerDocument, ticker: str, now: datetime
) -> tuple[ExecutionEvent | None, datetime | None]:
    """Return the first buy of the open episode and the most recent trade."""
    history = prepare_holding_history(ledger, now=now)
    episode = history.open_by_ticker.get(ticker)
    if episode is not None:
        return episode.first_buy, episode.last_trade.occurred_at
    return None, next(
        (
            item.last_trade.occurred_at
            for item in reversed(history.closed_episodes)
            if item.first_buy.ticker == ticker
        ),
        None,
    )


class UnresolvedHoldingQuantityError(ValueError):
    """A received share action contradicts an apparently closed ledger holding."""


def require_closed_holding_basis(
    history: HoldingHistory, *, connection: sqlite3.Connection
) -> None:
    """Do not let a reported full sale erase a known unresolved share-unit change."""
    for episode in history.closed_episodes:
        ticker = episode.first_buy.ticker
        if has_received_share_action(
            connection,
            ticker=ticker,
            after=episode.first_buy.occurred_at.astimezone(JST).date(),
            through=episode.last_trade.occurred_at.astimezone(JST).date(),
            conversions=history.conversions_by_ticker.get(ticker, ()),
        ):
            raise UnresolvedHoldingQuantityError(
                f"{ticker}: 保有中の株数変更を照合できず、"
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
            prepare_holding_history(ledger, now=now),
            quote=quote,
            ticker=ticker,
            sqlite_path=sqlite_path,
            connection=connection,
        )
    finally:
        if owns_connection:
            connection.close()


def holding_quote_basis_is_confirmed(
    history: HoldingHistory,
    *,
    quote: UnadjustedCloseObservation | None,
    ticker: str,
    sqlite_path: Path,
    connection: sqlite3.Connection,
) -> bool:
    """Check held share units using a quote from the caller's read snapshot."""
    episode = history.open_by_ticker.get(ticker)
    conversions = history.conversions_by_ticker.get(ticker, ())
    return (
        episode is not None
        and quote is not None
        and not any(event.occurred_on > quote.price_as_of for event in conversions)
        and market_basis_is_confirmed(
            basis="shares",
            sqlite_path=sqlite_path,
            ticker=ticker,
            from_date=min(episode.first_buy.occurred_at.astimezone(JST).date(), quote.price_as_of),
            through_date=max(
                episode.last_trade.occurred_at.astimezone(JST).date(), quote.price_as_of
            ),
            known_actions_through=history.as_of.astimezone(JST).date(),
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
            history = prepare_holding_history(ledger, now=now)
            require_closed_holding_basis(history, connection=connection)
            tickers = tuple(ticker for ticker, lots in state.lots.items() if lots)
            quotes = read_unadjusted_closes(
                sqlite_path=sqlite_path, tickers=tickers, at=now, connection=connection
            )
            for ticker in tickers:
                quote = quotes.get(ticker)
                confirmed = holding_quote_basis_is_confirmed(
                    history,
                    quote=quote,
                    ticker=ticker,
                    sqlite_path=sqlite_path,
                    connection=connection,
                )
                price = holding_market_price(
                    ticker=ticker, quote=quote, basis_confirmed=confirmed, now=now
                )
                if price is not None:
                    prices[ticker] = price
    return summarize_portfolio(ledger, state, prices)
