"""Retired J-Quants writer entry points reject before touching data or HTTP."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError

Progress = Callable[[int, int, date, date], None]


class JQuantsRefresh:
    def __init__(
        self,
        sqlite_path: Path,
        api_key: str | None,
        *,
        client: JQuantsHTTP | None = None,
    ) -> None:
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def ensure_master(self, day: date) -> int:
        raise JQuantsProviderError(
            "legacy master writer is disabled; acquire with jquants-master-native "
            "and rebuild the execution projection from its fixed input"
        )

    def ensure_market_calendar(self, start: date, end: date) -> int:
        raise JQuantsProviderError(
            "legacy calendar writer is disabled; use jquants-calendar-native acquire "
            "and rebuild-calendar-native"
        )

    def ensure_daily_bars(self, start: date, end: date) -> int:
        raise JQuantsProviderError(
            "legacy daily-bar writer is disabled; use jquants-price-native acquire "
            "and rebuild-price-native with its fixed input"
        )

    def ensure_fin_summaries(self, start: date, end: date) -> int:
        raise JQuantsProviderError(
            "legacy financial writer is disabled; use jquants-financial-native acquire "
            "and rebuild-financial-native with its fixed input"
        )

    def refresh_fin_summaries(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Progress | None = None,
    ) -> int:
        return self.ensure_fin_summaries(start, end)
