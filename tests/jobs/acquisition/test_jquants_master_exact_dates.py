"""Explicit master dates use the native owner's existing recovery input."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta

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

DAY = date(2026, 11, 5)


@pytest.mark.parametrize("fault", ["none", "head", "provider"])
def test_exact_dates_clean_only_success_and_retain_work_for_replay_without_http(
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

    monkeypatch.setattr(jquants_master_native, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(jquants_master_native, "JQuantsHTTP", lambda *_: Client())
    real_mkdtemp = jquants_master_native.tempfile.mkdtemp
    monkeypatch.setattr(
        jquants_master_native.tempfile, "mkdtemp", lambda **kw: real_mkdtemp(dir=tmp_path, **kw)
    )
    real_put = store.put_file
    reject_head = fault == "head"

    def put(key, path, **kwargs):
        if key == HEAD_KEY and reject_head:
            raise LakePublishError("private publication failure")
        return real_put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    assert jquants_master_native.main(["acquire", "--asof", DAY.isoformat()]) == (
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


def test_exact_dates_skip_unrequested_ranges_and_deduplicate(monkeypatch, capsys):
    next_date = DAY + timedelta(days=5)
    seen = []

    class Closeable:
        def close(self):
            pass

    def acquire(owner, *, start, end, client):
        assert start == end
        assert owner.directory.name == start.isoformat()
        seen.append(start)
        return jquants_master_native.MasterNativeAcquisition(
            dict.fromkeys(("failed", "held", "halted", "incomplete"), 0)
        )

    monkeypatch.setattr(jquants_master_native, "Boto3R2Store", lambda **_: Closeable())
    monkeypatch.setattr(jquants_master_native, "JQuantsHTTP", lambda *_: Closeable())
    monkeypatch.setattr(jquants_master_native, "acquire_master_native", acquire)
    assert (
        jquants_master_native.main(
            [
                "acquire",
                "--asof",
                next_date.isoformat(),
                "--asof",
                DAY.isoformat(),
                "--asof",
                DAY.isoformat(),
            ]
        )
        == 0
    )
    assert seen == [DAY, next_date]
    assert list(json.loads(capsys.readouterr().out)["independent_l1"]) == [
        day.isoformat() for day in seen
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["acquire", "--asof", DAY.isoformat(), "--start", DAY.isoformat()],
        ["acquire", "--asof", DAY.isoformat(), "--directory", "unused"],
        ["acquire", "--asof", DAY.isoformat(), "--force"],
        ["plan", "--asof", DAY.isoformat()],
        ["commit-work"],
    ],
)
def test_invalid_exact_date_combination_does_not_open_store(monkeypatch, argv):
    monkeypatch.setattr(
        jquants_master_native, "Boto3R2Store", lambda **_: pytest.fail("opened store")
    )
    with pytest.raises(SystemExit) as error:
        jquants_master_native.main(argv)
    assert error.value.code == 2


def test_exact_dates_refuse_future_before_opening_store(monkeypatch):
    monkeypatch.setattr(
        jquants_master_native, "Boto3R2Store", lambda **_: pytest.fail("opened store")
    )
    assert jquants_master_native.main(["acquire", "--asof", "9999-12-31"]) == 1
