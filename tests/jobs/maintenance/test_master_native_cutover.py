"""Finite master migration covers both historical market and TradingView writers."""

import gzip
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.master_native import MasterAttempt, MasterL1
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import master_native_cutover as cutover

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)
NAMES = ("request_id", "page_index", "source", "query_json", "received_at_utc", "body_gzip")


def row(identity, *, day=DAY, at=AT, query=None, index=0, partial=False, records=None):
    records = (
        records
        if records is not None
        else [
            {
                "Date": day.isoformat(),
                "Code": "13010",
                "CoName": "全製品原本",
                "MktNm": "プライム",
                "S33Nm": "情報・通信業",
                "ProdCat": "011",
                "unknown": {"raw": None},
            }
        ]
    )
    body = json.dumps({"data": records, **({"pagination_key": "next"} if partial else {})}).encode()
    return (
        identity,
        index,
        "master",
        json.dumps(query or {"date": day.isoformat()}),
        at.isoformat(),
        gzip.compress(body, mtime=0),
    )


def stage(store, directory, *, source, rows, declared=None, acquisition=None, receipts=None):
    directory.mkdir()
    with sqlite3.connect(directory / "receipts.sqlite") as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        conn.execute("CREATE TABLE tradingview_source_receipts(receipt_id TEXT PRIMARY KEY)")
        for item in rows:
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                (*item[:5], item[4][:10], item[5]),
            )
        for identity in receipts or []:
            conn.execute("INSERT INTO tradingview_source_receipts VALUES (?)", (identity,))
    identities = list({item[0] for item in rows}) if declared is None else declared
    report = {"source": source, "asof": DAY.isoformat(), "started_at": AT.isoformat()}
    if source == "tradingview":
        report.update(
            master_request_ids=identities,
            receipt_ids=receipts or [],
            master_asof=DAY.isoformat(),
            symbols=[],
        )
    else:
        report.update(request_ids=identities, request_ids_complete=True)
    if acquisition:
        report["acquisition"] = acquisition
    write_json_atomic(directory / "result.json", report)
    save_handoff(store, source + "-" + directory.name, directory, market=True)


def prepare(tmp_path, monkeypatch, rows, store=None):
    original = tmp_path / "original.parquet"
    pq.write_table(
        pa.Table.from_arrays([pa.array(column) for column in zip(*rows, strict=True)], names=NAMES),
        original,
    )
    monkeypatch.setattr(cutover, "accepted_dataset", lambda *a: cutover.JQUANTS_SOURCE_PAGES)
    monkeypatch.setattr(cutover, "selected_partitions", lambda *a: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *a, **kw: (original,))
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


