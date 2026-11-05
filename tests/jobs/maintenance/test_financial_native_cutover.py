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

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.integration.financial_native import model_input
from baibai_jobs.maintenance import financial_native_cutover as cutover

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def row(identity, value=1, *, at=AT, query=None, index=0, partial=False):
    body = json.dumps(
        {
            "data": [{"Code": "13010", "DiscDate": DAY.isoformat(), "DiscNo": "1", "Sales": value}],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return (
        identity,
        index,
        "financial_summaries",
        json.dumps(query or {"date": DAY.strftime("%Y%m%d")}),
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
                "source": "jquants-financial",
                "asof": DAY.isoformat(),
                "started_at": AT.isoformat(),
                "request_ids": [item[0] for item in stage_rows],
                "request_ids_complete": True,
                **({"acquisition": report} if report else {}),
            },
        )
        save_handoff(store, "jquants-financial-pending", packet, market=True)
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
    attempt = FinancialAttempt.from_value(saved)
    assert attempt.pages[0].body == gzip.decompress(old[-1])
    assert attempt.pages[0].received_at == AT
    owner = FinancialL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["changed"] == 2
    assert result["failed"] == 1
    cutover.verify_migration(FinancialL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
    fixed = owner.fixed()
    assert fixed.value()["scopes"][DAY.isoformat()]["failure"] == "partial"
    observed = model_input(store, fixed, as_of=DAY, evaluated_at=AT + timedelta(days=3))
    assert observed.identity.descriptor["summary"]["incomplete_days"] == 1
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
    owner = FinancialL1(store, tmp_path / "target")
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
    owner = FinancialL1(store, tmp_path / "target")
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
        report={"status": "failed", "planned_days": 2},
    )
    assert any(
        "no request original or known request scope" in item["reason"] for item in plan["blocked"]
    )


def test_failed_stage_with_preserved_partial_request_keeps_the_known_failure(tmp_path, monkeypatch):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "planned_days": 1},
    )
    assert not plan["blocked"]


def test_projection_comparison_reports_unreconstructible_and_changed_legacy_rows(
    tmp_path, monkeypatch
):
    store, _plan = prepare(tmp_path, monkeypatch, [row("one")])
    owner = FinancialL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    projection = tmp_path / "projection.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "ticker": "1301",
                    "disclosed_at": DAY.isoformat(),
                    "disclosure_number": "1",
                    "document_payload": '{"Sales":99}',
                },
                {
                    "ticker": "9999",
                    "disclosed_at": "2020-01-01",
                    "disclosure_number": "lost-original",
                    "document_payload": "{}",
                },
            ]
        ),
        projection,
    )
    monkeypatch.setattr(
        cutover, "accepted_dataset", lambda *a: SimpleNamespace(name="jquants.fin_summaries")
    )
    monkeypatch.setattr(cutover, "selected_partitions", lambda *a: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *a, **kw: (projection,))
    with lake_session() as session:
        report = cutover.compare_projection(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            owner=owner,
            directory=tmp_path / "plan",
        )
    assert report["legacy_rows"] == 2
    assert report["native_rows"] == 1
    assert len(report["differences"]) == 2
    assert report["days"]["2020-01-01"]["status"] == "unavailable"
    assert report["differences"][1]["native_sha256"] is None


def test_unknown_legacy_planning_failure_is_not_explained_by_an_unrelated_partial(
    tmp_path, monkeypatch
):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "planning_unknown_count": 1},
    )
    assert any(
        "unresolved acquisition planning scope" in item["reason"] for item in plan["blocked"]
    )
