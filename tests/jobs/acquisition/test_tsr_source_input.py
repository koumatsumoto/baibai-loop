"""TSR metadata-only acquisition seeds, using synthetic receipts and object stores."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
import requests
from tests.helpers.provider_fixtures import _FakeResponse
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.tsr.bankruptcies as tsr
import baibai_engine.data_collection.receipts.native_sources.store as native_store
import baibai_jobs.acquisition.source_inputs as inputs
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.acquisition.policy import (
    explicit_recovery_ranges,
    recent_start,
    recovery_ranges,
)
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
    export_tsr_input,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import load_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

SOURCE = "tsr-bankruptcies"
GROUP = "economic-tsr_bankruptcies"
DAY = date(2026, 11, 6)
WHEN = datetime(2026, 11, 6, 1, tzinfo=UTC)
MONTH = date(2026, 9, 1)


def body(value=100, *, padding=0):
    return json.dumps(
        [
            {
                "period_division": "月次",
                "title": f"2026年9月の全国企業倒産{value}件",
                "original_notes": "synthetic TSR original body " + "x" * padding,
            }
        ],
        ensure_ascii=False,
    ).encode()


def document(when=WHEN, payload=None):
    return SourceDocument(
        SOURCE,
        "history",
        require_source(SOURCE).source_url,
        "application/json",
        when,
        body() if payload is None else payload,
        {"params": {"data": "data"}},
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


def save(path, item, *, status="ok"):
    with closing(native_store.initialize_database(path)) as conn, conn:
        store_document(conn, item)
        run(
            conn,
            recent_start(require_source(SOURCE), DAY),
            DAY,
            when=item.fetched_at,
            status=status,
        )


def rows(path, table):
    with closing(native_store.open_read_only_connection(path)) as conn:
        return sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))  # nosec B608


def publish(remote, path, identity, monkeypatch, *, legacy=False):
    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(
                inputs,
                "export_tsr_input",
                lambda source, target: export_receipts(
                    source,
                    target,
                    source_ids=[SOURCE],
                    since=datetime.min.replace(tzinfo=UTC),
                ),
            )
        result = inputs.publish_inputs(
            remote,
            market=path.parent / "unused-market.sqlite",
            economic=path,
            identity=identity,
            asof=DAY,
            generations={"economic_etag": identity},
            only_group=GROUP,
        )
    assert result == {"updated": [GROUP], "failed": []}


@pytest.mark.parametrize("scenario", ["fresh", "covered", "gap", "outage", "partial", "broad"])
def test_export_keeps_exact_planner_state_without_document_pages(tmp_path, scenario):
    canonical = tmp_path / "canonical.sqlite"
    with closing(native_store.initialize_database(canonical)) as conn, conn:
        store_document(conn, document(payload=body(padding=32768)))
        if scenario != "fresh":
            run(conn, date(2020, 3, 1), DAY)
        if scenario in {"covered", "gap", "partial"}:
            run(conn, date(2020, 1, 1), DAY if scenario == "covered" else date(2020, 1, 31))
        if scenario == "outage":
            run(conn, date(2020, 2, 1), date(2020, 2, 10), status="failed")
        if scenario == "broad":
            run(conn, date(2003, 1, 1), date(2010, 1, 1), status="failed")
        if scenario == "partial":
            store_snapshot(
                conn,
                source_id=SOURCE,
                observed_at=date(2020, 2, 5),
                fetched_at=WHEN,
                records=[],
            )
    original = canonical.read_bytes()
    target = tmp_path / "input.sqlite"
    export_tsr_input(canonical, target)
    assert rows(target, "economic_source_documents") == []
    assert b"synthetic TSR original body" not in target.read_bytes()
    for table in (*native_store.RECEIPT_KEYS, *native_store.CONFIRMATION_TABLES):
        if table != "economic_source_documents":
            assert rows(target, table) == rows(canonical, table)
    plans = []
    for path in (canonical, target):
        with closing(native_store.open_read_only_connection(path)) as conn:
            plans.append(
                (
                    recovery_ranges(conn, require_source(SOURCE), DAY),
                    explicit_recovery_ranges(conn, require_source(SOURCE), DAY),
                )
            )
    assert plans[0] == plans[1]
    if scenario in {"gap", "outage", "partial"}:
        assert plans[1][0]
    if scenario == "broad":
        assert plans[1][1]
    assert canonical.read_bytes() == original


def test_legacy_fixed_input_and_reused_metadata_remain_generation_bound(tmp_path, monkeypatch):
    path = tmp_path / "canonical.sqlite"
    save(path, document())
    remote = MemoryR2Store()
    publish(remote, path, "legacy", monkeypatch, legacy=True)
    old = tmp_path / "old.sqlite"
    inputs.load_input(remote, GROUP, old, tmp_path / "old-input")
    assert rows(old, "economic_source_documents") == rows(path, "economic_source_documents")
    for identity in ("small1", "small2"):
        publish(remote, path, identity, monkeypatch)
        target = tmp_path / f"{identity}.sqlite"
        result = inputs.load_input(remote, GROUP, target, tmp_path / identity)
        assert result["canonical_generations"] == {"economic_etag": identity}
        assert rows(target, "economic_source_documents") == []
        assert rows(target, "economic_source_runs") == rows(path, "economic_source_runs")
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    load_handoff(
        remote, GROUP + "-legacy", tmp_path / "fixed", namespace="source-inputs", input_group=GROUP
    )
    assert (tmp_path / "fixed/receipts.sqlite").read_bytes() == old.read_bytes()
    pointer = json.loads(remote.values[inputs.input_key(GROUP)].body)
    pointer["canonical_generations"] = {"economic_etag": "wrong"}
    remote.store_object(inputs.input_key(GROUP), json.dumps(pointer).encode())
    with pytest.raises(LakePublishError, match="generation binding"):
        inputs.load_input(remote, GROUP, tmp_path / "wrong.sqlite", tmp_path / "wrong")
    with pytest.raises(LakePublishError, match="missing"):
        inputs.load_input(MemoryR2Store(), GROUP, tmp_path / "missing", tmp_path / "no-input")
    with pytest.raises(sqlite3.OperationalError):
        export_tsr_input(tmp_path / "no-canonical", tmp_path / "no-seed")
    assert not (tmp_path / "no-canonical").exists()
    with closing(native_store.open_connection(path)) as conn, conn:
        conn.execute("PRAGMA user_version=12")
    with pytest.raises(native_store.EconomicSchemaError, match="expected 13"):
        export_tsr_input(path, tmp_path / "old-schema")


@pytest.mark.parametrize("legacy", [False, True])
def test_original_get_corrections_failures_and_l2_are_unchanged(tmp_path, monkeypatch, legacy):
    canonical = tmp_path / "canonical.sqlite"
    save(canonical, document())
    remote = MemoryR2Store()
    clock, payload = WHEN, body()
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

    for module in (acquisition, tsr, native_store, handoff):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(acquisition, "FetchContext", context)
    events = [
        (1, body(), 1, 0, 100),
        (2, body(200), 0, 0, 200),
        (3, body(), 0, 0, 100),
        (2.5, body(300), 0, 1, 100),  # delayed handoff from the older fixed input
        (4, None, 0, 0, 100),
        (5, b"not JSON", 0, 0, 100),
        (6, b"[]", 0, 0, None),  # valid empty body is not the older value
        (7, b'{"unrecognized": null}', 0, 0, "unreadable"),
    ]
    for index, (hours, payload, unchanged, late, expected) in enumerate(events):
        clock = WHEN + timedelta(hours=hours)
        database, packet = tmp_path / f"copy{index}.sqlite", tmp_path / f"packet{index}"
        if late:
            fixed = tmp_path / f"input{index}"
            load_handoff(
                remote, GROUP + "-cycle0", fixed, namespace="source-inputs", input_group=GROUP
            )
            database.write_bytes((fixed / "receipts.sqlite").read_bytes())
        else:
            publish(remote, canonical, f"cycle{index}", monkeypatch, legacy=legacy)
            inputs.load_input(remote, GROUP, database, tmp_path / f"input{index}")
        failed = payload is None or payload == b"not JSON"
        assert handoff.acquire("tsr_bankruptcies", "", DAY, packet, database) == int(failed)
        assert len(requests_seen) == index + 1
        assert requests_seen[-1] == (require_source(SOURCE).source_url, {"data": "data"})
        received = rows(packet / "receipts.sqlite", "economic_source_documents")
        assert len(received) == int(not failed)
        if received:
            assert received[0][1:3] == ("history", clock.isoformat(timespec="microseconds"))
            assert received[0][5] == payload
            assert json.loads(received[0][6]) == {"params": {"data": "data"}}
        accepted = tmp_path / f"accepted{index}.sqlite"
        result = accept_rechecks(packet / "receipts.sqlite", canonical, accepted)
        assert result["unchanged_rechecks"] == unchanged
        assert len(result["late_recovery_required"]) == late
        merge_stores(accepted, canonical)
        verify_stores(accepted, canonical)
        fixed = tmp_path / f"l2-input{index}.sqlite"
        fixed.write_bytes(canonical.read_bytes())
        if expected == "unreadable":
            with pytest.raises(ValueError, match="root is not a list"):
                IndicatorsService(fixed).get_range("jp.bankruptcies", start=MONTH, end=MONTH)
        else:
            result = IndicatorsService(fixed).get_range("jp.bankruptcies", start=MONTH, end=MONTH)
            assert [item.value for item in result.observations] == (
                [] if expected is None else [expected]
            )
            assert result == IndicatorsService(canonical).get_range(
                "jp.bankruptcies", start=MONTH, end=MONTH
            )
    assert len(rows(canonical, "economic_source_documents")) == 5
    assert [row[6] for row in rows(canonical, "economic_source_runs")].count("failed") == 2


def test_synthetic_transfer_reduction_keeps_run_updates(tmp_path, monkeypatch, capsys):
    comparisons = {}
    for legacy in (True, False):
        root = tmp_path / ("legacy" if legacy else "metadata")
        canonical = root / "canonical.sqlite"
        for index in range(60):
            save(canonical, document(WHEN - timedelta(days=60 - index), body(index, padding=32768)))
        remote = MemoryR2Store()
        cycles = []
        for index in range(3):
            if index:
                save(
                    canonical,
                    document(WHEN + timedelta(days=index), body(100 + index, padding=32768)),
                )
            remote.get_keys.clear()
            remote.put_keys.clear()
            remote.head_keys.clear()
            publish(remote, canonical, f"cycle{index}", monkeypatch, legacy=legacy)
            published = {
                "get_count": len(remote.get_keys),
                "put_count": len(remote.put_keys),
                "put_bytes": sum(len(remote.values[key].body) for key in remote.put_keys),
                "head_count": len(remote.head_keys),
            }
            remote.get_keys.clear()
            remote.head_keys.clear()
            database = root / f"copy{index}.sqlite"
            inputs.load_input(remote, GROUP, database, root / f"input{index}")
            assert len(rows(database, "economic_source_runs")) == 60 + index
            cycles.append(
                {
                    "publish": published,
                    "load_get_count": len(remote.get_keys),
                    "load_get_bytes": sum(len(remote.values[key].body) for key in remote.get_keys),
                    "load_head_count": len(remote.head_keys),
                }
            )
        comparisons["legacy" if legacy else "metadata"] = {
            "cycles": cycles,
            "retained_bytes": sum(len(value.body) for value in remote.values.values()),
        }
    old, new = comparisons["legacy"], comparisons["metadata"]
    assert new["retained_bytes"] < old["retained_bytes"] / 10
    for before, after in zip(old["cycles"], new["cycles"], strict=True):
        assert before["load_get_count"] == after["load_get_count"] == 3
        assert before["load_head_count"] == after["load_head_count"]
        assert before["publish"]["get_count"] == after["publish"]["get_count"]
        assert before["publish"]["put_count"] == after["publish"]["put_count"] == 3
        assert after["load_get_bytes"] < before["load_get_bytes"] / 10
        assert after["publish"]["put_bytes"] < before["publish"]["put_bytes"] / 10
    with capsys.disabled():
        print("Synthetic TSR input transfer comparison: " + json.dumps(comparisons, sort_keys=True))
