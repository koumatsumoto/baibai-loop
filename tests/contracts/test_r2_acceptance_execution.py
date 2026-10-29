"""実R2受入は同じbucketを共有するため、接続前に並列実行を拒否する。"""

import pytest

from tests.integration.test_lake_r2_acceptance import _store


def test_parallel_acceptance_is_rejected_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv("R2_LAKE_ACCEPTANCE_BUCKET", raising=False)
    with pytest.raises(pytest.fail.Exception, match="serial execution: add -n 0"):
        _store()
