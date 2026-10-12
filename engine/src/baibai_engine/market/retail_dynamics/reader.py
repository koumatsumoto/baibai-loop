"""Cutoff reads from a current store; absent optional lake datasets hydrate empty."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from baibai_engine.market.sqlite.read import connect_read_only
from baibai_engine.market.ticker import normalize_ticker

from .contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
    Column,
)
from .models import (
    AnalystRatingHistory,
    AnalystRatingObservation,
    ModelValuationHistory,
    ModelValuationObservation,
    ObservationHistory,
    QuantsScoreHistory,
    QuantsScoreObservation,
    utc_datetime,
)


def _latest_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[Column, ...],
    ticker: str,
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
        f"SELECT {names} FROM {table} WHERE ticker=? AND fetched_at_utc<=? "  # nosec B608
        "ORDER BY fetched_at_utc DESC LIMIT 2",
        (ticker, cutoff.isoformat(timespec="microseconds")),
    ).fetchall()


def read_observations(sqlite_path: Path, ticker: str, cutoff: datetime) -> ObservationHistory:
    """Choose latest received row, even if its metrics are null, plus its predecessor."""
    ticker, cutoff = normalize_ticker(ticker), utc_datetime(cutoff)
    if not sqlite_path.exists():
        return ObservationHistory(ticker=ticker, cutoff=cutoff)
    conn = connect_read_only(sqlite_path)
    try:
        # Fix one SQLite read snapshot across both dataset reads.
        conn.execute("BEGIN")
        valuation = [
            ModelValuationObservation.model_validate(dict(row))
            for row in _latest_rows(
                conn, MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS, ticker, cutoff
            )
        ]
        rating = [
            AnalystRatingObservation.model_validate(dict(row))
            for row in _latest_rows(
                conn, ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS, ticker, cutoff
            )
        ]
        quants = [
            QuantsScoreObservation.model_validate(dict(row))
            for row in _latest_rows(conn, QUANTS_SCORE_TABLE, QUANTS_SCORE_COLUMNS, ticker, cutoff)
        ]
    finally:
        conn.close()
    return ObservationHistory(
        ticker=ticker,
        cutoff=cutoff,
        model_valuation=ModelValuationHistory(
            latest=valuation[0] if valuation else None,
            previous=valuation[1] if len(valuation) > 1 else None,
        ),
        quants_score=QuantsScoreHistory(
            latest=quants[0] if quants else None,
            previous=quants[1] if len(quants) > 1 else None,
        ),
        analyst_rating=AnalystRatingHistory(
            latest=rating[0] if rating else None,
            previous=rating[1] if len(rating) > 1 else None,
        ),
    )
