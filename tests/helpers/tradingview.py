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


def native_inputs(source: Path, directory: Path, *, day: date, remote=None):
    """Freeze the fixture's real market rows; fillers outside the TV markets keep its small universe."""
    import json

    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
    from baibai_engine.data_collection.providers.jquants.master_native import (
        HEAD_KEY,
        MasterAttempt,
        MasterL1,
    )
    from baibai_jobs.acquisition.calendar_dependencies import (
        fix_calendar_dependency,
        saved_calendar_context,
    )
    from baibai_jobs.integration.master_native import fixed_input
    from tests.helpers.master_calendar_native import seed_calendar
    from tests.helpers.r2_store import MemoryR2Store
    from tests.helpers.screening_sqlite import make_master_records

    if remote is None:
        remote = MemoryR2Store()
    seed_calendar(remote, source, directory / "tv-calendar", as_of=day)
    calendar = fix_calendar_dependency(
        remote, directory=directory / "tv-calendar", observation_date=day
    )
    basis, _ = saved_calendar_context(remote, calendar, observation_date=day)
    owner = MasterL1(remote, directory / "tv-master")
    if remote.head(HEAD_KEY) is None:
        with closing(sqlite3.connect(source)) as conn:
            rows = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT record_json FROM jquants_master_snapshots WHERE snapshot_date=?",
                    (basis.isoformat(),),
                )
            ]
        held = {row["Code"] for row in rows}
        rows.extend(
            {**row, "MktNm": "OTHER"}
            for row in make_master_records(basis)
            if row["Code"] not in held
        )
        params = {"date": basis.isoformat()}
        at = datetime.combine(basis, datetime.min.time(), tzinfo=UTC)
        page = JQuantsPage("master", params, at, json.dumps({"data": rows}).encode())
        owner.initialize()
        owner.commit([MasterAttempt("fixture-master", basis, at, (page,))])
    master = fixed_input(remote, owner.fixed(), as_of=basis, evaluated_at=NOW, days=[basis])
    return remote, master, calendar
