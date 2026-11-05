"""Finite legacy migration keeps real originals and blocks unknown scope/time."""

import gzip
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import records

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsAttempt,
    ShortSaleReportsL1,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import short_sale_reports_native_cutover as cutover

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def row(identity, value=1, *, at=AT, query=None, index=0, partial=False):
    body = json.dumps(
        {
            "data": [{**records(ratio=value)[0], "Unknown": {"raw": True}}],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return (
        identity,
        index,
        "short_sale_reports",
        json.dumps(query or {"disc_date": DAY.isoformat()}),
        at.isoformat(),
        gzip.compress(body, mtime=0),
    )


def prepare(tmp_path, monkeypatch, rows, *, stage_rows=(), entries=None, report=None):
    source = tmp_path / "legacy.parquet"
    names = ("request_id", "page_index", "source", "query_json", "received_at_utc", "body_gzip")
    pq.write_table(
        pa.Table.from_arrays([pa.array(column) for column in zip(*rows, strict=True)], names=names),
        source,
    )
    monkeypatch.setattr(cutover, "fixed_receipt_paths", lambda **_: (source,))
    store = MemoryR2Store()
    if stage_rows or report is not None:
        packet = tmp_path / "packet"
        packet.mkdir()
        with sqlite3.connect(packet / "receipts.sqlite") as conn:
            conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
            for item in stage_rows:
                conn.execute(
                    "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*item[:5], item[4][:10], item[5]),
                )
        write_json_atomic(
            packet / "result.json",
            {
                "source": "jquants-market",
                "asof": DAY.isoformat(),
                "started_at": AT.isoformat(),
                "request_ids": [item[0] for item in stage_rows],
                "request_ids_complete": True,
                **({"acquisition": report} if report else {}),
            },
        )
        save_handoff(store, "jquants-market-pending", packet, market=True)
    if entries is not None:
        monkeypatch.setattr(cutover, "available_handoffs", lambda *_, **__: entries)
    before = len(store.put_keys)
    with lake_session() as session:
        plan = cutover.prepare_migration(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            store=store,
            known_at=AT + timedelta(days=2),
            directory=tmp_path / "plan",
        )
    assert len(store.put_keys) == before
    return store, plan


def test_old_l1_and_pending_stage_keep_bytes_times_partial_and_idempotency(tmp_path, monkeypatch):
    old = row("a", value=1)
    pending = row("b", value=2, at=AT + timedelta(hours=1))
    partial = row("partial", at=AT + timedelta(hours=2), partial=True)
    store, plan = prepare(tmp_path, monkeypatch, [old], stage_rows=(old, pending, partial))
    assert not plan["blocked"]
    assert len(plan["attempts"]) == 3
    assert len(plan["attempts"][0]["origins"]) == 2
    saved = json.loads((tmp_path / "plan" / plan["attempts"][0]["file"]).read_bytes())
    assert saved["attempted_at"] is None
    attempt = ShortSaleReportsAttempt.from_value(saved)
    assert attempt.pages[0].body == gzip.decompress(old[-1])
    assert attempt.pages[0].received_at == AT
    owner = ShortSaleReportsL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["changed"] == 2
    assert result["failed"] == 1
    cutover.verify_migration(
        ShortSaleReportsL1(store, tmp_path / "cold"), directory=tmp_path / "plan"
    )
    fixed = owner.fixed()
    assert fixed.value()["scopes"][DAY.isoformat()]["failure"] == "partial"
    assert cutover.apply_migration(owner, directory=tmp_path / "plan")["retried"] == 3


@pytest.mark.parametrize(
    "bad",
    [
        row("code", query={"code": "13010"}),
        row("gap", index=1),
        row("future", at=AT + timedelta(days=4)),
    ],
)
def test_unknown_scope_page_position_or_boundary_blocks_remote_apply(tmp_path, monkeypatch, bad):
    store, plan = prepare(tmp_path, monkeypatch, [bad])
    assert plan["blocked"]
    owner = ShortSaleReportsL1(store, tmp_path / "target")
    owner.initialize()
    before = len(store.put_keys)
    with pytest.raises(ValueError, match="unresolved"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert len(store.put_keys) == before


def test_conflicting_legacy_identity_blocks_instead_of_rewriting_it(tmp_path, monkeypatch):
    _store, plan = prepare(tmp_path, monkeypatch, [row("same")], stage_rows=(row("same", value=2),))
    assert plan["blocked"][0]["reason"] == "legacy request identity has different pages"


def test_missing_handoff_and_changed_prepared_file_are_not_silent_success(tmp_path, monkeypatch):
    store, plan = prepare(
        tmp_path, monkeypatch, [row("one")], entries=[{"stage_id": "bad", "invalid": "ValueError"}]
    )
    assert plan["blocked"] == [{"origin": "stage:bad", "reason": "invalid fixed handoff"}]
    plan["blocked"] = []
    write_json_atomic(tmp_path / "plan" / "plan.json", plan)
    (tmp_path / "plan" / plan["attempts"][0]["file"]).write_bytes(b"changed")
    owner = ShortSaleReportsL1(store, tmp_path / "target")
    owner.initialize()
    with pytest.raises(ValueError, match="original changed"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")


@pytest.mark.parametrize("stage_rows", [(), (row("success"),)])
def test_failed_stage_without_failed_request_original_is_a_blocker(
    tmp_path, monkeypatch, stage_rows
):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=stage_rows,
        report={"status": "failed", "failed_sources": ["short_sale_reports"]},
    )
    assert any("no partial request original" in item["reason"] for item in plan["blocked"])


def test_failed_stage_with_preserved_partial_request_keeps_the_known_failure(tmp_path, monkeypatch):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "failed_sources": ["short_sale_reports"]},
    )
    assert not plan["blocked"]


def test_failed_other_market_source_does_not_block_reports_migration(tmp_path, monkeypatch):
    _, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("alerts")],
        report={
            "status": "failed",
            "failed_sources": ["margin_interest", "daily_bars"],
        },
    )
    assert plan["blocked"] == []


