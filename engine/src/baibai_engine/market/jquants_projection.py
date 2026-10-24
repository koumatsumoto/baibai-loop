"""Materialize market read tables from one retained J-Quants request, offline."""

from __future__ import annotations

import sqlite3
from contextlib import closing, nullcontext
from datetime import date
from pathlib import Path

from baibai_engine.collection.jquants.receipts import read_complete_request
from baibai_engine.market.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_margin_alerts,
    store_jquants_market_calendar,
    store_jquants_master,
    store_jquants_short_sale_reports,
    store_jquants_weekly_margin,
)
from baibai_engine.market.margin_publication import LEGACY_WEEKLY_LAST_BALANCE_DATE
from baibai_engine.storage.observations.schema import connect_current


def project_request(
    db_path: Path, request_id: str, *, connection: sqlite3.Connection | None = None
) -> int:
    """Project a complete full-cross-section query, never a filtered code subset.

    A source may receive arbitrary supported queries, but this materializer only
    replaces scopes it can prove from the explicit request. In particular a
    one-code result must not replace the whole market for that date.
    """
    conn = connection if connection is not None else connect_current(db_path)
    if conn is None:
        raise ValueError("J-Quants projection requires a retained source store")
    with closing(conn) if connection is None else nullcontext(conn):
        request = read_complete_request(conn, request_id)
    records = request.records()
    fetched_at = request.received_at.isoformat(timespec="microseconds")
    if request.source == "market_calendar":
        if set(request.params) != {"from", "to"}:
            raise ValueError("calendar projection requires an unfiltered explicit date range")
        return store_jquants_market_calendar(
            db_path,
            records,
            requested_start=date.fromisoformat(request.params["from"]),
            requested_end=date.fromisoformat(request.params["to"]),
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    date_key = "disc_date" if request.source == "short_sale_reports" else "date"
    if set(request.params) != {date_key}:
        raise ValueError("projection requires an unfiltered explicit source date")
    day = date.fromisoformat(request.params[date_key])
    if request.source == "master":
        return store_jquants_master(
            db_path,
            records,
            requested_asof=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    if request.source == "daily_bars":
        return store_jquants_daily_bars(
            db_path,
            records,
            requested_start=day,
            requested_end=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    if request.source == "financial_summaries":
        return store_jquants_fin_summaries(
            db_path,
            records,
            requested_start=day,
            requested_end=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    if request.source == "margin_interest":
        if day <= LEGACY_WEEKLY_LAST_BALANCE_DATE:
            return store_jquants_weekly_margin(
                db_path,
                records,
                week_end=day,
                fetched_at_utc=fetched_at,
                connection=connection,
            )
        return store_jquants_all_issues_daily_margin(
            db_path,
            records,
            balance_date=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    if request.source == "margin_alerts":
        return store_jquants_margin_alerts(
            db_path,
            records,
            requested_start=day,
            requested_end=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    if request.source == "short_sale_reports":
        return store_jquants_short_sale_reports(
            db_path,
            records,
            requested_start=day,
            requested_end=day,
            fetched_at_utc=fetched_at,
            connection=connection,
        )
    raise ValueError("unsupported J-Quants projection source")
