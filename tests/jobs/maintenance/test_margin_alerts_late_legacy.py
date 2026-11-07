"""Finite late legacy merge preserves an already accepted native baseline."""

import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers.margin_alerts_native import records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    HEAD_KEY,
    PREFIX,
    MarginAlertsL1,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.maintenance import margin_alerts_native_cutover as cutover

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def row(identity, *, day=DAY, at=AT, value=1, partial=False):
    body = json.dumps(
        {"data": records(day, long=value), **({"pagination_key": "next"} if partial else {})}
    ).encode()
    return (
        identity,
        0,
        "margin_alerts",
        json.dumps({"date": day.isoformat()}),
        at.isoformat(),
        gzip.compress(body, mtime=0),
    )


def attempt(item):
    return cutover._attempt(item[0], [item], AT + timedelta(days=20))


def prepare(tmp_path, monkeypatch, store, rows, baseline):
    source = tmp_path / "legacy.parquet"
    names = ("request_id", "page_index", "source", "query_json", "received_at_utc", "body_gzip")
    pq.write_table(
        pa.Table.from_arrays([pa.array(x) for x in zip(*rows, strict=True)], names=names), source
    )
    monkeypatch.setattr(cutover, "accepted_dataset", lambda *a: cutover.JQUANTS_SOURCE_PAGES)
    monkeypatch.setattr(cutover, "selected_partitions", lambda *a: ())
    monkeypatch.setattr(cutover, "materialize_partitions", lambda *a, **kw: (source,))
    before = len(store.put_keys)
    with lake_session() as session:
        plan = cutover.prepare_migration(
            session,
            release=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64),
            cache=object(),
            store=store,
            known_at=AT + timedelta(days=10),
            directory=tmp_path / "plan",
            baseline=baseline,
        )
    assert not plan["blocked"]
    assert len(store.put_keys) == before
    if baseline:
        assert plan["baseline_head_sha256"] == baseline.content_id
        assert (tmp_path / "plan" / "baseline-head.json").read_bytes() == baseline.payload
    return plan


def owner_with_baseline(tmp_path, *rows):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt(item) for item in rows])
    return store, owner, owner.fixed()


def test_late_older_original_is_retained_without_replacing_new_native_adoption(
    tmp_path, monkeypatch
):
    old = row("old", value=1)
    recovery = row("native-recovery", value=3, at=AT + timedelta(hours=2))
    other = row("other-day", day=DAY - timedelta(days=1), value=8)
    store, owner, baseline = owner_with_baseline(tmp_path, old, recovery, other)
    baseline_objects = {k: v.body for k, v in store.values.items() if k != HEAD_KEY}
    prepare(
        tmp_path,
        monkeypatch,
        store,
        [old, row("late-legacy", value=2, at=AT + timedelta(hours=1))],
        baseline,
    )
    store.get_keys.clear()
    report = cutover.apply_migration(owner, directory=tmp_path / "plan")
    head = owner.fixed().value()
    assert report == {
        "changed": 0,
        "equal": 0,
        "failed": 0,
        "held": 1,
        "retried": 1,
        "baseline_receipts": 3,
    }
    assert all(head["receipts"][key] == item for key, item in baseline.value()["receipts"].items())
    assert head["scopes"] == baseline.value()["scopes"]
    assert head["scopes"][DAY.isoformat()]["adopted"] == "native-recovery"
    assert "pack" in head["receipts"]["late-legacy"]
    assert not set(store.get_keys).intersection(baseline_objects)
    assert all(store.values[key].body == body for key, body in baseline_objects.items())
    before = dict(store.values), list(store.put_keys)
    repeated = cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert repeated["retried"] == 2
    assert (store.values, store.put_keys) == before


def test_newer_partial_is_preserved_as_failure_not_empty_success(tmp_path, monkeypatch):
    old = row("old")
    store, owner, baseline = owner_with_baseline(tmp_path, old)
    prepare(
        tmp_path,
        monkeypatch,
        store,
        [old, row("partial", partial=True, at=AT + timedelta(hours=2))],
        baseline,
    )
    result = cutover.apply_migration(owner, directory=tmp_path / "plan")
    scope = owner.fixed().value()["scopes"][DAY.isoformat()]
    assert result["failed"] == 1
    assert scope["adopted"] == "old"
    assert scope["failure"] == "partial"