def test_mixed_stage_keeps_all_declared_ids_but_migrates_only_reports(tmp_path, monkeypatch):
    unrelated = list(row("price"))
    unrelated[2] = "daily_bars"
    _, plan = prepare(
        tmp_path, monkeypatch, [row("old")], stage_rows=(row("new"), tuple(unrelated))
    )
    assert plan["blocked"] == []
    assert {item["request_id"] for item in plan["attempts"]} == {"old", "new"}


def test_compare_reports_ordinal_changes_and_historical_gaps(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
        JQUANTS_SHORT_SALE_REPORTS,
    )
    from baibai_jobs.integration.short_sale_reports_native import (
        fixed_input,
        materialize_saved_input,
    )

    store, _ = prepare(tmp_path, monkeypatch, [row("one")])
    owner = ShortSaleReportsL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    saved = fixed_input(store, owner.fixed(), start=DAY, end=DAY, evaluated_at=AT)
    database = tmp_path / "projection.sqlite"
    materialize_saved_input(store, saved, output=database)
    with sqlite3.connect(database) as connection:
        actual = connection.execute("SELECT * FROM jquants_short_sale_reports").fetchone()
    changed = [*actual[:7], 999.0, *actual[8:]]
    unbacked = ["2020-01-01", 0, "2019-12-30", *actual[3:]]
    projection = tmp_path / "projection.parquet"
    columns = [column.name for column in JQUANTS_SHORT_SALE_REPORTS.columns]
    pq.write_table(
        pa.Table.from_pylist(
            [dict(zip(columns, item, strict=True)) for item in [changed, unbacked]],
            schema=pa.schema(
                [(column.name, column.arrow_type) for column in JQUANTS_SHORT_SALE_REPORTS.columns]
            ),
        ),
        projection,
    )
    monkeypatch.setattr(
        cutover,
        "accepted_dataset",
        lambda *_: SimpleNamespace(name=JQUANTS_SHORT_SALE_REPORTS.name),
    )
    monkeypatch.setattr(cutover, "selected_partitions", lambda *_: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *_, **__: (projection,))
    with lake_session() as session:
        result = cutover.compare_projection(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            owner=owner,
            directory=tmp_path / "plan",
            as_of=DAY,
        )
    assert result["legacy_rows"] == 2
    assert result["native_rows"] == 1
    assert len(result["differences"]) == 2
    assert result["coverage_status"] == "partial"
    assert result["unavailable_days"] > 2000
    assert all(len(item["key"]) == 2 for item in result["differences"])


def test_prepared_future_stage_does_not_write_native_target(tmp_path, monkeypatch):
    store, plan = prepare(
        tmp_path, monkeypatch, [row("old")], stage_rows=(row("future", at=AT + timedelta(days=3)),)
    )
    assert any("later than" in item["reason"] for item in plan["blocked"])
    owner = ShortSaleReportsL1(store, tmp_path / "target")
    owner.initialize()
    before = dict(store.values)
    with pytest.raises(ValueError, match="unresolved"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert store.values == before
