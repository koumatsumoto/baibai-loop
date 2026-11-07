"""経済J-Quantsの計画は旧本文でなく受領日とrunを使う。"""

import json
import random
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.jquants.flows as flows
import baibai_engine.data_collection.providers.jquants.indices as indices
import baibai_engine.data_collection.providers.jquants.options as options
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
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.confirmations import confirm_snapshot
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
    stored_days,
)
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

GROUP = "economic-jquants"
INDICES, OPTIONS, FLOWS = (
    "jquants-indices-topix",
    "jquants-options-n225",
    "jquants-trades-spec",
)
TABLES = {
    INDICES: "economic_jquants_indices_topix_input_days",
    OPTIONS: "economic_jquants_options_n225_input_days",
    FLOWS: "economic_jquants_trades_spec_input_days",
}
SOURCES = sorted(
    (
        item
        for item in load_sources().values()
        if source_policy(item).acquisition_group == "jquants"
    ),
    key=lambda item: item.source_id,
)
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 5, tzinfo=UTC)


def _native(source, day, *, value="20.0", unit="native"):
    if source == INDICES:
        return [{"Date": str(day), "O": "10", "H": "22", "L": "9", "C": value, "Unit": unit}]
    if source == FLOWS:
        return [
            {
                "PubDate": str(day),
                "StartDate": str(day - timedelta(days=6)),
                "EndDate": str(day - timedelta(days=2)),
                "Section": "TSEPrime",
                "FrgnNet": value,
                "Unit": unit,
            }
        ]
    return [
        {"Date": str(day), "Code": code, "IV": value, "ContractUnit": unit}
        for code in ("synthetic-call", "synthetic-put")
    ]


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
                    (first, first + timedelta(days=2), "ok"),
                    (first + timedelta(days=3), first + timedelta(days=12), "failed"),
                    (date(2000, 1, 1), date(2025, 1, 31), "failed"),
                )
            ):
                connection.execute(
                    "INSERT INTO economic_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"seed-{index}-{number}",
                        item.source_id,
                        str(start),
                        str(end),
                        WHEN.isoformat(timespec="microseconds"),
                        WHEN.isoformat(timespec="microseconds"),
                        status,
                        0,
                        None if status == "ok" else "synthetic failed history",
                    ),
                )
            for offset in (5, 9):
                day = first + timedelta(days=offset)
                store_snapshot(
                    connection,
                    source_id=item.source_id,
                    observed_at=day,
                    fetched_at=WHEN,
                    records=[] if offset == 5 else _native(item.source_id, day),
                )
            confirm_snapshot(
                connection,
                source_id=item.source_id,
                observed_at=DAY,
                fetched_at=WHEN,
                records=_native(item.source_id, DAY),
            )


