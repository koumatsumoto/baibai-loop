"""Yahoo's grouped planning input keeps each symbol's receipt and recovery scope."""

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
from baibai_engine.data_collection.acquisition.policy import recent_start, recovery_ranges
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    EconomicReceiptConflict,
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.planning_inputs import (
    export_day_input,
    validate_planning_input,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.transport.context import FetchContext
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCES = sorted(
    (item for item in load_sources().values() if item.provider == "yahoo"),
    key=lambda item: item.source_id,
)
GROUP = "economic-yahoo"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 1, tzinfo=UTC)
GOLD = "yahoo-gc-f"
SOX = "yahoo-sox"


def state(path):
    with closing(store.open_read_only_connection(path)) as connection:
        return {
            table: sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in (*store.RECEIPT_KEYS, *store.CONFIRMATION_TABLES)
        }


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(clock=WHEN, serial=0, changes={}, failures=set(), calls=[])
    by_url = {item.source_url: (index, item) for index, item in enumerate(SOURCES)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def uid():
        control.serial += 1
        return SimpleNamespace(hex=f"run{int(control.clock.timestamp())}-{control.serial}")

    class Session:
        def get(self, url, **kwargs):
            index, item = by_url[url]
            control.calls.append((item.source_id, kwargs["params"]))
            control.clock += timedelta(seconds=1)
            value = control.changes.get(item.source_id, 100 + index)
            stamps = (
                []
                if value == "omit"
                else [int(datetime.combine(DAY, datetime.min.time(), UTC).timestamp())]
            )
            result = {
                "meta": {"symbol": item.provider_series_id, "currency": "USD"},
                "events": {"splits": {"synthetic": {"numerator": 2, "denominator": 1}}},
                "timestamp": stamps,
                "indicators": {
                    "quote": [
                        {
                            "open": [99] * len(stamps),
                            "volume": [123] * len(stamps),
                            "close": [value] * len(stamps),
                        }
                    ],
                    "adjclose": [{"adjclose": [95] * len(stamps)}],
                },
            }
            error = (
                {"code": "synthetic source failure"} if item.source_id in control.failures else None
            )
            return _FakeResponse(
                json.dumps({"chart": {"error": error, "result": [result]}}).encode()
            )

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", Context)
    monkeypatch.setattr(store.uuid, "uuid4", uid)
    return control


def seed(path):
    with closing(store.initialize_database(path)):
        pass
    outcomes = acquisition.refresh_sources(
        [item.source_id for item in SOURCES],
        db_path=path,
        start=recent_start(SOURCES[0], DAY),
        end=DAY,
    )
    assert len(outcomes) == 10
    assert all(isinstance(item, acquisition.SourceRefreshSuccess) for item in outcomes)


def acquire(directory, canonical, mode, source, monkeypatch, selected=""):
    remote = MemoryR2Store()
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
    if mode == "planning":
        assert not state(database)["economic_source_records"]
    source.calls.clear()
    packet = directory / "packet"
    status = handoff.acquire("yahoo", selected, DAY, packet, database)
    expected = [selected] if selected else [item.source_id for item in SOURCES]
    assert [item[0] for item in source.calls] == expected
    for _, params in source.calls:
        assert params == {
            "period1": str(
                int(
                    datetime.combine(
                        recent_start(SOURCES[0], DAY), datetime.min.time(), UTC
                    ).timestamp()
                )
            ),
            "period2": str(
                int(datetime.combine(DAY + timedelta(days=1), datetime.min.time(), UTC).timestamp())
            ),
            "interval": "1d",
        }
    handoff.validate_handoff(packet, "yahoo", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_yahoo_%_input_days'"
            ).fetchone()
            is None
        )
    return raw, status, json.loads((packet / "result.json").read_text())


def integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_group_rechecks_preserve_null_disappearance_revisions_and_partial_failure(
    tmp_path, source, monkeypatch
):
    original = tmp_path / "seed.sqlite"
    seed(original)
    original_bytes = original.read_bytes()
    results = []
    for mode in ("full", "planning"):
        canonical = tmp_path / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original_bytes)
        phases = []
        for hour, changes in enumerate(
            (
                {GOLD: 1500},
                {GOLD: 103},
                {GOLD: None, SOX: "omit"},
                {GOLD: None, SOX: "omit"},
                {GOLD: 0},
            ),
            1,
        ):
            source.clock, source.serial = WHEN + timedelta(hours=hour), 0
            source.changes = changes
            source.failures = {"yahoo-acwx"} if hour == 4 else set()
            before = state(canonical)
            with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
                directory = Path(temporary)
                raw, status, report = acquire(directory, canonical, mode, source, monkeypatch)
                assert status == int(bool(source.failures))
                assert {
                    row["source_id"] for row in report["sources"] if row["status"] == "failed"
                } == source.failures
                raw_bytes = raw.read_bytes()
                result = integrate(raw, canonical, directory / "accepted.sqlite")
                assert not result["late_recovery_required"]
                assert raw.read_bytes() == raw_bytes
                phases.append((state(raw), state(canonical), report, result))
            after = state(canonical)
            assert set(before["economic_source_records"]) <= set(after["economic_source_records"])
            for failed in source.failures:
                assert [row for row in after["economic_source_records"] if row[0] == failed] == [
                    row for row in before["economic_source_records"] if row[0] == failed
                ]
            with closing(store.open_read_only_connection(canonical)) as connection:
                gold = next(read_snapshots(connection, DAY, DAY, source_id=GOLD))
                assert gold.records[0]["indicators"] == {
                    "quote": [{"open": 99, "volume": 123, "close": changes[GOLD]}],
                    "adjclose": [{"adjclose": 95}],
                }
                assert gold.records[0]["metadata"] == {
                    "meta": {"symbol": "GC=F", "currency": "USD"},
                    "events": {"splits": {"synthetic": {"numerator": 2, "denominator": 1}}},
                }
                sox = next(read_snapshots(connection, DAY, DAY, source_id=SOX))
                assert bool(sox.records) == (hour not in (3, 4))
        results.append(phases)
    assert results[0] == results[1]
    assert original.read_bytes() == original_bytes
    changes = [
        row for row in state(canonical)["economic_source_records"] if row[:2] == (GOLD, str(DAY))
    ]
    assert [row[2] for row in changes] == [
        (WHEN + timedelta(hours=hour, seconds=4)).isoformat(timespec="microseconds")
        for hour in (0, 1, 2, 3, 5)
    ]


