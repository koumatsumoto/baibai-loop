"""Recent revision checks and outage recovery for economic source receipts.

Keep the existing windows until a source's publication/revision contract justifies
a change. A recent check is not a history rebuild or a bound on outage recovery.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .sources import SourceDefinition, load_sources

DEFAULT_LATEST_LOOKBACK_DAYS = 370
LATEST_FETCH_LOOKBACK_DAYS = {"daily": 14, "weekly": 60, "monthly": 370, "quarterly": 370}

# Providers sharing credentials/rate limits stay in one sequential acquisition.
_GROUPS = {
    "boj_timeseries": "boj",
    "estat_dashboard": "estat",
    "jquants_options": "jquants",
    "jquants_flows": "jquants",
    "jquants_indices": "jquants",
}
_BULK = frozenset(
    {"boj", "ecb_fx", "fred_csv", "estat", "estat_dashboard", "mof_jgb", "umich_sca", "multpl"}
)


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    acquisition_group: str
    mode: str
    lookback_days: int


def source_policy(source: SourceDefinition) -> RefreshPolicy:
    mode = "range"
    if source.provider in _BULK:
        mode = "bulk_then_filter"
    elif source.provider == "matsui_margin":
        mode = "current_only"
    elif source.provider in {"spglobal_pmi", "tsr_bankruptcies"}:
        mode = "documents"
    elif source.provider == "jquants_options":
        mode = "day_requests"
    return RefreshPolicy(
        acquisition_group=_GROUPS.get(source.provider, source.provider),
        mode=mode,
        lookback_days=LATEST_FETCH_LOOKBACK_DAYS.get(
            source.frequency, DEFAULT_LATEST_LOOKBACK_DAYS
        ),
    )


def acquisition_groups() -> tuple[str, ...]:
    return tuple(
        sorted({source_policy(source).acquisition_group for source in load_sources().values()})
    )


def recent_start(connection: sqlite3.Connection, source: SourceDefinition, end: date) -> date:
    """Recheck recent values and recover gaps since this source's first attempt.

    Successful *ranges*, including confirmed empty dates, are coverage. The latest
    observed value alone cannot establish coverage. Failed attempts anchor recovery
    too; a later narrow success must not hide an older failed or uncovered interval.
    A source with no previous attempt starts with the recent window; deep seeding
    remains the explicit all-history operation.
    """
    policy = source_policy(source)
    if policy.mode == "current_only":
        return end
    first = end - timedelta(days=policy.lookback_days)
    rows = connection.execute(
        "SELECT range_start, range_end, status FROM economic_source_runs "
        "WHERE source_id = ? AND range_start <= ? ORDER BY range_start, range_end",
        (source.source_id, end.isoformat()),
    ).fetchall()
    if not rows:
        return first
    cursor = date.fromisoformat(rows[0][0])
    for start_raw, end_raw, status in rows:
        if status != "ok":
            continue
        start, last = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
        if start > cursor:
            break
        if last >= cursor:
            cursor = last + timedelta(days=1)
    return min(first, cursor)
