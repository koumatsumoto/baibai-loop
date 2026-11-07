"""Publish disposable planning seeds without changing retained native receipts."""

import json
import random
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.acquisition.test_economic_planning_readers import (
    CASES,
    DAY,
    WHEN,
    _document,
    _plans,
    _seed,
)

import baibai_jobs.acquisition.source_inputs as inputs
from baibai_engine.data_collection.acquisition.policy import source_policy
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    read_document_metadata,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    export_pmi_input,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.planning_inputs import (
    export_day_input,
    validate_planning_input,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff


def _export(canonical, target, case):
    group, source, _, _ = case
    if group == "economic-spglobal_pmi":
        export_pmi_input(canonical, target, source_ids=[source])
    else:
        export_day_input(canonical, target, group=group)


def _rows(path, table, source):
    with closing(store.open_read_only_connection(path)) as connection:
        return connection.execute(
            f"SELECT * FROM {table} WHERE source_id=? ORDER BY 1,2,3", (source,)
        ).fetchall()


def _publish(remote, canonical, case, identity):
    return inputs.publish_inputs(
        remote,
        market=canonical.parent / "unused",
        economic=canonical,
        identity=identity,
        asof=DAY,
        generations={"economic_etag": identity},
        only_group=case[0],
    )


def _manifest(remote, group, identity):
    return json.loads(
        remote.get_bytes(f"coordination/source-inputs/{group}-{identity}/handoff.json")
    )


@pytest.mark.parametrize("case", CASES)
def test_export_keeps_native_plans_metadata_and_originals(tmp_path, case):
    group, source, _, table = case
    canonical = tmp_path / "canonical.sqlite"
    first = _seed(canonical, source)
    owners = (
        [
            item.source_id
            for item in load_sources().values()
            if source_policy(item).acquisition_group == group.removeprefix("economic-")
        ]
        if group != "economic-spglobal_pmi"
        else [source]
    )
    with closing(store.open_connection(canonical)) as connection, connection:
        for revision in range(4):
            when = WHEN + timedelta(minutes=revision + 1)
            payload = random.Random(revision).randbytes(32_768)
            if group == "economic-spglobal_pmi":
                store_document(
                    connection,
                    replace(_document(source, first.replace(day=1), when), payload=payload),
                )
            else:
                for owner in owners:
                    store_snapshot(
                        connection,
                        source_id=owner,
                        observed_at=first + timedelta(days=8),
                        fetched_at=when,
                        records=[{"original": payload.hex()}],
                    )
        store_snapshot(
            connection,
            source_id="fred-us-unemployment-rate",
            observed_at=DAY,
            fetched_at=WHEN,
            records=[{"outside": "must not leak into this group"}],
        )
    before = canonical.read_bytes()
    target = tmp_path / "planning.sqlite"
    _export(canonical, target, case)
    validate_planning_input(target, group)
    assert _plans(target, source) == _plans(canonical, source)
    assert canonical.read_bytes() == before
    assert target.stat().st_size < len(before) / 2
    for metadata in ("economic_source_runs", *store.CONFIRMATION_TABLES):
        assert _rows(target, metadata, source) == _rows(canonical, metadata, source)
    with closing(store.open_read_only_connection(target)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 0
        )
        if group == "economic-spglobal_pmi":
            with closing(store.open_read_only_connection(canonical)) as original:
                assert list(read_document_metadata(connection, source)) == list(
                    read_document_metadata(original, source)
                )
        else:
            assert (
                connection.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0]
                == 0
            )
            with closing(store.open_read_only_connection(canonical)) as original:
                expected = original.execute(
                    "SELECT DISTINCT observed_at FROM economic_source_records "
                    "WHERE source_id=? ORDER BY observed_at",
                    (source,),
                ).fetchall()
            assert (
                connection.execute(
                    f"SELECT observed_at FROM {table} ORDER BY observed_at"
                ).fetchall()
                == expected
            )
    assert b"must not leak" not in target.read_bytes()


@pytest.mark.parametrize("case", CASES)
def test_new_publication_reuses_planning_blob_and_keeps_old_fixed_input(tmp_path, case):
    group, source, receipt_format, _ = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    before = canonical.read_bytes()
    remote = MemoryR2Store()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    export_receipts(
        canonical,
        legacy / "receipts.sqlite",
        source_ids=[source],
        since=datetime.min.replace(tzinfo=UTC),
    )
    (legacy / "result.json").write_text(
        json.dumps({"group": group, "canonical_generations": {"economic_etag": "legacy"}})
    )
    save_handoff(remote, group + "-legacy", legacy, namespace="source-inputs")
    original = {key: value.body for key, value in remote.values.items()}
    for identity in ("one", "two"):
        assert _publish(remote, canonical, case, identity) == {"updated": [group], "failed": []}
        manifest = _manifest(remote, group, identity)
        assert manifest["receipt_format"] == receipt_format
        assert "receipt_key" not in manifest
        target = tmp_path / f"{identity}.sqlite"
        report = inputs.load_input(remote, group, target, tmp_path / identity)
        assert report["canonical_generations"] == {"economic_etag": identity}
        assert _plans(target, source) == _plans(canonical, source)
    assert (
        _manifest(remote, group, "one")["planning_key"]
        == _manifest(remote, group, "two")["planning_key"]
    )
    assert len([key for key in remote.values if key.endswith(".sqlite")]) == 2
    assert all(remote.values[key].body == body for key, body in original.items())
    load_handoff(
        remote,
        group + "-legacy",
        tmp_path / "fixed-old",
        namespace="source-inputs",
        input_group=group,
    )
    assert (tmp_path / "fixed-old/receipts.sqlite").read_bytes() == (
        legacy / "receipts.sqlite"
    ).read_bytes()
    assert canonical.read_bytes() == before
    assert all(key.startswith("coordination/source-inputs/") for key in remote.put_keys)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("missing", [False, True])
