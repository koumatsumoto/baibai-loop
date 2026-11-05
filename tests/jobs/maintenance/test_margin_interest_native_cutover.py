"""Finite legacy migration keeps real originals and blocks unknown scope/time."""

import gzip
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers.margin_interest_native import records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    MarginInterestAttempt,
    MarginInterestL1,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import margin_interest_native_cutover as cutover

DAY = date(2026, 9, 25)
AT = datetime(2026, 10, 1, tzinfo=UTC)


def row(identity, value=1, *, at=AT, query=None, index=0, partial=False):
    body = json.dumps(
        {
            "data": [{**records(long=value)[0], "Unknown": {"raw": True}}],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return (
        identity,
        index,
        "margin_interest",
        json.dumps(query or {"date": DAY.isoformat()}),
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
    monkeypatch.setattr(cutover, "accepted_dataset", lambda *a: cutover.JQUANTS_SOURCE_PAGES)
    monkeypatch.setattr(cutover, "selected_partitions", lambda *a: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *a, **kw: (source,))
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
    attempt = MarginInterestAttempt.from_value(saved)
    assert attempt.pages[0].body == gzip.decompress(old[-1])
    assert attempt.pages[0].received_at == AT
    owner = MarginInterestL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["changed"] == 2
    assert result["failed"] == 1
    cutover.verify_migration(
        MarginInterestL1(store, tmp_path / "cold"), directory=tmp_path / "plan"
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
    owner = MarginInterestL1(store, tmp_path / "target")
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
    owner = MarginInterestL1(store, tmp_path / "target")
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
        report={"status": "failed", "failed_sources": ["margin_interest"]},
    )
    assert any("no partial request original" in item["reason"] for item in plan["blocked"])


def test_failed_stage_with_preserved_partial_request_keeps_the_known_failure(tmp_path, monkeypatch):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "failed_sources": ["margin_interest"]},
    )
    assert not plan["blocked"]


def test_failed_other_market_source_does_not_block_alerts_migration(tmp_path, monkeypatch):
    _, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("alerts")],
        report={
            "status": "failed",
            "failed_sources": ["margin_alerts", "daily_bars"],
        },
    )
    assert plan["blocked"] == []


def test_mixed_stage_keeps_all_declared_ids_but_migrates_only_alerts(tmp_path, monkeypatch):
    unrelated = list(row("price"))
    unrelated[2] = "daily_bars"
    _, plan = prepare(
        tmp_path, monkeypatch, [row("old")], stage_rows=(row("new"), tuple(unrelated))
    )
    assert plan["blocked"] == []
    assert {item["request_id"] for item in plan["attempts"]} == {"old", "new"}


def test_prepared_future_stage_does_not_write_native_target(tmp_path, monkeypatch):
    store, plan = prepare(
        tmp_path, monkeypatch, [row("old")], stage_rows=(row("future", at=AT + timedelta(days=3)),)
    )
    assert any("later than" in item["reason"] for item in plan["blocked"])
    owner = MarginInterestL1(store, tmp_path / "target")
    owner.initialize()
    before = dict(store.values)
    with pytest.raises(ValueError, match="unresolved"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert store.values == before


@pytest.mark.parametrize(("empty", "blocked"), [(True, False), (False, True)])
def test_daily_incomplete_requires_preserved_empty_or_partial_original(
    tmp_path, monkeypatch, empty, blocked
):
    item = list(row("daily"))
    if empty:
        item[-1] = gzip.compress(b'{"data":[]}', mtime=0)
    _, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(tuple(item),),
        report={"status": "failed", "failed_sources": ["daily_margin_incomplete"]},
    )
    assert bool(plan["blocked"]) is blocked


