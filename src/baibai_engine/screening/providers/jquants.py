"""Screening J-Quants provider: fundamentals fetch on top of the market adapter.

The price/calendar fetch engine, error type, code parsing, and bar/calendar
normalization live in `baibai_engine.market`; this module re-exposes them and adds
the screening-only endpoints (master snapshot and financial summaries) by
extending `JQuantsMarketProvider`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from pydantic import field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.bars import (
    MODEL_CONFIG,
    validate_finite,
)
from baibai_engine.market.bars import (
    JQuantsDailyBar as JQuantsDailyBar,
)
from baibai_engine.market.bars import (
    JQuantsMarketCalendarDay as JQuantsMarketCalendarDay,
)
from baibai_engine.market.jquants import (
    JQuantsProviderError as JQuantsProviderError,
)
from baibai_engine.market.jquants import (
    coalesce_field,
    first_value,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    to_float,
    to_period,
)
from baibai_engine.market.jquants import (
    normalize_daily_bar as normalize_daily_bar,
)
from baibai_engine.market.jquants import (
    normalize_market_calendar as normalize_market_calendar,
)
from baibai_engine.market.jquants import (
    parse_jquants_code as parse_jquants_code,
)
from baibai_engine.market.provider import JQuantsMarketProvider

from ..master_snapshot import normalize_sector_name, validate_master_snapshot
from ..schema import SecurityMaster, normalize_ticker


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsWeeklyMargin:
    """One ticker's margin balances as of one weekly balance date.

    ``issue_type`` says whether a short balance is possible at all: a 信用銘柄
    carries no stock lending, so its zero short balance describes the instrument
    rather than positioning. Standard and negotiable balances are separate
    because only the standard side carries a six-month settlement deadline.
    """

    ticker: str
    week_end: date
    long_vol: float | None = None
    short_vol: float | None = None
    long_std_vol: float | None = None
    long_neg_vol: float | None = None
    short_std_vol: float | None = None
    short_neg_vol: float | None = None
    issue_type: str | None = None


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
    # 会社予想の当期純利益・経常利益。forecast_eps と同一予想期 (当期予想 or 翌期予想)
    # から採ったペアで、両方揃うときだけ純利益>経常の一時益 flag を機械判定できる。
    forecast_profit: float | None = None
    forecast_ordinary_profit: float | None = None
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
        "forecast_profit",
        "forecast_ordinary_profit",
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

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        """Fetch every ticker's margin balance for one weekly balance date.

        The endpoint answers per balance date, and not every week has one: the
        exchange skips weeks it does not publish, so an empty answer is a fact
        about that week rather than a failure. The coverage row records the
        attempt either way, which is what keeps a skipped week from being asked
        for again on every pass.
        """
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_weekly_margin

            cached = read_weekly_margin(self._sqlite_path, week_end)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_weekly_margin", week_end.isoformat())
        records = self._load_or_fetch(
            "get_mkt_margin_interest",
            store_params={"week_end": week_end},
            date_yyyymmdd=week_end.strftime("%Y%m%d"),
        )
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_weekly_margin

            stored = read_weekly_margin(self._sqlite_path, week_end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_weekly_margin "
                    f"for {week_end.isoformat()}"
                )
            return stored
        return [
            margin
            for record in records
            if (margin := normalize_weekly_margin(record, week_end)) is not None
        ]

    def get_adjustment_factor_bars_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        """Return only split events while still proving the full bar range is covered."""
        if self._sqlite_path is not None:
            from baibai_engine.market.store import read_adjustment_factor_bars

            cached = read_adjustment_factor_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_eq_bars_daily_range", start, end)
            cached = read_adjustment_factor_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching split-normalization bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_eq_bars_daily_range", start, end)
        return [
            bar
            for record in records
            if (bar := normalize_daily_bar(record)) is not None
            and bar.adjustment_factor not in (None, 0.0, 1.0)
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

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        """Return only FY rows while still proving the full summary range is covered."""
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_fy_summaries

            cached = read_fy_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_fin_summaries", f"{start.isoformat()}..{end.isoformat()}"
        )
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_fin_summary_range", start, end)
            cached = read_fy_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching normalized-profit summaries "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_fin_summary_range", start, end)
        return [
            summary
            for record in records
            if (summary := normalize_financial_summary(record)) is not None
            and summary.fiscal_period == "FY"
        ]

    def refresh_fin_summary_range(
        self, start: date, end: date, *, revision_overlap_days: int
    ) -> int:
        """Bring the summaries store current for `[start, end]` and count what it holds.

        Two things are fetched: what the coverage says is missing, and the trailing
        ``revision_overlap_days`` regardless of coverage. The overlap is the only
        way a filing the provider published late — for a date already fetched — ever
        lands, and today that protection exists only as an accident of the sliding
        chunk grid, which re-reads between 1 and 31 days depending on the as-of.

        The two are merged before anything is requested, so a stop longer than the
        overlap costs the outage and not the outage plus a week. Asking once, here,
        is also what keeps the 730-day and 2,200-day windows of the same source from
        each paying for the same trailing week.
        """
        window = f"{start.isoformat()}..{end.isoformat()}"
        if self._sqlite_path is None:
            return len(self._load_or_fetch_range("get_fin_summary_range", start, end))
        self._raise_if_cache_only("jquants_fin_summaries", window)
        from baibai_engine.market.sqlite import merge_date_ranges

        from ..sqlite_reader import count_fin_summaries

        overlap_start = max(start, end - timedelta(days=revision_overlap_days))
        planned = merge_date_ranges(
            [*self._missing_subranges("get_fin_summary_range", start, end), (overlap_start, end)]
        )
        for subrange_start, subrange_end in planned:
            self._load_or_fetch_range("get_fin_summary_range", subrange_start, subrange_end)
        return count_fin_summaries(self._sqlite_path, start, end)

    def _range_chunk_is_cached(self, method: str, start: date, end: date) -> bool:
        if self._sqlite_path is not None and method == "get_fin_summary_range":
            from ..sqlite_reader import fin_summaries_covered

            return fin_summaries_covered(self._sqlite_path, start, end)
        return super()._range_chunk_is_cached(method, start, end)

    def _missing_subranges(
        self, method: str, start: date, end: date
    ) -> tuple[tuple[date, date], ...]:
        """Ask the summaries store what it lacks instead of walking the request.

        The request window is ``asof - N days``, so its chunk grid shifts by a day
        every run and the chunk holding the as-of covers up to 31 days that are
        already stored. J-Quants expands a range into per-day calls, so that grid
        costs a month of calls to add one day. Reading the coverage complement
        instead makes the fetch as wide as the gap: one day on a daily run, exactly
        the outage after a stop, and the full window on an empty store.
        """
        if self._sqlite_path is None or method != "get_fin_summary_range":
            return super()._missing_subranges(method, start, end)
        from baibai_engine.market.sqlite import connect_current, missing_intervals

        conn = connect_current(self._sqlite_path)
        if conn is None:
            return super()._missing_subranges(method, start, end)
        try:
            return missing_intervals(conn, "jquants_fin_summaries", start, end)
        finally:
            conn.close()

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
        if method == "get_mkt_margin_interest":
            from ..sqlite_cache import store_jquants_weekly_margin

            week_end = params.get("week_end")
            if not isinstance(week_end, date):
                raise JQuantsProviderError("get_mkt_margin_interest store requires week_end")
            store_jquants_weekly_margin(self._sqlite_path, records, week_end=week_end)
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


def normalize_weekly_margin(
    record: Mapping[str, Any], week_end: date
) -> JQuantsWeeklyMargin | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # `coalesce_field` throughout: a zero balance is an observation, and for a
    # 信用銘柄 the zero short balance is the normal state rather than a gap.
    return JQuantsWeeklyMargin(
        ticker=ticker,
        week_end=week_end,
        long_vol=to_float(coalesce_field(record, "LongVol", "long_vol")),
        short_vol=to_float(coalesce_field(record, "ShrtVol", "shrt_vol")),
        long_std_vol=to_float(coalesce_field(record, "LongStdVol", "long_std_vol")),
        long_neg_vol=to_float(coalesce_field(record, "LongNegVol", "long_neg_vol")),
        short_std_vol=to_float(coalesce_field(record, "ShrtStdVol", "shrt_std_vol")),
        short_neg_vol=to_float(coalesce_field(record, "ShrtNegVol", "shrt_neg_vol")),
        issue_type=_issue_type(coalesce_field(record, "IssType", "iss_type")),
    )


def _issue_type(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def normalize_financial_summary(record: Mapping[str, Any]) -> JQuantsFinancialSummary | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # 会社予想の純利益/経常のペアは forecast_eps と同一予想期から採る。当期予想 EPS
    # (FEPS) が埋まっていれば当期予想の FNP/FOdP、本決算開示で FEPS が空なら翌期
    # ガイダンスの NxFNp/NxFOdP を採る (forecast_eps の FEPS→NxFEPS と同じ期選択)。
    # 期をまたいだ比較 (当期 EPS 期 と翌期利益の突合) は一時益 flag の誤検出になるので混ぜない。
    if coalesce_field(record, "FEPS") is not None:
        forecast_profit = to_float(coalesce_field(record, "FNP"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "FOdP"))
    else:
        forecast_profit = to_float(coalesce_field(record, "NxFNp"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "NxFOdP"))
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
        forecast_profit=forecast_profit,
        forecast_ordinary_profit=forecast_ordinary_profit,
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
