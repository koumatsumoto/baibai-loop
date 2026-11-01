"""Offline provider doubles for explicit Jobs maintenance and CLI wiring."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.calendar import JQuantsMarketCalendarDay
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.machine_analysis.observations.trading_activity.models import JQuantsWeeklyMargin


@dataclass
class FakeJQuantsProvider:
    business_day: bool = True
    treasury_shares: float = 0.0
    calls: list[tuple[str, date | None, date | None]] = field(default_factory=list)
    revision_overlap_days: list[int] = field(default_factory=list)

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        self.calls.append(("get_mkt_calendar", start, end))
        return [JQuantsMarketCalendarDay(day=start, is_business_day=self.business_day)]

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        self.calls.append(("get_eq_master", requested_asof, requested_asof))
        return [
            SecurityMaster(
                ticker="130A",
                name="Alpha",
                market_segment="Prime",
                sector_33="情報・通信業",
                is_common_stock=True,
            )
        ]

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        self.calls.append(("get_mkt_margin_interest_week", week_end, week_end))
        return [
            JQuantsWeeklyMargin(
                ticker="130A",
                week_end=week_end,
                long_vol=1000.0,
                short_vol=250.0,
                long_std_vol=800.0,
                long_neg_vol=200.0,
                short_std_vol=200.0,
                short_neg_vol=50.0,
                issue_type="2",
            )
        ]

    def refresh_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        self.calls.append(("refresh_mkt_margin_interest_week", week_end, week_end))
        return self.get_mkt_margin_interest_week(week_end)

    def get_mkt_short_sale_report_range(self, start: date, end: date) -> list[object]:
        self.calls.append(("get_mkt_short_sale_report_range", start, end))
        return []

    def refresh_mkt_short_sale_report_range(self, start: date, end: date) -> list[object]:
        self.calls.append(("refresh_mkt_short_sale_report_range", start, end))
        return []

    def get_mkt_margin_alert_range(self, start: date, end: date) -> list[object]:
        self.calls.append(("get_mkt_margin_alert_range", start, end))
        return []

    def refresh_mkt_margin_alert_range(self, start: date, end: date) -> list[object]:
        self.calls.append(("refresh_mkt_margin_alert_range", start, end))
        return []

    def get_mkt_all_issues_daily_margin(self, balance_date: date) -> list[object]:
        self.calls.append(("get_mkt_all_issues_daily_margin", balance_date, balance_date))
        return [object()]

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        self.calls.append(("get_eq_bars_daily_range", start, end))
        # Long observed price history supplies the historical metrics in this fixture.
        # Observation span is not evidence of the issuer's listing date.
        total = 800
        base = end - timedelta(days=total - 1)
        return [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=base + timedelta(days=index),
                close=100 + index,
                turnover_value=300_000_000.0,
            )
            for index in range(total)
        ]

    def ensure_eq_bars_daily_range(self, start: date, end: date) -> int:
        self.calls.append(("ensure_eq_bars_daily_range", start, end))
        return len(self.get_eq_bars_daily_range(start, end))

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int:
        self.calls.append(("refresh_fin_summary_range", start, end))
        self.revision_overlap_days.append(revision_overlap_days)
        del progress
        return len(self.get_fin_summary_range(start, end))

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        self.calls.append(("get_fin_summary_range", start, end))
        return [
            JQuantsFinancialSummary(
                # Calculation fixture: guidance has already been projected for this snapshot.
                forecast_status="confirmed",
                ticker="130A",
                disclosed_at=date(2025, 12, 31),
                forecast_eps=20.0,
                cumulative_eps=15.0,
                bps=120.0,
                shares_outstanding=400_000_000.0,
                sales=1000.0,
                operating_profit=100.0,
                cash_eq=200_000_000_000.0,
                total_assets=1_000_000_000_000.0,
                equity=600_000_000_000.0,
                equity_to_asset_ratio=0.6,
                treasury_shares=self.treasury_shares,
                ordinary_profit=None,
                profit=None,
            ),
            JQuantsFinancialSummary(
                # Calculation fixture: guidance has already been projected for this snapshot.
                forecast_status="confirmed",
                ticker="130A",
                disclosed_at=date(2026, 3, 31),
                forecast_eps=22.0,
                cumulative_eps=18.0,
                bps=130.0,
                shares_outstanding=400_000_000.0,
                sales=1100.0,
                operating_profit=110.0,
                cash_eq=210_000_000_000.0,
                total_assets=1_000_000_000_000.0,
                equity=600_000_000_000.0,
                equity_to_asset_ratio=0.6,
                treasury_shares=self.treasury_shares,
                ordinary_profit=None,
                profit=None,
            ),
        ]

    def ensure_master(self, day: date) -> int:
        return len(self.get_eq_master(day))

    def ensure_market_calendar(self, start: date, end: date) -> int:
        return len(self.get_mkt_calendar(start, end))

    def ensure_daily_bars(self, start: date, end: date) -> int:
        return self.ensure_eq_bars_daily_range(start, end)

    def ensure_fin_summaries(self, start: date, end: date) -> int:
        return len(self.get_fin_summary_range(start, end))

    def refresh_fin_summaries(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int:
        return self.refresh_fin_summary_range(
            start, end, revision_overlap_days=revision_overlap_days, progress=progress
        )

    def ensure_weekly_margin(self, day: date) -> int:
        return len(self.get_mkt_margin_interest_week(day))

    def refresh_weekly_margin(self, day: date) -> int:
        return len(self.refresh_mkt_margin_interest_week(day))

    def ensure_all_issues_daily_margin(self, day: date) -> int:
        return len(self.get_mkt_all_issues_daily_margin(day))

    def ensure_margin_alerts(self, start: date, end: date) -> int:
        return len(self.get_mkt_margin_alert_range(start, end))

    def refresh_margin_alerts(self, start: date, end: date) -> int:
        return len(self.refresh_mkt_margin_alert_range(start, end))

    def ensure_short_sale_reports(self, start: date, end: date) -> int:
        return len(self.get_mkt_short_sale_report_range(start, end))

    def refresh_short_sale_reports(self, start: date, end: date) -> int:
        return len(self.refresh_mkt_short_sale_report_range(start, end))

    def close(self) -> None:
        pass



@dataclass
class FakeEDINETProvider:
    documents: list[dict[str, object]] | None = None
    zip_by_doc_id: dict[str, bytes] | None = None
    bootstrap_calls: list[tuple[date, date]] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)

    def close(self) -> None:
        pass

    def list_documents(self, on_date: date) -> list[dict[str, object]]:
        del on_date
        return list(self.documents or [])

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.download_calls.append(doc_id)
        return (self.zip_by_doc_id or {})[doc_id]

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        self.bootstrap_calls.append((start, end))
        return {"ok": 1}



@dataclass
class FakeJPXProvider:
    fail_bootstrap: bool = False
    source_names: tuple[str, ...] = (
        "上場廃止警告",
        "取引停止",
        "整理銘柄",
        "特別注意銘柄",
    )
    bootstrap_calls: list[date] = field(default_factory=list)
    earnings_requests: list[date] = field(default_factory=list)
    close_count: int = 0

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        self.earnings_requests.append(asof_date)
        return JPXEarningsCalendarSnapshot(
            entries=(
                JPXEarningsCalendarEntry(
                    ticker="130A", announcement_date=asof_date + timedelta(days=7)
                ),
            ),
            source_urls=("https://www.jpx.co.jp/test/kessan.xlsx",),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=None,
        )

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        del asof_date
        return JPXRegulationSnapshot(flags_by_ticker={}, source_names=self.source_names)

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        self.bootstrap_calls.append(asof_date)
        if self.fail_bootstrap:
            raise JPXProviderError("missing jpx source")
        return {"ok": 1}

    def close(self) -> None:
        self.close_count += 1
