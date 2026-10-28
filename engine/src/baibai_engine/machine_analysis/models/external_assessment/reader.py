"""Cutoff reads from a current store; absent optional lake datasets hydrate empty."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.machine_analysis.models.external_assessment.observations.contract import (
    ANALYST_FORECAST_COLUMNS,
    ANALYST_FORECAST_TABLE,
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
    Column,
)
from baibai_engine.machine_analysis.models.external_assessment.observations.models import (
    AnalystForecastObservation,
    AnalystRatingObservation,
    ModelValuationObservation,
    QuantsScoreObservation,
    ReceivedObservation,
    utc_datetime,
)
from baibai_engine.machine_analysis.models.external_assessment.tradingview.contract import (
    COLUMNS as TV_COLUMNS,
)
from baibai_engine.machine_analysis.models.external_assessment.tradingview.contract import (
    TABLE as TV_TABLE,
)
from baibai_engine.storage.observations.read import connect_read_only

from baibai_engine.machine_analysis.models.external_assessment.models import (
    AnalystForecastHistory,
    AnalystRatingHistory,
    ModelValuationHistory,
    ObservationHistory,
    QuantsScoreHistory,
    TradingViewHistory,
    TradingViewObservation,
)


def _latest_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[Column, ...],
    ticker: str | None,
    cutoff: datetime,
) -> list[sqlite3.Row]:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ):
        return []
    names = ",".join(column[0] for column in columns)
    # Identifiers come only from the two leaf contracts; caller values are bound.
    return conn.execute(
        f"SELECT {names} FROM (SELECT {names}, "  # nosec B608
        "row_number() OVER (PARTITION BY ticker, distributor ORDER BY fetched_at_utc DESC) "
        "AS receive_rank "
        f"FROM {table} WHERE (? IS NULL OR ticker=?) AND fetched_at_utc<=?) "
        "WHERE receive_rank<=2 ORDER BY distributor, fetched_at_utc DESC",
        (ticker, ticker, cutoff.isoformat(timespec="microseconds")),
    ).fetchall()


def _pairs[T: ReceivedObservation](rows: list[T]) -> list[tuple[T, T | None]]:
    groups: dict[str, list[T]] = {}
    for row in rows:
        groups.setdefault(str(row.distributor), []).append(row)
    return [(members[0], members[1] if len(members) > 1 else None) for members in groups.values()]


def tradingview_rows_at_cutoff(
    conn: sqlite3.Connection,
    cutoff: datetime,
    *,
    ticker: str | None = None,
) -> list[sqlite3.Row]:
    """Read saved TradingView values at the exact received-time cutoff."""
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TV_TABLE,)
        ).fetchone()
        is None
    ):
        return []
    names = ",".join(column[0] for column in TV_COLUMNS)
    day = cutoff.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    predicate = "snapshot_date<=?" + (" AND ticker=?" if ticker is not None else "")
    # Writers normalize received time to UTC with optional fractional seconds.
    # Parse it here so single-company and population reads agree exactly,
    # also for equivalent aware ISO representations in saved input.
    rows = conn.execute(
        f"SELECT {names} FROM {TV_TABLE} WHERE {predicate}",  # nosec B608
        (day, ticker) if ticker is not None else (day,),
    ).fetchall()
    return [
        row for row in rows if utc_datetime(datetime.fromisoformat(row["fetched_at_utc"])) <= cutoff
    ]


def read_observations(sqlite_path: Path, ticker: str, cutoff: datetime) -> ObservationHistory:
    """Read newest and previous received rows per distributor, including normal nulls."""
    ticker = normalize_ticker(ticker)
    return read_observation_population(sqlite_path, tickers=(ticker,), cutoff=cutoff)[ticker]


def read_observation_population(
    sqlite_path: Path, *, tickers: Sequence[str], cutoff: datetime
) -> dict[str, ObservationHistory]:
    """同一read transactionで必要企業の履歴を読む。全社生成で接続を反復しない。"""
    requested = sorted({normalize_ticker(ticker) for ticker in tickers})
    cutoff = utc_datetime(cutoff)
    if not requested or not sqlite_path.exists():
        return {ticker: ObservationHistory(ticker=ticker, cutoff=cutoff) for ticker in requested}
    selected = set(requested)
    ticker = requested[0] if len(requested) == 1 else None
    conn = connect_read_only(sqlite_path)
    try:
        conn.execute("BEGIN")
        valuation = [
            ModelValuationObservation.model_validate(dict(row))
            for row in _latest_rows(
                conn, MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS, ticker, cutoff
            )
            if row["ticker"] in selected
        ]
        rating = [
            AnalystRatingObservation.model_validate(dict(row))
            for row in _latest_rows(
                conn, ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS, ticker, cutoff
            )
            if row["ticker"] in selected
        ]
        quants = [
            QuantsScoreObservation.model_validate(dict(row))
            for row in _latest_rows(conn, QUANTS_SCORE_TABLE, QUANTS_SCORE_COLUMNS, ticker, cutoff)
            if row["ticker"] in selected
        ]
        minkabu = [
            AnalystForecastObservation.model_validate(dict(row))
            for row in _latest_rows(
                conn, ANALYST_FORECAST_TABLE, ANALYST_FORECAST_COLUMNS, ticker, cutoff
            )
            if row["ticker"] in selected
        ]
        tradingview = [
            TradingViewObservation.model_validate(dict(row))
            for row in tradingview_rows_at_cutoff(conn, cutoff, ticker=ticker)
            if row["ticker"] in selected
        ]
    finally:
        conn.close()
    valuations = _group_by_ticker(valuation)
    ratings = _group_by_ticker(rating)
    quant_groups = _group_by_ticker(quants)
    forecasts = _group_by_ticker(minkabu)
    tradingviews = _group_by_ticker(tradingview)
    return {
        code: _history(
            code,
            cutoff,
            valuation=valuations.get(code, []),
            rating=ratings.get(code, []),
            quants=quant_groups.get(code, []),
            minkabu=forecasts.get(code, []),
            tradingview=sorted(
                tradingviews.get(code, []), key=lambda row: row.fetched_at_utc, reverse=True
            )[:2],
        )
        for code in requested
    }


def _group_by_ticker[T: ReceivedObservation | TradingViewObservation](
    rows: list[T],
) -> dict[str, list[T]]:
    groups: dict[str, list[T]] = {}
    for row in rows:
        groups.setdefault(row.ticker, []).append(row)
    return groups


def _history(
    ticker: str,
    cutoff: datetime,
    *,
    valuation: list[ModelValuationObservation],
    rating: list[AnalystRatingObservation],
    quants: list[QuantsScoreObservation],
    minkabu: list[AnalystForecastObservation],
    tradingview: list[TradingViewObservation],
) -> ObservationHistory:
    return ObservationHistory(
        ticker=ticker,
        cutoff=cutoff,
        model_valuations=tuple(
            ModelValuationHistory(latest=latest, previous=previous)
            for latest, previous in _pairs(valuation)
        ),
        analyst_ratings=tuple(
            AnalystRatingHistory(latest=latest, previous=previous)
            for latest, previous in _pairs(rating)
        ),
        quants_scores=tuple(
            QuantsScoreHistory(latest=latest, previous=previous)
            for latest, previous in _pairs(quants)
        ),
        minkabu=AnalystForecastHistory(
            latest=minkabu[0] if minkabu else None,
            previous=minkabu[1] if len(minkabu) > 1 else None,
        ),
        tradingview=TradingViewHistory(
            latest=tradingview[0] if tradingview else None,
            previous=tradingview[1] if len(tradingview) > 1 else None,
        ),
    )
