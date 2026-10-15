"""Read-only path identity and failure boundaries of market-derived readers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.store import read_daily_bars, read_daily_bars_for_tickers
from baibai_engine.screening.calibration.grid import days_with_bars, month_end_asof_grid
from baibai_engine.screening.calibration.panel import _coverage_floors
from baibai_engine.screening.sqlite_coverage.required_fields import (
    plan_required_field_repair,
    read_required_field_coverage,
)
from baibai_engine.storage.observations.readiness import unreadable_store_reason
from baibai_engine.storage.observations.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_engine.tasks.earnings_schedule import read_published_earnings_dates


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
        conn.execute(
            "INSERT INTO jquants_fin_summaries(ticker, disclosed_at) VALUES ('1301', '2026-01-30')"
        )
        conn.execute("INSERT INTO jpx_earnings_calendar VALUES ('2026-02-03', '1301')")
    before = path.read_bytes()
    start, end = date(2026, 1, 1), date(2026, 2, 2)

    assert month_end_asof_grid(path, start=start, end=end, min_tickers=1) == [date(2026, 1, 30)]
    assert days_with_bars(path, [end], min_tickers=1) == {end}
    assert _coverage_floors(path, end) == (date(2026, 1, 30), date(2026, 1, 30))
    assert read_published_earnings_dates(path, end) == {"1301": date(2026, 2, 3)}
    assert unreadable_store_reason(path) is None
    coverage = read_required_field_coverage(path, start=start, asof=end)
    repair = plan_required_field_repair(path, start=start, asof=end)
    assert coverage is not None
    assert repair is not None
    assert path.read_bytes() == before

    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
    reason = unreadable_store_reason(path)
    assert reason is not None
    assert f"user_version {SQLITE_SCHEMA_VERSION - 1}," in reason


@pytest.mark.parametrize("reader", [read_required_field_coverage, plan_required_field_repair])
def test_missing_field_coverage_store_remains_uncreated(tmp_path: Path, reader) -> None:
    path = tmp_path / "missing.sqlite"
    assert reader(path, start=date(2026, 1, 1), asof=date(2026, 2, 2)) is None
    assert not path.exists()


@pytest.mark.parametrize("reader", [read_required_field_coverage, plan_required_field_repair])
def test_field_coverage_query_failure_is_not_reported_as_missing(tmp_path: Path, reader) -> None:
    path = tmp_path / "broken.sqlite"
    with closing(open_connection(path)) as conn, conn:
        conn.execute("DROP TABLE jquants_master_snapshots")
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        reader(path, start=date(2026, 1, 1), asof=date(2026, 2, 2))
    assert path.read_bytes() == before


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