def test_overlap_conflict_is_rejected_before_any_new_receipt_put(tmp_path, monkeypatch):
    old = row("old")
    store, owner, baseline = owner_with_baseline(tmp_path, old)
    prepare(
        tmp_path,
        monkeypatch,
        store,
        [row("new-before-conflict", at=AT - timedelta(hours=1)), row("old", value=999)],
        baseline,
    )
    before = dict(store.values), list(store.put_keys)
    with pytest.raises(ValueError, match="conflicts"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert (store.values, store.put_keys) == before


@pytest.mark.parametrize(
    "change", ["extra", "baseline-entry", "other-scope", "sidecar", "head-missing"]
)
def test_changed_baseline_or_unplanned_target_stops_without_write(tmp_path, monkeypatch, change):
    old = row("old", day=DAY - timedelta(days=1))
    store, owner, baseline = owner_with_baseline(tmp_path, old)
    prepare(tmp_path, monkeypatch, store, [row("late")], baseline)
    if change == "extra":
        owner.commit([attempt(row("unplanned", at=AT + timedelta(hours=1)))])
    elif change in {"baseline-entry", "other-scope"}:
        head = baseline.value()
        if change == "baseline-entry":
            head["receipts"]["old"]["fingerprint"] = "b" * 64
        else:
            head["scopes"][(DAY - timedelta(days=1)).isoformat()]["held"] = "old"
        store.store_object(HEAD_KEY, cutover._encoded(head))
    elif change == "sidecar":
        (tmp_path / "plan" / "baseline-head.json").write_text("{}")
    else:
        del store.values[HEAD_KEY]
    before = dict(store.values), list(store.put_keys)
    with pytest.raises((ValueError, LakePublishError)):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert (store.values, store.put_keys) == before


def test_existing_unused_target_contract_remains_closed(tmp_path, monkeypatch):
    store, owner, _ = owner_with_baseline(tmp_path, row("native-only"))
    prepare(tmp_path, monkeypatch, store, [row("legacy")], None)
    before = dict(store.values), list(store.put_keys)
    with pytest.raises(ValueError, match="outside this migration"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert (store.values, store.put_keys) == before


@pytest.mark.parametrize("head_arrived", [False, True])
def test_partial_put_and_unknown_head_response_resume_from_same_plan(
    tmp_path, monkeypatch, head_arrived
):
    store, owner, baseline = owner_with_baseline(tmp_path, row("old"))
    late = row("late", value=2, at=AT + timedelta(hours=1))
    prepare(tmp_path, monkeypatch, store, [late], baseline)
    original_put = store.put_file
    original_get = store.get_bytes
    broken = False

    def put(key, path, **kwargs):
        nonlocal broken
        if key == HEAD_KEY:
            if head_arrived:
                original_put(key, path, **kwargs)
            broken = True
            raise LakePublishError("lost acknowledgement")
        return original_put(key, path, **kwargs)

    def get(key):
        if broken and key == HEAD_KEY:
            raise LakePublishError("readback unavailable")
        return original_get(key)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "get_bytes", get)
    with pytest.raises(LakePublishError):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    packs_before = {k: v.body for k, v in store.values.items() if k.startswith(PREFIX + "packs/")}
    monkeypatch.setattr(store, "put_file", original_put)
    monkeypatch.setattr(store, "get_bytes", original_get)
    recovered = cutover.apply_migration(
        MarginAlertsL1(store, tmp_path / "fresh"), directory=tmp_path / "plan"
    )
    assert recovered["retried" if head_arrived else "changed"] == 1
    assert all(store.values[key].body == value for key, value in packs_before.items())
    head = owner.fixed().value()
    assert all(
        head["receipts"][key] == value for key, value in baseline.value()["receipts"].items()
    )
    assert (
        head["receipts"]["late"]["fingerprint"]
        == hashlib.sha256(cutover._encoded(attempt(late).value())).hexdigest()
    )
    before = dict(store.values), list(store.put_keys)
    cutover.apply_migration(owner, directory=tmp_path / "plan")
    assert (store.values, store.put_keys) == before
