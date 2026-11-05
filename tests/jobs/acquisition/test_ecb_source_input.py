"""ECB FX planning seeds keep daily coverage without prior currency bodies."""

import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
import requests
from tests.helpers.provider_fixtures import _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.receipts.native_sources.store as native_store
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import (
    acquisition_groups,
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
    source_policy,
)
from baibai_engine.data_collection.catalog import load_sources, require_source
from baibai_engine.data_collection.receipts.native_sources.ecb_input import (
    SOURCE,
    TABLE,
    export_ecb_input,
    validate_ecb_input,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    decode_records,
    store_snapshot,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

GROUP = "economic-ecb_fx"
DAY = date(2026, 11, 5)
WHEN = datetime(2026, 11, 5, 1, tzinfo=UTC)
OBSERVED = date(2026, 11, 2)
FIRST = date(2026, 10, 1)


def row(value="180", *, day=OBSERVED, padding="kept"):
    return {
        "Date": day.isoformat(),
        "USD": "1.2",
        "JPY": value,
        "AUD": "1.5",
        "CHF": "N/A",
        "unused": padding,
    }


def archive(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("eurofxref-hist.csv", text)
    return buffer.getvalue()


def csv(value="180"):
    return archive(f"Date,USD,JPY,AUD,CHF,unused\n{OBSERVED},1.2,{value},1.5,N/A,kept\n")


def record(conn, day=OBSERVED, when=WHEN, value="180", *, empty=False, padding=None):
    store_snapshot(
        conn,
        source_id=SOURCE,
        observed_at=day,
        fetched_at=when,
        records=[] if empty else [row(value, day=day, **({"padding": padding} if padding else {}))],
    )


def run(conn, first, last, *, when=WHEN, status="ok"):
    native_store.record_source_run(
        conn,
        source_id=SOURCE,
        start=first,
        end=last,
        started_at=when,
        status=status,
        record_count=int(status == "ok"),
        error_message="synthetic failure" if status == "failed" else None,
    )


def rows(path, table):
    with closing(native_store.open_read_only_connection(path)) as conn:
        return sorted(tuple(item) for item in conn.execute(f"SELECT * FROM {table}"))  # nosec B608


def plans(path):
    with closing(native_store.open_read_only_connection(path)) as conn:
        source = require_source(SOURCE)
        return recovery_ranges(conn, source, DAY), explicit_recovery_ranges(conn, source, DAY)


def publish(remote, path, identity, monkeypatch, *, legacy=False, group=GROUP):
    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(
                inputs,
                "export_ecb_input",
                lambda source, target: export_receipts(
                    source, target, source_ids=[SOURCE], since=datetime.min.replace(tzinfo=UTC)
                ),
            )

            def legacy_save(*args, **kwargs):
                kwargs.update(input_group=None, reusable_receipt=None)
                return save_handoff(*args, **kwargs)

            patch.setattr(inputs, "save_handoff", legacy_save)
        result = inputs.publish_inputs(
            remote,
            market=path.parent / "unused-market.sqlite",
            economic=path,
            identity=identity,
            asof=DAY,
            generations={"economic_etag": identity},
            only_group=group,
        )
    assert result == {"updated": [group], "failed": []}


@pytest.mark.parametrize(
    "scenario", ["fresh", "covered", "gap", "outage", "partial", "native_only", "broad", "empty"]
)
def test_export_keeps_exact_coverage_plan_without_original_bodies(tmp_path, scenario):
    assert recent_start(require_source(SOURCE), DAY) == DAY - timedelta(days=14)
    assert source_policy(require_source(SOURCE)).mode == "bulk_then_filter"
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        record(conn, padding="synthetic original ECB FX native body" * 1024)
        if scenario not in {"fresh", "native_only"}:
            run(conn, date(2026, 10, 15), DAY)
        if scenario in {"covered", "gap", "partial", "empty"}:
            run(conn, FIRST, DAY if scenario == "covered" else date(2026, 10, 5))
        if scenario == "outage":
            run(conn, FIRST, date(2026, 10, 14), status="failed")
        if scenario == "broad":
            run(conn, date(2000, 1, 1), date(2010, 1, 1), status="failed")
        if scenario in {"partial", "native_only", "empty", "broad"}:
            record(conn, date(2026, 10, 6), empty=scenario == "empty")
            record(conn, date(2005, 1, 1), empty=True)
    before = canonical.read_bytes()
    target = tmp_path / "input.sqlite"
    export_ecb_input(canonical, target)
    validate_ecb_input(target)
    assert rows(target, "economic_source_records") == []
    assert rows(target, "economic_source_documents") == []
    for table in ("economic_source_runs", *native_store.CONFIRMATION_TABLES):
        assert rows(target, table) == rows(canonical, table)
    assert rows(target, TABLE) == sorted(
        {(item[1],) for item in rows(canonical, "economic_source_records")}
    )
    assert plans(canonical) == plans(target)
    if scenario in {"gap", "outage", "partial", "empty"}:
        assert plans(target)[0]
    if scenario == "broad":
        assert plans(target)[1]
    if scenario == "partial":
        old_plan = plans(target)
        with closing(native_store.open_connection(target)) as conn, conn:
            record(conn, date(2026, 10, 7), when=WHEN + timedelta(hours=1), empty=True)
        assert plans(target) != old_plan
    assert canonical.read_bytes() == before


def test_export_never_reads_payload_and_preserves_nonempty_confirmations(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        record(conn)
        record(conn, empty=True, day=date(2026, 11, 3))
        record(conn, when=WHEN + timedelta(hours=1), value="60")
        stamp = WHEN.isoformat(timespec="microseconds")
        conn.execute(
            "INSERT INTO economic_source_record_confirmations VALUES (?,?,?,?,?)",
            (SOURCE, OBSERVED.isoformat(), stamp, stamp, "a" * 64),
        )
        conn.execute(
            "INSERT INTO economic_source_collection_confirmations VALUES (?,?,?)",
            (SOURCE, stamp, "b" * 64),
        )
    original = native_store.open_read_only_connection

    def without_payload(path):
        conn = original(path)
        conn.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table in {"economic_source_records", "economic_source_documents"}
                and column == "payload"
                else sqlite3.SQLITE_OK
            )
        )
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(native_store, "open_read_only_connection", without_payload)
        export_ecb_input(canonical, tmp_path / "input.sqlite")
    for table in native_store.CONFIRMATION_TABLES:
        assert rows(tmp_path / "input.sqlite", table) == rows(canonical, table)
    assert rows(tmp_path / "input.sqlite", TABLE) == [("2026-11-02",), ("2026-11-03",)]


def test_format_gate_fixed_legacy_input_and_reused_planning_blob(tmp_path, monkeypatch):
    path = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(path)) as conn, conn:
        record(conn)
        run(conn, FIRST, DAY)
    remote = MemoryR2Store()
    publish(remote, path, "legacy", monkeypatch, legacy=True)
    old = tmp_path / "old.sqlite"
    inputs.load_input(remote, GROUP, old, tmp_path / "old-input")
    assert rows(old, "economic_source_records") == rows(path, "economic_source_records")
    for identity in ("small1", "small2"):
        publish(remote, path, identity, monkeypatch)
        target = tmp_path / f"{identity}.sqlite"
        result = inputs.load_input(remote, GROUP, target, tmp_path / identity)
        assert result["canonical_generations"] == {"economic_etag": identity}
        assert rows(target, "economic_source_records") == []
        assert plans(target) == plans(old)
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    prefix = f"coordination/source-inputs/{GROUP}-small2"
    manifest = json.loads(remote.values[prefix + "/handoff.json"].body)
    assert manifest["receipt_format"] == "ecb-fx-acquisition-input-v1"
    assert "planning_key" in manifest
    assert "receipt_key" not in manifest
    # Same explicit gate as the old reader: a planning file cannot be a native receipt.
    assert not isinstance(manifest.get("receipt_key"), str)
    for kwargs in (
        {"namespace": "source-inputs"},
        {"namespace": "source-inputs", "input_group": "economic-tsr_bankruptcies"},
    ):
        with pytest.raises(ValueError, match="not a source receipt handoff"):
            load_handoff(remote, GROUP + "-small2", tmp_path / "wrong-group", **kwargs)
    fixed = tmp_path / "fixed"
    load_handoff(remote, GROUP + "-legacy", fixed, namespace="source-inputs", input_group=GROUP)
    assert (fixed / "receipts.sqlite").read_bytes() == old.read_bytes()
    pointer = json.loads(remote.values[inputs.input_key(GROUP)].body)
    pointer["canonical_generations"] = {"economic_etag": "wrong"}
    remote.store_object(inputs.input_key(GROUP), json.dumps(pointer).encode())
    with pytest.raises(LakePublishError, match="generation binding"):
        inputs.load_input(remote, GROUP, tmp_path / "wrong.sqlite", tmp_path / "wrong")
    with pytest.raises(LakePublishError, match="missing"):
        inputs.load_input(MemoryR2Store(), GROUP, tmp_path / "missing", tmp_path / "no-input")
    with pytest.raises(sqlite3.OperationalError):
        export_ecb_input(tmp_path / "no-canonical", tmp_path / "no-seed")
    assert not (tmp_path / "no-canonical").exists()
    with closing(native_store.open_connection(path)) as conn, conn:
        conn.execute("PRAGMA user_version=12")
    with pytest.raises(native_store.EconomicSchemaError, match="expected 13"):
        export_ecb_input(path, tmp_path / "old-schema")


