"""Validate one batch without interpreting absent metrics as absent coverage."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

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
        raise FetchError("Malformed TradingView batch envelope")
    if any(
        not isinstance(item, dict) or not isinstance(item.get("symbol"), str) for item in missing
    ):
        raise FetchError("Malformed TradingView missing symbols")
    missing = [item["symbol"] for item in missing]
    if len(missing) != len(set(missing)):
        raise FetchError("Duplicate TradingView missing symbol")
    if set(data) & set(missing) or set(data) | set(missing) != set(symbols):
        raise FetchError("TradingView response does not account for requested symbols")
    if payload.get("count") != len(data) or payload.get("missing_count") != len(missing):
        raise FetchError("TradingView response counts disagree")
    if not data:
        raise FetchError("TradingView returned an all-missing chunk; refusing false coverage")
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
            raise FetchError("TradingView row lacks requested fields")
        for column, field in FIELDS.items():
            value = None if raw is None else raw[field]
            if value is not None:
                if column in {"analyst_rating", "quote_currency"}:
                    if not isinstance(value, str):
                        raise FetchError("TradingView text field has invalid type")
                elif column == "recommendation_total":
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise FetchError("TradingView analyst count is not a nonnegative integer")
                elif (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                ):
                    raise FetchError("TradingView metric is not finite numeric data")
            row[column] = value
        rows.append(row)
    return rows
