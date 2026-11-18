"""Only confirmed immutable monthly input objects can be reused in one daily pass."""

from collections import Counter
from datetime import date
from io import BytesIO

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation import full_day_inputs
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    read_calendar_input,
    retain_calendar_input,
)
from baibai_engine.machine_analysis.generation.full_day_inputs import share_full_day_input_reads
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError

DAY = date(2026, 11, 17)
SCOPE = {"adopted": "receipt"}


def save(store, *, immediate_full_replay=False):
    return retain_calendar_input(
        store,
        {
            "as_of": DAY.isoformat(),
            "head_sha256": "0" * 64,
            "summary": {},
            "scopes": {DAY.isoformat(): SCOPE},
            "receipts": {"receipt": {"pack": "1" * 64}},
        },
        immediate_full_replay=immediate_full_replay,
    )


def test_verified_reads_are_private_to_one_pass_and_store():
    store = MemoryR2Store()
    saved = save(store)
    keys = set(store.values)
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()
    with share_full_day_input_reads(store):
        assert save(store) == save(store) == saved
        for _ in range(2):
            current = read_calendar_input(store, saved, days=[DAY])
            assert current["scopes"][DAY.isoformat()] == SCOPE
            current["scopes"][DAY.isoformat()].clear()
        with pytest.raises(LakePublishError, match="unavailable"):
            read_calendar_input(MemoryR2Store(), saved, days=[DAY])
    assert Counter(store.head_keys) == Counter(store.get_keys) == dict.fromkeys(keys, 1)
    assert store.put_keys == []
    month = next(key for key in keys if "/months/" in key)
    store.values[month].body = b"wrong"
    with (
        share_full_day_input_reads(store),
        pytest.raises(LakePublishError, match="failed integrity"),
    ):
        read_calendar_input(store, saved, days=[DAY])


def test_retain_metadata_is_not_a_verified_body():
    store = MemoryR2Store()
    saved = save(store)
    with share_full_day_input_reads(store):
        assert save(store) == saved
        month = next(key for key in store.values if "/months/" in key)
        store.values[month].body = b"wrong"
        with pytest.raises(LakePublishError, match="failed integrity"):
            read_calendar_input(store, saved, days=[DAY])


def test_new_put_requires_actual_readback_despite_an_earlier_verified_read(monkeypatch):
    store = MemoryR2Store()
    saved = save(store)
    month = next(key for key in store.values if "/months/" in key)
    original_put = store.put_file

    def corrupt(*args, **kwargs):
        result = original_put(*args, **kwargs)
        store.values[month].body = b"wrong"
        return result

    with share_full_day_input_reads(store):
        assert read_calendar_input(store, saved, days=[DAY])["scopes"][DAY.isoformat()] == SCOPE
        del store.values[month]
        monkeypatch.setattr(store, "put_file", corrupt)
        with pytest.raises(LakePublishError, match="failed integrity"):
            save(store)


def _warm_reader(memory, monkeypatch):
    """Serve pre-existing objects to the actual bounded R2 reader, without credentials or I/O."""
    reader = Boto3R2Store(
        bucket="fixture",
        env={"R2_ACCOUNT_ID": "a" * 32, "R2_ACCESS_KEY_ID": "test", "R2_SECRET_ACCESS_KEY": "test"},
    )
    requests = []

    class Client:
        def _headers(self, key):
            item = memory.values[key]
            return {"ContentLength": len(item.body), "ETag": item.etag, "Metadata": item.metadata}

        def head_object(self, *, Bucket, Key):
            requests.append(("HEAD", Key))
            return self._headers(Key)

        def get_object(self, *, Bucket, Key):
            requests.append(("GET", Key))
            return {**self._headers(Key), "Body": BytesIO(memory.values[Key].body)}

    monkeypatch.setattr(reader, "_client", lambda *_: Client())
    return reader, requests


def test_immediate_replay_reads_every_warm_block_once_and_keeps_cold_reader_independent(
    monkeypatch,
):
    memory = MemoryR2Store()
    saved = save(memory)
    store, requests = _warm_reader(memory, monkeypatch)
    keys = set(memory.values)
    with share_full_day_input_reads(store):
        assert save(store, immediate_full_replay=True) == saved
        assert read_calendar_input(store, saved, days=[DAY])["scopes"] == {DAY.isoformat(): SCOPE}
    assert Counter(requests) == Counter(("GET", key) for key in keys)
    requests.clear()
    assert read_calendar_input(store, saved, days=[DAY])["scopes"] == {DAY.isoformat(): SCOPE}
    assert Counter(requests) == Counter(("GET", key) for key in keys)


def test_only_immediate_replay_with_space_reads_bodies_early(monkeypatch):
    memory = MemoryR2Store()
    saved = save(memory)
    month, root = sorted(memory.values)
    store, requests = _warm_reader(memory, monkeypatch)
    with share_full_day_input_reads(store):
        assert save(store) == saved
    assert requests == [("HEAD", month), ("HEAD", root)]
    requests.clear()
    monkeypatch.setattr(full_day_inputs, "_MAX_KEYS", 1)
    with share_full_day_input_reads(store):
        assert save(store, immediate_full_replay=True) == saved
        assert requests == [("GET", month), ("HEAD", root)]
        assert read_calendar_input(store, saved, days=[DAY])["scopes"] == {DAY.isoformat(): SCOPE}
    assert requests == [("GET", month), ("HEAD", root), ("GET", root)]


def test_same_response_with_intact_metadata_cannot_pin_different_bytes(monkeypatch):
    memory = MemoryR2Store()
    save(memory)
    month = min(memory.values)
    stored = memory.values[month]
    stored.body = b"x" * len(stored.body)
    reader, requests = _warm_reader(memory, monkeypatch)
    with share_full_day_input_reads(reader), pytest.raises(LakePublishError, match="changed"):
        save(reader, immediate_full_replay=True)
    assert requests == [("GET", month)]  # No weaker HEAD check or overwrite after a rejected read.
