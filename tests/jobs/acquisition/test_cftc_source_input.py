"""CFTC planning keeps contract-specific coverage without copying native history."""

import json
import re
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
    (item for item in load_sources().values() if item.provider == "cftc"),
    key=lambda item: item.source_id,
)
GROUP = "economic-cftc"
DAY = date(2026, 11, 6)
REPORT = date(2026, 11, 3)
WHEN = datetime(2026, 11, 5, tzinfo=UTC)
JPY, SP500, UST = (item.source_id for item in SOURCES)


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
                    ("2026-08-10", "2026-08-12", "ok"),
                    ("2026-08-13", "2026-08-31", "failed"),
                    ("2026-09-01", "2026-09-03", "ok"),
                    ("2000-01-01", "2019-12-31", "failed"),
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
                observed_at=date(2026, 8, 14 + index),
                fetched_at=WHEN,
                records=[] if index % 2 == 0 else [{"old": "retained native value"}],
            )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(
        clock=WHEN,
        serial=0,
        changes={},
        failures=set(),
        calls=[],
        invalid="root",
        fail_recent=False,
    )

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
            match = re.fullmatch(
                r"cftc_contract_market_code='([^']+)' "
                r"AND report_date_as_yyyy_mm_dd between '(\d{4}-\d{2}-\d{2})T00:00:00' "
                r"and '(\d{4}-\d{2}-\d{2})T23:59:59'",
                params["$where"],
            )
            assert match
            assert params["$order"] == "report_date_as_yyyy_mm_dd"
            assert params["$limit"] == "50000"
            code, first, last = match.groups()
            start, end = date.fromisoformat(first), date.fromisoformat(last)
            item = next(item for item in SOURCES if item.provider_series_id == code)
            assert url == item.source_url
            control.calls.append((item.source_id, dict(params)))
            control.clock += timedelta(seconds=1)
            if item.source_id in control.failures and (
                control.fail_recent or start < date(2026, 9, 7)
            ):
                invalid = {
                    "root": {"error": "synthetic incomplete response"},
                    "code": [{"cftc_contract_market_code": "wrong"}],
                    "limit": [{}] * 50_000,
                }[control.invalid]
                return _FakeResponse(json.dumps(invalid).encode())
            value = control.changes.get(item.source_id, "110")
            rows = []
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                if day.weekday() != 1 or (value == "omit" and day == REPORT):
                    continue
                row = {
                    "report_date_as_yyyy_mm_dd": str(day) + "T00:00:00.000",
                    "cftc_contract_market_code": code,
                    "market_and_exchange_names": "synthetic native label",
                    "noncomm_positions_long_all": value,
                    "noncomm_positions_short_all": "0",
                    "native_extra": {"untouched": ["01", "x"]},
                }
                rows.append(row)
                if day == REPORT and item.source_id == UST:
                    rows.append(dict(row, id="second-native-row"))
            return _FakeResponse(json.dumps(rows).encode())

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("CFTC must not require a browser")

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
    status = handoff.acquire("cftc", selected, DAY, packet, database)
    assert list(dict.fromkeys(owner for owner, _ in source.calls)) == [
        item.source_id for item in SOURCES if not selected or item.source_id == selected
    ]
    assert len({json.dumps(params, sort_keys=True) for _, params in source.calls}) == len(
        source.calls
    )
    handoff.validate_handoff(packet, "cftc", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_cftc_%_input_days'"
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
                source.failures = {SP500} if phase == 1 else set()
                source.changes = {JPY: None, SP500: "omit"} if phase == 2 else {}
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(directory, canonical, mode, source, monkeypatch)
                    assert status == int(phase == 1)
                    assert len(source.calls) == (10, 6, 3)[phase - 1]
                    raw_bytes = raw.read_bytes()
                    acceptance = _integrate(raw, canonical, directory / "accepted.sqlite")
                    assert not acceptance["late_recovery_required"]
                    assert raw.read_bytes() == raw_bytes
                    assert all(row["history_recovery_required"] for row in report["sources"])
                    plans = _plans(canonical)
                    for item in SOURCES:
                        old = initial[item.source_id]
                        expected = old[0] if phase == 1 and item.source_id == SP500 else ()
                        assert plans[item.source_id] == (expected, old[1])
                    phases.append(
                        (_state(raw), _state(canonical), report, acceptance, list(source.calls))
                    )
                with closing(store.open_read_only_connection(canonical)) as connection:
                    jpy = next(read_snapshots(connection, REPORT, REPORT, source_id=JPY))
                    assert jpy.records[0]["noncomm_positions_long_all"] == (
                        None if phase == 2 else "110"
                    )
                    assert jpy.records[0]["noncomm_positions_short_all"] == "0"
                    assert jpy.records[0]["native_extra"] == {"untouched": ["01", "x"]}
                    sp = next(read_snapshots(connection, REPORT, REPORT, source_id=SP500))
                    assert bool(sp.records) == (phase != 2)
                    ust = next(read_snapshots(connection, REPORT, REPORT, source_id=UST))
                    assert len(ust.records) == 2
                    assert ust.records[1]["id"] == "second-native-row"
            received = _state(canonical)["economic_source_records"]
            assert set(original["economic_source_records"]) <= set(received)
            changes = [row for row in received if row[:2] == (JPY, str(REPORT))]
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
                source.changes = {JPY: value}
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, _ = _acquire(directory, original, mode, source, monkeypatch, JPY)
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


@pytest.mark.parametrize("invalid", ["root", "code", "limit"])
def test_incomplete_response_is_not_confirmed_as_empty(tmp_path, source, monkeypatch, invalid):
    results = []
    source.invalid, source.fail_recent, source.failures = invalid, True, {JPY}
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            source.clock, source.serial = WHEN + timedelta(days=1), 0
            before = _state(canonical)
            raw, status, report = _acquire(root, canonical, mode, source, monkeypatch, JPY)
            assert status == 1
            assert not _state(raw)["economic_source_records"]
            acceptance = _integrate(raw, canonical, root / "accepted.sqlite")
            after = _state(canonical)
            for table in ("economic_source_records", *store.CONFIRMATION_TABLES):
                assert after[table] == before[table]
            assert report["sources"][0]["status"] == "failed"
            results.append((_state(raw), after, report, acceptance))
    assert results[0] == results[1]
