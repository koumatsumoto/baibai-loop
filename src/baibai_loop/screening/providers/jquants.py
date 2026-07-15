"""Screening J-Quants provider: fundamentals fetch on top of the market adapter.

The price/calendar fetch engine, error type, code parsing, and bar/calendar
normalization live in `baibai_loop.market`; this module re-exposes them and adds
the screening-only endpoints (master snapshot and financial summaries) by
extending `JQuantsMarketProvider`.
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
    coalesce_field,
    first_value,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    to_float,
    to_period,
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

from ..master_snapshot import normalize_sector_name, validate_master_snapshot
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
    dps_actual_annual: float | None = None
    dps_forecast_annual: float | None = None

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
        "dps_actual_annual",
        "dps_forecast_annual",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return validate_finite(value)


class JQuantsProvider(JQuantsMarketProvider):
    """Cache-first adapter for jquantsapi.ClientV2: price/calendar + fundamentals."""

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        if self._sqlite_path is not None:
            # Imported lazily to avoid a circular import: sqlite_reader pulls in
            # this module's schema dataclasses to materialise rows.
            from ..sqlite_reader import read_eq_master_exact

            cached = read_eq_master_exact(self._sqlite_path, requested_asof)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_master_snapshots", requested_asof.isoformat())
        records = self._load_or_fetch(
            "get_eq_master",
            store_params={"requested_asof": requested_asof},
            date=requested_asof.isoformat(),
        )
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_eq_master_exact

            stored = read_eq_master_exact(self._sqlite_path, requested_asof)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_master_snapshots "
                    f"for {requested_asof.isoformat()}"
                )
            return stored
        validated = validate_master_snapshot(records, requested_asof)
        return [
            SecurityMaster(
                code=ticker,
                name=name,
                market_segment=market,
                sector_33=sector,
                is_common_stock=bool(is_common),
            )
            for _, ticker, name, market, sector, is_common in validated.rows
        ]

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

            requested_asof = params.get("requested_asof")
            if not isinstance(requested_asof, date):
                raise JQuantsProviderError("get_eq_master store requires requested_asof")
            store_jquants_master(self._sqlite_path, records, requested_asof=requested_asof)
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


def normalize_security_master(record: Mapping[str, Any]) -> SecurityMaster:
    code, common_code = parse_jquants_code_parts(
        first_value(record, "Code", "code", "LocalCode", "local_code")
    )
    name = first_value(record, "CompanyName", "company_name", "Name", "name", "CoName", "co_name")
    market_segment = first_value(
        record,
        "MarketCodeName",
        "market_segment",
        "Section",
        "section",
        "MktNm",
        "mkt_nm",
    )
    sector_33 = normalize_sector_name(
        first_value(
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
        else first_value(
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
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # すべて `coalesce_field` 経由にして 0.0 の数値フィールドを欠損と誤判定しないようにする
    # (EPS=0 の赤字転換点、Sales=0 の新規事業初期、OP=0 の損益分岐点ちょうど、など)。
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=parse_date(
            first_value(
                record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date", "date"
            )
        ),
        # ClientV2 の fin-summary は短縮キーを返す。FEPS=当期予想 EPS は本決算(FY)
        # 開示で空になり、翌期ガイダンスは NxFEPS に入る。FEPS→NxFEPS の順で各時点の
        # 最良 forward EPS(per_forward の基)を埋める。
        forecast_eps=to_float(coalesce_field(record, "FEPS", "NxFEPS")),
        eps_ttm=to_float(
            coalesce_field(
                record,
                "EpsTtm",
                "eps_ttm",
                "EarningsPerShare",
                "earnings_per_share",
                "EPS",
                "eps",
            )
        ),
        bps=to_float(
            coalesce_field(record, "BPS", "bps", "BookValuePerShare", "book_value_per_share")
        ),
        shares_outstanding=to_float(
            coalesce_field(
                record,
                "SharesOutstanding",
                "shares_outstanding",
                "IssuedShareEquityQuote",
                "issued_share_equity_quote",
                "ShOutFY",
                "AvgSh",
            )
        ),
        sales=to_float(coalesce_field(record, "NetSales", "net_sales", "Sales", "sales")),
        cfo=to_float(
            coalesce_field(
                record,
                "CashFlowsFromOperatingActivities",
                "cash_flows_from_operating_activities",
                "OperatingCashFlow",
                "operating_cash_flow",
                "CFO",
                "cfo",
            )
        ),
        cash_eq=to_float(
            coalesce_field(
                record,
                "CashAndEquivalents",
                "cash_and_equivalents",
                "CashEq",
                "cash_eq",
            )
        ),
        total_assets=to_float(coalesce_field(record, "TotalAssets", "total_assets", "TA", "ta")),
        equity=to_float(coalesce_field(record, "Equity", "equity", "Eq", "eq")),
        operating_profit=to_float(
            coalesce_field(record, "OperatingProfit", "operating_profit", "OP")
        ),
        ordinary_profit=to_float(
            coalesce_field(record, "OrdinaryProfit", "ordinary_profit", "OdP")
        ),
        profit=to_float(coalesce_field(record, "Profit", "profit", "NP")),
        fiscal_period=to_period(
            coalesce_field(record, "TypeOfCurrentPeriod", "type_of_current_period")
        ),
        fiscal_year_end=parse_optional_date(
            coalesce_field(record, "CurrentFiscalYearEndDate", "current_fiscal_year_end_date")
        ),
        period_start=parse_optional_date(
            coalesce_field(record, "CurrentPeriodStartDate", "current_period_start_date")
        ),
        period_end=parse_optional_date(
            coalesce_field(record, "CurrentPeriodEndDate", "current_period_end_date")
        ),
        # DivAnn=実績年間 DPS。予想年間は FDivAnn (四半期) → NxFDivAnn (本決算の
        # 進行期ガイダンス) の順で埋める (FEPS→NxFEPS と同型)。
        dps_actual_annual=to_float(coalesce_field(record, "DivAnn")),
        dps_forecast_annual=to_float(coalesce_field(record, "FDivAnn", "NxFDivAnn")),
    )
