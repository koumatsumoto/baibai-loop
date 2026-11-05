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
from baibai_engine.data_collection.providers.jquants.price_native import (
    PriceAttempt,
    PriceL1,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import price_native_cutover as cutover

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def row(identity, value=1, *, at=AT, query=None, index=0, partial=False):
    body = json.dumps(
        {
            "data": [
                {"Code": "13010", "Date": DAY.isoformat(), "C": value, "Unknown": {"raw": True}}
            ],
            **({"pagination_key": "next"} if partial else {}),
        }
    ).encode()
    return (
        identity,
        index,
        "daily_bars",
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
    attempt = PriceAttempt.from_value(saved)
    assert attempt.pages[0].body == gzip.decompress(old[-1])
    assert attempt.pages[0].received_at == AT
    owner = PriceL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["changed"] == 2
    assert result["failed"] == 1
    cutover.verify_migration(PriceL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
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
    owner = PriceL1(store, tmp_path / "target")
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
    owner = PriceL1(store, tmp_path / "target")
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
        report={"status": "failed", "failed_sources": ["daily_bars"]},
    )
    assert any("no partial request original" in item["reason"] for item in plan["blocked"])


def test_failed_stage_with_preserved_partial_request_keeps_the_known_failure(tmp_path, monkeypatch):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "failed_sources": ["daily_bars"]},
    )
    assert not plan["blocked"]


def test_projection_comparison_reports_unreconstructible_and_changed_legacy_rows(
    tmp_path, monkeypatch
):
    store, _plan = prepare(tmp_path, monkeypatch, [row("one")])
    owner = PriceL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    projection = tmp_path / "projection.parquet"
    from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
        JQUANTS_DAILY_BARS,
    )

    columns = [column.name for column in JQUANTS_DAILY_BARS.columns]
    rows = [
        ["1301", DAY.isoformat(), None, None, None, 99.0, None, None, None, None, None, None, None],
        ["9999", "2020-01-01", None, None, None, 1.0, None, None, None, None, None, None, None],
    ]
    pq.write_table(
        pa.Table.from_pylist([dict(zip(columns, row, strict=True)) for row in rows]), projection
    )
    monkeypatch.setattr(
        cutover, "accepted_dataset", lambda *a: SimpleNamespace(name="jquants.daily_bars")
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


def test_mixed_market_stage_validates_all_ids_without_importing_other_sources(
    tmp_path, monkeypatch
):
    price = row("price")
    master = (*row("master")[:2], "master", *row("master")[3:])
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(master, price),
        report={"status": "failed", "failed_sources": ["margin_interest", "master_incomplete"]},
    )
    assert not plan["blocked"]
    assert {item["request_id"] for item in plan["attempts"]} == {"old", "price"}


@pytest.mark.parametrize("failures", [None, [], ["ValueError"], ["unavailable_originals"]])
def test_market_failure_with_unknown_source_scope_blocks(tmp_path, monkeypatch, failures):
    _store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("old")],
        stage_rows=(row("partial", partial=True),),
        report={"status": "failed", "failed_sources": failures},
    )
    assert any("unknown source scope" in item["reason"] for item in plan["blocked"])


def test_mixed_source_request_is_not_silently_split(tmp_path, monkeypatch):
    mixed = (*row("mixed", index=1)[:2], "master", *row("mixed", index=1)[3:])
    _store, plan = prepare(tmp_path, monkeypatch, [row("mixed"), mixed])
    assert any("mixed source" in item["reason"] for item in plan["blocked"])


def test_missing_declared_nonprice_original_also_blocks(tmp_path, monkeypatch):
    original = cutover.available_handoffs

    def with_missing(*args, **kwargs):
        entries = original(*args, **kwargs)
        entries[0]["request_ids"].append("missing-unidentifiable-source")
        return entries

    monkeypatch.setattr(cutover, "available_handoffs", with_missing)
    _store, plan = prepare(tmp_path, monkeypatch, [row("old")], stage_rows=(row("price"),))
    assert any("missing request originals" in item["reason"] for item in plan["blocked"])


def test_complete_pagination_and_empty_day_keep_exact_native_bytes(tmp_path, monkeypatch):
    first = row("paged", partial=True)
    second = row(
        "paged",
        index=1,
        value=2,
        at=AT + timedelta(seconds=1),
        query={"date": DAY.strftime("%Y%m%d"), "pagination_key": "next"},
    )
    # Preserve whitespace and unknown envelope fields as well as a successful empty response.
    body = b'{ "data": [], "future_field": {"value": null} }'
    empty = (*row("empty", at=AT + timedelta(hours=1))[:-1], gzip.compress(body, mtime=0))
    store, plan = prepare(tmp_path, monkeypatch, [first, second, empty])
    assert not plan["blocked"]
    owner = PriceL1(store, tmp_path / "target")
    owner.initialize()
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    cutover.verify_migration(PriceL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
    attempts = {
        item["request_id"]: PriceAttempt.from_value(
            json.loads((tmp_path / "plan" / item["file"]).read_bytes())
        )
        for item in plan["attempts"]
    }
    assert attempts["empty"].pages[0].body == body
    assert [page.body for page in attempts["paged"].pages] == [
        gzip.decompress(first[-1]),
        gzip.decompress(second[-1]),
    ]


def test_comparison_rejects_different_legacy_release(tmp_path, monkeypatch):
    store, _plan = prepare(tmp_path, monkeypatch, [row("old")])
    owner = PriceL1(store, tmp_path / "target")
    owner.initialize()
    with lake_session() as session, pytest.raises(ValueError, match="exact old release"):
        cutover.compare_projection(
            session,
            release=SimpleNamespace(release_id="other", manifest_sha256="a" * 64),
            cache=object(),
            owner=owner,
            directory=tmp_path / "plan",
        )


def test_equal_confirmation_preserves_identity_without_claiming_raw_relocation(
    tmp_path, monkeypatch
):
    store, plan = prepare(
        tmp_path,
        monkeypatch,
        [row("original"), row("confirmation", at=AT + timedelta(hours=1))],
    )
    owner = PriceL1(store, tmp_path / "target")
    owner.initialize()
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert result["changed"] == 1
    assert result["equal"] == 1
    receipt = owner.fixed().value()["receipts"]["confirmation"]
    assert "pack" not in receipt
    assert receipt["fingerprint"] == next(
        item["sha256"] for item in plan["attempts"] if item["request_id"] == "confirmation"
    )
    assert (tmp_path / "legacy.parquet").is_file()
    cutover.verify_migration(PriceL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
