"""J-Quants共通の値・日付・code正規化。取得処理から独立したprovider固有の変換。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from math import isfinite
from typing import Any

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    JQuantsMarketCalendarDay,
)
from baibai_engine.market.ticker import SecurityCodeError, parse_security_code_parts


class JQuantsProviderError(RuntimeError):
    """Raised when a required J-Quants fetch or normalization fails."""


def parse_jquants_code(code: Any) -> str:
    """Return the 4-char ticker portion of a J-Quants 5-char issue code."""
    return parse_jquants_code_parts(code)[0]


def parse_jquants_code_parts(code: Any) -> tuple[str, bool]:
    try:
        return parse_security_code_parts(code)
    except SecurityCodeError as exc:
        raise JQuantsProviderError(f"invalid J-Quants code: {code!r}") from exc


def normalize_daily_bar(record: Mapping[str, Any]) -> JQuantsDailyBar | None:
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    # Decode native V2 fields without substituting adjusted values or legacy aliases.
    close = to_float(record.get("C"))
    if close is None:
        # An adjusted-only observation cannot supply a raw price bar. The
        # independent factor event remains available through the store.
        return None
    adjustment_factor = to_float(record.get("AdjFactor"))
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=parse_date(record.get("Date")),
        close=close,
        adjustment_factor=adjustment_factor,
        ex_right_type=normalize_ex_right_type(record.get("ExRT")),
        volume=to_float(record.get("Vo")),
        turnover_value=to_float(record.get("Va")),
    )


def normalize_adjustment_factor_event(
    record: Mapping[str, Any],
) -> JQuantsAdjustmentFactorEvent | None:
    """Decode a corporate action without requiring a close or a non-unit factor."""

    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    factor = to_float(record.get("AdjFactor"))
    kind = normalize_ex_right_type(record.get("ExRT"))
    if factor is None or (factor == 1.0 and kind is None):
        return None
    return JQuantsAdjustmentFactorEvent(
        ticker=ticker,
        traded_at=parse_date(record.get("Date")),
        adjustment_factor=factor,
        ex_right_type=kind,
    )


def normalize_ex_right_type(value: Any) -> str | None:
    """Keep the source event kind; unknown future values are not relabelled as splits."""
    return None if is_missing_scalar(value) else str(value).strip() or None


def normalize_market_calendar(record: Mapping[str, Any]) -> JQuantsMarketCalendarDay:
    """Project the source calendar to cash-equity sessions, not derivatives sessions."""
    return JQuantsMarketCalendarDay(
        day=parse_date(record.get("Date")),
        is_business_day=str(record["HolDiv"]) in {"1", "2"},
    )


def _payload_to_records(payload: Any) -> list[dict[str, Any]]:
    """Read ClientV2 record rows without guessing obsolete envelope shapes."""
    to_dict = getattr(payload, "to_dict", None)
    records = to_dict(orient="records") if callable(to_dict) else payload
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise JQuantsProviderError("J-Quants payload must contain record rows")
    if any(not isinstance(row, Mapping) for row in records):
        raise JQuantsProviderError("J-Quants payload contains a non-record row")
    return [dict(row) for row in records]


def _stringify_dates(params: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in params.items():
        output[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return output


def parse_date(value: Any) -> date:
    if is_missing_scalar(value):
        raise JQuantsProviderError("missing date field in payload")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_yyyymmdd_param(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def parse_optional_date(value: Any) -> date | None:
    if is_missing_scalar(value):
        return None
    return parse_date(value)


def to_period(value: Any) -> str | None:
    if is_missing_scalar(value):
        return None
    return str(value).strip().upper()


def is_missing_scalar(value: Any) -> bool:
    """Recognize scalar missing markers produced by JSON and pandas without importing pandas."""

    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "NaT", "nan", "null", "<NA>"}
    if str(value).strip() in {"NaT", "nan", "<NA>"}:
        return True
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return False


def to_float(value: Any) -> float | None:
    if is_missing_scalar(value) or value == "-":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # ClientV2 DataFrames preserve pandas' non-finite markers when converted to
    # records. They are not numeric observations. Fiscal-period selection is independent
    # of value availability; an unknown number cannot select another forecast period.
    if not isfinite(result):
        return None
    return result


def has_period_slot(record: Mapping[str, Any], key: str) -> bool:
    """Whether a period-specific field is present rather than explicitly empty.

    A non-finite DataFrame scalar represents an unknown value for that period.  It is
    still a selected period slot and must not make a different forecast period win.
    """

    if key not in record or record[key] is None:
        return False
    value = record[key]
    return not (isinstance(value, str) and value.strip() in {"", "-", "null"})
