from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import JQUANTS_CLIENT_V2_METHODS
from ..schema import SecurityMaster, normalize_ticker


class JQuantsProviderError(RuntimeError):
    """Raised when a required J-Quants fetch or normalization fails."""


@dataclass(frozen=True)
class JQuantsDailyBar:
    ticker: str
    traded_at: date
    close: float
    turnover_value: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))


@dataclass(frozen=True)
class JQuantsFinancialSummary:
    ticker: str
    disclosed_at: date
    forecast_eps: float | None
    eps_ttm: float | None
    bps: float | None
    shares_outstanding: float | None
    sales: float | None
    operating_profit: float | None
    ordinary_profit: float | None
    profit: float | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))


@dataclass(frozen=True)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool


class JQuantsProvider:
    """Thin cache-first adapter for jquantsapi.ClientV2 only."""

    _RANGE_CHUNK_DAYS = {
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

    def __init__(self, refresh_token: str, cache_dir: Path, client: Any | None = None) -> None:
        self._refresh_token = refresh_token
        self._cache_dir = Path(cache_dir) / "jquants"
        self._client = client

    def get_eq_master(self) -> list[SecurityMaster]:
        records = self._load_or_fetch("get_eq_master")
        return [normalize_security_master(record) for record in records]

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        records = self._load_or_fetch_range("get_eq_bars_daily_range", start, end)
        return [bar for record in records if (bar := normalize_daily_bar(record)) is not None]

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        records = self._load_or_fetch_range("get_fin_summary_range", start, end)
        return [summary for record in records if (summary := normalize_financial_summary(record)) is not None]

    def get_eq_earnings_cal(self, start: date, end: date) -> list[dict[str, Any]]:
        records = self._load_or_fetch("get_eq_earnings_cal")
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        return [
            record
            for record in records
            if start_iso <= str(record.get("Date") or record.get("date") or record.get("AnnouncementDate") or "")[:10] <= end_iso
        ]

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        # holiday_division フィルタは掛けない。"1"=営業日だけでなく "2"=半日営業 (大納会など)
        # も取引あり扱いすべきで、事前フィルタで "2" を落とすと正しい営業日で asof が
        # reject される。normalize 側で "1"/"2" を営業扱いにする。
        records = self._load_or_fetch(
            "get_mkt_calendar",
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
        return [normalize_market_calendar(record) for record in records]

    def bootstrap_cache(self, start: date, end: date) -> dict[str, int]:
        return {
            "eq_master": len(self.get_eq_master()),
            "bars": len(self.get_eq_bars_daily_range(start, end)),
            "fin_summary": len(self.get_fin_summary_range(start, end)),
            "earnings_cal": len(self.get_eq_earnings_cal(start, end)),
            "market_calendar": len(self.get_mkt_calendar(start, end)),
        }

    def _load_or_fetch_range(self, method: str, start: date, end: date) -> list[dict[str, Any]]:
        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        if chunk_days is None or (end - start).days < chunk_days:
            return self._load_or_fetch(method, start_dt=start, end_dt=end)

        records: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            cache_path = self._cache_path(method, {"start_dt": cursor, "end_dt": chunk_end})
            was_cached = cache_path.exists()
            records.extend(self._load_or_fetch(method, start_dt=cursor, end_dt=chunk_end))
            cursor = chunk_end + timedelta(days=1)
            # Pace fresh fetches to avoid tripping J-Quants rate limits. Cache
            # hits skip the sleep so re-runs over already-cached ranges stay fast.
            if not was_cached and cursor <= end:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)
        return records

    def _load_or_fetch(self, method: str, **params: Any) -> list[dict[str, Any]]:
        if method not in JQUANTS_CLIENT_V2_METHODS:
            raise JQuantsProviderError(f"unsupported ClientV2 method: {method}")
        cache_path = self._cache_path(method, params)
        if cache_path.exists():
            return _ensure_list(json.loads(cache_path.read_text(encoding="utf-8")))

        client = self._get_client()
        call = getattr(client, method, None)
        if call is None:
            raise JQuantsProviderError(f"ClientV2 missing method: {method}")
        payload = self._call_with_retry(method, call, **params)

        records = _payload_to_records(payload)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                [_make_json_safe(record) for record in records],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return records

    def _call_with_retry(self, method: str, call: Any, **params: Any) -> Any:
        last_exc: Exception | None = None
        for attempt, delay_seconds in enumerate((0, *self._RATE_LIMIT_BACKOFF_SECONDS)):
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
        raise JQuantsProviderError(
            f"failed to fetch J-Quants payload via {method}: {type(last_exc).__name__ if last_exc else 'unknown'}: {sanitized}"
        ) from None

    def _sanitize_secret(self, text: str) -> str:
        if self._refresh_token and self._refresh_token in text:
            return text.replace(self._refresh_token, "<redacted>")
        return text

    def _cache_path(self, method: str, params: Mapping[str, Any]) -> Path:
        suffix = "-".join(f"{key}-{value.isoformat() if hasattr(value, 'isoformat') else value}" for key, value in sorted(params.items()))
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
_RETRYABLE_TEXT_SIGNALS = (
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
    if any(signal in text for signal in _RETRYABLE_TEXT_SIGNALS):
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
        # ClientV2 master/bars/summary payloads use 5-char local codes.
        # Unlike EDINET/JPX parsers, this public parser truncates the suffix
        # because J-Quants uses the 4-char code to request common stock only.
        # Common stock is typically suffixed with "0"; non-zero suffixes represent
        # preferred/other lines that should not be merged into the 4-char common ticker.
        return normalize_ticker(raw[:4]), raw.endswith("0")
    raise JQuantsProviderError(f"invalid J-Quants code: {code!r}")


def normalize_security_master(record: Mapping[str, Any]) -> SecurityMaster:
    code, common_code = _parse_jquants_code(_first_value(record, "Code", "code", "LocalCode", "local_code"))
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
    sector_33 = _first_value(
        record,
        "Sector33CodeName",
        "sector_33",
        "Sector33Name",
        "S33Nm",
        "s33_nm",
    )
    # J-Quants v2 /listed/info (get_eq_master) does not return a listing date field.
    # Consumers that need listing_span compute it from bars history instead of
    # relying on a master-side listing_date proxy.
    is_common_stock = bool(
        record.get("is_common_stock")
        if "is_common_stock" in record
        else _first_value(record, "TypeOfDocument", "SecurityType", "security_type", default="common").lower() in {"common", "common stock", "普通株"}
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
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=_parse_date(_first_value(record, "Date", "date", "TradedAt", "traded_at")),
        close=close,
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
        disclosed_at=_parse_date(_first_value(record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date", "date")),
        forecast_eps=_to_float(_coalesce_field(record, "ForecastEPS", "forecast_eps", "FEPS", "f_eps")),
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
        bps=_to_float(_coalesce_field(record, "BPS", "bps", "BookValuePerShare", "book_value_per_share")),
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
        operating_profit=_to_float(_coalesce_field(record, "OperatingProfit", "operating_profit", "OP")),
        ordinary_profit=_to_float(_coalesce_field(record, "OrdinaryProfit", "ordinary_profit", "OdP")),
        profit=_to_float(_coalesce_field(record, "Profit", "profit", "NP")),
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
    if isinstance(value, date):
        return value
    if not value:
        raise JQuantsProviderError("missing date field in payload")
    return date.fromisoformat(str(value)[:10])


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