@pytest.fixture
def source(monkeypatch):
    control = SimpleNamespace(
        clock=WHEN, serial=0, changes={}, failures=set(), empty_days=set(), calls=[]
    )
    monkeypatch.setenv("JQUANTS_API_KEY", "synthetic-jquants-key")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return control.clock.astimezone(tz or UTC)

    def uid():
        control.serial += 1
        return SimpleNamespace(hex=f"run{int(control.clock.timestamp())}-{control.serial}")

    class Client:
        JQUANTS_API_BASE = "https://synthetic.jquants.test/v2"

        def _get(self, url, *, params):
            endpoint = url.removeprefix(self.JQUANTS_API_BASE)
            if endpoint == "/derivatives/bars/daily/options/225":
                owner = OPTIONS
                assert set(params) <= {"date", "pagination_key"}
                first = last = date.fromisoformat(params["date"])
            else:
                owner = FLOWS if endpoint == "/equities/investor-types" else INDICES
                assert endpoint in {"/equities/investor-types", "/indices/bars/daily/topix"}
                assert set(params) <= {"from", "to", "section", "pagination_key"}
                assert (params.get("section") == "TSEPrime") == (owner == FLOWS)
                first, last = (date.fromisoformat(params[key]) for key in ("from", "to"))
            control.clock += timedelta(seconds=1)
            control.calls.append((owner, endpoint, params.copy(), control.clock))
            if (owner, first) in control.failures:
                raise ValueError("synthetic incomplete provider response")
            rows = []
            for offset in range((last - first).days + 1):
                day = first + timedelta(days=offset)
                if (owner, day) in control.empty_days:
                    continue
                if day != DAY and day.weekday() != (3 if owner == FLOWS else 0):
                    continue
                change = control.changes.get(owner, {}) if day == DAY else {}
                if change != "omit":
                    rows.extend(_native(owner, day, **change))
            if params.get("pagination_key") == "page-two":
                payload = {"data": rows[1:]}
            else:
                payload = {"data": rows[:1]}
                if len(rows) > 1:
                    payload["pagination_key"] = "page-two"
            return SimpleNamespace(json=lambda: payload, close=lambda: None)

        def _request_session(self):
            return self

        def close(self):
            pass

    for module in (acquisition, handoff, inputs, store):
        monkeypatch.setattr(module, "datetime", Clock)
    for module in (flows, indices, options):
        monkeypatch.setattr(module, "jquants_read_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(store.uuid, "uuid4", uid)
    return control


def _input(directory, canonical, mode, monkeypatch, *, remote=None, identity="fixed"):
    remote = remote or MemoryR2Store()
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
            identity=identity,
            asof=DAY,
            generations={"economic_etag": identity},
            only_group=GROUP,
        ) == {"updated": [GROUP], "failed": []}
    database = directory / "input.sqlite"
    inputs.load_input(remote, GROUP, database, directory / "loaded")
    assert canonical.read_bytes() == before
    assert _plans(database) == _plans(canonical)
    return database, remote


def _acquire(directory, canonical, mode, source, monkeypatch, selected=""):
    database, _ = _input(directory, canonical, mode, monkeypatch)
    source.calls.clear()
    packet = directory / "packet"
    status = handoff.acquire("jquants", selected, DAY, packet, database)
    handoff.validate_handoff(packet, "jquants", selected or None)
    raw = packet / "receipts.sqlite"
    with closing(store.open_read_only_connection(raw)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'economic_jquants_%_input_days'"
        ).fetchone()
    return raw, status, json.loads((packet / "result.json").read_text())


def _integrate(raw, canonical, accepted):
    before = raw.read_bytes()
    result = accept_rechecks(raw, canonical, accepted)
    merge_stores(accepted, canonical)
    verify_stores(accepted, canonical)
    assert raw.read_bytes() == before
    return result


def test_group_export_keeps_only_exact_days_runs_and_confirmations(tmp_path):
    assert {item.source_id for item in SOURCES} == set(TABLES)
    canonical, target = tmp_path / "canonical.sqlite", tmp_path / "input.sqlite"
    _seed(canonical)
    with closing(store.open_connection(canonical)) as connection, connection:
        for index, item in enumerate(SOURCES):
            for revision in range(4):
                store_snapshot(
                    connection,
                    source_id=item.source_id,
                    observed_at=DAY,
                    fetched_at=WHEN + timedelta(minutes=revision + 1),
                    records=[
                        {"old_native_body": random.Random(index + revision).randbytes(32_768).hex()}
                    ],
                )
        store_snapshot(
            connection,
            source_id="fred-payems",
            observed_at=DAY,
            fetched_at=WHEN,
            records=[{"unrelated": "never copy this source"}],
        )
    before, original = canonical.read_bytes(), _state(canonical)
    export_day_input(canonical, target, group=GROUP)
    validate_planning_input(target, GROUP)
    assert canonical.read_bytes() == before
    assert target.stat().st_size < len(before) / 2
    actual = _state(target)
    assert not actual["economic_source_records"]
    assert not actual["economic_source_documents"]
    for metadata in ("economic_source_runs", *store.CONFIRMATION_TABLES):
        assert actual[metadata] == original[metadata]
    with (
        closing(store.open_read_only_connection(canonical)) as received,
        closing(store.open_read_only_connection(target)) as planned,
    ):
        for owner, table in TABLES.items():
            expected = received.execute(
                "SELECT DISTINCT observed_at FROM economic_source_records WHERE source_id=? ORDER BY observed_at",
                (owner,),
            ).fetchall()
            assert (
                planned.execute(f"SELECT observed_at FROM {table} ORDER BY observed_at").fetchall()
                == expected
            )
            assert stored_days(received, date.min, DAY, source_id=owner) == stored_days(
                planned, date.min, DAY, source_id=owner
            )
        assert not stored_days(planned, date.min, DAY, source_id="fred-payems")


