"""BOJ planning needs ranges and received days, not past workbook/API values."""

import calendar
import json
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _boj_workbook_bytes, _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.acquisition.economic_staging as staging
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import (
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
    source_policy,
)
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.providers.boj.timeseries import parse_boj_timeseries_json
from baibai_engine.data_collection.providers.boj.workbooks import parse_boj_xlsx
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.confirmations import confirm_snapshot
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
    (item for item in load_sources().values() if source_policy(item).acquisition_group == "boj"),
    key=lambda item: item.source_id,
)
GROUP = "economic-boj"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 5, tzinfo=UTC)
WORKBOOK = "boj-mblong"
MONTHLY = "boj-cgpi"
DAILY = "boj-timeseries-strdclucon"
QUARTERLY = "boj-tankan-lmfg"
OMITTED_QUARTERLY = "boj-tankan-lnonmfg"
FAILED = "boj-cai"


def _periods(item):
    if item.frequency == "quarterly":
        return [date(2024, 3, 1), date(2024, 6, 1), date(2026, 9, 1), date(2026, 12, 1)]
    if item.frequency == "daily":
        return [date(2024, 3, 1), date(2026, 10, 5), date(2026, 11, 5), DAY]
    return [date(2024, 3, 1), date(2025, 10, 1), date(2026, 10, 1), DAY.replace(day=1)]


def _primary(item):
    return _periods(item)[2 if item.frequency == "quarterly" else 3]


def _body(
    item, *, value=20, unit="original unit", provisional="provisional", old_value=10, omitted=()
):
    values = {
        day: value if day == _primary(item) else old_value if index == 0 else 30
        for index, day in enumerate(_periods(item))
        if day not in omitted
    }
    if item.provider == "boj":
        return _boj_workbook_bytes(
            [
                (None, day.replace(day=calendar.monthrange(day.year, day.month)[1]), cell, "p")
                for day, cell in values.items()
            ],
            metadata="Unit: " + unit,
            other_metadata=(5, provisional),
        )
    labels = [
        int(day.strftime("%Y%m%d"))
        if item.frequency == "daily"
        else day.year * 100 + day.month // 3
        if item.frequency == "quarterly"
        else int(day.strftime("%Y%m"))
        for day in values
    ]
    return json.dumps(
        {
            "STATUS": 200,
            "MESSAGE": "synthetic response metadata",
            "RESULTSET": [
                {
                    "SERIES_CODE": item.provider_series_id.split(":")[1],
                    "UNIT": unit,
                    "PROVISIONAL": provisional,
                    "VALUES": {
                        "SURVEY_DATES": labels,
                        "VALUES": list(values.values()),
                        "NOTE": "original period labels",
                    },
                }
            ],
        }
    ).encode()


