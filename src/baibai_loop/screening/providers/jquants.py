from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from ..config import JQUANTS_CLIENT_V2_METHODS
from ..schema import SecurityMaster, normalize_ticker

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


class JQuantsProviderError(RuntimeError):
    """Raised when a required J-Quants fetch or normalization fails."""


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JQuantsDailyBar:
    ticker: str
    traded_at: date
    close: float
    turnover_value: float | None
    adjustment_close: float | None = None
    adjustment_factor: float | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("close", "turnover_value", "adjustment_close", "adjustment_factor")
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
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
        return _validate_finite(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool


class JQuantsProvider:
    """Thin cache-first adapter for jquantsapi.ClientV2 only."""

    _RANGE_CHUNK_DAYS: ClassVar[Mapping[str, int]] = {
        # ClientV2 range helpers fan out to per-day API calls internally.
        # Long windows can hit J-Quants 429s, so persist smaller chunks to make
        # retries resumable and keep the behavior understandable from static code.
        # Both methods use 31 days to equalize per-chunk burst and make recovery
        # from 429 predictable across method transitions.
        "get_eq_bars_daily_range": 31,
        "get_fin_summary_range": 31,
    }
    # Backoff to absorb J-Quants Light 429s. Observed in practice that the
    # rate window can be several minutes, so the tail of the tuple keeps
    # climbing past 5 minutes. Total worst-case wait per chunk ~= 17.5 min.
    _RATE_LIMIT_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
    _INTER_CHUNK_SLEEP_SECONDS = 3.0

    def __init__(
        self,
        refresh_token: str,
        cache_dir: Path,
        client: Any | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
    ) -> None:
        self._refresh_token = refresh_token
        self._cache_dir = Path(cache_dir) / "jquants"
        self._client = client
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only

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

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_daily_bars

            cached = read_daily_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_eq_bars_daily_range", start, end)
            cached = read_daily_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_daily_bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_eq_bars_daily_range", start, end)
        return [bar for record in records if (bar := normalize_daily_bar(record)) is not None]

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

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_market_calendar

            cached = read_market_calendar(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_market_calendar", f"{start.isoformat()}..{end.isoformat()}"
        )
        # holiday_division フィルタは掛けない。"1"=営業日だけでなく "2"=半日営業 (大納会など)
        # も取引あり扱いすべきで、事前フィルタで "2" を落とすと正しい営業日で asof が
        # reject される。normalize 側で "1"/"2" を営業扱いにする。
        records = self._load_or_fetch(
            "get_mkt_calendar",
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
        return [normalize_market_calendar(record) for record in records]

    def _load_or_fetch_range(self, method: str, start: date, end: date) -> list[dict[str, Any]]:
        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        if chunk_days is None or (end - start).days < chunk_days:
            return self._load_or_fetch(method, start_dt=start, end_dt=end)

        records: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            records.extend(self._load_or_fetch(method, start_dt=cursor, end_dt=chunk_end))
            cursor = chunk_end + timedelta(days=1)
            # Pace provider fetches to avoid tripping J-Quants rate limits.
            if cursor <= end:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)
        return records

    def _fetch_missing_range_chunks(self, method: str, start: date, end: date) -> None:
        if self._sqlite_path is None:
            return
        from ..sqlite_reader import read_daily_bars, read_fin_summaries

        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        if chunk_days is None:
            self._load_or_fetch(method, start_dt=start, end_dt=end)
            return

        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            did_fetch = False
            if method == "get_eq_bars_daily_range":
                chunk_is_cached = read_daily_bars(self._sqlite_path, cursor, chunk_end) is not None
            elif method == "get_fin_summary_range":
                chunk_is_cached = (
                    read_fin_summaries(self._sqlite_path, cursor, chunk_end) is not None
                )
            else:
                chunk_is_cached = False
            if not chunk_is_cached:
                self._load_or_fetch(method, start_dt=cursor, end_dt=chunk_end)
                did_fetch = True
            cursor = chunk_end + timedelta(days=1)
            if did_fetch and cursor <= end:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)

    def _load_or_fetch(
        self,
        method: str,
        *,
        store_params: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        if method not in JQUANTS_CLIENT_V2_METHODS:
            raise JQuantsProviderError(f"unsupported ClientV2 method: {method}")

        client = self._get_client()
        call = getattr(client, method, None)
        if call is None:
            raise JQuantsProviderError(f"ClientV2 missing method: {method}")
        payload = self._call_with_retry(method, call, **params)

        records = _payload_to_records(payload)
        self._store_records(method, records, store_params or params)
        return records

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise JQuantsProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: bootstrap or repair "
            "SQLite before running screening."
        )

    def _store_records(
        self,
        method: str,
        records: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> None:
        if self._sqlite_path is None:
            return
        from ..sqlite_cache import (
            store_jquants_daily_bars,
            store_jquants_earnings_calendar,
            store_jquants_fin_summaries,
            store_jquants_market_calendar,
            store_jquants_master,
        )

        if method == "get_eq_master":
            store_jquants_master(self._sqlite_path, records)
            return
        if method == "get_eq_earnings_cal":
            requested_start = params.get("requested_start")
            requested_end = params.get("requested_end")
            store_jquants_earnings_calendar(
                self._sqlite_path,
                records,
                requested_start=requested_start if isinstance(requested_start, date) else None,
                requested_end=requested_end if isinstance(requested_end, date) else None,
            )
            return
        if method in {"get_eq_bars_daily_range", "get_fin_summary_range"}:
            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                return
            if method == "get_eq_bars_daily_range":
                store_jquants_daily_bars(
                    self._sqlite_path,
                    records,
                    requested_start=start,
                    requested_end=end,
                )
            else:
                store_jquants_fin_summaries(
                    self._sqlite_path,
                    records,
                    requested_start=start,
                    requested_end=end,
                )
            return
        if method == "get_mkt_calendar":
            start = _parse_yyyymmdd_param(params.get("from_yyyymmdd"))
            end = _parse_yyyymmdd_param(params.get("to_yyyymmdd"))
            if start is not None and end is not None:
                store_jquants_market_calendar(
                    self._sqlite_path,
                    records,
                    requested_start=start,
                    requested_end=end,
                )

    def _call_with_retry(self, method: str, call: Any, **params: Any) -> Any:
        last_exc: Exception | None = None
        attempts = 0
        for attempt, delay_seconds in enumerate((0, *self._RATE_LIMIT_BACKOFF_SECONDS), start=1):
            attempts = attempt
            try:
                return call(**_stringify_dates(params))
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_jquants_error(exc):
                    break
                if delay_seconds:
                    time.sleep(delay_seconds)
        # refresh_token / id_token 等の secret が exception 文字列に含まれる可能性に備えて
        # sanitize、さらに `from None` で原因チェーンを切って traceback 漏洩も遮断する。
        sanitized = self._sanitize_secret(str(last_exc)) if last_exc else ""
        exception_name = type(last_exc).__name__ if last_exc else "unknown"
        raise JQuantsProviderError(
            f"failed to fetch J-Quants payload via {method} after {attempts} attempts: "
            f"{exception_name}: {sanitized}"
        ) from None

    def _sanitize_secret(self, text: str) -> str:
        if self._refresh_token and self._refresh_token in text:
            return text.replace(self._refresh_token, "<redacted>")
        return text

    def _cache_path(self, method: str, params: Mapping[str, Any]) -> Path:
        suffix = "-".join(
            f"{key}-{value.isoformat() if hasattr(value, 'isoformat') else value}"
            for key, value in sorted(params.items())
        )
        filename = f"{method}.json" if not suffix else f"{method}-{suffix}.json"
        return self._cache_dir / filename

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import jquantsapi
        except ModuleNotFoundError as exc:
            raise JQuantsProviderError("jquantsapi is not installed") from exc
        self._client = jquantsapi.ClientV2(api_key=self._refresh_token)
        return self._client


_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_TEXT_MARKERS = (
    "Too Many Requests",
    "Service Unavailable",
    "Gateway Timeout",
    "Bad Gateway",
    "rate limit",
)


def _is_retryable_jquants_error(exc: Exception) -> bool:
    """Detect transient HTTP errors worth retrying.

    jquantsapi wraps urllib3 / requests exceptions, so status_code is not always
    directly accessible. Prefer status_code (via `exc.response.status_code`) when
    available; fall back to textual signatures that are unlikely to collide with
    unrelated error messages. Non-retryable errors (auth, schema) fail fast.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in _RETRYABLE_HTTP_STATUSES
    text = str(exc)
    if any(evidence_hit in text for evidence_hit in _RETRYABLE_TEXT_MARKERS):
        return True
    # Last-resort substring check for the raw status tokens. Narrower than pure
    # "429" match because it requires the status to appear as a standalone token.
    for status in _RETRYABLE_HTTP_STATUSES:
        token = f" {status} "
        if token in f" {text} ":
            return True
    return False


def parse_jquants_code(code: Any) -> str:
    """Return the 4-char ticker portion of a J-Quants 5-char issue code."""
    return _parse_jquants_code(code)[0]


def _parse_jquants_code(code: Any) -> tuple[str, bool]:
    raw = str(code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw), True
    if len(raw) == 5 and raw[:4].isalnum():
        # ClientV2 payloads use 5-char local codes, while J-Quants 4-char
        # code queries target common stock. Preserve the suffix only as the
        # common-code flag instead of merging non-zero suffix lines.
        return normalize_ticker(raw[:4]), raw.endswith("0")
    raise JQuantsProviderError(f"invalid J-Quants code: {code!r}")


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


def normalize_daily_bar(record: Mapping[str, Any]) -> JQuantsDailyBar | None:
    ticker, common_code = _parse_jquants_code(_first_value(record, "Code", "code"))
    if not common_code:
        return None
    # `or` チェーンは 0.0 を falsy 扱いして次キーに進むため、0 close/turnover の銘柄を
    # 欠損と誤判定する。キー存在と非 None を基準に最初の値を取る。
    close = _to_float(_coalesce_field(record, "Close", "close", "C", "c", "AdjC", "adj_close"))
    if close is None:
        # Non-trading day or missing close; treat as absent bar so history stays clean.
        return None
    # AdjustmentClose is the split-adjusted close. We keep it separate from the
    # raw `close` so that latest-day calculations stay on the unadjusted price
    # while historical series can use the adjusted value to avoid jumps at
    # split dates.
    adjustment_close = _to_float(
        _coalesce_field(record, "AdjustmentClose", "adjustment_close", "AdjC", "adj_c")
    )
    adjustment_factor = _to_float(
        _coalesce_field(record, "AdjustmentFactor", "adjustment_factor", "AdjFactor")
    )
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=_parse_date(_first_value(record, "Date", "date", "TradedAt", "traded_at")),
        close=close,
        adjustment_close=adjustment_close,
        adjustment_factor=adjustment_factor,
        turnover_value=_to_float(
            _coalesce_field(
                record,
                "TurnoverValue",
                "turnover_value",
                "TradingValue",
                "trading_value",
                "Va",
                "va",
            )
        ),
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


def normalize_market_calendar(record: Mapping[str, Any]) -> JQuantsMarketCalendarDay:
    return JQuantsMarketCalendarDay(
        day=_parse_date(_first_value(record, "Date", "date")),
        is_business_day=bool(
            record.get("is_business_day")
            if "is_business_day" in record
            # "1"=営業日、"2"=半日営業 (大納会など取引あり)。両方を営業扱い。
            else str(
                record.get("HolidayDivision")
                or record.get("holiday_division")
                or record.get("HolDiv")
                or record.get("hol_div")
                or ""
            ).lower()
            in {"1", "2", "business_day", "trading_day", "half_day"}
        ),
    )


def _payload_to_records(payload: Any) -> list[dict[str, Any]]:
    if hasattr(payload, "to_dict"):
        try:
            return _ensure_list(payload.to_dict(orient="records"))
        except TypeError:
            converted = payload.to_dict()
            return _ensure_list(converted)
    if isinstance(payload, Mapping):
        for key in ("data", "items", "info", "daily_quotes", "fin_summary", "calendar"):
            if key in payload and isinstance(payload[key], Sequence):
                return [dict(item) for item in payload[key]]
        return [dict(payload)]
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return [dict(item) if isinstance(item, Mapping) else {"value": item} for item in payload]
    raise JQuantsProviderError("unsupported J-Quants payload shape")


def _ensure_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) if isinstance(item, Mapping) else {"value": item} for item in value]
    if isinstance(value, Mapping):
        return [dict(value)]
    raise JQuantsProviderError("cached payload is not a list-like mapping")


def _stringify_dates(params: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in params.items():
        output[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return output


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        raise JQuantsProviderError("missing date field in payload")
    return date.fromisoformat(str(value)[:10])


def _parse_yyyymmdd_param(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def _parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return _parse_date(value)


def _to_period(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().upper()


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # J-Quants returns NaN for non-trading days on calendar-aligned payloads;
    # treat NaN as missing so downstream metrics do not propagate it.
    if result != result:
        return None
    return result


def _first_value(record: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    if default is not None:
        return default
    joined = ", ".join(keys)
    raise JQuantsProviderError(f"missing required field in payload: {joined}")


def _coalesce_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return the first key's value that is explicitly present and non-null/empty.

    Unlike chained `or`, this does NOT treat 0 / 0.0 / False as missing — so it is safe
    to use for numeric payload fields that may legitimately be zero.
    """
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _make_json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_make_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except TypeError:
            pass
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        return to_pydatetime().isoformat()
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return item_method()
        except Exception:
            return str(value)
    return value
