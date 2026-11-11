"""The maintenance alias must preserve the native owner's existing recovery input."""

import gzip
import json
from argparse import Namespace
from datetime import UTC, date, datetime

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import (
    HEAD_KEY,
    PREFIX,
    MasterL1,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition import jquants_master_native
from baibai_jobs.maintenance import master_native_backfill as backfill

DAY = date(2026, 11, 5)


@pytest.mark.parametrize("fault", ["none", "head", "provider"])
def test_backfill_cleans_only_success_and_retained_work_replays_without_http(
    tmp_path, monkeypatch, capsys, fault
):
    class Store(MemoryR2Store):
        def close(self):
            pass

    store = Store()
    MasterL1(store, tmp_path / "initialize").initialize()
    calls = []

    class Client:
        def close(self):
            pass

        def pages(self, source, params):
            calls.append((source, params))
            body = {"data": [{"Code": "13010", "Date": DAY.isoformat()}]}
            if fault == "provider":
                body["pagination_key"] = "next"
            yield JQuantsPage(source, params, datetime.now(UTC), json.dumps(body).encode())
            if fault == "provider":
                raise ConnectionError("private provider failure")

    monkeypatch.setattr(backfill, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(backfill, "JQuantsHTTP", lambda *_: Client())
    real_mkdtemp = backfill.tempfile.mkdtemp
    monkeypatch.setattr(backfill.tempfile, "mkdtemp", lambda **kw: real_mkdtemp(dir=tmp_path, **kw))
    real_put = store.put_file
    reject_head = fault == "head"

    def put(key, path, **kwargs):
        if key == HEAD_KEY and reject_head:
            raise LakePublishError("private publication failure")
        return real_put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    assert backfill.backfill_native_master(Namespace(asofs=[DAY.isoformat()])) == (
        0 if fault == "none" else 1
    )
    output = capsys.readouterr()
    assert "private" not in output.err
    assert len(calls) == 1
    work = list(tmp_path.glob("master-native-backfill-*"))
    if fault == "none":
        assert work == []
        assert "work retained" not in output.err
        return

    assert len(work) == 1
    assert str(work[0]) in output.err
    directory = work[0] / DAY.isoformat()
    attempts = list(directory.glob("attempt-*.json"))
    assert len(attempts) == 1
    original = attempts[0].read_bytes()
    if fault == "head":
        assert MasterL1(store, tmp_path / "reader").fixed().value()["receipts"] == {}
        reject_head = False
        monkeypatch.setattr(jquants_master_native, "Boto3R2Store", lambda **_: store)
        assert jquants_master_native.main(["commit-work", "--directory", str(directory)]) == 0
        assert len(calls) == 1
        assert attempts[0].read_bytes() == original
        saved = json.loads(original)
        receipts = MasterL1(store, tmp_path / "reader").fixed().value()["receipts"]
        assert set(receipts) == {saved["request_id"]}
        pack = store.get_bytes(
            PREFIX + "packs/" + receipts[saved["request_id"]]["pack"] + ".json.gz"
        )
        assert json.loads(gzip.decompress(pack)) == [saved]