def _records(item, **changes):
    body = _body(item, **changes)
    arguments = {"start": date(2024, 1, 1), "end": date(2026, 12, 31)}
    return (
        parse_boj_xlsx(item, body, **arguments)
        if item.provider == "boj"
        else parse_boj_timeseries_json(item, body.decode(), **arguments)
    )


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
            first = recent_start(item, DAY) - timedelta(days=20)
            for number, (start, end, status) in enumerate(
                (
                    (first, first + timedelta(days=3), "ok"),
                    (first + timedelta(days=4), first + timedelta(days=12), "failed"),
                    (date(1970, 1, 1), date(2010, 12, 31), "failed"),
                )
            ):
                connection.execute(
                    "INSERT INTO economic_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"seed-{index}-{number}",
                        item.source_id,
                        start.isoformat(),
                        end.isoformat(),
                        WHEN.isoformat(timespec="microseconds"),
                        WHEN.isoformat(timespec="microseconds"),
                        status,
                        0,
                        None if status == "ok" else "synthetic failed history",
                    ),
                )
            for offset, records in ((5, []), (8, [{"partial": "retained original"}])):
                store_snapshot(
                    connection,
                    source_id=item.source_id,
                    observed_at=first + timedelta(days=offset),
                    fetched_at=WHEN,
                    records=records,
                )
            for day, records in _records(item).items():
                confirm_snapshot(
                    connection,
                    source_id=item.source_id,
                    observed_at=day,
                    fetched_at=WHEN,
                    records=records,
                )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(
        clock=WHEN, serial=0, changes={}, failures=set(), calls=[], plans=[], current=None
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
            item, start, end = control.current
            assert url == item.source_url
            params = kwargs["params"]
            if item.provider == "boj":
                assert params is None
            else:
                database, code = item.provider_series_id.split(":")
                expected = {"format": "json", "lang": "en", "db": database, "code": code}
                if item.frequency != "quarterly":
                    expected.update(startDate=start.strftime("%Y%m"), endDate=end.strftime("%Y%m"))
                assert params == expected
            control.calls.append((item.source_id, url, params))
            control.clock += timedelta(seconds=1)
            body = (
                b"invalid BOJ response"
                if item.source_id in control.failures
                else _body(item, **control.changes.get(item.source_id, {}))
            )
            return _FakeResponse(body)

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("BOJ must not use a browser")

    fetch = acquisition.fetch_native_records

    def observed_fetch(item, *, start, end, context):
        control.current = item, start, end
        control.plans.append((item.source_id, start, end))
        return fetch(item, start=start, end=end, context=context)

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", Context)
    monkeypatch.setattr(acquisition, "fetch_native_records", observed_fetch)
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
    assert inputs.load_input(remote, GROUP, database, directory / "loaded")[
        "canonical_generations"
    ] == {"economic_etag": "fixed-seed"}
    assert canonical.read_bytes() == before
    assert _plans(database) == _plans(canonical)
    if mode == "planning":
        actual, original = _state(database), _state(canonical)
        assert not actual["economic_source_records"]
        assert not actual["economic_source_documents"]
        for table in ("economic_source_runs", *store.CONFIRMATION_TABLES):
            assert actual[table] == original[table]
    source.calls.clear()
    source.plans.clear()
    packet = directory / "packet"
    status = handoff.acquire("boj", selected, DAY, packet, database)
    handoff.validate_handoff(packet, "boj", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_boj_%_input_days'"
        ).fetchone()
    return raw, status, json.loads((packet / "result.json").read_text())


def _integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_workbook_and_api_plans_receipts_revisions_and_recovery_match_full_input(
    tmp_path, source, monkeypatch
):
    assert len(SOURCES) == 10
    assert {item.provider for item in SOURCES} == {"boj", "boj_timeseries"}
    results = []
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            original, initial = _state(canonical), _plans(canonical)
            phases = []
            for phase in (1, 2, 3):
                source.clock, source.serial = WHEN + timedelta(days=phase), 0
                source.failures = {FAILED} if phase == 1 else set()
                source.changes = (
                    {
                        WORKBOOK: {"value": None, "unit": "revised unit", "provisional": "final"},
                        MONTHLY: {"value": "0", "unit": "rebased index", "provisional": "final"},
                        DAILY: {"omitted": [DAY]},
                        QUARTERLY: {
                            "value": None,
                            "old_value": 11,
                            "omitted": [date(2024, 6, 1)],
                            "unit": "revised D.I.",
                        },
                        OMITTED_QUARTERLY: {"omitted": [date(2026, 9, 1)]},
                    }
                    if phase == 2
                    else {}
                )
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(directory, canonical, mode, source, monkeypatch)
                    assert status == int(phase == 1)
                    raw_bytes = raw.read_bytes()
                    acceptance = _integrate(raw, canonical, directory / "accepted.sqlite")
                    assert not acceptance["late_recovery_required"]
                    assert raw.read_bytes() == raw_bytes
                    assert {call[0] for call in source.calls} == {
                        item.source_id for item in SOURCES
                    }
                    assert all(row["history_recovery_required"] for row in report["sources"])
                    plans = _plans(canonical)
                    for item in SOURCES:
                        old = initial[item.source_id]
                        gap_remains = item.frequency == "quarterly" or (
                            phase == 1 and item.source_id == FAILED
                        )
                        assert plans[item.source_id] == (old[0] if gap_remains else (), old[1])
                        if item.frequency == "quarterly":
                            assert [plan for plan in source.plans if plan[0] == item.source_id] == [
                                (item.source_id, recent_start(item, DAY), DAY)
                            ]
                    phases.append(
                        (
                            _state(raw),
                            _state(canonical),
                            report,
                            acceptance,
                            source.calls[:],
                            source.plans[:],
                        )
                    )
                if phase == 2:
                    with closing(store.open_read_only_connection(canonical)) as connection:
                        for owner in (WORKBOOK, MONTHLY, QUARTERLY):
                            item = next(item for item in SOURCES if item.source_id == owner)
                            day = _primary(item)
                            snapshot = next(read_snapshots(connection, day, day, source_id=owner))
                            assert snapshot.records == tuple(
                                _records(item, **source.changes[owner])[day]
                            )
                            assert snapshot.fetched_at.date() != snapshot.observed_at
                        assert (
                            next(read_snapshots(connection, DAY, DAY, source_id=DAILY)).records
                            == ()
                        )
                        for owner, day in (
                            (QUARTERLY, date(2024, 6, 1)),
                            (OMITTED_QUARTERLY, date(2026, 9, 1)),
                        ):
                            snapshot = next(read_snapshots(connection, day, day, source_id=owner))
                            assert snapshot.fetched_at < source.clock - timedelta(days=1)
                            assert snapshot.records[0]["value"] in (20, 30)
                        assert not connection.execute(
                            "SELECT 1 FROM economic_source_collection_confirmations"
                        ).fetchone()
            final = _state(canonical)
            assert set(original["economic_source_records"]) <= set(final["economic_source_records"])
            for owner in (WORKBOOK, MONTHLY):
                item = next(item for item in SOURCES if item.source_id == owner)
                assert (
                    len(
                        [
                            row
                            for row in final["economic_source_records"]
                            if row[:2] == (owner, _primary(item).isoformat())
                        ]
                    )
                    == 3
                )
            results.append(phases)
    assert results[0] == results[1]


@pytest.mark.parametrize("selected", [DAILY, QUARTERLY])
@pytest.mark.parametrize("hour", [1, 2, 3])
def test_stale_seed_return_to_a_uses_fresh_canonical_and_real_clock(
    tmp_path, source, monkeypatch, selected, hour
):
    seed = tmp_path / "seed.sqlite"
    _seed(seed)
    original_bytes = seed.read_bytes()
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            canonical.write_bytes(original_bytes)
            for index, (when, value) in enumerate(((2, 30), (hour, 20))):
                source.clock, source.serial = WHEN + timedelta(hours=when), 0
                source.changes, source.failures = {selected: {"value": value}}, set()
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(
                        directory, seed, mode, source, monkeypatch, selected
                    )
                    assert status == 0
                    assert [row["source_id"] for row in report["sources"]] == [selected]
                    assert {call[0] for call in source.calls} == {selected}
                    before = _state(canonical)
                    if index == 1 and hour == 2:
                        with pytest.raises(EconomicReceiptConflict):
                            _integrate(raw, canonical, directory / "accepted.sqlite")
                    else:
                        result = _integrate(raw, canonical, directory / "accepted.sqlite")
                        assert bool(result["late_recovery_required"]) == (index == 1 and hour < 2)
                    if index == 1 and hour <= 2:
                        assert (
                            _state(canonical)["economic_source_records"]
                            == before["economic_source_records"]
                        )
                    elif index == 1:
                        item = next(item for item in SOURCES if item.source_id == selected)
                        day = _primary(item)
                        with closing(store.open_read_only_connection(canonical)) as connection:
                            snapshot = next(
                                read_snapshots(connection, day, day, source_id=selected)
                            )
                            assert snapshot.records[0]["value"] == 20
                            assert snapshot.fetched_at >= WHEN + timedelta(hours=3)
    assert seed.read_bytes() == original_bytes
