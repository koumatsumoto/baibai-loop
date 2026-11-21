"""明示旧readerと低水準projectionの契約は維持する。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from tests.helpers.financial_documents import seed_legacy_financial_sqlite
from tests.helpers.price_native import project_price_records

from baibai_engine.machine_analysis.observations.financials.reader import read_fin_summaries
from baibai_engine.machine_analysis.observations.prices.price_reader import read_daily_bars
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def day(value: str) -> date:
    return date.fromisoformat(value)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    return path


def test_legacy_financial_reader_does_not_claim_partial_coverage(database):
    target = day("2026-05-15")
    seed_legacy_financial_sqlite(
        database,
        [{"Code": "", "DiscDate": target.isoformat()}],
        requested_start=target,
        requested_end=target,
    )
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, "jquants_fin_summaries", target, target)
        assert conn.execute("SELECT record_count, status FROM source_coverage").fetchall() == [
            (0, "partial")
        ]
    assert read_fin_summaries(database, target, target) is None


def test_legacy_financial_reader_keeps_covered_empty_days_outside_partial_window(database):
    start, end, bad_day = day("2026-05-01"), day("2026-05-31"), day("2026-05-15")
    seed_legacy_financial_sqlite(database, [], requested_start=start, requested_end=end)
    seed_legacy_financial_sqlite(
        database,
        [{"Code": "", "DiscDate": bad_day.isoformat()}],
        requested_start=bad_day,
        requested_end=bad_day,
    )
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, day("2026-05-14"))
        assert not range_covered(conn, "jquants_fin_summaries", bad_day, bad_day)
        assert range_covered(conn, "jquants_fin_summaries", day("2026-05-16"), end)
    assert read_fin_summaries(database, start, day("2026-05-14")) == []
    assert read_fin_summaries(database, bad_day, bad_day) is None
    assert read_fin_summaries(database, day("2026-05-16"), end) == []


def test_partial_daily_bar_response_does_not_satisfy_coverage(database):
    target = day("2026-10-07")
    project_price_records(database, [{"Code": "", "Date": target.isoformat(), "C": 100.0}])
    assert read_daily_bars(database, target, target) is None
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, "jquants_daily_bars", target, target)
