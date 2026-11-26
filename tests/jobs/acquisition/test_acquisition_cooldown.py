from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.cooldown import extend, quota_key, read_deadline


def test_quota_wait_is_shared_across_work_ids_but_not_other_providers():
    store = MemoryR2Store()
    now = datetime(2026, 11, 1, 9, tzinfo=UTC)
    key = quota_key("minkabu")
    extend(store, key, now + timedelta(hours=5))
    assert read_deadline(store, quota_key("minkabu"))[0] == now + timedelta(hours=5)
    writes = list(store.put_keys)
    extend(store, key, now + timedelta(hours=1))
    assert store.put_keys == writes
    assert read_deadline(store, quota_key("monex")) == (None, None)


@pytest.mark.parametrize("quota", ["", "../minkabu", "Minkabu", "a/b", "x" * 65])
def test_quota_identity_does_not_create_arbitrary_object_paths(quota):
    with pytest.raises(ValueError, match="invalid acquisition quota"):
        quota_key(quota)
