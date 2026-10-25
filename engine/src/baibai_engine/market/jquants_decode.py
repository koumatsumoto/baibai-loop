"""J-Quants共通の値・日付・code正規化。取得処理から独立したprovider固有の変換。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from math import isfinite
from typing import Any

from baibai_engine.data_collection.identifiers import SecurityCodeError, parse_security_code_parts
from baibai_engine.market.calendar import JQuantsMarketCalendarDay
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar


class JQuantsProviderError(RuntimeError):
    """Raised when a required J-Quants fetch or normalization fails."""


def parse_jquants_code_parts(code: Any) -> tuple[str, bool]:
    """Decode the primary issue suffix; ETF/REIT classification requires master ProdCat."""
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
    if factor is None:
        if kind is not None:
            raise JQuantsProviderError("reported corporate action has no usable adjustment factor")
        return None
    if factor == 1.0 and kind is None:
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


def parse_date(value: Any) -> date:
    if is_missing_scalar(value):
        raise JQuantsProviderError("missing date field in payload")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


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


def provider_record_payload(record: Mapping[str, Any]) -> str:
    """Retain every SDK field; non-finite missing markers are strings, not JSON NaN."""
    values = {
        str(key): str(value) if isinstance(value, float) and not isfinite(value) else value
        for key, value in record.items()
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
