"""Physical snapshot columns shared by SQLite and the existing Lake contract."""

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

TABLE = "tradingview_forecast_snapshots"
# name, SQLite type, nullable, primary-key order
COLUMNS: tuple[tuple[str, str, bool, int], ...] = (
    ("snapshot_date", "TEXT", False, 1),
    ("ticker", "TEXT", False, 2),
    ("provider_symbol", "TEXT", False, 0),
    ("fetched_at_utc", "TEXT", False, 0),
    ("fetch_status", "TEXT", False, 0),
    *(
        (name, "TEXT", True, 0)
        for name in (
            "quote_at_utc",
            "estimate_currency",
            "estimate_unit",
            "provider_updated_at_utc",
            "target_period_label",
            "target_period_end",
        )
    ),
    *(
        (
            name,
            "TEXT"
            if name in {"analyst_rating", "quote_currency"}
            else "INTEGER"
            if name == "recommendation_total"
            else "REAL",
            True,
            0,
        )
        for name in FIELDS
    ),
)
DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE}("
    + ",".join(
        f"{name} {kind}" + (" NOT NULL" if not nullable else "")
        for name, kind, nullable, _ in COLUMNS
    )
    + ", PRIMARY KEY(snapshot_date,ticker),"
    " CHECK(fetch_status IN ('ok','unresolved')));"
    f"CREATE INDEX IF NOT EXISTS idx_tv_forecasts_ticker_date ON {TABLE}(ticker,snapshot_date);"
)
