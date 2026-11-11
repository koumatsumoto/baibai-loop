"""SQLite転送の順序を変えても全値・固定入力・coverageと原子性を保つ。"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_states,
)
from baibai_jobs.integration import price_native

DAY = date(2026, 10, 31)
AT = datetime(2026, 11, 4, tzinfo=UTC)
TABLES = ("jquants_daily_bars", "jquants_source_pages", "source_coverage")


def rows_at(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in TABLES
        }


@pytest.fixture
def fixed_prices(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    attempts = []
    for offset in range(3):
        day = DAY + timedelta(days=offset)
        rows = (
            []
            if offset == 1
            else [
                {
                    "Code": ticker,
                    "Date": day.isoformat(),
                    "O": 101 + offset,
                    "H": 105 + offset,
                    "L": 99 + offset,
                    "C": 103 + offset,
                    "Vo": 1234,
                    "Va": 127102,
                    "AdjFactor": 0.5,
                    "UL": "1",
                    "LL": "0",
                    "ExRT": "1" if ticker == "13010" else None,
                    "MktCap": 12345.5 if ticker == "13010" else None,
                }
                for ticker in ("13020", "13010")
            ]
        )
        payload = json.dumps({"data": rows}).encode()
        attempts.append(
            PriceAttempt(
                str(offset),
                day,
                AT,
                (JQuantsPage("daily_bars", {"date": day.isoformat()}, AT, payload),),
            )
        )
    owner.commit(attempts)
    failed_at = AT + timedelta(hours=1)
    owner.commit(
        [
            PriceAttempt(
                "failed",
                DAY,
                failed_at,
                (
                    JQuantsPage(
                        "daily_bars",
                        {"date": DAY.isoformat()},
                        failed_at,
                        b'{"data":[],"pagination_key":"next"}',
                    ),
                ),
            )
        ]
    )
    return store, owner.fixed()


def transfer_order(monkeypatch, order):
    """Only replace the transfer cursor, retaining real native rebuild and SQLite writes."""
    transferred = []

    class Projection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, *args):
            if query.startswith("SELECT * FROM daily_bars"):
                assert query == "SELECT * FROM daily_bars"
                query += (
                    " ORDER BY traded_at,ticker"
                    if order == "legacy"
                    else " ORDER BY traded_at DESC,ticker DESC"
                )
                self.connection.execute(query)
                return self
            return self.connection.execute(query, *args)

        def fetchmany(self, size):
            assert size == 10000
            rows = self.connection.fetchmany(2)
            if order == "duplicate" and rows and transferred:
                rows.append(transferred[0])
            transferred.extend(rows)
            return rows

    @contextmanager
    def connect(*args):
        with duckdb.connect(*args) as connection:
            yield Projection(connection)

    monkeypatch.setattr(price_native, "duckdb", SimpleNamespace(connect=connect))
    return transferred


def test_transfer_preserves_all_values_identity_and_coverage_in_any_order(
    tmp_path, monkeypatch, fixed_prices
):
    store, fixed = fixed_prices
    results = []
    orders = []
    for order in ("legacy", "reverse"):
        transferred = transfer_order(monkeypatch, order)
        market = market_store(tmp_path / f"{order}.sqlite")
        saved = price_native.model_input(
            store,
            fixed,
            as_of=DAY + timedelta(days=3),
            evaluated_at=AT + timedelta(hours=1),
            market=market,
        )
        with sqlite3.connect(market) as connection:
            states = read_price_day_states(
                connection, start=DAY, end=DAY + timedelta(days=3), tickers=("1301", "1302")
            )
        prices = read_price_bars(market, start=DAY, end=DAY + timedelta(days=3))
        assert len(prices) == len(states) == 2
        results.append((saved, rows_at(market), prices, states))
        orders.append(transferred)
    assert orders[0] == list(reversed(orders[1]))
    assert len(orders[0]) == 4
    assert results[0] == results[1]
    summary = results[0][0].descriptor["summary"]
    assert (
        summary["failed_days"] == summary["normal_empty_days"] == summary["unavailable_days"] == 1
    )
    assert summary["coverage_status"] == "partial"


@pytest.mark.parametrize("failure", ["duplicate", "coverage"])
def test_transfer_or_coverage_failure_rolls_back_all_replaced_tables(
    tmp_path, monkeypatch, fixed_prices, failure
):
    store, fixed = fixed_prices
    market = market_store(tmp_path / "market.sqlite", daily_bars=(("9999", DAY.isoformat(), 9, 1),))
    with sqlite3.connect(market) as connection:
        connection.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            ("daily_bars", 0, "old", "{}", AT.isoformat(), DAY.isoformat(), b"old"),
        )
        price_native.record_source_coverage(
            connection,
            source="jquants_daily_bars",
            coverage_key="old",
            coverage_start=DAY.isoformat(),
            coverage_end=DAY.isoformat(),
            record_count=1,
            status="ok",
            fetched_at_utc=AT.isoformat(),
        )
    before = rows_at(market)
    transferred = transfer_order(monkeypatch, failure)
    if failure == "coverage":
        record = price_native.record_source_coverage

        def fail_after_write(*args, **kwargs):
            record(*args, **kwargs)
            raise sqlite3.IntegrityError("coverage failure")

        monkeypatch.setattr(price_native, "record_source_coverage", fail_after_write)
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE constraint failed|coverage failure"):
        price_native.model_input(
            store,
            fixed,
            as_of=DAY + timedelta(days=3),
            evaluated_at=AT + timedelta(hours=1),
            market=market,
        )
    assert len(transferred) >= 4  # Failure follows a successful insert batch.
    assert rows_at(market) == before
