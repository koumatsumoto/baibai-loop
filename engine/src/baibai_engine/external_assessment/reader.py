"""Cutoff reads from a current store; absent optional lake datasets hydrate empty."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.external_assessment.observations.contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
    Column,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
    QuantsScoreObservation,
    ReceivedObservation,
    utc_datetime,
)
from baibai_engine.external_assessment.tradingview.contract import COLUMNS as TV_COLUMNS
from baibai_engine.external_assessment.tradingview.contract import TABLE as TV_TABLE
from baibai_engine.storage.observations.read import connect_read_only

from .models import (
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
        f"SELECT {names} FROM (SELECT {names}, "  # nosec B608
        "row_number() OVER (PARTITION BY distributor ORDER BY fetched_at_utc DESC) AS receive_rank "
        f"FROM {table} WHERE ticker=? AND fetched_at_utc<=?) "
        "WHERE receive_rank<=2 ORDER BY distributor, fetched_at_utc DESC",
        (ticker, cutoff.isoformat(timespec="microseconds")),
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
    columns: tuple[Column, ...] = TV_COLUMNS,
) -> list[sqlite3.Row]:
    """Use the same exact received-time predicate for identities and value reads."""
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TV_TABLE,)
        ).fetchone()
        is None
    ):
        return []
    names = ",".join(column[0] for column in columns)
    day = cutoff.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    predicate = "snapshot_date<=?" + (" AND ticker=?" if ticker is not None else "")
    # Writers normalize received time to UTC with optional fractional seconds.
    # Parse it here so enumerating identities and reading values agree exactly,
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
    ticker, cutoff = normalize_ticker(ticker), utc_datetime(cutoff)
    if not sqlite_path.exists():
        return ObservationHistory(ticker=ticker, cutoff=cutoff)
    conn = connect_read_only(sqlite_path)
    try:
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
        tradingview = sorted(
            (
                TradingViewObservation.model_validate(dict(row))
                for row in tradingview_rows_at_cutoff(conn, cutoff, ticker=ticker)
            ),
            key=lambda row: row.fetched_at_utc,
            reverse=True,
        )[:2]
    finally:
        conn.close()
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
        tradingview=TradingViewHistory(
            latest=tradingview[0] if tradingview else None,
            previous=tradingview[1] if len(tradingview) > 1 else None,
        ),
    )