@pytest.mark.parametrize("hour", [1, 2])
def test_selected_symbol_late_or_same_clock_response_keeps_group_originals(
    tmp_path, source, monkeypatch, hour
):
    original = tmp_path / "seed.sqlite"
    seed(original)
    original_bytes = original.read_bytes()
    source.changes = {GOLD: 1500}
    for mode in ("full", "planning"):
        canonical = tmp_path / f"canonical-{mode}.sqlite"
        canonical.write_bytes(original_bytes)
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            directory = Path(temporary)
            source.clock, source.serial, source.changes = WHEN + timedelta(hours=2), 0, {GOLD: 1500}
            raw, status, _ = acquire(directory, original, mode, source, monkeypatch, GOLD)
            assert status == 0
            integrate(raw, canonical, directory / "accepted.sqlite")
        before = state(canonical)
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            directory = Path(temporary)
            source.clock, source.serial, source.changes = (
                WHEN + timedelta(hours=hour),
                0,
                {GOLD: 1700},
            )
            raw, status, _ = acquire(directory, original, mode, source, monkeypatch, GOLD)
            assert status == 0
            raw_bytes = raw.read_bytes()
            if hour == 2:
                with pytest.raises(EconomicReceiptConflict):
                    integrate(raw, canonical, directory / "accepted.sqlite")
            else:
                result = integrate(raw, canonical, directory / "accepted.sqlite")
                assert result["late_recovery_required"]
            assert raw.read_bytes() == raw_bytes
        assert state(canonical)["economic_source_records"] == before["economic_source_records"]
    assert original.read_bytes() == original_bytes


def test_empty_received_day_of_one_symbol_does_not_cover_another_symbols_outage(tmp_path):
    first = recent_start(SOURCES[0], DAY) - timedelta(days=5)
    last = recent_start(SOURCES[0], DAY) - timedelta(days=1)
    canonical, planning = tmp_path / "canonical.sqlite", tmp_path / "planning.sqlite"
    with closing(store.initialize_database(canonical)) as connection, connection:
        for index, item in enumerate(SOURCES[:2]):
            store.record_source_run(
                connection,
                source_id=item.source_id,
                start=first,
                end=last,
                started_at=WHEN,
                status="failed",
                record_count=0,
            )
            store_snapshot(
                connection,
                source_id=item.source_id,
                observed_at=first + timedelta(days=index),
                fetched_at=WHEN,
                records=[] if index == 0 else [{"native": "other symbol"}],
            )
    before = canonical.read_bytes()
    export_day_input(canonical, planning, group=GROUP)
    with (
        closing(store.open_read_only_connection(canonical)) as original,
        closing(store.open_read_only_connection(planning)) as small,
    ):
        for item in SOURCES:
            assert recovery_ranges(original, item, DAY) == recovery_ranges(small, item, DAY)
        assert recovery_ranges(small, SOURCES[0], DAY) == ((first + timedelta(days=1), last),)
        assert recovery_ranges(small, SOURCES[1], DAY) == (
            (first, first),
            (first + timedelta(days=2), last),
        )
        assert not small.execute("SELECT 1 FROM economic_source_records").fetchone()
    assert canonical.read_bytes() == before


@pytest.mark.parametrize("invalid", ["source", "empty_table"])
def test_group_input_requires_every_symbol_table_and_rejects_foreign_metadata(tmp_path, invalid):
    canonical, planning = tmp_path / "canonical.sqlite", tmp_path / "planning.sqlite"
    with closing(store.initialize_database(canonical)):
        pass
    export_day_input(canonical, planning, group=GROUP)
    with closing(store.open_connection(planning)) as connection, connection:
        if invalid == "empty_table":
            connection.execute("DROP TABLE economic_yahoo_stoxx600_input_days")
        else:
            store.record_source_run(
                connection,
                source_id="mof-jgbcm-all",
                start=DAY,
                end=DAY,
                started_at=WHEN,
                status="failed",
                record_count=0,
            )
    with pytest.raises(ValueError, match=r"source|table"):
        validate_planning_input(planning, GROUP)
