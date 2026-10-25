"""Read-only path identity and failure boundaries of market-derived readers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import insert_financial_record

from baibai_engine.market import jpx_reader, jquants_reader
from baibai_engine.market.edinet_metrics import store as edinet_store
from baibai_engine.market.jpx_reader import read_next_earnings_dates
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.price_reader import read_daily_bars, read_daily_bars_for_tickers
from baibai_engine.machine_analysis.screening.calibration.grid import days_with_bars, month_end_asof_grid
from baibai_engine.machine_analysis.screening.calibration.panel import _coverage_floors
from baibai_engine.storage.observations.coverage import (
    record_range_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.observations.readiness import unreadable_store_reason
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection


@pytest.mark.parametrize(
    "filename", ["market?copy.sqlite", "market#copy.sqlite", "market%20.sqlite"]
)
def test_readers_use_the_exact_filesystem_path(tmp_path: Path, filename: str) -> None:
    path = tmp_path / filename
    with closing(open_connection(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', ?, 100)",
            [("2026-01-30",), ("2026-02-02",)],
        )
        insert_financial_record(
            conn, {"Code": "13010", "DiscDate": "2026-01-30", "DiscNo": "statement"}
        )
        conn.execute("INSERT INTO jpx_earnings_calendar VALUES ('2026-02-03', '1301')")
        record_source_coverage(
            conn,
            source="jpx_earnings_calendar",
            coverage_key="get_earnings_calendar_snapshot:current",
            coverage_start="2026-02-03",
            coverage_end="2026-02-03",
            fetched_at_utc="2026-02-02T00:00:00Z",
            record_count=1,
        )
    before = path.read_bytes()
    start, end = date(2026, 1, 1), date(2026, 2, 2)

    assert month_end_asof_grid(path, start=start, end=end, min_tickers=1) == [date(2026, 1, 30)]
    assert days_with_bars(path, [end], min_tickers=1) == {end}
    assert _coverage_floors(path, end) == (date(2026, 1, 30), date(2026, 1, 30))
    assert read_next_earnings_dates(path, end) == {"1301": date(2026, 2, 3)}
    assert unreadable_store_reason(path) is None
    assert path.read_bytes() == before

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
    reason = unreadable_store_reason(path)
    assert reason is not None
    assert f"user_version={SQLITE_SCHEMA_VERSION - 1}," in reason


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("corrupt", [False, True])
def test_bar_readers_share_decoding_and_corrupt_row_behavior(tmp_path, scoped, corrupt):
    path = tmp_path / "market.sqlite"
    day = date(2026, 1, 30)
    with closing(open_connection(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) VALUES (?, ?, ?, ?)",
            [
                ("1301", day.isoformat(), "broken" if corrupt else 101.0, 10.0),
                ("1302", day.isoformat(), 202.0, 20.0),
                ("1303", day.isoformat(), None, 30.0),
            ],
        )
        record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=day,
            requested_end=day,
            status="ok",
            error=None,
        )

    def read():
        if scoped:
            return read_daily_bars_for_tickers(path, ("1301",), day, day)
        return read_daily_bars(path, day, day)

    if corrupt:
        with pytest.raises(JQuantsProviderError, match="corrupt SQLite row"):
            read()
    else:
        rows = read()
        assert rows is not None
        expected = [("1301", 101.0, 10.0)]
        if not scoped:
            expected.append(("1302", 202.0, 20.0))
        assert [(row.ticker, row.close, row.volume) for row in rows] == expected


@pytest.mark.parametrize("missing_coverage", [False, True])
@pytest.mark.parametrize(
    ("module", "reader_name", "source"),
    [
        (edinet_store, "read_edinet_documents", "edinet_documents"),
        (edinet_store, "read_edinet_metrics", "edinet_metrics"),
        (jpx_reader, "read_jpx_regulations", "jpx_regulation_flags"),
        (jpx_reader, "read_jpx_earnings_calendar_snapshot", "jpx_earnings_calendar"),
    ],
)
def test_snapshot_query_failure_is_not_a_cache_miss(
    tmp_path, monkeypatch, module, reader_name, source, missing_coverage
):
    path = tmp_path / "existing.sqlite"
    open_connection(path).close()
    conn = sqlite3.connect(":memory:")
    if not missing_coverage:
        conn.execute(
            "CREATE TABLE source_coverage(source, coverage_key, coverage_start, coverage_end, "
            "fetched_at_utc, record_count, status)"
        )
        conn.execute(
            "INSERT INTO source_coverage VALUES (?, ?, ?, ?, ?, 1, 'ok')",
            (
                source,
                "get_earnings_calendar_snapshot:current",
                "2026-10-15",
                "2026-10-15",
                "2026-10-15T00:00:00+00:00",
            ),
        )
    # Inject a query failure after schema connection, for both coverage and data reads.
    monkeypatch.setattr(module, "connect_current", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        getattr(module, reader_name)(path, date(2026, 10, 15))


def test_master_query_failure_propagates_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "existing.sqlite"
    path.touch()
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(jquants_reader, "connect_current", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        jquants_reader.read_eq_master_exact(path, date(2026, 10, 15))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