def test_stored_days_unions_bounded_real_and_seed_days_without_crossing_sources(tmp_path):
    canonical, target = tmp_path / "canonical.sqlite", tmp_path / "input.sqlite"
    _seed(canonical)
    export_day_input(canonical, target, group=GROUP)
    before = canonical.read_bytes()
    first = recent_start(next(item for item in SOURCES if item.source_id == OPTIONS), DAY)
    empty, nonempty, new = first - timedelta(days=15), first - timedelta(days=11), first
    with closing(store.open_connection(target)) as connection, connection:
        assert stored_days(connection, empty, nonempty, source_id=OPTIONS) == {empty, nonempty}
        assert not stored_days(
            connection, empty + timedelta(days=1), nonempty - timedelta(days=1), source_id=OPTIONS
        )
        store_snapshot(connection, source_id=OPTIONS, observed_at=new, fetched_at=WHEN, records=[])
        assert stored_days(connection, date.min, DAY, source_id=OPTIONS) == {
            empty,
            nonempty,
            new,
            DAY,
        }
        assert new not in stored_days(connection, date.min, DAY, source_id=INDICES)
        assert not stored_days(connection, date.min, DAY, source_id="jquants_daily_bars")
        rows = connection.execute(
            "SELECT source_id,observed_at,record_count FROM economic_source_records"
        ).fetchall()
        assert [tuple(row) for row in rows] == [(OPTIONS, str(new), 0)]
    with closing(store.open_read_only_connection(canonical)) as connection:
        expected = {
            date.fromisoformat(row[0])
            for row in connection.execute(
                "SELECT observed_at FROM economic_source_records WHERE source_id=?", (OPTIONS,)
            )
        }
        assert stored_days(connection, date.min, DAY, source_id=OPTIONS) == expected
    assert canonical.read_bytes() == before


def test_group_http_requests_receipts_recovery_and_a_b_a_match_full_seed(
    tmp_path, source, monkeypatch
):
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
                source.failures = {(OPTIONS, date(2026, 10, 29))} if phase == 1 else set()
                source.changes = (
                    {
                        INDICES: {"value": "21.0", "unit": "rebased"},
                        OPTIONS: {"value": None, "unit": "changed"},
                        FLOWS: "omit",
                    }
                    if phase == 2
                    else {}
                )
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, report = _acquire(directory, canonical, mode, source, monkeypatch)
                    assert status == int(phase == 1)
                    accepted = _integrate(raw, canonical, directory / "accepted.sqlite")
                    assert not accepted["late_recovery_required"]
                    assert {call[0] for call in source.calls} == set(TABLES)
                    assert any("pagination_key" in call[2] for call in source.calls)
                    for row in _state(raw)["economic_source_records"]:
                        assert row[2] in {
                            call[3].isoformat(timespec="microseconds")
                            for call in source.calls
                            if call[0] == row[0]
                        }
                    assert all(row["history_recovery_required"] for row in report["sources"])
                    for owner, plan in _plans(canonical).items():
                        assert plan == (
                            initial[owner][0] if phase == 1 and owner == OPTIONS else (),
                            initial[owner][1],
                        )
                    phases.append(
                        (_state(raw), _state(canonical), report, accepted, source.calls[:])
                    )
                if phase == 2:
                    with closing(store.open_read_only_connection(canonical)) as connection:
                        for owner in TABLES:
                            snapshot = next(read_snapshots(connection, DAY, DAY, source_id=owner))
                            expected = (
                                []
                                if owner == FLOWS
                                else _native(owner, DAY, **source.changes[owner])
                            )
                            assert snapshot.records == tuple(expected)
                            assert snapshot.fetched_at > WHEN
            current = _state(canonical)
            assert set(original["economic_source_records"]) <= set(
                current["economic_source_records"]
            )
            for owner in TABLES:
                assert (
                    len(
                        [
                            row
                            for row in current["economic_source_records"]
                            if row[:2] == (owner, str(DAY))
                        ]
                    )
                    == 3
                )
            results.append(phases)
    assert results[0] == results[1]


