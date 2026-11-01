"""Read-only path identity and failure boundaries of market-derived readers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import insert_financial_record

import baibai_engine.machine_analysis.observations.disclosures.jpx_reader as jpx_reader
import baibai_engine.machine_analysis.observations.prices.price_reader as price_reader
import baibai_engine.machine_analysis.observations.trading_activity.margin_reader as margin_reader
from baibai_engine.machine_analysis.historical_evaluation.grid import (
    days_with_bars,
    month_end_asof_grid,
)
from baibai_engine.machine_analysis.historical_evaluation.panel import _coverage_floors
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import (
    read_next_earnings_dates,
)
from baibai_engine.machine_analysis.observations.financials import reader as financial_reader
from baibai_engine.machine_analysis.observations.financials.edinet_metrics import (
    store as edinet_store,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices import calendar_reader
from baibai_engine.machine_analysis.observations.prices.price_reader import (
    read_daily_bars,
    read_daily_bars_for_tickers,
)
from baibai_engine.machine_analysis.observations.stocks import reader as stock_reader
from baibai_engine.storage.sqlite.source_cache.coverage import (
    record_range_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.readiness import unreadable_store_reason
from baibai_engine.storage.sqlite.source_cache.schema import SQLITE_SCHEMA_VERSION, open_connection


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
    conn.commit()  # A fresh read connection has no pending fixture-writing transaction.
    # Inject a query failure after schema connection, for both coverage and data reads.
    monkeypatch.setattr(module, "connect_current", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        getattr(module, reader_name)(path, date(2026, 10, 15))


def test_master_query_failure_propagates_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "existing.sqlite"
    path.touch()
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(stock_reader, "connect_current", lambda path: conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        stock_reader.read_eq_master_exact(path, date(2026, 10, 15))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("reader_kind", ["all", "scoped", "actions"])
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
        if reader_kind == "scoped":
            return read_daily_bars_for_tickers(path, ("1301",), day, day)
        return read_daily_bars(path, day, day)

    rows = read()
    assert rows is not None
    assert len(rows) == 1
    assert rows[0].adjustment_factor == 0.5
    if reader_kind != "actions":
        assert rows[0].close == 100
    # The next call sees the new incomplete acquisition, not the old successful coverage.
    assert read() is None


@pytest.mark.parametrize("source", ["jquants_fin_summaries", "jquants_market_calendar"])
def test_financial_and_calendar_coverage_share_a_read_snapshot(tmp_path, monkeypatch, source):
    path = tmp_path / "market.sqlite"
    day = date(2026, 1, 30)
    with closing(open_connection(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode = WAL")
        insert_financial_record(
            conn, {"Code": "13010", "DiscDate": day.isoformat(), "DiscNo": "statement"}
        )
        conn.execute(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, '1')",
            (day.isoformat(),),
        )
        record_source_coverage(
            conn,
            source=source,
            coverage_key="test-range",
            coverage_start=day.isoformat(),
            coverage_end=day.isoformat(),
            fetched_at_utc="2026-01-30T00:00:00Z",
            record_count=1,
        )
    owner = financial_reader if source == "jquants_fin_summaries" else calendar_reader
    original = owner.range_covered
    changed = False

    def covered_then_refresh(conn, selected_source, start, end):
        nonlocal changed
        result = original(conn, selected_source, start, end)
        if not changed:
            changed = True
            with closing(sqlite3.connect(path)) as writer, writer:
                writer.execute("DELETE FROM jquants_fin_summaries")
                writer.execute("DELETE FROM jquants_market_calendar")
                writer.execute("UPDATE source_coverage SET status = 'partial'")
        return result

    monkeypatch.setattr(owner, "range_covered", covered_then_refresh)
    reader = (
        financial_reader.read_fin_summaries
        if source == "jquants_fin_summaries"
        else calendar_reader.read_market_calendar
    )
    rows = reader(path, day, day)
    assert rows is not None
    assert len(rows) == 1
    assert reader(path, day, day) is None


@pytest.mark.parametrize(
    ("source", "table", "day", "reader_name"),
    [
        ("margin_interest", "jquants_weekly_margin", "2026-09-18", "read_weekly_margin"),
        (
            "margin_interest",
            "jquants_all_issues_daily_margin",
            "2026-10-02",
            "read_all_issues_daily_margin",
        ),
        ("margin_alerts", "jquants_margin_alerts", "2026-10-02", "read_margin_alerts"),
        ("margin_interest", "jquants_weekly_margin", "2026-09-18", "published_margin_week_ends"),
        (
            "margin_interest",
            "jquants_all_issues_daily_margin",
            "2026-10-02",
            "_published_all_issues_daily_balance_dates",
        ),
        (
            "short_sale_reports",
            "jquants_short_sale_reports",
            "2026-10-02",
            "read_short_sale_reports",
        ),
    ],
)
def test_margin_coverage_and_observations_share_a_snapshot(
    tmp_path, monkeypatch, source, table, day, reader_name
):
    from tests.helpers.jquants_receipts import FIRST_AT, body, receive, source_record

    from baibai_engine.machine_analysis.observations.jquants_projection import project_request

    path = tmp_path / "market.sqlite"
    query = {"disc_date" if source == "short_sale_reports" else "date": day}
    request_id = receive(path, source, query, body([source_record(source, day)]), FIRST_AT)
    project_request(path, request_id)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    if "published" in reader_name:
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executemany(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', ?, 100)",
                [
                    ((date.fromisoformat(day) + timedelta(days=offset)).isoformat(),)
                    for offset in range(1, 8)
                ],
            )
    original_connect = margin_reader.connect_current
    changed = False

    class ConcurrentRead:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            nonlocal changed
            if not changed and f"FROM {table} " in sql:
                changed = True
                with closing(sqlite3.connect(path)) as writer, writer:
                    writer.execute(f"DELETE FROM {table}")
                    writer.execute("UPDATE source_coverage SET status = 'partial'")
            return self.conn.execute(sql, parameters)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(
        margin_reader, "connect_current", lambda path: ConcurrentRead(original_connect(path))
    )
    reader = getattr(margin_reader, reader_name)
    on_date = date.fromisoformat(day)
    args = (
        (path, on_date, on_date)
        if source in {"margin_alerts", "short_sale_reports"}
        else (path, on_date)
    )
    if "published" in reader_name:
        args = (path, on_date + timedelta(days=7))
    rows = reader(*args)
    assert changed
    assert rows is not None
    assert len(rows) == 1
    if "published" in reader_name:
        assert rows == [on_date]
        assert reader(*args) == []
    else:
        assert rows[0].ticker == "1301"
        assert reader(*args) is None


@pytest.mark.parametrize("reader_kind", ["baseline", "metrics", "documents"])
def test_edinet_coverage_and_rows_share_a_snapshot(tmp_path, monkeypatch, reader_kind):
    from tests.engine.test_edinet_metric_eligibility import BASE, _seed

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

    monkeypatch.setattr(
        edinet_store,
        "connect_current",
        lambda path: sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, factory=ConcurrentRead
        ),
    )
    reader = getattr(
        edinet_store,
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