def test_retired_planning_blob_is_recreated_without_touching_originals(tmp_path, case, missing):
    group, source, _, _ = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    before = canonical.read_bytes()
    remote = MemoryR2Store()
    _publish(remote, canonical, case, "one")
    key = _manifest(remote, group, "one")["planning_key"]
    if missing:
        del remote.values[key]
    else:
        remote.values[key].metadata["sha256"] = "bad"
    assert _publish(remote, canonical, case, "two")["failed"] == []
    assert _manifest(remote, group, "two")["planning_key"] != key
    target = tmp_path / "loaded.sqlite"
    inputs.load_input(remote, group, target, tmp_path / "load")
    assert _plans(target, source) == _plans(canonical, source)
    assert canonical.read_bytes() == before


def test_failed_group_retains_pointer_while_other_groups_progress(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    remote = MemoryR2Store()
    for case in CASES:
        _seed(canonical, case[1])
        _publish(remote, canonical, case, "old")
    previous = remote.values[inputs.input_key(CASES[1][0])].body
    monkeypatch.setattr(inputs, "SOURCE_INPUT_GROUPS", ())
    monkeypatch.setattr(
        inputs, "economic_acquisition_groups", lambda: tuple(case[0][9:] for case in CASES)
    )
    actual = inputs.export_day_input

    def fail_umich(source, target, *, group):
        if group == CASES[1][0]:
            raise ValueError("unavailable planning source")
        actual(source, target, group=group)

    monkeypatch.setattr(inputs, "export_day_input", fail_umich)
    args = dict(
        market=tmp_path / "unused",
        economic=canonical,
        asof=DAY,
        generations={"economic_etag": "next"},
    )
    result = inputs.publish_inputs(remote, **args, identity="partial")
    assert result == {
        "updated": [case[0] for case in CASES if case != CASES[1]],
        "failed": [{"group": CASES[1][0], "reason": "ValueError"}],
    }
    assert remote.values[inputs.input_key(CASES[1][0])].body == previous
    monkeypatch.setattr(inputs, "export_day_input", actual)
    assert inputs.publish_inputs(remote, **args, identity="resumed")["failed"] == []
    for number, case in enumerate(CASES):
        target = tmp_path / f"loaded-{number}.sqlite"
        inputs.load_input(remote, case[0], target, tmp_path / f"read-{number}")
        assert _plans(target, case[1]) == _plans(canonical, case[1])


@pytest.mark.parametrize("point", ["blob", "manifest", "pointer"])
def test_interrupted_group_publication_keeps_previous_pointer_and_can_resume(
    tmp_path, monkeypatch, point
):
    case = CASES[2]
    group, source, _, _ = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    remote = MemoryR2Store()
    _publish(remote, canonical, case, "old")
    previous = remote.values[inputs.input_key(group)].body
    with closing(store.open_connection(canonical)) as connection, connection:
        store_snapshot(connection, source_id=source, observed_at=DAY, fetched_at=WHEN, records=[])
    actual = remote.put_file

    def fail(key, *args, **kwargs):
        selected = (
            key.endswith(".sqlite")
            if point == "blob"
            else key.endswith("/handoff.json")
            if point == "manifest"
            else key == inputs.input_key(group)
        )
        if selected:
            raise R2Error("interrupted publication")
        return actual(key, *args, **kwargs)

    monkeypatch.setattr(remote, "put_file", fail)
    with pytest.raises(R2Error, match="interrupted"):
        _publish(remote, canonical, case, "failed")
    assert remote.values[inputs.input_key(group)].body == previous
    monkeypatch.setattr(remote, "put_file", actual)
    assert _publish(remote, canonical, case, "resumed")["failed"] == []
    target = tmp_path / "loaded.sqlite"
    inputs.load_input(remote, group, target, tmp_path / "read")
    assert _plans(target, source) == _plans(canonical, source)


@pytest.mark.parametrize("case", CASES)
def test_generation_mismatch_is_not_reinterpreted_as_current_input(tmp_path, case):
    group, source, _, _ = case
    canonical = tmp_path / "canonical.sqlite"
    _seed(canonical, source)
    remote = MemoryR2Store()
    _publish(remote, canonical, case, "one")
    pointer = json.loads(remote.values[inputs.input_key(group)].body)
    pointer["canonical_generations"] = {"economic_etag": "wrong"}
    remote.store_object(inputs.input_key(group), json.dumps(pointer).encode())
    with pytest.raises(LakePublishError, match="generation binding"):
        inputs.load_input(remote, group, tmp_path / "wrong.sqlite", tmp_path / "read")
    assert not (tmp_path / "wrong.sqlite").exists()


@pytest.mark.parametrize("case", CASES)
def test_export_requires_current_canonical_and_fresh_destination(tmp_path, case):
    canonical = tmp_path / "canonical.sqlite"
    target = tmp_path / "planning.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        _export(canonical, target, case)
    assert not canonical.exists()
    assert not target.exists()
    _seed(canonical, case[1])
    _export(canonical, target, case)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        _export(canonical, target, case)
    assert target.read_bytes() == before
    with closing(store.open_connection(canonical)) as connection, connection:
        connection.execute("PRAGMA user_version=12")
    with pytest.raises(store.EconomicSchemaError, match="expected 13"):
        _export(canonical, tmp_path / "invalid.sqlite", case)
