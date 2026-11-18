"""The self-contained root must fit the replaced root and still prove every native row."""

import base64
import json
import lzma
import sqlite3
from contextlib import closing
from datetime import date

import pytest
from tests.engine.machine_analysis.generation.test_economic_fixed_inputs import (
    ASOF,
    EVALUATED,
    run,
    seed,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation import economic_inputs as inputs
from baibai_engine.machine_analysis.generation import economic_membership as membership


def seed_history(path):
    seed(path)
    run(path, "latest-failed")
    with closing(initialize_database(path)) as connection, connection:
        for i in range(40):
            day = date(2020 + i // 12, i % 12 + 1, 1).isoformat()
            store_document(
                connection,
                SourceDocument(
                    source_id="matsui-margin",
                    request_key=day,
                    fetched_at=EVALUATED,
                    source_url="https://example.invalid/fixture",
                    content_type="text/html",
                    payload=b"<html>unreadable</html>",
                    metadata={"receipt_jst_date": day},
                ),
            )


def legacy(store, path):
    return inputs._retain_economic_input(
        lambda value: inputs._retain(store, value),
        path,
        as_of=ASOF,
        evaluation_time=EVALUATED,
        rules_path=inputs.DEFAULT_RULES_PATH,
    )


def retain(store, path):
    return inputs.retain_economic_input(store, path, as_of=ASOF, evaluation_time=EVALUATED)


def test_single_root_cold_rebuild_keeps_originals_and_storage_budget(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_history(path)
    old, new = MemoryR2Store(), MemoryR2Store()
    baseline, compact = legacy(old, path), retain(new, path)
    assert baseline.descriptor["format"] == "economic-input-v2"
    assert compact.descriptor["format"] == "economic-input-v3"
    assert len(new.values) == len(new.put_keys) == 1
    old_root = old.values[inputs._key(baseline.descriptor["selection_sha256"])].body
    assert len(next(iter(new.values.values())).body) <= len(old_root)
    new.get_keys.clear()
    assert (
        inputs.rebuild_economic_input(new, path, compact).context
        == inputs.rebuild_economic_input(old, path, baseline).context
    )
    assert len(new.get_keys) == 1
    new.put_keys.clear()
    assert retain(new, path) == compact
    assert not new.put_keys

    with sqlite3.connect(path) as database:
        database.execute(
            "UPDATE economic_source_documents SET payload=? WHERE request_key='2021-05-01'",
            (b"altered",),
        )
    with pytest.raises(ValueError, match="original failed integrity"):
        inputs.rebuild_economic_input(new, path, compact)
    with sqlite3.connect(path) as database:
        database.execute("DELETE FROM economic_source_documents WHERE request_key='2020-01-01'")
    with pytest.raises(ValueError, match="original is unavailable"):
        inputs.rebuild_economic_input(new, path, compact)


def test_existing_legacy_selection_is_never_repacked_or_left_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "economic.sqlite"
    seed_history(path)
    store = MemoryR2Store()
    baseline = legacy(store, path)
    saved_keys = set(store.values)
    store.put_keys.clear()
    assert retain(store, path) == baseline
    assert not store.put_keys

    root = json.loads(store.values[inputs._key(baseline.descriptor["selection_sha256"])].body)
    lost = inputs._key(next(iter(root["months"].values())))
    del store.values[lost]
    assert retain(store, path) == baseline
    assert store.put_keys == [lost]
    assert set(store.values) == saved_keys

    monkeypatch.setattr(inputs, "encode_membership", lambda *_: {"large": "x" * 100_000})
    another = MemoryR2Store()
    assert retain(another, path) == baseline
    assert set(another.values) == saved_keys


def test_oversized_or_truncated_compressed_root_is_rejected(monkeypatch):
    monkeypatch.setattr(membership, "_MAX_EXPANDED", 32)
    for stream in (lzma.compress(b"x" * 33), lzma.compress(b"{}")[:-4]):
        payload = json.dumps(
            {"format": "economic-membership-xz-v1", "data": base64.b64encode(stream).decode()}
        ).encode()
        with pytest.raises(ValueError, match="membership envelope"):
            membership.decode_membership(payload)
