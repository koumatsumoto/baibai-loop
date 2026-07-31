"""J-Quants adapter primitives shared by the market fetch/store layers.

Holds the J-Quants error type, issue-code parsing, daily-bar / market-calendar
normalization, and the payload-shape helpers used to coerce ClientV2 responses
into the market dataclasses. Screening's fundamentals provider reuses these so
both the price/calendar path (market) and the master/fin-summary path
(screening) share one J-Quants decoding contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from baibai_engine.market.bars import JQuantsDailyBar, JQuantsMarketCalendarDay
from baibai_engine.market.ticker import normalize_ticker


class JQuantsProviderError(RuntimeError):
    """Raised when a required J-Quants fetch or normalization fails."""


def parse_jquants_code(code: Any) -> str:
    """Return the 4-char ticker portion of a J-Quants 5-char issue code."""
    return parse_jquants_code_parts(code)[0]


def parse_jquants_code_parts(code: Any) -> tuple[str, bool]:
    raw = str(code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw), True
    if len(raw) == 5 and raw[:4].isalnum():
        # ClientV2 payloads use 5-char local codes, while J-Quants 4-char
        # code queries target common stock. Preserve the suffix only as the
        # common-code flag instead of merging non-zero suffix lines.
        return normalize_ticker(raw[:4]), raw.endswith("0")
    raise JQuantsProviderError(f"invalid J-Quants code: {code!r}")


def normalize_daily_bar(record: Mapping[str, Any]) -> JQuantsDailyBar | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # `or` チェーンは 0.0 を falsy 扱いして次キーに進むため、0 close/turnover の銘柄を
    # 欠損と誤判定する。キー存在と非 None を基準に最初の値を取る。
    close = to_float(coalesce_field(record, "Close", "close", "C", "c", "AdjC", "adj_close"))
    if close is None:
        # Non-trading day or missing close; treat as absent bar so history stays clean.
        return None
    # AdjustmentClose is the split-adjusted close. We keep it separate from the
    # raw `close` so that latest-day calculations stay on the unadjusted price
    # while historical series can use the adjusted value to avoid jumps at
    # split dates.
    adjustment_close = to_float(
        coalesce_field(record, "AdjustmentClose", "adjustment_close", "AdjC", "adj_c")
    )
    adjustment_factor = to_float(
        coalesce_field(record, "AdjustmentFactor", "adjustment_factor", "AdjFactor")
    )
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=parse_date(first_value(record, "Date", "date", "TradedAt", "traded_at")),
        close=close,
        adjustment_close=adjustment_close,
        adjustment_factor=adjustment_factor,
        volume=to_float(coalesce_field(record, "Volume", "volume", "Vo", "vo")),
        turnover_value=to_float(
            coalesce_field(
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


def normalize_market_calendar(record: Mapping[str, Any]) -> JQuantsMarketCalendarDay:
    return JQuantsMarketCalendarDay(
        day=parse_date(first_value(record, "Date", "date")),
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


def parse_date(value: Any) -> date:
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


def parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return parse_date(value)


def to_period(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().upper()


def to_float(value: Any) -> float | None:
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


def first_value(record: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    if default is not None:
        return default
    joined = ", ".join(keys)
    raise JQuantsProviderError(f"missing required field in payload: {joined}")


def coalesce_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return the first key's value that is explicitly present and non-null/empty.

    Unlike chained `or`, this does NOT treat 0 / 0.0 / False as missing — so it is safe
    to use for numeric payload fields that may legitimately be zero.
    """
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None