@pytest.mark.parametrize("mode", ["recent", "rebuild", "explicit"])
def test_options_request_days_match_full_seed_including_known_empty_and_unknown(
    tmp_path, source, monkeypatch, mode
):
    results = []
    old_empty, old_nonempty, unknown, fail = (date(2016, 11, day) for day in (7, 8, 9, 10))
    for seed in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            _seed(canonical)
            with closing(store.open_connection(canonical)) as connection, connection:
                for day, records in (
                    (old_empty, []),
                    (old_nonempty, _native(OPTIONS, old_nonempty)),
                ):
                    store_snapshot(
                        connection,
                        source_id=OPTIONS,
                        observed_at=day,
                        fetched_at=WHEN,
                        records=records,
                    )
            source.clock, source.serial = WHEN + timedelta(days=1), 0
            source.calls.clear()
            source.failures = {(OPTIONS, fail)} if mode != "recent" else set()
            source.empty_days = {(OPTIONS, old_empty), (OPTIONS, unknown)}
            database, _ = _input(root, canonical, seed, monkeypatch)
            outcomes = acquisition.refresh_sources(
                [OPTIONS],
                db_path=database,
                start=old_empty if mode == "explicit" else None,
                end=DAY,
                recent=mode == "recent",
            )
            calls = [
                date.fromisoformat(call[2]["date"])
                for call in source.calls
                if "pagination_key" not in call[2]
            ]
            if mode == "rebuild":
                assert calls == [unknown, fail]
            elif mode == "explicit":
                assert calls == [old_empty, old_nonempty, unknown, fail]
            else:
                item = next(item for item in SOURCES if item.source_id == OPTIONS)
                recent = recent_start(item, DAY)
                assert calls[:11] == [
                    recent + timedelta(days=offset)
                    for offset in range(15)
                    if (recent + timedelta(days=offset)).weekday() < 5
                ]
                assert recent - timedelta(days=15) not in calls  # retained empty 2026-10-08
                assert date(2026, 10, 6) in calls  # no successful receipt
                assert not isinstance(outcomes[0], acquisition.SourceRefreshFailure)
            results.append((outcomes, source.calls[:], _plans(database)))
            with closing(store.open_read_only_connection(database)) as connection:
                assert old_empty in stored_days(connection, old_empty, DAY, source_id=OPTIONS)
                if mode != "recent":
                    assert unknown in stored_days(connection, unknown, unknown, source_id=OPTIONS)
                    assert fail not in stored_days(connection, fail, fail, source_id=OPTIONS)
                assert not stored_days(connection, old_empty, DAY, source_id=INDICES) & {
                    old_empty,
                    old_nonempty,
                }
    assert results[0] == results[1]


@pytest.mark.parametrize("owner", TABLES)
@pytest.mark.parametrize("hour", [1, 2])
def test_stale_input_cannot_roll_back_or_conflict_with_fresh_canonical(
    tmp_path, source, monkeypatch, owner, hour
):
    original = tmp_path / "seed.sqlite"
    _seed(original)
    before = original.read_bytes()
    for mode in ("full", "planning"):
        with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
            root = Path(temporary)
            canonical = root / "canonical.sqlite"
            canonical.write_bytes(before)
            for index, (when, value) in enumerate(((2, "30.0"), (hour, "40.0"))):
                source.clock, source.serial = WHEN + timedelta(hours=when), 0
                source.changes, source.failures = {owner: {"value": value}}, set()
                with tempfile.TemporaryDirectory(dir=root) as work:
                    directory = Path(work)
                    raw, status, _ = _acquire(directory, original, mode, source, monkeypatch, owner)
                    assert status == 0
                    current = _state(canonical)
                    if index == 1 and hour == 2:
                        with pytest.raises(EconomicReceiptConflict):
                            _integrate(raw, canonical, directory / "accepted.sqlite")
                    else:
                        accepted = _integrate(raw, canonical, directory / "accepted.sqlite")
                        assert bool(accepted["late_recovery_required"]) == (index == 1)
                    if index == 1:
                        assert (
                            _state(canonical)["economic_source_records"]
                            == current["economic_source_records"]
                        )
    assert original.read_bytes() == before


