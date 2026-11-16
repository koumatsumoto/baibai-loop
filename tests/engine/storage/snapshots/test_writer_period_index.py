"""Calendar extraction keeps LIKE's row contract while using existing date indexes."""

import sqlite3
from dataclasses import replace

import pytest

from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import JQUANTS_DAILY_BARS
from baibai_engine.machine_analysis.observations.prices.storage_contracts import (
    JQUANTS_DAILY_BARS_DDL,
)
from baibai_engine.storage.snapshots import writer


@pytest.mark.parametrize("period", [(2026,), (2026, 11)])
@pytest.mark.parametrize("date_first", [False, True])
def test_period_rows_preserve_prefix_and_order(period, date_first):
    dataset = replace(JQUANTS_DAILY_BARS, partition_grain="year" if len(period) == 1 else "month")
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(JQUANTS_DAILY_BARS_DDL)
        for i, day in enumerate(
            [
                "2025-11-16",
                "2026-10-31",
                "2026-11-01",
                "2026-11-30",
                "2026-12-01",
                "2027-01-01",
                "2026-11",
                "2026-11-",
                "2026-11-bad",
                "2026-11-☃",
                "2026-11-\x00tail",
            ]
        ):
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES(?,?,?)",
                (str(9999 - i), day, float(i)),
            )
        names = ",".join(column.name for column in dataset.columns)
        order = "traded_at,ticker" if date_first else "ticker,traded_at"
        expected = connection.execute(
            f"SELECT {names} FROM jquants_daily_bars WHERE traded_at LIKE ? ORDER BY {order}",
            (writer.period_label(period) + "-%",),
        ).fetchall()
        statements = []
        connection.set_trace_callback(statements.append)
        actual = list(writer._period_rows(connection, dataset, period, date_first=date_first))
        connection.set_trace_callback(None)
        assert actual == expected
        query = next(statement for statement in statements if statement.startswith("SELECT"))
        plan = connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        assert any(
            "SEARCH" in item[3] and "idx_jquants_daily_bars_traded_at" in item[3] for item in plan
        )


@pytest.mark.parametrize("grain", ["month", "year"])
@pytest.mark.parametrize("indexed", [False, True])
def test_period_inventory_deduplicates_dates_without_changing_periods(grain, indexed):
    dataset = replace(JQUANTS_DAILY_BARS, partition_grain=grain)
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(JQUANTS_DAILY_BARS_DDL)
        if not indexed:
            connection.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
        assert writer._selected_periods(connection, dataset) == ()
        for i, day in enumerate(
            ["2026-12-01", "2025-01-01", "2026-11-01", "2026-11-02", "2026-11-01"]
        ):
            connection.execute(
                "INSERT INTO jquants_daily_bars(ticker,traded_at) VALUES(?,?)", (str(i), day)
            )
        parts = writer._period_sql_parts(dataset)
        reference = connection.execute(
            f"SELECT DISTINCT {', '.join(parts)} FROM jquants_daily_bars ORDER BY "
            + ", ".join(str(index) for index in range(1, len(parts) + 1))
        ).fetchall()
        assert writer._selected_periods(connection, dataset) == tuple(
            tuple(int(part) for part in row) for row in reference
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at) VALUES('bad','bad-date')"
        )
        with pytest.raises(ValueError, match="invalid literal"):
            writer._selected_periods(connection, dataset)