@pytest.mark.parametrize("damage", ["body", "other_source", "missing_table"])
def test_planning_input_is_not_a_synthetic_source_receipt(tmp_path, damage):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)):
        pass
    seed = tmp_path / "seed.sqlite"
    export_ecb_input(canonical, seed)
    with closing(native_store.open_connection(seed)) as conn, conn:
        if damage == "body":
            record(conn)
        elif damage == "missing_table":
            conn.execute(f"DROP TABLE {TABLE}")
        else:
            native_store.record_source_run(
                conn,
                source_id="tsr-bankruptcies",
                start=DAY,
                end=DAY,
                started_at=WHEN,
                status="ok",
                record_count=0,
            )
    with pytest.raises(ValueError, match="ECB FX acquisition"):
        validate_ecb_input(seed)


@pytest.mark.parametrize("legacy", [False, True])
def test_actual_zip_rechecks_corrections_late_failures_and_cross_rate_l2(
    tmp_path, monkeypatch, legacy
):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        record(conn)
        run(conn, recent_start(require_source(SOURCE), DAY), DAY)
    remote = MemoryR2Store()
    clock, payload = WHEN, csv()
    requests_seen = []
    real_context = acquisition.FetchContext

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz or UTC)

    class Session:
        def get(self, url, **kwargs):
            requests_seen.append((url, kwargs["params"]))
            if payload is None:
                raise requests.Timeout("synthetic failure")
            return _FakeResponse(payload)

        def close(self):
            pass

    def context(**kwargs):
        value = real_context(**kwargs)
        value.session.close()
        value.session = Session()
        return value

    for module in (acquisition, native_store, handoff):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", context)
    normal = {"usd_jpy": 150, "eur_jpy": 180, "aud_jpy": 120}
    missing = dict.fromkeys(normal)
    no_usd = {key: value for key, value in row().items() if key != "USD"}
    events = [
        (1, csv(), row(), normal),
        (2, csv("192"), row("192"), {"usd_jpy": 160, "eur_jpy": 192, "aud_jpy": 128}),
        (3, csv(), row(), normal),
        (2.5, csv("186"), row("186"), normal),
        (4, None, None, normal),
        (5, b"not a ZIP archive", None, normal),
        (6, archive("Date,USD,JPY\nbad-date,1.2,180\n"), None, normal),
        (7, csv("N/A"), row("N/A"), missing),
        (8, archive("Date,USD,JPY,AUD,CHF,unused\n"), [], missing),
        (
            9,
            archive(f"Date,JPY,AUD,CHF,unused\n{OBSERVED},180,1.5,N/A,kept\n"),
            no_usd,
            {"usd_jpy": None, "eur_jpy": 180, "aud_jpy": 120},
        ),
    ]
    for index, (hours, payload, expected_row, expected_l2) in enumerate(events):
        clock = WHEN + timedelta(hours=hours)
        database, packet = tmp_path / f"copy{index}.sqlite", tmp_path / f"packet{index}"
        late = hours == 2.5
        if late:
            fixed = tmp_path / f"input{index}"
            load_handoff(
                remote, GROUP + "-cycle0", fixed, namespace="source-inputs", input_group=GROUP
            )
            database.write_bytes((fixed / "receipts.sqlite").read_bytes())
        else:
            publish(remote, canonical, f"cycle{index}", monkeypatch, legacy=legacy)
            inputs.load_input(remote, GROUP, database, tmp_path / f"input{index}")
        failed = expected_row is None
        before_requests = len(requests_seen)
        assert handoff.acquire("ecb_fx", "", DAY, packet, database) == int(failed)
        assert len(requests_seen) - before_requests == (2 if payload is None else 1)
        assert requests_seen[-1] == (require_source(SOURCE).source_url, None)
        received = rows(packet / "receipts.sqlite", "economic_source_records")
        assert bool(received) == (not failed)
        if not failed:
            target = next(item for item in received if item[1] == OBSERVED.isoformat())
            assert target[2] == clock.isoformat(timespec="microseconds")
            assert decode_records(target[4]) == (() if expected_row == [] else (expected_row,))
        with closing(native_store.open_read_only_connection(packet / "receipts.sqlite")) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
        accepted = tmp_path / f"accepted{index}.sqlite"
        result = accept_rechecks(packet / "receipts.sqlite", canonical, accepted)
        if index == 0:
            assert result["unchanged_rechecks"] == 1
        assert bool(result["late_recovery_required"]) == late
        merge_stores(accepted, canonical)
        verify_stores(accepted, canonical)
        fixed = tmp_path / f"l2-input{index}.sqlite"
        fixed.write_bytes(canonical.read_bytes())
        for series, expected in expected_l2.items():
            result = IndicatorsService(fixed).get_range(series, start=OBSERVED, end=OBSERVED)
            assert [item.value for item in result.observations] == (
                [] if expected is None else [expected]
            )
    assert (
        len(
            [
                item
                for item in rows(canonical, "economic_source_records")
                if item[1] == OBSERVED.isoformat()
            ]
        )
        == 6
    )
    assert [item[6] for item in rows(canonical, "economic_source_runs")].count("failed") == 3


