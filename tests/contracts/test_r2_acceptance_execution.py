"""実R2受入は専用bucketの逐次実行だけを接続前に認める。"""

import pytest
from tests.integration import test_lake_r2_acceptance as acceptance


def test_parallel_acceptance_is_rejected_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv("R2_LAKE_ACCEPTANCE_BUCKET", raising=False)
    with pytest.raises(pytest.fail.Exception, match="serial execution: add -n 0"):
        acceptance._store()


@pytest.mark.parametrize("bucket", ["baibai-stores", "ordinary-bucket"])
def test_acceptance_rejects_other_buckets_before_creating_a_client(monkeypatch, bucket):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("R2_LAKE_ACCEPTANCE_BUCKET", bucket)
    monkeypatch.setattr(
        acceptance,
        "Boto3R2Store",
        lambda **_: pytest.fail("invalid acceptance bucket reached the client"),
    )
    with pytest.raises(pytest.fail.Exception, match="dedicated non-production bucket"):
        acceptance._store()


def test_acceptance_passes_only_the_explicit_bucket_to_its_client(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("R2_LAKE_ACCEPTANCE_BUCKET", "baibai-lake-acceptance")
    calls = []
    client = object()

    def store(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr(acceptance, "Boto3R2Store", store)
    assert acceptance._store() is client
    assert calls == [{"bucket": "baibai-lake-acceptance"}]
