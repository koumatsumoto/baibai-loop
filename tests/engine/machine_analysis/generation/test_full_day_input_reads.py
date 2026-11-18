"""Only confirmed immutable monthly input objects can be reused in one daily pass."""

from collections import Counter
from datetime import date

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation.calendar_inputs import (
    read_calendar_input,
    retain_calendar_input,
)
from baibai_engine.machine_analysis.generation.full_day_inputs import share_full_day_input_reads
from baibai_engine.storage.objects.r2 import LakePublishError

DAY = date(2026, 11, 17)
SCOPE = {"adopted": "receipt"}


def save(store):
    return retain_calendar_input(
        store,
        {
            "as_of": DAY.isoformat(),
            "head_sha256": "0" * 64,
            "summary": {},
            "scopes": {DAY.isoformat(): SCOPE},
            "receipts": {"receipt": {"pack": "1" * 64}},
        },
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
