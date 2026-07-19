"""Provider adapter protocols and the bundle injected into CLI commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from baibai_engine.screening.providers.edinet import (
    EdinetMetricRecord,
)
from baibai_engine.screening.providers.jpx import (
    JPXEarningsCalendarSnapshot,
    JPXRegulationSnapshot,
)
from baibai_engine.screening.providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarketCalendarDay,
)
from baibai_engine.screening.schema import (
    SecurityMaster,
)


class JQuantsAdapter(Protocol):
    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]: ...

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]: ...

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]: ...

    def get_fin_summary_range(
        self,
        start: date,
        end: date,
    ) -> list[JQuantsFinancialSummary]: ...


class EDINETAdapter(Protocol):
    def load_metric_records(self, asof_date: date) -> Mapping[str, EdinetMetricRecord]: ...

    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def bootstrap_cache(self, start: date, end: date) -> Mapping[str, int]: ...


class JPXAdapter(Protocol):
    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot: ...

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot: ...

    def has_regulation_cache(self, asof_date: date) -> bool: ...

    def bootstrap_cache(self, asof_date: date) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    jquants: JQuantsAdapter
    edinet: EDINETAdapter | None
    jpx: JPXAdapter
