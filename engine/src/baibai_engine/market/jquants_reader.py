"""Offline J-Quants read models; acquisition is an explicit separate operation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.collection.prices import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market.calendar import JQuantsMarketCalendarDay
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.margin_publication import (
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.store import (
    read_adjustment_factor_bars,
    read_daily_bars,
    read_market_calendar,
)
from baibai_engine.storage.observations.reader import (
    read_all_issues_daily_margin,
    read_eq_master_exact,
    read_fin_summaries,
    read_margin_alerts,
    read_short_sale_reports,
    read_weekly_margin,
)


def _required[T](value: T | None, source: str) -> T:
    if value is None:
        raise JQuantsProviderError(
            f"Stored J-Quants input is incomplete: {source}; "
            "explicitly acquire and project it before reading"
        )
    return value


class JQuantsReader:
    """Reads never obtain credentials, call a provider, or change stored data."""

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        return _required(read_market_calendar(self._sqlite_path, start, end), "market_calendar")

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        return _required(read_eq_master_exact(self._sqlite_path, requested_asof), "master")

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        return _required(read_daily_bars(self._sqlite_path, start, end), "daily_bars")

    def get_adjustment_factor_bars_range(
        self, start: date, end: date
    ) -> list[JQuantsAdjustmentFactorEvent]:
        return _required(
            read_adjustment_factor_bars(self._sqlite_path, start, end), "adjustment_factors"
        )

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        return _required(read_fin_summaries(self._sqlite_path, start, end), "financial_summaries")

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        return [row for row in self.get_fin_summary_range(start, end) if row.fiscal_period == "FY"]

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        require_legacy_weekly_balance_date(week_end)
        return _required(read_weekly_margin(self._sqlite_path, week_end), "weekly_margin")

    def get_mkt_all_issues_daily_margin(
        self, balance_date: date
    ) -> list[JQuantsAllIssuesDailyMargin]:
        require_all_issues_daily_balance_date(balance_date)
        return _required(
            read_all_issues_daily_margin(self._sqlite_path, balance_date), "all_issues_daily_margin"
        )

    def get_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]:
        return _required(read_margin_alerts(self._sqlite_path, start, end), "margin_alerts")

    def get_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]:
        return _required(
            read_short_sale_reports(self._sqlite_path, start, end), "short_sale_reports"
        )