def test_old_new_old_inputs_and_planning_blob_reuse_keep_original_bytes(
    tmp_path, source, monkeypatch
):
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical)
    remote = MemoryR2Store()
    manifests = []
    for number, mode in enumerate(("full", "planning", "planning", "full")):
        root = tmp_path / str(number)
        root.mkdir()
        database, _ = _input(
            root, canonical, mode, monkeypatch, remote=remote, identity=str(number)
        )
        manifests.append(
            json.loads(
                remote.get_bytes(f"coordination/source-inputs/{GROUP}-{number}/handoff.json")
            )
        )
        if mode == "full":
            assert _state(database) == _state(canonical)
            assert "receipt_key" in manifests[-1]
            assert "planning_key" not in manifests[-1]
        else:
            validate_planning_input(database, GROUP)
    assert manifests[1]["planning_key"] == manifests[2]["planning_key"]
    staging.load_handoff(
        remote, GROUP + "-0", tmp_path / "old-fixed", namespace="source-inputs", input_group=GROUP
    )
    assert (tmp_path / "old-fixed/receipts.sqlite").read_bytes() == remote.get_bytes(
        manifests[0]["receipt_key"]
    )
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 3
    assert all(key.startswith("coordination/source-inputs/") for key in remote.put_keys)


@pytest.mark.parametrize(
    "damage",
    [
        "other_group",
        "handoff",
        "market",
        "ambiguous",
        "unknown",
        "unknown_legacy",
        "null_format",
        "integer_format",
    ],
)
def test_planning_wire_cannot_be_loaded_in_another_scope(tmp_path, source, monkeypatch, damage):
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical)
    _, remote = _input(tmp_path, canonical, "planning", monkeypatch)
    key = f"coordination/source-inputs/{GROUP}-fixed/handoff.json"
    manifest = json.loads(remote.get_bytes(key))
    options = {"namespace": "source-inputs", "input_group": GROUP}
    if damage == "other_group":
        options["input_group"] = "economic-fred_csv"
    elif damage == "handoff":
        options["namespace"] = "economic-sources"
        remote.store_object(
            f"coordination/economic-sources/{GROUP}-fixed/handoff.json",
            json.dumps(manifest).encode(),
        )
    elif damage == "market":
        options["market"] = True
    else:
        if damage != "unknown":
            manifest["receipt_key"] = manifest["planning_key"]
        if damage.startswith("unknown"):
            manifest["receipt_format"] = "jquants-acquisition-input-v999"
        elif damage == "null_format":
            manifest["receipt_format"] = None
        elif damage == "integer_format":
            manifest["receipt_format"] = 1
        remote.store_object(key, json.dumps(manifest).encode())
    remote.get_keys.clear()
    with pytest.raises(ValueError, match=r"planning input|receipt format"):
        staging.load_handoff(remote, GROUP + "-fixed", tmp_path / "rejected", **options)
    assert not (tmp_path / "rejected").exists()
    assert not any(key.endswith(".sqlite") for key in remote.get_keys)


@pytest.mark.parametrize("damage", ["table", "body", "foreign_run", "foreign_confirmation"])
def test_planning_contents_require_all_three_sources_and_no_originals(tmp_path, damage):
    canonical, target = tmp_path / "canonical.sqlite", tmp_path / "input.sqlite"
    _seed(canonical)
    export_day_input(canonical, target, group=GROUP)
    with closing(store.open_connection(target)) as connection, connection:
        if damage == "table":
            connection.execute(f"DROP TABLE {TABLES[OPTIONS]}")
        elif damage == "body":
            store_snapshot(
                connection, source_id=OPTIONS, observed_at=DAY, fetched_at=WHEN, records=[]
            )
        elif damage == "foreign_run":
            store.record_source_run(
                connection,
                source_id="fred-payems",
                start=DAY,
                end=DAY,
                started_at=WHEN,
                status="ok",
                record_count=0,
            )
        else:
            connection.execute(
                "INSERT INTO economic_source_record_confirmations VALUES ('fred-payems', '2026-11-06', ?, ?, 'synthetic')",
                (str(WHEN), str(WHEN)),
            )
    with pytest.raises(ValueError, match=r"table|original|another source"):
        validate_planning_input(target, GROUP)
