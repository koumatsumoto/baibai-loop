"""Synthetic saved TradingView receipts for offline tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from mcp.types import CallToolResult

from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.receipts import retain_tool_result
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.tradingview_rebuild import rebuild_from_receipts
from tests.helpers.master import insert_master_rows

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


def store(path: Path, count: int = 51) -> Path:
    with closing(open_connection(path)) as conn, conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
        insert_master_rows(
            conn,
            [
                (DAY.isoformat(), str(1000 + i), "IPO", "プライム", "機械", "011")
                for i in range(count)
            ],
        )
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
) -> dict[str, object]:
    """Seed finite provider-native fixtures and reparse explicitly retained receipts."""
    retain_result(
        path,
        _sdk(payload(symbols, missing=list(missing))),
        day=day,
        master=master,
        received=received,
        symbols=tuple(symbols),
    )
    with closing(open_connection(path)) as conn:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT receipt_id FROM tradingview_source_receipts "
                "WHERE snapshot_date=? ORDER BY rowid",
                (day.isoformat(),),
            )
        ]
    return rebuild_from_receipts(path, day, ids)


class Timer:
    """Controllable elapsed clock shared by finite acquisition scenarios."""

    value = 0.0

    def __call__(self):
        return self.value
