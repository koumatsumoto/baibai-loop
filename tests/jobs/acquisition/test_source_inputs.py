from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from mcp.types import CallToolResult
from tests.helpers.tradingview import DAY, payload, retain_result, seed_snapshot, store

from baibai_engine.machine_analysis.observations.stocks.master_snapshot import master_coverage_key
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.inputs import export_source_input


@pytest.mark.parametrize(
    ("asof", "days"),
    [
        (DAY, 2),
        (date(2026, 9, 19), 1),
        (date(2026, 9, 21), 0),
    ],
)
def test_tradingview_input_copies_retained_state_from_asof_without_sql_dependencies(
    tmp_path, asof, days
):
    canonical = store(tmp_path / "canonical.sqlite", 2)
    saturday = date(2026, 9, 19)
    with closing(open_connection(canonical)) as conn, conn:
        conn.execute(
            "INSERT INTO jquants_market_calendar VALUES (?, ?)", (saturday.isoformat(), "0")
        )
        conn.execute(
            "INSERT INTO jquants_master_snapshots SELECT ?, security_code, record_json FROM jquants_master_snapshots",
            (saturday.isoformat(),),
        )
        for day in (DAY, saturday):
            record_source_coverage(
                conn,
                source="jquants_master_snapshots",
                coverage_key=master_coverage_key(day),
                coverage_start=day.isoformat(),
                coverage_end=day.isoformat(),
                record_count=2,
            )
        record_source_coverage(
            conn,
            source="jquants_market_calendar",
            coverage_key="calendar",
            coverage_start=DAY.isoformat(),
            coverage_end=saturday.isoformat(),
            record_count=2,
        )
        conn.execute("INSERT INTO lake_store_origin VALUES (1, ?, ?)", ("fixed", "a" * 64))
    for day in (DAY, saturday):
        seed_snapshot(
            canonical,
            ["TSE:1000", "TSE:1001"],
            day=day,
            master=DAY,
            received=datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=7),
            missing=("TSE:1001",),
        )

    scoped = tmp_path / "scoped.sqlite"
    report = export_source_input(canonical, scoped, group="tradingview", asof=asof)
    assert report == {
        "group": "tradingview",
        "asof": asof.isoformat(),
        "rows": {
            "tradingview_forecast_snapshots": days * 2,
            "tradingview_source_receipts": days,
        },
    }
    with closing(open_connection(canonical)) as source, closing(open_connection(scoped)) as target:
        assert (
            target.execute(
                "SELECT * FROM tradingview_forecast_snapshots ORDER BY snapshot_date,ticker"
            ).fetchall()
            == source.execute(
                "SELECT * FROM tradingview_forecast_snapshots WHERE snapshot_date>=? "
                "ORDER BY snapshot_date,ticker",
                (asof.isoformat(),),
            ).fetchall()
        )
        assert (
            target.execute(
                "SELECT * FROM tradingview_source_receipts ORDER BY receipt_id"
            ).fetchall()
            == source.execute(
                "SELECT * FROM tradingview_source_receipts WHERE snapshot_date>=? ORDER BY receipt_id",
                (asof.isoformat(),),
            ).fetchall()
        )
        assert target.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM source_coverage").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM lake_store_origin").fetchone()[0] == 0


def test_tradingview_input_keeps_unprojected_receipt_without_calendar_or_master(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    with closing(open_connection(canonical)):
        pass
    monday = date(2026, 9, 21)
    receipt_id = retain_result(
        canonical,
        CallToolResult(isError=False, structuredContent=payload(["TSE:1000"]), content=[]),
        day=monday,
        master=monday,
        received=datetime(2026, 9, 21, 7, tzinfo=UTC),
    )
    scoped = tmp_path / "scoped.sqlite"
    report = export_source_input(canonical, scoped, group="tradingview", asof=monday)
    assert report["rows"] == {
        "tradingview_forecast_snapshots": 0,
        "tradingview_source_receipts": 1,
    }
    with closing(open_connection(scoped)) as conn:
        assert conn.execute(
            "SELECT receipt_id,master_asof FROM tradingview_source_receipts"
        ).fetchall() == [(receipt_id, monday.isoformat())]
        assert conn.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 0


def test_market_l1_acquisition_does_not_publish_scoped_l2_input():
    from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS

    assert "jquants-market" not in SOURCE_INPUT_GROUPS
    assert "jquants-financial" not in SOURCE_INPUT_GROUPS
    assert not any(group.startswith("edinet-") for group in SOURCE_INPUT_GROUPS)
