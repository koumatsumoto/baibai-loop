"""Nikkei planning keeps metric-specific days without copying received HTML rows."""

import calendar
import json
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.provider_fixtures import _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.acquisition.economic_staging as staging
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import (
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
)
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.providers.nikkei.indices import _DATALOAD_HEADERS
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.transport.context import FetchContext
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCES = sorted(
    (item for item in load_sources().values() if item.provider == "nikkei_indexes"),
    key=lambda item: item.source_id,
)
GROUP = "economic-nikkei_indexes"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 18, tzinfo=UTC)
PBR, PER = (item.source_id for item in SOURCES)


def _state(path):
    with closing(store.open_read_only_connection(path)) as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in (*store.RECEIPT_KEYS, *store.CONFIRMATION_TABLES)
        }


def _plans(path):
    with closing(store.open_read_only_connection(path)) as connection:
        return {
            item.source_id: (
                recovery_ranges(connection, item, DAY),
                explicit_recovery_ranges(connection, item, DAY),
            )
            for item in SOURCES
        }


def _seed(path):
    with closing(store.initialize_database(path)) as connection, connection:
        for index, item in enumerate(SOURCES):
            for number, (first, last, status) in enumerate(
                (
                    ("2026-09-28", "2026-09-30", "ok"),
                    ("2026-10-01", "2026-10-10", "failed"),
                    ("2026-10-15", "2026-10-17", "ok"),
                    ("2010-01-01", "2019-12-31", "failed"),
                )
            ):
                connection.execute(
                    "INSERT INTO economic_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"seed-{index}-{number}",
                        item.source_id,
                        first,
                        last,
                        WHEN.isoformat(timespec="microseconds"),
                        WHEN.isoformat(timespec="microseconds"),
                        status,
                        0,
                        None if status == "ok" else "synthetic failed history",
                    ),
                )
            store_snapshot(
                connection,
                source_id=item.source_id,
                observed_at=date(2026, 10, 4 + index),
                fetched_at=WHEN,
                records=[] if index == 0 else [{"prior": "retained native receipt"}],
            )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(clock=WHEN, serial=0, changes={}, omitted=set(), calls=[], fail=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def uid():
        control.serial += 1
        return SimpleNamespace(hex=f"run{int(control.clock.timestamp())}-{control.serial}")

    class Session:
        def get(self, url, **kwargs):
            params = kwargs["params"]
            assert set(params) == {"list", "type", "year", "month"}
            assert params["type"] == "1"
            assert kwargs["headers"] == _DATALOAD_HEADERS
            item = next(item for item in SOURCES if item.provider_series_id == params["list"])
            assert url == item.source_url
            year, month = int(params["year"]), int(params["month"])
            control.calls.append((url, dict(params)))
            control.clock += timedelta(seconds=1)
            if control.fail and item.source_id == PER and month == 11:
                return _FakeResponse(b"<html>synthetic missing month table</html>")
            rows = []
            for number in range(1, calendar.monthrange(year, month)[1] + 1):
                day = date(year, month, number)
                if day > DAY or day.weekday() >= 5 or (item.source_id, day) in control.omitted:
                    continue
                weighted, index = control.changes.get(item.source_id, ("2.01", "2.80"))
                rows.append(
                    f"<tr><!--daily_changing--><td> {day:%Y.%m.%d} </td>"
                    f"<td>{weighted}</td><td>{index}</td></tr>"
                )
                if item.source_id == PER and day == DAY:
                    rows.append(f"<tr><td>{day:%Y.%m.%d}</td><td>17.00</td><td>25.00</td></tr>")
            return _FakeResponse(("<table>" + "".join(rows) + "</table>").encode())

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("Nikkei must not require a browser")

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", Context)
    monkeypatch.setattr(store.uuid, "uuid4", uid)
    return control


def _acquire(directory, canonical, mode, source, monkeypatch, selected=""):
    remote = MemoryR2Store()
    before = canonical.read_bytes()
    with monkeypatch.context() as patch:
        if mode == "full":
            patch.setattr(
                inputs,
                "export_day_input",
                lambda original, target, **_: export_receipts(
                    original,
                    target,
                    source_ids=[item.source_id for item in SOURCES],
                    since=datetime.min.replace(tzinfo=UTC),
                ),
            )
            patch.delitem(staging._PLANNING_INPUT_FORMATS, GROUP)
        assert inputs.publish_inputs(
            remote,
            market=directory / "unused",
            economic=canonical,
            identity="fixed",
            asof=DAY,
            generations={"economic_etag": "fixed-seed"},
            only_group=GROUP,
        ) == {"updated": [GROUP], "failed": []}
    database = directory / "input.sqlite"
    metadata = inputs.load_input(remote, GROUP, database, directory / "loaded")
    assert metadata["canonical_generations"] == {"economic_etag": "fixed-seed"}
    assert canonical.read_bytes() == before
    assert _plans(database) == _plans(canonical)
    if mode == "planning":
        actual, original = _state(database), _state(canonical)
        assert not actual["economic_source_records"]
        for table in ("economic_source_runs", *store.CONFIRMATION_TABLES):
            assert actual[table] == original[table]
    source.calls.clear()
    packet = directory / "packet"
    status = handoff.acquire("nikkei_indexes", selected, DAY, packet, database)
    expected = [
        (
            item.source_url,
            {"list": item.provider_series_id, "type": "1", "year": "2026", "month": str(month)},
        )
        for item in SOURCES
        if not selected or item.source_id == selected
        for month in (10, 11)
    ]
    assert source.calls == expected  # October gaps reuse that metric/month's bytes.
    handoff.validate_handoff(packet, "nikkei_indexes", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_nikkei_%_input_days'"
        ).fetchone()
    return raw, status, json.loads((packet / "result.json").read_text())


def _integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_metric_plans_originals_and_month_failure_recovery_match_full_input(
    tmp_path, source, monkeypatch
):
    results = []
    assert all(recent_start(item, DAY) == date(2026, 10, 23) for item in SOURCES)
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            original, initial = _state(canonical), _plans(canonical)
            assert len(set(initial.values())) == 2
            phases = []
            for phase in (1, 2, 3):
                source.clock, source.serial = WHEN + timedelta(days=phase), 0
                source.fail = phase == 1
                source.changes = {PBR: ("&mdash;", "2.90"), PER: ("0", "")} if phase == 2 else {}
                source.omitted = {(PBR, date(2026, 11, 5))} if phase == 2 else set()
                before = _state(canonical)
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(directory, canonical, mode, source, monkeypatch)
                    assert status == int(phase == 1)
                    raw_bytes = raw.read_bytes()
                    acceptance = _integrate(raw, canonical, directory / "accepted.sqlite")
                    assert not acceptance["late_recovery_required"]
                    assert raw.read_bytes() == raw_bytes
                    after = _state(canonical)
                    if phase == 1:
                        # November failed, so October's successful HTTP is not a partial receipt.
                        assert not [
                            row for row in _state(raw)["economic_source_records"] if row[0] == PER
                        ]
                        for table in ("economic_source_records", *store.CONFIRMATION_TABLES):
                            assert [row for row in before[table] if row[0] == PER] == [
                                row for row in after[table] if row[0] == PER
                            ]
                    plans = _plans(canonical)
                    for item in SOURCES:
                        old = initial[item.source_id]
                        expected = old[0] if phase == 1 and item.source_id == PER else ()
                        assert plans[item.source_id] == (expected, old[1])
                    phases.append((_state(raw), after, report, acceptance, list(source.calls)))
                with closing(store.open_read_only_connection(canonical)) as connection:
                    pbr = next(read_snapshots(connection, DAY, DAY, source_id=PBR))
                    assert list(pbr.records) == [
                        {
                            "date_text": " 2026.11.06 ",
                            "weighted_text": "—" if phase == 2 else "2.01",
                            "index_based_text": "2.90" if phase == 2 else "2.80",
                        }
                    ]
                    missing = date(2026, 11, 5)
                    assert bool(
                        next(read_snapshots(connection, missing, missing, source_id=PBR)).records
                    ) == (phase != 2)
                    if phase >= 2:
                        per = next(read_snapshots(connection, DAY, DAY, source_id=PER)).records
                        assert len(per) == 2
                        assert per[0]["weighted_text"] == ("0" if phase == 2 else "2.01")
                        assert per[0]["index_based_text"] == ("" if phase == 2 else "2.80")
                        assert per[1]["weighted_text"] == "17.00"
            received = _state(canonical)["economic_source_records"]
            assert set(original["economic_source_records"]) <= set(received)
            changes = [row for row in received if row[:2] == (PBR, str(DAY))]
            assert [row[2] for row in changes] == [
                (WHEN + timedelta(days=phase, seconds=2)).isoformat(timespec="microseconds")
                for phase in (1, 2, 3)
            ]
            assert changes[0][3:] == changes[2][3:]
            results.append(phases)
    assert results[0] == results[1]


@pytest.mark.parametrize("hour", [1, 2])
def test_late_or_same_clock_change_keeps_preceding_revision(tmp_path, source, monkeypatch, hour):
    original = tmp_path / "seed.sqlite"
    _seed(original)
    original_bytes = original.read_bytes()
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            canonical.write_bytes(original_bytes)
            for index, (when, value) in enumerate(((2, "3.01"), (hour, "4.01"))):
                source.clock, source.serial = WHEN + timedelta(hours=when), 0
                source.changes = {PBR: (value, "2.80")}
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, _ = _acquire(directory, original, mode, source, monkeypatch, PBR)
                    assert status == 0
                    raw_bytes, before = raw.read_bytes(), _state(canonical)
                    if index == 1 and hour == 2:
                        with pytest.raises(EconomicReceiptConflict):
                            _integrate(raw, canonical, directory / "accepted.sqlite")
                    else:
                        result = _integrate(raw, canonical, directory / "accepted.sqlite")
                        assert bool(result["late_recovery_required"]) == (index == 1)
                    assert raw.read_bytes() == raw_bytes
                    if index == 1:
                        assert (
                            _state(canonical)["economic_source_records"]
                            == before["economic_source_records"]
                        )
    assert original.read_bytes() == original_bytes
