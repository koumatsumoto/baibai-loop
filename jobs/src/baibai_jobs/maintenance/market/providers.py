"""Provider adapter protocols and the bundle injected into CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)


class EDINETMaintenance(Protocol):
    def close(self) -> None: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def retained_archives(self) -> FixedEdinetArchives: ...


class JPXMaintenance(Protocol):
    def refresh_earnings_calendar_snapshot(
        self, asof_date: date
    ) -> JPXEarningsCalendarSnapshot: ...

    def refresh_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    edinet_refresh: EDINETMaintenance | None = None
    jpx_refresh: JPXMaintenance | None = None
