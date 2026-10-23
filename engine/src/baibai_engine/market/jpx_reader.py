"""Offline readers of deterministically processed JPX snapshots."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
    next_earnings_dates,
)
from baibai_engine.storage.observations.reader import (
    read_jpx_earnings_calendar_snapshot,
    read_jpx_regulations,
)


def read_next_earnings_dates(sqlite_path: Path, asof_date: date) -> dict[str, date] | None:
    """Read verified current dates; None means unavailable, not a published empty set.

    Coverage identity, source freshness and snapshot integrity belong to the stored
    snapshot reader. Schema/query failures and corrupt snapshots propagate. This
    optional read never acquires data or makes unrelated market sources mandatory.
    """
    snapshot = read_jpx_earnings_calendar_snapshot(sqlite_path, asof_date)
    return None if snapshot is None else next_earnings_dates(snapshot.entries, asof_date)


class JPXReader:
    def __init__(self, sqlite_path: Path, *, allow_stale_snapshot: bool = False) -> None:
        self._sqlite_path = sqlite_path
        self._allow_stale_snapshot = allow_stale_snapshot

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        snapshot = read_jpx_regulations(self._sqlite_path, asof_date)
        if snapshot is None:
            raise JPXProviderError(
                f"stored JPX regulation snapshot is unavailable for {asof_date.isoformat()}; "
                "acquire and project the source explicitly before reading"
            )
        return snapshot

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        snapshot = read_jpx_earnings_calendar_snapshot(
            self._sqlite_path, asof_date, allow_stale=self._allow_stale_snapshot
        )
        if snapshot is None:
            raise JPXProviderError(
                f"stored JPX earnings calendar is unavailable for {asof_date.isoformat()}; "
                "acquire and project the source explicitly before reading"
            )
        return snapshot
