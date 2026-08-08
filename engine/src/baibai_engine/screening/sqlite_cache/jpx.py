"""JPX snapshot ingest for earnings dates and regulation flags."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.market.sqlite.convert import normalize_ticker_or_none, to_str_or_none
from baibai_engine.market.sqlite.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    record_source_coverage,
)
from baibai_engine.market.sqlite.schema import open_connection
from baibai_engine.screening.providers.jpx import JPXEarningsCalendarSnapshot

_LOGGER = logging.getLogger(__name__)


def store_jpx_earnings_calendar_snapshot(
    db_path: Path,
    snapshot: JPXEarningsCalendarSnapshot,
    *,
    fetched_at_utc: str | None = None,
) -> int:
    """Store a JPX snapshot in the compatibility earnings-calendar table.

    The physical table keeps its historical J-Quants name until a coordinated
    schema migration. Source authority is the logical source_coverage row and
    the JPX-only writer/reader API, not the table identifier.
    """
    if not snapshot.entries:
        raise ValueError("JPX earnings calendar snapshot must contain at least one valid row")
    conn = open_connection(db_path)
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    try:
        conn.execute("DELETE FROM jquants_earnings_calendar")
        delete_source_coverage(conn, "jquants_earnings_calendar")
        delete_source_coverage(conn, "jpx_earnings_calendar")
        rows = [(entry.announcement_date.isoformat(), entry.ticker) for entry in snapshot.entries]
        conn.executemany(
            "INSERT INTO jquants_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
            rows,
        )
        persisted_count = int(
            conn.execute("SELECT COUNT(*) FROM jquants_earnings_calendar").fetchone()[0]
        )
        record_source_coverage(
            conn,
            source="jpx_earnings_calendar",
            coverage_key="get_earnings_calendar_snapshot:current",
            coverage_start=snapshot.min_date.isoformat(),
            coverage_end=snapshot.max_date.isoformat(),
            fetched_at_utc=fetched_at,
            record_count=persisted_count,
            status="ok",
            error=None,
        )
        conn.commit()
        _LOGGER.info(
            "stored JPX earnings calendar snapshot: sources=%s raw=%d valid=%d "
            "excluded=%d superseded=%s min=%s max=%s",
            ",".join(snapshot.source_urls),
            snapshot.raw_record_count,
            snapshot.valid_record_count,
            snapshot.excluded_record_count,
            snapshot.superseded_record_count,
            snapshot.min_date.isoformat(),
            snapshot.max_date.isoformat(),
        )
        return persisted_count
    finally:
        conn.close()


def store_jpx_regulations(
    db_path: Path,
    asof_date: date,
    *,
    flags_by_ticker: Mapping[str, Iterable[str]],
    source_names: Iterable[str],
    fetched_at_utc: str | None = None,
) -> int:
    conn = open_connection(db_path)
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    source_name_tuple = tuple(source_names)
    try:
        conn.execute(
            "DELETE FROM jpx_regulation_flags WHERE asof_date = ?", (asof_date.isoformat(),)
        )
        conn.execute(
            "DELETE FROM jpx_regulation_sources WHERE asof_date = ?",
            (asof_date.isoformat(),),
        )
        delete_overlapping_source_coverage(conn, "jpx_regulation_flags", asof_date, asof_date)
        source_rows = [(asof_date.isoformat(), str(name), fetched_at) for name in source_name_tuple]
        if source_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_sources("
                "asof_date, source_name, fetched_at_utc"
                ") VALUES (?, ?, ?)",
                source_rows,
            )
        rows: list[tuple[Any, ...]] = []
        raw_record_count = 0
        rejected_count = 0
        for raw_ticker, flags in flags_by_ticker.items():
            raw_record_count += 1
            ticker = normalize_ticker_or_none(raw_ticker)
            if ticker is None:
                rejected_count += 1
                continue
            accepted_for_ticker = 0
            rejected_for_ticker = 0
            for flag in flags:
                flag_text = to_str_or_none(flag)
                if flag_text is None:
                    rejected_for_ticker += 1
                    continue
                rows.append((asof_date.isoformat(), flag_text, ticker, flag_text, fetched_at))
                accepted_for_ticker += 1
            rejected_count += rejected_for_ticker
            if accepted_for_ticker == 0 and rejected_for_ticker == 0:
                rejected_count += 1
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "jpx_regulation_flags", "asof_date", asof_date, asof_date
        )
        record_source_coverage(
            conn,
            source="jpx_regulation_flags",
            operation="regulations",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            requested_start=asof_date.isoformat(),
            requested_end=asof_date.isoformat(),
            params={"asof_date": asof_date.isoformat(), "source_names": sorted(source_name_tuple)},
            record_count=persisted_count,
            raw_record_count=raw_record_count,
            skipped_record_count=0,
            rejected_record_count=rejected_count,
            status="partial" if rejected_count else "ok",
            error=(
                f"{rejected_count} JPX regulation records were rejected" if rejected_count else None
            ),
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()
