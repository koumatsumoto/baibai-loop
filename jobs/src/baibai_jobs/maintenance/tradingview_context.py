"""Saved TradingView receipt context and completeness; no acquisition or OAuth."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import TABLE
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    observation_calendar_context,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import masters_from_payloads
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema
from baibai_jobs.observability.tradingview import SourceDataError, TimeGuardError

JST = ZoneInfo("Asia/Tokyo")


def universe(conn: sqlite3.Connection, day: date) -> list[str]:
    rows = conn.execute(
        "SELECT record_json FROM jquants_master_snapshots "
        "WHERE snapshot_date=? ORDER BY security_code",
        (day.isoformat(),),
    ).fetchall()
    if not rows:
        raise SourceDataError("Exact-date J-Quants master is unavailable")
    result = []
    for security in masters_from_payloads([row[0] for row in rows]):
        if not security.is_common_stock or security.market_segment.upper() not in ELIGIBLE_MARKETS:
            continue
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", security.ticker):
            raise SourceDataError("Invalid J-Quants ticker")
        result.append("TSE:" + security.ticker)
    if not result:
        raise SourceDataError("Empty TradingView universe")
    return result


def validate_time(day: date, now: datetime, *, is_business_day: bool = True) -> None:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware observation clock required")
    local = now.astimezone(JST)
    opening = daytime(15, 30) if is_business_day else daytime(9)
    if local.date() != day or local.time() < opening:
        raise TimeGuardError(
            "Receipt time must match its recorded day and observation window"
        )


def _stored_quality(conn: sqlite3.Connection, day: date) -> dict[str, object]:
    # Identifiers are fixed by the physical snapshot contract.
    null_counts = ",".join(
        f"SUM(fetch_status='ok' AND {name} IS NULL)" for name in observations_owner.FIELDS
    )
    row = conn.execute(
        f"SELECT SUM(fetch_status='unresolved'),MIN(fetched_at_utc),MAX(fetched_at_utc),"  # nosec B608
        f"{null_counts} FROM {TABLE} WHERE snapshot_date=?",
        (day.isoformat(),),
    ).fetchone()
    return {
        "unresolved": int(row[0] or 0),
        "first_fetched_at_utc": row[1],
        "last_fetched_at_utc": row[2],
        "normal_nulls": {
            name: int(count or 0)
            for name, count in zip(observations_owner.FIELDS, row[3:], strict=True)
        },
    }


def calendar_context(conn: sqlite3.Connection, day: date) -> tuple[date, bool]:
    """Read the exact saved-calendar basis for historical replay."""
    try:
        return observation_calendar_context(conn, day)
    except ValueError as error:
        raise SourceDataError(str(error)) from error


def snapshot_state(path: Path, day: date) -> dict[str, object]:
    """Read historical completeness without treating missing rows as an acquisition plan."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        basis, business = calendar_context(conn, day)
        symbols = universe(conn, basis)
        expected = {symbol.removeprefix("TSE:") for symbol in symbols}
        stored = {
            row[0]
            for row in conn.execute(
                f"SELECT ticker FROM {TABLE} WHERE snapshot_date=?", (day.isoformat(),)  # nosec B608
            )
        }
        if not stored <= expected:
            raise SourceDataError("Stored TradingView ticker is outside exact-date universe")
        return {
            "snapshot_date": day.isoformat(),
            "master_asof": basis.isoformat(),
            "is_business_day": business,
            "rows": len(stored),
            "expected_universe": len(expected),
            "remaining": len(expected - stored),
            **_stored_quality(conn, day),
        }
