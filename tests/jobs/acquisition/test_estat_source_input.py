"""e-Stat requests and fresh-canonical acceptance do not need old native values."""

import json
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _dashboard_payload, _estat_payload, _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_jobs.acquisition.economic_staging as staging
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import (
    explicit_recovery_ranges,
    recovery_ranges,
    source_policy,
)
from baibai_engine.data_collection.catalog import load_sources
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
    (item for item in load_sources().values() if source_policy(item).acquisition_group == "estat"),
    key=lambda item: item.source_id,
)
GROUP = "economic-estat"
DAY = date(2026, 11, 6)
MONTH = DAY.replace(day=1)
WHEN = datetime(2026, 11, 5, tzinfo=UTC)
CPI = "estat-cpi"
WAGES = "estat-dashboard-nominal-wage-index"
UNEMPLOYMENT = "estat-dashboard-unemployment-sa"
FAILED = "estat-dashboard-real-wage-index"


def _native(item, month, *, value="20.0", unit="index", provisional="1"):
    if item.provider == "estat":
        query = item.provider_series_id.partition("?")[2]
        identity = {"@" + key[2:].lower(): value for key, value in parse_qsl(query)}
        period = f"{month.year:04d}00{month.month:02d}{month.month:02d}"
    else:
        query = dict(parse_qsl(item.source_url.partition("?")[2]))
        identity = {
            "@indicator": query["IndicatorCode"],
            "@cycle": query["Cycle"],
            "@isSeasonal": query["IsSeasonalAdjustment"],
            "@regionCode": query["RegionCode"],
            "@isProvisional": provisional,
        }
        period = f"{month.year:04d}{month.month:02d}00"
    return {**identity, "@time": period, "@unit": unit, "$": value}


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
                    ("2025-10-01", "2025-10-03", "ok"),
                    ("2025-10-04", "2025-10-20", "failed"),
                    ("1970-01-01", "2025-01-31", "failed"),
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
                observed_at=date(2025, 10, 5 + index % 4),
                fetched_at=WHEN,
                records=[] if index % 2 == 0 else [{"old": "retained partial response"}],
            )
            confirm_snapshot(
                connection,
                source_id=item.source_id,
                observed_at=MONTH,
                fetched_at=WHEN,
                records=[_native(item, MONTH)],
            )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(
        clock=WHEN, serial=0, changes={}, failures=set(), calls=[], plans=[], current=None
    )
    monkeypatch.setenv("ESTAT_APP_ID", "synthetic-estat-app-id")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def uid():
        control.serial += 1
        return SimpleNamespace(hex=f"run{int(control.clock.timestamp())}-{control.serial}")

    class Session:
        def get(self, url, **kwargs):
            item, end = control.current
            assert url == item.source_url
            params = kwargs["params"]
            if item.provider == "estat":
                data_id, _, query = item.provider_series_id.partition("?")
                assert params == {
                    "appId": "synthetic-estat-app-id",
                    "statsDataId": data_id,
                    "metaGetFlg": "N",
                    "cntGetFlg": "N",
                    **dict(parse_qsl(query)),
                }
            else:
                assert params == {"TimeFrom": "19480100", "TimeTo": f"{end:%Y%m}00"}
            control.calls.append((item.source_id, url, params.copy()))
            control.clock += timedelta(seconds=1)
            if item.source_id in control.failures:
                payload = (
                    {"GET_STATS_DATA": {"RESULT": {"STATUS": 100, "ERROR_MSG": "unavailable"}}}
                    if item.provider == "estat"
                    else {"GET_STATS": {"RESULT": {"status": "1", "errorMsg": "unavailable"}}}
                )
                return _FakeResponse(json.dumps(payload).encode())
            change = control.changes.get(item.source_id, {})
            records = [
                _native(item, month)
                for month in (date(2000, 1, 1), date(2025, 10, 1), date(2026, 10, 1))
            ]
            if change != "omit":
                records.append(_native(item, MONTH, **change))
            text = (
                _estat_payload(records)
                if item.provider == "estat"
                else _dashboard_payload([{"VALUE": record} for record in records])
            )
            return _FakeResponse(text.encode())

        def close(self):
            pass

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = Session()

        def browser_fetcher(self):
            raise AssertionError("e-Stat must not use a browser")

    fetch = acquisition.fetch_native_records

    def observed_fetch(item, *, start, end, context):
        control.current = item, end
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
    status = handoff.acquire("estat", selected, DAY, packet, database)
    handoff.validate_handoff(packet, "estat", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_estat_%_input_days'"
        ).fetchone()
    return raw, status, json.loads((packet / "result.json").read_text())


