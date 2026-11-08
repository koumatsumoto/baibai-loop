"""明示旧readerと低水準projectionの契約は維持する。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.machine_analysis.observations.financials.reader import read_fin_summaries
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
    store_jquants_master,
)
from baibai_engine.machine_analysis.observations.prices.price_reader import read_daily_bars
from baibai_engine.machine_analysis.observations.stocks.reader import read_eq_master_exact
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def day(value: str) -> date:
    return date.fromisoformat(value)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "market.sqlite"
    open_connection(path).close()
    return path


def test_cached_exact_master_uses_offline_reader_without_acquisition(database):
    target = day("2026-05-07")
    records = make_master_records(target)
    records[0]["CoName"] = "極洋"
    store_jquants_master(database, records, requested_asof=target)
    masters = read_eq_master_exact(database, target)
    assert masters is not None
    assert masters[0].name == "極洋"


def test_rejected_financial_quality_does_not_claim_coverage(database):
    target = day("2026-05-15")
    store_jquants_fin_summaries(
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


def test_partial_financial_refetch_preserves_empty_outer_windows(database):
    start, end, bad_day = day("2026-05-01"), day("2026-05-31"), day("2026-05-15")
    store_jquants_fin_summaries(database, [], requested_start=start, requested_end=end)
    store_jquants_fin_summaries(
        database,
        [{"Code": "", "DiscDate": bad_day.isoformat()}],
        requested_start=bad_day,
        requested_end=bad_day,
    )
    with sqlite3.connect(database) as conn:
        assert range_covered(conn, "jquants_fin_summaries", start, day("2026-05-14"))
        assert not range_covered(conn, "jquants_fin_summaries", bad_day, bad_day)
        assert range_covered(conn, "jquants_fin_summaries", day("2026-05-16"), end)


def test_partial_daily_bar_response_does_not_satisfy_coverage(database):
    target = day("2026-10-07")
    store_jquants_daily_bars(
        database,
        [{"Code": "", "Date": target.isoformat(), "C": 100.0}],
        requested_start=target,
        requested_end=target,
    )
    assert read_daily_bars(database, target, target) is None
    with sqlite3.connect(database) as conn:
        assert not range_covered(conn, "jquants_daily_bars", target, target)
