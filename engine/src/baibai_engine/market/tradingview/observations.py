"""Validate one batch without interpreting absent metrics as absent coverage."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal, get_args

# Raw field names below were observed with a 50-symbol Official MCP request.
FIELDS = {
    "analyst_rating": "AnalystRating",
    "recommendation_total": "recommendation_total",
    "price_target_average": "price_target_average",
    "price_target_high": "price_target_high",
    "price_target_low": "price_target_low",
    "eps_forecast_next_fq": "earnings_per_share_forecast_next_fq",
    "eps_forecast_next_fy": "earnings_per_share_forecast_next_fy",
    "revenue_forecast_next_fq": "revenue_forecast_next_fq",
    "revenue_forecast_next_fy": "revenue_forecast_next_fy",
    "forward_pe": "price_earnings_fwd",
    "forward_ps": "price_sales_fwd",
    "forward_ev_ebitda": "enterprise_value_ebitda_fwd",
    "ebitda_estimate_fy": "ebitda_estimate_fy",
    "free_cash_flow_estimate_fy": "free_cash_flow_estimate_fy",
    "capex_estimate_fy": "capital_expenditures_estimate_fy",
    "total_debt_estimate_fy": "total_debt_estimate_fy",
    "quote_close": "close",
    "quote_currency": "currency",
}
COLUMNS = list(FIELDS.values())


class FetchError(RuntimeError):
    """A provider or accounting failure; never a canonical missing observation."""


class ProviderHTTPError(FetchError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("TradingView provider HTTP error")


PayloadReason = Literal[
    "malformed_envelope",
    "malformed_missing_symbols",
    "duplicate_missing_symbol",
    "response_accounting_mismatch",
    "response_count_mismatch",
    "row_missing_requested_fields",
    "invalid_text_type",
    "invalid_analyst_count",
    "invalid_numeric",
]


class ProviderPayloadError(FetchError):
    def __init__(self, reason: PayloadReason, field: str | None = None) -> None:
        if reason not in get_args(PayloadReason) or (field is not None and field not in FIELDS):
            raise ValueError("Invalid payload diagnostic")
        self.reason = reason
        self.field = field
        super().__init__("TradingView provider payload failed validation")


class AllMissingError(FetchError):
    """Provider returned no usable symbols for a whole chunk."""


def normalize_batch(
    payload: dict[str, Any], symbols: list[str], fetched_at: datetime
) -> list[dict[str, Any]]:
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    data, missing = payload.get("data"), payload.get("missing")
    if (
        payload.get("success") is not True
        or not isinstance(data, dict)
        or not isinstance(missing, list)
    ):
        raise ProviderPayloadError("malformed_envelope")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("symbol"), str) for item in missing
    ):
        raise ProviderPayloadError("malformed_missing_symbols")
    missing = [item["symbol"] for item in missing]
    if len(missing) != len(set(missing)):
        raise ProviderPayloadError("duplicate_missing_symbol")
    if set(data) & set(missing) or set(data) | set(missing) != set(symbols):
        raise ProviderPayloadError("response_accounting_mismatch")
    if payload.get("count") != len(data) or payload.get("missing_count") != len(missing):
        raise ProviderPayloadError("response_count_mismatch")
    if not data:
        raise AllMissingError("TradingView returned an all-missing chunk; refusing false coverage")
    rows = []
    for symbol in symbols:
        row: dict[str, Any] = {
            "ticker": symbol.removeprefix("TSE:"),
            "provider_symbol": symbol,
            "fetched_at_utc": fetched_at.astimezone(UTC).isoformat(),
            "fetch_status": "unresolved" if symbol in missing else "ok",
        }
        raw = data.get(symbol)
        if symbol not in missing and (not isinstance(raw, dict) or set(COLUMNS) - set(raw)):
            raise ProviderPayloadError(
                "row_missing_requested_fields",
                next(
                    (
                        name
                        for name, field in FIELDS.items()
                        if not isinstance(raw, dict) or field not in raw
                    ),
                    None,
                ),
            )
        for column, field in FIELDS.items():
            value = None if raw is None else raw[field]
            if value is not None:
                if column in {"analyst_rating", "quote_currency"}:
                    if not isinstance(value, str):
                        raise ProviderPayloadError("invalid_text_type", column)
                elif column == "recommendation_total":
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ProviderPayloadError("invalid_analyst_count", column)
                elif (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                ):
                    raise ProviderPayloadError("invalid_numeric", column)
            row[column] = value
        rows.append(row)
    return rows
