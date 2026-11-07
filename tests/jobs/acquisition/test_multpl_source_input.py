"""Multpl planning keeps source-specific holes without copying historical rows."""

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
    recovery_ranges,
)
from baibai_engine.data_collection.catalog import load_sources
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
    (item for item in load_sources().values() if item.provider == "multpl"),
    key=lambda item: item.source_id,
)
GROUP = "economic-multpl"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 5, tzinfo=UTC)
SHILLER, YIELD, PE = (item.source_id for item in SOURCES)


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
                    ("2026-10-10", "2026-10-12", "ok"),
                    ("2026-10-13", "2026-10-22", "failed"),
                    ("1871-01-01", "2025-01-31", "failed"),
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
                observed_at=date(2026, 10, 14 + index),
                fetched_at=WHEN,
                records=[] if index % 2 == 0 else [{"old": "retained native value"}],
            )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(clock=WHEN, serial=0, changes={}, failures=set(), calls=[])
    by_url = {item.source_url: item.source_id for item in SOURCES}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def uid():
        control.serial += 1
        return SimpleNamespace(hex=f"run{int(control.clock.timestamp())}-{control.serial}")

    class Session:
        def get(self, url, **kwargs):
            owner = by_url[url]
            assert kwargs["params"] is None
            control.calls.append(url)
            control.clock += timedelta(seconds=1)
            if owner in control.failures:
                return _FakeResponse(b"<html>synthetic missing table</html>")
            value = control.changes.get(owner, "21.0")
            current = "" if value == "omit" else f"<tr><td>Nov 6, 2026</td><td>{value}</td></tr>"
            return _FakeResponse(
                (
                    '<table id="datatable"><caption>synthetic caption</caption>'
                    + current
                    + '<tr><td>Nov 1, 2026</td><td><abbr title="Estimate">*</abbr> 20.0%</td></tr>'
                    + "<tr><td>Oct 1, 2026</td><td>19.0</td></tr></table>"
                    + '<div id="description">Source <a href="https://fixture.example/method">'
                    + "method</a>.</div>"
                ).encode()
            )

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("Multpl must not require a browser")

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
    status = handoff.acquire("multpl", selected, DAY, packet, database)
    assert source.calls == [
        item.source_url for item in SOURCES if not selected or item.source_id == selected
    ]
    handoff.validate_handoff(packet, "multpl", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_multpl_%_input_days'"
        ).fetchone()
    return raw, status, json.loads((packet / "result.json").read_text())


def _integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_group_plans_originals_and_partial_failure_recovery_match_full_input(
    tmp_path, source, monkeypatch
):
    results = []
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            original, initial = _state(canonical), _plans(canonical)
            assert len(set(initial.values())) == 3
            phases = []
            for phase in (1, 2, 3):
                source.clock, source.serial = WHEN + timedelta(days=phase), 0
                source.failures = {YIELD} if phase == 1 else set()
                source.changes = {SHILLER: "null", PE: "omit"} if phase == 2 else {}
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(directory, canonical, mode, source, monkeypatch)
                    assert status == int(phase == 1)
                    raw_bytes = raw.read_bytes()
                    acceptance = _integrate(raw, canonical, directory / "accepted.sqlite")
                    assert not acceptance["late_recovery_required"]
                    assert raw.read_bytes() == raw_bytes
                    assert all(row["history_recovery_required"] for row in report["sources"])
                    plans = _plans(canonical)
                    for item in SOURCES:
                        old = initial[item.source_id]
                        expected = old[0] if phase == 1 and item.source_id == YIELD else ()
                        assert plans[item.source_id] == (expected, old[1])
                    phases.append((_state(raw), _state(canonical), report, acceptance))
                if phase == 2:
                    with closing(store.open_read_only_connection(canonical)) as connection:
                        shiller = next(read_snapshots(connection, DAY, DAY, source_id=SHILLER))
                        assert shiller.records[0]["value_text"] == "null"
                        assert (
                            next(read_snapshots(connection, DAY, DAY, source_id=PE)).records == ()
                        )
                        first = DAY.replace(day=1)
                        month = next(read_snapshots(connection, first, first, source_id=YIELD))
                        assert month.records[0]["value_annotations"] == [
                            {"text": "*", "title": "Estimate"}
                        ]
                        assert month.records[0]["description_links"] == [
                            {"text": "method", "href": "https://fixture.example/method"}
                        ]
            received = _state(canonical)["economic_source_records"]
            assert set(original["economic_source_records"]) <= set(received)
            changes = [row for row in received if row[:2] == (SHILLER, str(DAY))]
            assert [row[2] for row in changes] == [
                (WHEN + timedelta(days=phase, seconds=1)).isoformat(timespec="microseconds")
                for phase in (1, 2, 3)
            ]
            results.append(phases)
    assert results[0] == results[1]


@pytest.mark.parametrize("hour", [1, 2])
def test_late_or_same_clock_response_does_not_replace_canonical_originals(
    tmp_path, source, monkeypatch, hour
):
    original = tmp_path / "seed.sqlite"
    _seed(original)
    original_bytes = original.read_bytes()
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            canonical.write_bytes(original_bytes)
            for index, (when, value) in enumerate(((2, "30.0"), (hour, "40.0"))):
                source.clock, source.serial = WHEN + timedelta(hours=when), 0
                source.changes = {SHILLER: value}
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, _ = _acquire(
                        directory, original, mode, source, monkeypatch, SHILLER
                    )
                    assert status == 0
                    raw_bytes = raw.read_bytes()
                    before = _state(canonical)
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
