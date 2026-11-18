"""Native projections finish their SQLite transaction before releasing its connection."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import MasterAttempt, MasterL1
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration import calendar_native, master_native, price_native

DAY = date(2026, 11, 5)
AT = datetime(2026, 11, 5, tzinfo=UTC)


def _targets_market(database, market):
    return database in (market, market.resolve().as_uri() + "?mode=rw")


def _prepare(tmp_path, source):
    store = MemoryR2Store()
    market = market_store(tmp_path / "market.sqlite")
    query = {"date": DAY.isoformat()}
    if source == "calendar":
        module, owner = calendar_native, CalendarL1(store, tmp_path / "native")
        query = {"from": DAY.isoformat(), "to": DAY.isoformat()}
        page = JQuantsPage(
            "market_calendar",
            query,
            AT,
            json.dumps({"data": [{"Date": DAY.isoformat(), "HolDiv": "1"}]}).encode(),
        )
        attempt = CalendarAttempt("calendar", DAY, DAY, None, (page,))
        table, count = "jquants_market_calendar", 1
    elif source == "master":
        module, owner = master_native, MasterL1(store, tmp_path / "native")
        page = JQuantsPage(
            "master", query, AT, json.dumps({"data": make_master_records(DAY)}).encode()
        )
        attempt = MasterAttempt("master", DAY, AT, (page,))
        table, count = "jquants_master_snapshots", 2500
    else:
        module, owner = price_native, PriceL1(store, tmp_path / "native")
        page = JQuantsPage(
            "daily_bars",
            query,
            AT,
            json.dumps({"data": [{"Code": "13010", "Date": DAY.isoformat(), "C": 123}]}).encode(),
        )
        attempt = PriceAttempt("price", DAY, AT, (page,))
        table, count = "jquants_daily_bars", 1
    owner.initialize()
    owner.commit([attempt])
    fixed = owner.fixed()

    def project():
        return module.model_input(store, fixed, as_of=DAY, evaluated_at=AT, market=market)

    return store, market, project, table, count


@pytest.mark.parametrize("source", ["calendar", "master", "price"])
@pytest.mark.parametrize("failure", [None, sqlite3.OperationalError, KeyboardInterrupt])
def test_projection_closes_after_commit_or_rollback(tmp_path, monkeypatch, source, failure):
    store, market, project, table, count = _prepare(tmp_path, source)
    before = market.read_bytes()
    raw = {key: value.body for key, value in store.values.items()}
    connect = sqlite3.connect
    connections = []
    events = []

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if failure and "INSERT" in sql and "INTO source_coverage" in sql:
                assert self.in_transaction
                raise failure("injected after projection and coverage writes")
            return result

        def __exit__(self, *args):
            result = super().__exit__(*args)
            events.append(("transaction", self.in_transaction))
            return result

        def close(self):
            events.append(("close", self.in_transaction))
            super().close()

    def tracked(database, *args, **kwargs):
        if _targets_market(database, market):
            connection = connect(database, *args, factory=TrackedConnection, **kwargs)
            connections.append(connection)
            return connection
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked)
    try:
        if failure:
            with pytest.raises(failure, match="injected"):
                project()
            assert market.read_bytes() == before
        else:
            project()
            with closing(connect(market)) as reader:
                assert reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (count,)
        assert len(connections) == 1
        assert events == [("transaction", False), ("close", False)]
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
        assert all(store.values[key].body == value for key, value in raw.items())
    finally:
        for connection in connections:
            sqlite3.Connection.close(connection)


@pytest.mark.parametrize("source", ["calendar", "master", "price"])
@pytest.mark.parametrize("failure", ["missing-pack", "connect"])
def test_projection_rejection_does_not_change_execution_store(
    tmp_path, monkeypatch, source, failure
):
    store, market, project, _, _ = _prepare(tmp_path, source)
    before = market.read_bytes()
    if failure == "missing-pack":
        del store.values[next(key for key in store.values if "/packs/" in key)]
    connect = sqlite3.connect
    opened = []

    def unavailable(database, *args, **kwargs):
        if _targets_market(database, market):
            opened.append(market)
            if failure == "connect":
                raise sqlite3.OperationalError("injected open failure")
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", unavailable)
    error = LakePublishError if failure == "missing-pack" else sqlite3.OperationalError
    with pytest.raises(error):
        project()
    # Price reserves the disposable working store for day equality checks even if a later
    # pack is missing. Both raw rejection and open failure leave its main bytes untouched.
    assert opened == ([market] if failure == "connect" or source == "price" else [])
    assert market.read_bytes() == before
