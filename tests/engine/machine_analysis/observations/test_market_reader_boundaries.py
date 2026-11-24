"""Read-only path identity and failure boundaries of market-derived readers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date

import pytest

import baibai_engine.machine_analysis.observations.prices.price_reader as price_reader
from baibai_engine.machine_analysis.observations.disclosures import edinet_document_store
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import (
    store as edinet_store,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices import calendar_reader
from baibai_engine.machine_analysis.observations.prices.price_reader import read_daily_bars
from baibai_engine.machine_analysis.observations.stocks import reader as stock_reader
from baibai_engine.storage.sqlite.source_cache.coverage import (
    record_range_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    open_connection,
)


@pytest.mark.parametrize("corrupt", [False, True])
def test_daily_bars_keep_volume_and_reject_corrupt_closes(tmp_path, corrupt):
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

    if corrupt:
        with pytest.raises(JQuantsProviderError, match="corrupt SQLite row"):
            read_daily_bars(path, day, day)
    else:
        rows = read_daily_bars(path, day, day)
        assert rows is not None
        assert [(row.ticker, row.close, row.volume) for row in rows] == [
            ("1301", 101.0, 10.0),
            ("1302", 202.0, 20.0),
        ]


@pytest.mark.parametrize("missing_coverage", [False, True])
@pytest.mark.parametrize(
    ("module", "reader_name", "source"),
    [
        (edinet_document_store, "read_edinet_documents", "edinet_documents"),
        (edinet_store, "read_edinet_metrics", "edinet_metrics"),
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
                "2026-10-15",
                "2026-10-15",
                "2026-10-15",
                "2026-10-15T00:00:00+00:00",
            ),
        )
    conn.commit()  # A fresh read connection has no pending fixture-writing transaction.
    # Inject a query failure after schema connection, for both coverage and data reads.
    monkeypatch.setattr(module, "connect_current", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        getattr(module, reader_name)(path, date(2026, 10, 15))


def test_master_query_failure_propagates_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "existing.sqlite"
    open_connection(path).close()
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(stock_reader, "connect_read_only", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        stock_reader.read_master_snapshot(path, as_of=date(2026, 10, 15))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("reader_kind", ["all", "actions"])
def test_price_coverage_and_rows_share_a_read_snapshot(tmp_path, monkeypatch, reader_kind):
    path = tmp_path / "market.sqlite"
    day = date(2026, 1, 30)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES ('1301', ?, 100, 0.5)",
            (day.isoformat(),),
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
    original = price_reader.range_covered
    changed = False

    def covered_then_refresh(conn, source, start, end):
        nonlocal changed
        result = original(conn, source, start, end)
        if not changed:
            changed = True
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute(
                    "UPDATE jquants_daily_bars SET close = 200, adjustment_factor = 0.25"
                )
                writer.execute("UPDATE source_coverage SET status = 'partial'")
        return result

    monkeypatch.setattr(price_reader, "range_covered", covered_then_refresh)

    def read():
        if reader_kind == "actions":
            return price_reader.read_adjustment_factor_bars(path, day, day)
        return read_daily_bars(path, day, day)

    rows = read()
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].adjustment_factor == 0.5
    if reader_kind != "actions":
        assert rows[0].close == 100
    # The next call sees the new incomplete acquisition, not the old successful coverage.
    assert read() is None


def test_calendar_coverage_and_rows_share_a_read_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    day = date(2026, 1, 30)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, '1')",
            (day.isoformat(),),
        )
        record_source_coverage(
            conn,
            source="jquants_market_calendar",
            coverage_key="test-range",
            coverage_start=day.isoformat(),
            coverage_end=day.isoformat(),
            fetched_at_utc="2026-01-30T00:00:00Z",
            record_count=1,
        )
    original = calendar_reader.range_covered
    changed = False

    def covered_then_refresh(conn, selected_source, start, end):
        nonlocal changed
        result = original(conn, selected_source, start, end)
        if not changed:
            changed = True
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("DELETE FROM jquants_market_calendar")
                writer.execute("UPDATE source_coverage SET status = 'partial'")
        return result

    monkeypatch.setattr(calendar_reader, "range_covered", covered_then_refresh)
    rows = calendar_reader.read_market_calendar(path, day, day)
    assert rows is not None
    assert len(rows) == 1
    assert calendar_reader.read_market_calendar(path, day, day) is None


@pytest.mark.parametrize("reader_kind", ["baseline", "metrics", "documents"])
def test_edinet_coverage_and_rows_share_a_snapshot(tmp_path, monkeypatch, reader_kind):
    from tests.helpers.edinet_saved_inputs import BASE, _seed

    path = tmp_path / "market.sqlite"
    _seed(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    changed = False
    table = "edinet_documents" if reader_kind == "documents" else "edinet_metrics"

    class ConcurrentRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal changed
            if not changed and f"FROM {table} " in sql:
                changed = True
                with closing(sqlite3.connect(path)) as writer, writer:
                    writer.execute(f"DELETE FROM {table}")
                    writer.execute("UPDATE source_coverage SET status = 'failed'")
            return super().execute(sql, parameters)

    store = edinet_document_store if reader_kind == "documents" else edinet_store
    monkeypatch.setattr(
        store,
        "connect_current",
        lambda path: sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, factory=ConcurrentRead
        ),
    )
    reader = getattr(
        store,
        "read_edinet_metric_baseline"
        if reader_kind == "baseline"
        else f"read_edinet_{reader_kind}",
    )
    result = reader(path, BASE)
    assert changed
    assert result is not None
    rows = result.rows if reader_kind == "baseline" else result
    assert len(rows) == 2
    assert reader(path, BASE) is None


@pytest.mark.parametrize("damage", ["none", "old_schema", "broken_file", "count_mismatch"])
def test_edinet_failure_recording_uses_one_baseline_read(tmp_path, monkeypatch, damage):
    from tests.helpers.edinet_saved_inputs import BASE, _seed

    from baibai_engine.machine_analysis.observations.financials.edinet_metrics.service import (
        _record_edinet_extraction_failure,
    )

    path = tmp_path / "market.sqlite"
    _seed(path)
    if damage == "broken_file":
        path.write_bytes(b"not a SQLite database")
    elif damage != "none":
        with closing(sqlite3.connect(path)) as conn, conn:
            if damage == "old_schema":
                conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
            else:
                conn.execute(
                    "UPDATE source_coverage SET record_count = 99 WHERE source = 'edinet_metrics'"
                )
    before = path.read_bytes()
    connect = sqlite3.connect
    opened = []

    def track(database, *args, **kwargs):
        opened.append(str(database))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", track)
    _record_edinet_extraction_failure(path, BASE, "provider unavailable")
    assert opened == [path.resolve().as_uri() + "?mode=ro"]
    assert path.read_bytes() == before