def test_market_and_tradingview_stages_keep_all_master_bytes_partial_and_empty(
    tmp_path, monkeypatch
):
    store = MemoryR2Store()
    old = row("old")
    newer = row("new", at=AT + timedelta(hours=1))
    partial = row("partial", at=AT + timedelta(hours=2), partial=True)
    empty = row("empty", day=DAY + timedelta(days=1), at=AT + timedelta(hours=3), records=[])
    other = (*row("other")[:2], "daily_bars", *row("other")[3:])
    stage(store, tmp_path / "market", source="jquants-market", rows=[old, other, newer])
    stage(store, tmp_path / "tv", source="tradingview", rows=[partial, empty], receipts=["mcp"])
    store, plan = prepare(tmp_path, monkeypatch, [old], store)
    assert not plan["blocked"]
    assert {item["request_id"] for item in plan["attempts"]} == {"old", "new", "partial", "empty"}
    saved = MasterAttempt.from_value(
        json.loads((tmp_path / "plan" / plan["attempts"][0]["file"]).read_bytes())
    )
    assert saved.attempted_at is None
    assert saved.pages[0].body == gzip.decompress(old[-1])
    assert saved.pages[0].received_at == AT
    owner = MasterL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["equal"] == 1
    assert result["failed"] == 1
    assert result["changed"] == 2
    cutover.verify_migration(MasterL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
    assert cutover.apply_migration(owner, directory=tmp_path / "plan")["retried"] == 4
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["failure"] == "partial"


@pytest.mark.parametrize("source", ["jquants-market", "tradingview"])
def test_missing_declared_request_in_either_writer_blocks(tmp_path, monkeypatch, source):
    store = MemoryR2Store()
    stage(store, tmp_path / "stage", source=source, rows=[], declared=["missing"])
    _, plan = prepare(tmp_path, monkeypatch, [row("old")], store)
    assert any("missing or undeclared request" in item["reason"] for item in plan["blocked"])


@pytest.mark.parametrize(
    "bad",
    [
        row("filtered", query={"code": "13010"}),
        row("gap", index=1),
        row("future", at=AT + timedelta(days=5)),
    ],
)
def test_unknown_scope_page_gap_or_future_receipt_blocks_before_write(tmp_path, monkeypatch, bad):
    store, plan = prepare(tmp_path, monkeypatch, [bad])
    assert plan["blocked"]
    owner = MasterL1(store, tmp_path / "target")
    owner.initialize()
    before = len(store.put_keys)
    with pytest.raises(ValueError, match="unresolved"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert len(store.put_keys) == before


def test_identity_conflict_between_old_writers_blocks(tmp_path, monkeypatch):
    store = MemoryR2Store()
    stage(store, tmp_path / "tv", source="tradingview", rows=[row("same", records=[])])
    _, plan = prepare(tmp_path, monkeypatch, [row("same")], store)
    assert any("different pages" in item["reason"] for item in plan["blocked"])


def test_tradingview_declared_master_cannot_be_another_source(tmp_path, monkeypatch):
    store = MemoryR2Store()
    other = (*row("other")[:2], "daily_bars", *row("other")[3:])
    stage(store, tmp_path / "tv", source="tradingview", rows=[other])
    _, plan = prepare(tmp_path, monkeypatch, [row("old")], store)
    assert any("another J-Quants source" in item["reason"] for item in plan["blocked"])


def test_nonempty_population_validation_is_not_a_raw_migration_requirement(tmp_path, monkeypatch):
    store, plan = prepare(
        tmp_path, monkeypatch, [row("small"), row("empty", at=AT + timedelta(hours=1), records=[])]
    )
    assert not plan["blocked"]
    owner = MasterL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"] == "empty"


@pytest.mark.parametrize("failures", [["ValueError"], ["unavailable_originals"], ["master"]])
def test_unknown_or_unrepresented_master_failure_blocks(tmp_path, monkeypatch, failures):
    store = MemoryR2Store()
    stage(
        store,
        tmp_path / "market",
        source="jquants-market",
        rows=[],
        acquisition={"status": "failed", "failed_sources": failures},
    )
    _, plan = prepare(tmp_path, monkeypatch, [row("old")], store)
    assert plan["blocked"]


def test_known_unrelated_failure_and_empty_tradingview_master_set_remain_valid(
    tmp_path, monkeypatch
):
    store = MemoryR2Store()
    stage(
        store,
        tmp_path / "market",
        source="jquants-market",
        rows=[],
        acquisition={"status": "failed", "failed_sources": ["margin_interest"]},
    )
    stage(store, tmp_path / "tv", source="tradingview", rows=[])
    _, plan = prepare(tmp_path, monkeypatch, [row("old")], store)
    assert not plan["blocked"]


def test_compare_covers_every_sparse_historical_snapshot_and_missing_old_scope(
    tmp_path, monkeypatch
):
    import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master

    monkeypatch.setattr(master, "MIN_COMMON_STOCK_MASTER_ROWS", 1)
    older = DAY - timedelta(days=30)
    store, _ = prepare(tmp_path, monkeypatch, [row("older", day=older), row("current")])
    owner = MasterL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    records = []
    for day in (older, DAY):
        record = json.loads(gzip.decompress(row("x", day=day)[-1]))["data"][0]
        records.extend(master.validate_master_snapshot([record], day).rows)
    records.append(("2020-01-01", "99990", "{}"))
    projection = tmp_path / "projection.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                dict(zip(("snapshot_date", "security_code", "record_json"), item, strict=True))
                for item in records
            ]
        ),
        projection,
    )
    monkeypatch.setattr(
        cutover, "accepted_dataset", lambda *_: SimpleNamespace(name="jquants.master_snapshots")
    )
    monkeypatch.setattr(cutover, "selected_partitions", lambda *_: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *_, **__: (projection,))
    with lake_session() as session:
        report = cutover.compare_projection(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            owner=owner,
            directory=tmp_path / "plan",
        )
    assert report["native_rows"] == report["matched_rows"] == 2
    assert report["legacy_rows"] == 3
    assert len(report["differences"]) == 1
    assert report["days"]["2020-01-01"]["status"] == "unavailable"
    assert set(report["days"]) == {older.isoformat(), DAY.isoformat(), "2020-01-01"}
