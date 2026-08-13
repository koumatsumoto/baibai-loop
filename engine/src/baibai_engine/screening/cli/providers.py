"""Provider adapter protocols and the bundle injected into CLI commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    JQuantsAdjustmentFactorEvent,
    JQuantsAllIssuesDailyMargin,
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsMarketCalendarDay,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.screening.schema import (
    SecurityMaster,
)


class JQuantsAdapter(Protocol):
    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]: ...

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]: ...

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]: ...

    def ensure_eq_bars_daily_range(self, start: date, end: date) -> int: ...

    def get_adjustment_factor_bars_range(
        self, start: date, end: date
    ) -> list[JQuantsAdjustmentFactorEvent]: ...

    def get_fin_summary_range(
        self,
        start: date,
        end: date,
    ) -> list[JQuantsFinancialSummary]: ...

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        repair_ranges: Sequence[tuple[date, date]] = (),
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int: ...

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]: ...

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]: ...

    def refresh_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]: ...

    def get_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]: ...

    def refresh_mkt_margin_alert_range(
        self, start: date, end: date
    ) -> list[JQuantsMarginAlert]: ...

    def get_mkt_all_issues_daily_margin(
        self, balance_date: date
    ) -> list[JQuantsAllIssuesDailyMargin]: ...

    def get_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]: ...

    def refresh_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]: ...


class EDINETAdapter(Protocol):
    def load_metric_records(self, asof_date: date) -> Mapping[str, EdinetMetricRecord]: ...

    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...

    def bootstrap_cache(self, start: date, end: date) -> Mapping[str, int]: ...

    def refresh_document_state(self, asof_date: date) -> Mapping[str, int]: ...

    def backfill_document_identity(self, start: date, end: date) -> Mapping[str, int]: ...


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
