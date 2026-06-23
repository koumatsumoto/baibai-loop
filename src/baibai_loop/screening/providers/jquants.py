"""Screening J-Quants provider: fundamentals fetch on top of the market adapter.

The price/calendar fetch engine, error type, code parsing, and bar/calendar
normalization live in `baibai_loop.market`; this module re-exposes them and adds
the screening-only endpoints (master snapshot, financial summaries, earnings
calendar) by extending `JQuantsMarketProvider`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import field_validator
from pydantic.dataclasses import dataclass

from baibai_loop.market.bars import (
    MODEL_CONFIG,
    validate_finite,
)
from baibai_loop.market.bars import (
    JQuantsDailyBar as JQuantsDailyBar,
)
from baibai_loop.market.bars import (
    JQuantsMarketCalendarDay as JQuantsMarketCalendarDay,
)
from baibai_loop.market.jquants import (
    JQuantsProviderError as JQuantsProviderError,
)
from baibai_loop.market.jquants import (
    _coalesce_field,
    _first_value,
    _parse_date,
    _parse_jquants_code,
    _parse_optional_date,
    _to_float,
    _to_period,
)
from baibai_loop.market.jquants import (
    normalize_daily_bar as normalize_daily_bar,
)
from baibai_loop.market.jquants import (
    normalize_market_calendar as normalize_market_calendar,
)
from baibai_loop.market.jquants import (
    parse_jquants_code as parse_jquants_code,
)
from baibai_loop.market.provider import JQuantsMarketProvider

from ..schema import SecurityMaster, normalize_ticker


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsFinancialSummary:
    ticker: str
    disclosed_at: date
    forecast_eps: float | None = None
    eps_ttm: float | None = None
    bps: float | None = None
    shares_outstanding: float | None = None
    sales: float | None = None
    cfo: float | None = None
    cash_eq: float | None = None
    total_assets: float | None = None
    equity: float | None = None
    operating_profit: float | None = None
    ordinary_profit: float | None = None
    profit: float | None = None
    fiscal_period: str | None = None
    fiscal_year_end: date | None = None
    period_start: date | None = None
    period_end: date | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "forecast_eps",
        "eps_ttm",
        "bps",
        "shares_outstanding",
        "sales",
        "cfo",
        "cash_eq",
        "total_assets",
        "equity",
        "operating_profit",
        "ordinary_profit",
        "profit",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return validate_finite(value)


class JQuantsProvider(JQuantsMarketProvider):
    """Cache-first adapter for jquantsapi.ClientV2: price/calendar + fundamentals."""

    def get_eq_master(self) -> list[SecurityMaster]:
        if self._sqlite_path is not None:
            # Imported lazily to avoid a circular import: sqlite_reader pulls in
            # this module's schema dataclasses to materialise rows.
            from ..sqlite_reader import read_eq_master

            cached = read_eq_master(self._sqlite_path)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_master_snapshots", "latest master snapshot")
        records = self._load_or_fetch("get_eq_master")
        return [normalize_security_master(record) for record in records]

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_fin_summaries

            cached = read_fin_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_fin_summaries", f"{start.isoformat()}..{end.isoformat()}"
        )
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_fin_summary_range", start, end)
            cached = read_fin_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_fin_summaries "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_fin_summary_range", start, end)
        return [
            summary
            for record in records
            if (summary := normalize_financial_summary(record)) is not None
        ]

    def get_eq_earnings_cal(self, start: date, end: date) -> list[dict[str, Any]]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_eq_earnings_cal

            cached = read_eq_earnings_cal(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_earnings_calendar", f"{start.isoformat()}..{end.isoformat()}"
        )
        records = self._load_or_fetch(
            "get_eq_earnings_cal",
            store_params={"requested_start": start, "requested_end": end},
        )
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        return [
            record
            for record in records
            if start_iso
            <= str(
                record.get("Date") or record.get("date") or record.get("AnnouncementDate") or ""
            )[:10]
            <= end_iso
        ]

    def _range_chunk_is_cached(self, method: str, start: date, end: date) -> bool:
        if self._sqlite_path is not None and method == "get_fin_summary_range":
            from ..sqlite_reader import read_fin_summaries

            return read_fin_summaries(self._sqlite_path, start, end) is not None
        return super()._range_chunk_is_cached(method, start, end)

    def _store_records(
        self,
        method: str,
        records: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> None:
        if self._sqlite_path is None:
            return
        if method == "get_eq_master":
            from ..sqlite_cache import store_jquants_master

            store_jquants_master(self._sqlite_path, records)
            return
        if method == "get_eq_earnings_cal":
            from ..sqlite_cache import store_jquants_earnings_calendar

            requested_start = params.get("requested_start")
            requested_end = params.get("requested_end")
            store_jquants_earnings_calendar(
                self._sqlite_path,
                records,
                requested_start=requested_start if isinstance(requested_start, date) else None,
                requested_end=requested_end if isinstance(requested_end, date) else None,
            )
            return
        if method == "get_fin_summary_range":
            from ..sqlite_cache import store_jquants_fin_summaries

            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                return
            store_jquants_fin_summaries(
                self._sqlite_path,
                records,
                requested_start=start,
                requested_end=end,
            )
            return
        super()._store_records(method, records, params)


def normalize_sector_name(value: Any) -> str:
    # J-Quants payloads sometimes return the half-width katakana middle dot
    # ("情報･通信業", U+FF65) and other times the full-width middle dot
    # ("情報・通信業", U+30FB) for the same TSE 33 sector. Pick the full-width
    # form as canonical so downstream views and matchers see one spelling.
    text = str(value or "")
    return text.replace("･", "・")


def normalize_security_master(record: Mapping[str, Any]) -> SecurityMaster:
    code, common_code = _parse_jquants_code(
        _first_value(record, "Code", "code", "LocalCode", "local_code")
    )
    name = _first_value(record, "CompanyName", "company_name", "Name", "name", "CoName", "co_name")
    market_segment = _first_value(
        record,
        "MarketCodeName",
        "market_segment",
        "Section",
        "section",
        "MktNm",
        "mkt_nm",
    )
    sector_33 = normalize_sector_name(
        _first_value(
            record,
            "Sector33CodeName",
            "sector_33",
            "Sector33Name",
            "S33Nm",
            "s33_nm",
        )
    )
    # J-Quants v2 /listed/info (get_eq_master) does not return a listing date field.
    # Consumers that need listing_span compute it from bars history instead of
    # relying on a master-side listing_date proxy.
    is_common_stock = bool(
        record.get("is_common_stock")
        if "is_common_stock" in record
        else _first_value(
            record, "TypeOfDocument", "SecurityType", "security_type", default="common"
        ).lower()
        in {"common", "common stock", "普通株"}
    )
    if not common_code:
        is_common_stock = False
    return SecurityMaster(
        code=code,
        name=str(name),
        market_segment=str(market_segment),
        sector_33=str(sector_33),
        is_common_stock=is_common_stock,
    )


def normalize_financial_summary(record: Mapping[str, Any]) -> JQuantsFinancialSummary | None:
    ticker, common_code = _parse_jquants_code(_first_value(record, "Code", "code"))
    if not common_code:
        return None
    # すべて `_coalesce_field` 経由にして 0.0 の数値フィールドを欠損と誤判定しないようにする
    # (EPS=0 の赤字転換点、Sales=0 の新規事業初期、OP=0 の損益分岐点ちょうど、など)。
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=_parse_date(
            _first_value(
                record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date", "date"
            )
        ),
        forecast_eps=_to_float(
            _coalesce_field(record, "ForecastEPS", "forecast_eps", "FEPS", "f_eps")
        ),
        eps_ttm=_to_float(
            _coalesce_field(
                record,
                "EpsTtm",
                "eps_ttm",
                "EarningsPerShare",
                "earnings_per_share",
                "EPS",
                "eps",
            )
        ),
        bps=_to_float(
            _coalesce_field(record, "BPS", "bps", "BookValuePerShare", "book_value_per_share")
        ),
        shares_outstanding=_to_float(
            _coalesce_field(
                record,
                "SharesOutstanding",
                "shares_outstanding",
                "IssuedShareEquityQuote",
                "issued_share_equity_quote",
                "ShOutFY",
                "AvgSh",
            )
        ),
        sales=_to_float(_coalesce_field(record, "NetSales", "net_sales", "Sales", "sales")),
        cfo=_to_float(
            _coalesce_field(
                record,
                "CashFlowsFromOperatingActivities",
                "cash_flows_from_operating_activities",
                "OperatingCashFlow",
                "operating_cash_flow",
                "CFO",
                "cfo",
            )
        ),
        cash_eq=_to_float(
            _coalesce_field(
                record,
                "CashAndEquivalents",
                "cash_and_equivalents",
                "CashEq",
                "cash_eq",
            )
        ),
        total_assets=_to_float(_coalesce_field(record, "TotalAssets", "total_assets", "TA", "ta")),
        equity=_to_float(_coalesce_field(record, "Equity", "equity", "Eq", "eq")),
        operating_profit=_to_float(
            _coalesce_field(record, "OperatingProfit", "operating_profit", "OP")
        ),
        ordinary_profit=_to_float(
            _coalesce_field(record, "OrdinaryProfit", "ordinary_profit", "OdP")
        ),
        profit=_to_float(_coalesce_field(record, "Profit", "profit", "NP")),
        fiscal_period=_to_period(
            _coalesce_field(record, "TypeOfCurrentPeriod", "type_of_current_period")
        ),
        fiscal_year_end=_parse_optional_date(
            _coalesce_field(record, "CurrentFiscalYearEndDate", "current_fiscal_year_end_date")
        ),
        period_start=_parse_optional_date(
            _coalesce_field(record, "CurrentPeriodStartDate", "current_period_start_date")
        ),
        period_end=_parse_optional_date(
            _coalesce_field(record, "CurrentPeriodEndDate", "current_period_end_date")
        ),
    )
