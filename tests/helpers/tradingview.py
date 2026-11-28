"""Synthetic saved TradingView receipts for offline tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from mcp.types import CallToolResult

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.receipts import retain_tool_result
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    COLUMNS as TABLE_COLUMNS,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
    normalize_batch,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

DAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 7, tzinfo=UTC)

# Provider-native keys are fixed independently of the production field mapping.
PROVIDER_ROW = {
    "AnalystRating": None,
    "recommendation_total": None,
    "price_target_average": None,
    "price_target_high": None,
    "price_target_low": None,
    "earnings_per_share_forecast_next_fq": None,
    "earnings_per_share_forecast_next_fy": -5,
    "revenue_forecast_next_fq": None,
    "revenue_forecast_next_fy": None,
    "price_earnings_fwd": None,
    "price_sales_fwd": None,
    "enterprise_value_ebitda_fwd": None,
    "ebitda_estimate_fy": None,
    "free_cash_flow_estimate_fy": None,
    "capital_expenditures_estimate_fy": None,
    "total_debt_estimate_fy": None,
    "close": 100,
    "currency": "JPY",
}


def _sdk(body: dict) -> CallToolResult:
    return CallToolResult(isError=False, structuredContent=body, content=[])


def store(path: Path, _count: int = 51) -> Path:
    with closing(open_connection(path)) as conn, conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
    return path


def payload(symbols: list[str], *, missing: list[str] | None = None) -> dict:
    missing = missing or []
    return {
        "success": True,
        "data": {s: dict(PROVIDER_ROW) for s in symbols if s not in missing},
        "count": len(symbols) - len(missing),
        "missing": [{"symbol": s, "reason": "Quote data not found"} for s in missing],
        "missing_count": len(missing),
    }


def stored(path: Path) -> list:
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(
            "SELECT * FROM tradingview_forecast_snapshots ORDER BY ticker"
        ).fetchall()


def retain_result(
    path: Path,
    result: CallToolResult,
    *,
    day: date = DAY,
    master: date = DAY,
    columns: tuple[str, ...] = tuple(COLUMNS),
    received=NOW,
    symbols: tuple[str, ...] = ("TSE:1000",),
) -> str:
    with closing(open_connection(path)) as conn:
        return retain_tool_result(
            conn,
            snapshot_date=day,
            master_asof=master,
            tool_name=BATCH_TOOL,
            received_at_utc=received,
            requested_symbols=symbols,
            requested_columns=columns,
            result=result,
        )


def seed_snapshot(
    path: Path,
    symbols: list[str],
    *,
    day: date = DAY,
    master: date = DAY,
    received: datetime = NOW,
    missing: tuple[str, ...] = (),
) -> None:
    """Seed exact synthetic originals and their already-projected rows."""
    body = payload(symbols, missing=list(missing))
    retain_result(
        path, _sdk(body), day=day, master=master, received=received, symbols=tuple(symbols)
    )
    rows = normalize_batch(body, symbols, received)
    names = [name for name, *_ in TABLE_COLUMNS]
    with closing(open_connection(path)) as conn, conn:
        conn.executemany(
            f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            [
                tuple(
                    day.isoformat() if name == "snapshot_date" else row.get(name) for name in names
                )
                for row in rows
            ],
        )
