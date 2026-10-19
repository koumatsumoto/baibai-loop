"""Offline readers of deterministically processed JPX snapshots."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.storage.observations.reader import (
    read_jpx_earnings_calendar_snapshot,
    read_jpx_regulations,
)


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