def _integrate(raw, canonical, accepted):
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    return result


def test_both_estat_providers_preserve_group_requests_receipts_and_recovery(
    tmp_path, source, monkeypatch
):
    assert len(SOURCES) == 12
    assert {item.provider for item in SOURCES} == {"estat", "estat_dashboard"}
    results = []
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            original, initial = _state(canonical), _plans(canonical)
            assert len(set(initial.values())) == 4
            phases = []
            for phase in (1, 2, 3):
                source.clock, source.serial = WHEN + timedelta(days=phase), 0
                source.failures = {FAILED} if phase == 1 else set()
                source.changes = (
                    {
                        CPI: {"value": "21.0", "unit": "rebased index"},
                        WAGES: {"value": "null", "unit": "指数", "provisional": "2"},
                        UNEMPLOYMENT: "omit",
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
                    assert {item[0] for item in source.calls} == {
                        item.source_id for item in SOURCES
                    }
                    assert all(row["history_recovery_required"] for row in report["sources"])
                    plans = _plans(canonical)
                    for item in SOURCES:
                        old = initial[item.source_id]
                        expected = old[0] if phase == 1 and item.source_id == FAILED else ()
                        assert plans[item.source_id] == (expected, old[1])
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
                        for owner, expected in (
                            (CPI, {"value": "21.0", "unit": "rebased index"}),
                            (WAGES, {"value": "null", "unit": "指数", "provisional": "2"}),
                        ):
                            item = next(item for item in SOURCES if item.source_id == owner)
                            snapshot = next(
                                read_snapshots(connection, MONTH, MONTH, source_id=owner)
                            )
                            assert snapshot.records == (_native(item, MONTH, **expected),)
                        assert (
                            next(
                                read_snapshots(connection, MONTH, MONTH, source_id=UNEMPLOYMENT)
                            ).records
                            == ()
                        )
            assert set(original["economic_source_records"]) <= set(
                _state(canonical)["economic_source_records"]
            )
            # Same content in phase 1 advances confirmation, not a new body vintage.
            changed = [
                row
                for row in _state(canonical)["economic_source_records"]
                if row[:2] == (CPI, MONTH.isoformat())
            ]
            assert len(changed) == 3  # initial A, corrected B, and actual return to A
            results.append(phases)
    assert results[0] == results[1]


@pytest.mark.parametrize("hour", [1, 2])
def test_late_or_conflicting_same_clock_estat_response_keeps_fresh_canonical(
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
                source.changes, source.failures = {CPI: {"value": value}}, set()
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, _ = _acquire(directory, original, mode, source, monkeypatch, CPI)
                    assert status == 0
                    before = _state(canonical)
                    if index == 1 and hour == 2:
                        with pytest.raises(EconomicReceiptConflict):
                            _integrate(raw, canonical, directory / "accepted.sqlite")
                    else:
                        result = _integrate(raw, canonical, directory / "accepted.sqlite")
                        assert bool(result["late_recovery_required"]) == (index == 1)
                    if index == 1:
                        assert (
                            _state(canonical)["economic_source_records"]
                            == before["economic_source_records"]
                        )
    assert original.read_bytes() == original_bytes


def test_unknown_estat_planning_format_cannot_become_a_receipt_handoff(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical)
    remote = MemoryR2Store()
    assert (
        inputs.publish_inputs(
            remote,
            market=tmp_path / "unused",
            economic=canonical,
            identity="unknown-wire",
            asof=DAY,
            generations={"economic_etag": "fixed"},
            only_group=GROUP,
        )["failed"]
        == []
    )
    key = f"coordination/source-inputs/{GROUP}-unknown-wire/handoff.json"
    manifest = json.loads(remote.get_bytes(key))
    manifest["receipt_format"] = "estat-acquisition-input-v999"
    remote.store_object(key, json.dumps(manifest).encode())
    remote.get_keys.clear()
    with pytest.raises(ValueError, match="invalid source handoff manifest"):
        inputs.load_input(remote, GROUP, tmp_path / "rejected.sqlite", tmp_path / "download")
    assert not (tmp_path / "rejected.sqlite").exists()
    assert not any(key.endswith(".sqlite") for key in remote.get_keys)