def test_partial_outage_keeps_real_request_count_and_recovery_ranges(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        run(conn, FIRST, date(2026, 10, 14), status="failed")
        record(conn, date(2026, 10, 6), empty=True)
    calls = []
    real_context = acquisition.FetchContext

    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResponse(csv())

        def close(self):
            pass

    def context(**kwargs):
        value = real_context(**kwargs)
        value.session.close()
        value.session = Session()
        return value

    monkeypatch.setattr(acquisition, "FetchContext", context)
    results = []
    for legacy in (True, False):
        base = tmp_path / str(legacy)
        base.mkdir()
        seed = base / "seed.sqlite"
        if legacy:
            export_receipts(
                canonical, seed, source_ids=[SOURCE], since=datetime.min.replace(tzinfo=UTC)
            )
        else:
            export_ecb_input(canonical, seed)
        assert len(plans(seed)[0]) == 2
        calls.clear()
        assert handoff.acquire("ecb_fx", "", DAY, base / "stage", seed) == 0
        assert len(calls) == 1
        assert len(rows(base / "stage/receipts.sqlite", "economic_source_runs")) == 3
        report = json.loads((base / "stage/result.json").read_text())
        result_rows = rows(base / "stage/receipts.sqlite", "economic_source_records")
        results.append(
            (report["sources"], [(item[0], item[1], item[3], item[4]) for item in result_rows])
        )
    assert results[0] == results[1]
    assert len(results[0][0]) == 1
    assert results[0][0][0]["start"] == FIRST.isoformat()


def test_other_fourteen_group_exports_are_byte_identical(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    catalog = load_sources()
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        for source in catalog.values():
            store_snapshot(
                conn,
                source_id=source.source_id,
                observed_at=DAY,
                fetched_at=WHEN,
                records=[{"native": source.source_id}],
            )
            native_store.record_source_run(
                conn,
                source_id=source.source_id,
                start=DAY,
                end=DAY,
                started_at=WHEN,
                status="ok",
                record_count=1,
            )
    groups = [group for group in acquisition_groups() if group != "ecb_fx"]
    assert len(groups) == 14
    for group in groups:
        remote = MemoryR2Store()
        publish(remote, canonical, "same", monkeypatch, group="economic-" + group)
        target = tmp_path / f"{group}.sqlite"
        inputs.load_input(remote, "economic-" + group, target, tmp_path / group)
        expected = tmp_path / f"{group}-legacy.sqlite"
        export_receipts(
            canonical,
            expected,
            source_ids=sorted(
                source.source_id
                for source in catalog.values()
                if source_policy(source).acquisition_group == group
            ),
            since=datetime.min.replace(tzinfo=UTC),
        )
        assert target.read_bytes() == expected.read_bytes()


def test_changed_catalog_does_not_silently_omit_a_new_ecb_source(tmp_path, monkeypatch):
    catalog = load_sources()
    extra = replace(require_source(SOURCE), source_id="ecb-future-resource")
    monkeypatch.setattr(
        inputs, "load_economic_sources", lambda: {**catalog, extra.source_id: extra}
    )
    remote = MemoryR2Store()
    result = inputs.publish_inputs(
        remote,
        market=tmp_path / "unused-market",
        economic=tmp_path / "unused-economic",
        identity="changed-catalog",
        asof=DAY,
        generations={},
        only_group=GROUP,
    )
    assert result == {"updated": [], "failed": [{"group": GROUP, "reason": "ValueError"}]}
    assert not remote.put_keys


def test_synthetic_transfer_reduction_keeps_run_and_coverage_updates(tmp_path, monkeypatch, capsys):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        for index in range(120):
            # Synthetic entropy stays visible after the native receipt's gzip encoding.
            padding = "".join(
                hashlib.sha256(f"{index}-{chunk}".encode()).hexdigest() for chunk in range(128)
            )
            record(
                conn,
                OBSERVED - timedelta(days=index),
                WHEN - timedelta(days=120 - index),
                str(index),
                padding=padding,
            )
            run(conn, FIRST, DAY, when=WHEN - timedelta(days=120 - index))
    comparisons = {}
    for legacy in (True, False):
        root = tmp_path / ("legacy" if legacy else "planning")
        root.mkdir()
        source = root / "canonical.sqlite"
        source.write_bytes(canonical.read_bytes())
        remote, cycles = MemoryR2Store(), []
        for index in range(3):
            if index:
                with closing(native_store.open_connection(source)) as conn, conn:
                    record(conn, OBSERVED, WHEN + timedelta(hours=index), str(50 + index))
                    run(conn, FIRST, DAY, when=WHEN + timedelta(hours=index))
            remote.get_keys.clear()
            remote.put_keys.clear()
            publish(remote, source, f"cycle{index}", monkeypatch, legacy=legacy)
            published = {
                "get_count": len(remote.get_keys),
                "put_count": len(remote.put_keys),
                "put_bytes": sum(len(remote.values[key].body) for key in remote.put_keys),
            }
            remote.get_keys.clear()
            remote.head_keys.clear()
            target = root / f"input{index}.sqlite"
            inputs.load_input(remote, GROUP, target, root / str(index))
            assert len(rows(target, "economic_source_runs")) == 120 + index
            cycles.append(
                {
                    "publish": published,
                    "load_get_count": len(remote.get_keys),
                    "load_head_count": len(remote.head_keys),
                    "load_get_bytes": sum(len(remote.values[key].body) for key in remote.get_keys),
                }
            )
        comparisons["legacy" if legacy else "planning"] = {
            "cycles": cycles,
            "retained_bytes": sum(len(value.body) for value in remote.values.values()),
        }
    old, new = comparisons["legacy"], comparisons["planning"]
    assert new["retained_bytes"] < old["retained_bytes"] / 4
    for before, after in zip(old["cycles"], new["cycles"], strict=True):
        assert before["load_get_count"] == after["load_get_count"] == 3
        assert before["load_head_count"] == after["load_head_count"]
        assert before["publish"]["get_count"] == after["publish"]["get_count"]
        assert before["publish"]["put_count"] == after["publish"]["put_count"] == 3
        assert after["load_get_bytes"] < before["load_get_bytes"] / 4
    with capsys.disabled():
        print("Synthetic ECB FX input comparison: " + json.dumps(comparisons, sort_keys=True))
