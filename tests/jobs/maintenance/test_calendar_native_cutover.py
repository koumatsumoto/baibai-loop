"""Finite calendar migration keeps overlapping ranges and old dependency-only raws."""

import gzip
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY, attempt
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import complete_request
from baibai_engine.data_collection.providers.jquants.dependencies import dependency_from_request
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import calendar_native_cutover as cutover

NAMES = ("request_id", "page_index", "source", "query_json", "received_at_utc", "body_gzip")


def rows(item):
    return [
        (
            item.request_id,
            i,
            p.source,
            json.dumps(dict(p.query)),
            p.received_at.isoformat(),
            gzip.compress(p.body, mtime=0),
        )
        for i, p in enumerate(item.pages)
    ]


def stage(store, directory, items, *, source="jquants-market", dependency=None, acquisition=None):
    directory.mkdir()
    with sqlite3.connect(directory / "receipts.sqlite") as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        conn.execute("CREATE TABLE tradingview_source_receipts(receipt_id TEXT PRIMARY KEY)")
        for item in items:
            for row in rows(item):
                conn.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (*row[:5], row[4][:10], row[5]),
                )
    report = {"source": source, "asof": DAY.isoformat(), "started_at": AT.isoformat()}
    if source == "tradingview":
        report.update(
            master_request_ids=[], receipt_ids=[], master_native_input={"fixed": "elsewhere"}
        )
    else:
        report.update(request_ids=[item.request_id for item in items], request_ids_complete=True)
    if acquisition:
        report["acquisition"] = acquisition
    write_json_atomic(directory / "result.json", report)
    if dependency:
        write_json_atomic(directory / "dependency-calendar.json", dependency)
    save_handoff(store, source + "-" + directory.name, directory, market=True)


def prepare(tmp_path, monkeypatch, items, store=None):
    all_rows = [row for item in items for row in rows(item)]
    paths = ()
    if all_rows:
        path = tmp_path / "original.parquet"
        pq.write_table(
            pa.Table.from_arrays(
                [pa.array(column) for column in zip(*all_rows, strict=True)], names=NAMES
            ),
            path,
        )
        paths = (path,)
    monkeypatch.setattr(cutover, "fixed_receipt_paths", lambda **_: paths)
    store = store or MemoryR2Store()
    before = len(store.put_keys)
    with lake_session() as session:
        plan = cutover.prepare_migration(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            store=store,
            known_at=AT + timedelta(days=3),
            directory=tmp_path / "plan",
        )
    assert len(store.put_keys) == before
    return store, plan


def test_overlap_partial_empty_and_tv_dependency_raws_are_finitely_migrated(tmp_path, monkeypatch):
    store = MemoryR2Store()
    a = attempt("A", end=DAY + timedelta(days=2))
    b = attempt("B", DAY + timedelta(days=1), DAY + timedelta(days=3), hour=1, partial=True)
    c = attempt("C", DAY + timedelta(days=2), hour=2, rows=[])
    dep = attempt("dependency-only", DAY - timedelta(days=5), DAY - timedelta(days=4), hour=3)
    stage(store, tmp_path / "market", [a, b, c])
    stage(
        store,
        tmp_path / "tv",
        [],
        source="tradingview",
        dependency=dependency_from_request(dep.request_id, complete_request(dep.pages)),
    )
    store, plan = prepare(tmp_path, monkeypatch, [a], store)
    assert not plan["blocked"]
    assert {item["request_id"] for item in plan["attempts"]} == {"A", "B", "C", "dependency-only"}
    source = CalendarL1(store, tmp_path / "target")
    source.initialize()
    result = cutover.apply_migration(source, directory=tmp_path / "plan")
    assert result["failed"] == 1
    assert result["changed"] == 3
    cutover.verify_migration(source, directory=tmp_path / "plan")
    fixed = source.fixed()
    assert fixed.plan(start=DAY, end=DAY + timedelta(days=3)) == (
        DAY + timedelta(days=1),
        DAY + timedelta(days=2),
        DAY + timedelta(days=3),
    )
    for raw in fixed.requests(store):
        assert raw.attempted_at is None
        expected = {item.request_id: item for item in (a, b, c, dep)}[raw.request_id]
        assert raw.value() == expected.value()


@pytest.mark.parametrize(
    "kind", ["filtered", "missing-page", "future-clock", "unknown-failure", "no-partial"]
)
def test_unknown_scope_and_incomplete_identity_block_apply_without_writes(
    tmp_path, monkeypatch, kind
):
    store = MemoryR2Store()
    a = attempt("A")
    if kind == "future-clock":
        a = attempt("A", hour=100)
    if kind in {"unknown-failure", "no-partial"}:
        stage(
            store,
            tmp_path / "stage",
            [a],
            acquisition={
                "status": "failed",
                "failed_sources": ["unknown" if kind == "unknown-failure" else "market_calendar"],
            },
        )
    if kind in {"missing-page", "filtered"}:
        original = rows(a)[0]
        path = tmp_path / "original.parquet"
        altered = (
            (original[0], 1, *original[2:])
            if kind == "missing-page"
            else (
                *original[:3],
                json.dumps({**json.loads(original[3]), "code": "13010"}),
                *original[4:],
            )
        )
        pq.write_table(pa.Table.from_arrays([pa.array([x]) for x in altered], names=NAMES), path)
        monkeypatch.setattr(cutover, "fixed_receipt_paths", lambda **_: (path,))
        with lake_session() as session:
            plan = cutover.prepare_migration(
                session,
                release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
                cache=object(),
                store=store,
                known_at=AT,
                directory=tmp_path / "plan",
            )
    else:
        store, plan = prepare(tmp_path, monkeypatch, [a], store)
    assert plan["blocked"]
    target = CalendarL1(store, tmp_path / "target")
    target.initialize()
    before = len(store.put_keys)
    with pytest.raises(ValueError, match="blockers"):
        cutover.apply_migration(target, directory=tmp_path / "plan")
    assert len(store.put_keys) == before


def test_conflicting_release_stage_identity_blocks_migration(tmp_path, monkeypatch):
    store = MemoryR2Store()
    stage(store, tmp_path / "stage", [attempt("A", hour=1, rows=[])])
    _, plan = prepare(tmp_path, monkeypatch, [attempt("A")], store)
    assert any("different pages" in item["reason"] for item in plan["blocked"])


def test_prepared_body_tampering_is_detected_before_first_native_put(tmp_path, monkeypatch):
    store, plan = prepare(tmp_path, monkeypatch, [attempt("A")])
    source = CalendarL1(store, tmp_path / "target")
    source.initialize()
    (tmp_path / "plan" / plan["attempts"][0]["file"]).write_text("{}")
    before = len(store.put_keys)
    with pytest.raises(ValueError, match="changed"):
        cutover.apply_migration(source, directory=tmp_path / "plan")
    assert len(store.put_keys) == before
