"""Provider adapter protocols and the bundle injected into CLI commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)


class JQuantsMaintenance(Protocol):
    def ensure_master(self, day: date) -> int: ...
    def ensure_market_calendar(self, start: date, end: date) -> int: ...
    def ensure_daily_bars(self, start: date, end: date) -> int: ...
    def ensure_fin_summaries(self, start: date, end: date) -> int: ...
    def refresh_fin_summaries(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int: ...
    def close(self) -> None: ...


class EDINETMaintenance(Protocol):
    def close(self) -> None: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def retained_archives(self) -> FixedEdinetArchives: ...


class JPXMaintenance(Protocol):
    def refresh_earnings_calendar_snapshot(
        self, asof_date: date
    ) -> JPXEarningsCalendarSnapshot: ...

    def refresh_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot: ...

    def bootstrap_cache(self, asof_date: date) -> Mapping[str, int]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    jquants_refresh: JQuantsMaintenance | None = None
    edinet_refresh: EDINETMaintenance | None = None
    jpx_refresh: JPXMaintenance | None = None
