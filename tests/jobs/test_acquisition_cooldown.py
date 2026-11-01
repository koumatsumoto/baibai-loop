from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_jobs.acquisition.articles import cooldown_key
from baibai_jobs.acquisition.cooldown import extend, quota_key, read_deadline


def test_quota_wait_is_shared_across_work_ids_but_not_other_providers():
    store = MemoryR2Store()
    now = datetime(2026, 11, 1, 9, tzinfo=UTC)
    key = quota_key("minkabu")
    extend(store, key, now + timedelta(hours=5))
    assert cooldown_key("minkabu_news") == key
    assert read_deadline(store, cooldown_key("minkabu_news"))[0] == now + timedelta(hours=5)
    writes = list(store.put_keys)
    extend(store, key, now + timedelta(hours=1))
    assert store.put_keys == writes
    assert read_deadline(store, quota_key("monex")) == (None, None)
    assert cooldown_key("rakuten_news") == cooldown_key("toushiru") == quota_key("rakuten")
    assert cooldown_key("tsr_data") == cooldown_key("tsr_flash")
    assert cooldown_key("mof_news") != cooldown_key("boj_news")


@pytest.mark.parametrize("quota", ["", "../minkabu", "Minkabu", "a/b", "x" * 65])
def test_quota_identity_does_not_create_arbitrary_object_paths(quota):
    with pytest.raises(ValueError):
        quota_key(quota)