def test_compare_two_fixed_l2_datasets_and_calendar_boundary(tmp_path, monkeypatch):
    from tests.helpers.margin_interest_native import calendar

    from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
        JQUANTS_ALL_ISSUES_DAILY_MARGIN,
        JQUANTS_WEEKLY_MARGIN,
    )
    from baibai_jobs.integration.margin_interest_native import fixed_input, materialize_saved_input

    week = date(2026, 9, 18)
    weekly = list(row("weekly"))
    weekly[3] = json.dumps({"date": week.isoformat()})
    weekly[-1] = gzip.compress(json.dumps({"data": records(week)}).encode(), mtime=0)
    store, _ = prepare(tmp_path, monkeypatch, [row("daily"), tuple(weekly)])
    owner = MarginInterestL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    cal = calendar(store, tmp_path / "calendar", week, DAY)
    saved = fixed_input(store, owner.fixed(), cal, start=week, end=DAY, evaluated_at=AT)
    db = tmp_path / "projection.sqlite"
    materialize_saved_input(store, saved, output=db)
    paths = {}
    with sqlite3.connect(db) as conn:
        for contract in (JQUANTS_WEEKLY_MARGIN, JQUANTS_ALL_ISSUES_DAILY_MARGIN):
            actual = conn.execute("SELECT * FROM " + contract.sqlite_table).fetchall()
            path = tmp_path / (contract.name + ".parquet")
            columns = [c.name for c in contract.columns]
            pq.write_table(
                pa.Table.from_pylist(
                    [dict(zip(columns, r, strict=True)) for r in actual],
                    schema=pa.schema([(c.name, c.arrow_type) for c in contract.columns]),
                ),
                path,
            )
            paths[contract.name] = path
    monkeypatch.setattr(cutover, "accepted_dataset", lambda _, c: c)
    monkeypatch.setattr(
        cutover, "materialize_partitions", lambda *_, dataset, **__: (paths[dataset.name],)
    )
    release = SimpleNamespace(
        release_id="fixed", manifest_sha256="a" * 64, manifest=SimpleNamespace(datasets=paths)
    )
    with lake_session() as session:
        result = cutover.compare_projection(
            session,
            release=release,
            cache=object(),
            owner=owner,
            calendar=cal,
            directory=tmp_path / "plan",
            as_of=DAY,
        )
    assert result["legacy_rows"] == result["native_rows"] == result["matched_rows"] == 2
    assert result["differences"] == []
    assert result["coverage_status"] == "complete"
    assert result["weekly_rows"] == result["daily_rows"] == 1
    # Migration boundary, not a later compare invocation, fixes permissible evidence.
    future = calendar(MemoryR2Store(), tmp_path / "future", week, DAY, at=AT + timedelta(days=3))
    with lake_session() as session, pytest.raises(ValueError, match="clock"):
        cutover.compare_projection(
            session,
            release=release,
            cache=object(),
            owner=owner,
            calendar=future,
            directory=tmp_path / "plan",
            as_of=DAY,
        )


def test_compare_matching_empty_final_week_is_not_cutover_complete(tmp_path, monkeypatch):
    from tests.helpers.margin_interest_native import calendar

    final = date(2026, 9, 18)
    empty = list(row("final-empty", query={"date": final.isoformat()}))
    empty[-1] = gzip.compress(b'{"data":[]}', mtime=0)
    store, _ = prepare(tmp_path, monkeypatch, [tuple(empty)])
    owner = MarginInterestL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    cal = calendar(store, tmp_path / "calendar", final, AT.date())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *_, **__: ())
    release = SimpleNamespace(
        release_id="fixed",
        manifest_sha256="a" * 64,
        manifest=SimpleNamespace(datasets={cutover.JQUANTS_WEEKLY_MARGIN.name: object()}),
    )
    with lake_session() as session:
        result = cutover.compare_projection(
            session,
            release=release,
            cache=object(),
            owner=owner,
            calendar=cal,
            directory=tmp_path / "plan",
            as_of=final,
        )
    assert result["differences"] == []
    assert result["coverage_status"] == "partial"
    assert result["unresolved_days"] == [final.isoformat()]
