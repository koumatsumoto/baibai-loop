"""固定ZIPの有限並列は返却順・検証・失敗停止を保つ。実通信は行わない。"""

from io import BytesIO
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from tests.engine.machine_analysis.observations.financials.test_edinet_comparison_native import (
    ByteConnection,
    fixed_archives,
)

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.financials import edinet_comparison_reader as owner
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, R2OperationStopped

MIB = 1024**2


class BatchedArchives:
    def __init__(self, sizes, groups):
        self.sizes = dict(sizes)
        self.archive_keys = frozenset((key, 1) for key in self.sizes)
        self.barriers = {
            key: barrier for group in groups for barrier in [Barrier(len(group))] for key in group
        }
        self.lock = Lock()
        self.active = {}
        self.max_active = self.max_bytes = 0
        self.calls = []

    def metadata(self, doc_id, source_type):
        assert source_type == 1
        return SimpleNamespace(size=self.sizes[doc_id]) if doc_id in self.sizes else None

    def read_xbrl_zip(self, doc_id):
        with self.lock:
            self.calls.append(doc_id)
            self.active[doc_id] = self.sizes[doc_id]
            self.max_active = max(self.max_active, len(self.active))
            self.max_bytes = max(self.max_bytes, sum(self.active.values()))
        try:
            self.barriers[doc_id].wait(timeout=2)
            return doc_id.encode()
        finally:
            with self.lock:
                del self.active[doc_id]


@pytest.mark.parametrize(
    ("sizes", "groups", "max_active", "max_bytes"),
    [
        ([MIB] * 5, [("a",), ("b", "c", "d", "e")], 4, 4 * MIB),
        ([8 * MIB] * 5, [("a",), ("b", "c", "d", "e")], 4, 32 * MIB),
        ([20 * MIB] * 3, [("a",), ("b",), ("c",)], 1, 20 * MIB),
        ([40 * MIB, MIB, MIB], [("a",), ("b", "c")], 2, 40 * MIB),
    ],
)
def test_native_prefetch_batches_bound_count_and_declared_bytes(
    sizes, groups, max_active, max_bytes
):
    keys = tuple("abcde"[: len(sizes)])
    archives = BatchedArchives(zip(keys, sizes, strict=True), groups)
    result = list(
        owner._archive_contents(
            ByteConnection([], forbidden=True),
            doc_ids=(*keys, "not-retained"),
            tickers=None,
            archives=archives,
        )
    )
    assert result == [(key, key.encode()) for key in keys]
    assert sorted(archives.calls) == list(keys)
    assert archives.max_active == max_active
    assert archives.max_bytes == max_bytes


def test_unretained_identity_is_not_newly_validated_or_looked_up():
    store, archives = fixed_archives({("S100GOOD", 1): b"retained"})
    result = list(owner._native_archive_contents(archives, ("not-retained", "S100GOOD")))
    assert result == [("S100GOOD", b"retained")]
    assert len(store.get_keys) == 1


def test_immediate_failure_admits_no_later_read_or_next_batch():
    archives = BatchedArchives(((str(i), 1) for i in range(9)), [])
    error = LakePublishError("fixed original missing")

    def fail(doc_id):
        archives.calls.append(doc_id)
        raise error

    archives.read_xbrl_zip = fail
    with pytest.raises(LakePublishError) as caught:
        list(
            owner._archive_contents(
                ByteConnection([], forbidden=True),
                doc_ids=tuple(archives.sizes),
                tickers=None,
                archives=archives,
            )
        )
    assert caught.value is error
    assert archives.calls == ["0"]


def test_failed_batch_joins_in_flight_reads_and_does_not_start_next_batch():
    archives = BatchedArchives(((str(i), 1) for i in range(9)), [])
    barrier = Barrier(4)
    error = LakePublishError("fixed original changed")
    completed = []

    def read(doc_id):
        archives.calls.append(doc_id)
        if doc_id != "0":
            barrier.wait(timeout=2)
        completed.append(doc_id)
        if doc_id == "2":
            raise error
        return doc_id.encode()

    archives.read_xbrl_zip = read
    with pytest.raises(LakePublishError) as caught:
        list(
            owner._archive_contents(
                ByteConnection([], forbidden=True),
                doc_ids=tuple(archives.sizes),
                tickers=None,
                archives=archives,
            )
        )
    assert caught.value is error
    assert sorted(archives.calls) == sorted(completed) == ["0", "1", "2", "3", "4"]


@pytest.mark.parametrize("status", [403, 429])
def test_strict_store_still_stops_queued_batch_requests(tmp_path, monkeypatch, status):
    ids = tuple(f"S100AA{i:02d}" for i in range(8))
    memory, fixed = fixed_archives({(doc, 1): doc.encode() for doc in ids})
    stopped = tmp_path / "rejection.stop"
    store = Boto3R2Store(
        bucket="test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "fake",
            "R2_SECRET_ACCESS_KEY": "fake",
            "R2_STOP_ON_REJECTION": "1",
            "R2_REJECTION_STOP_FILE": str(stopped),
        },
    )
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs["Key"])
        if len(calls) == 1:
            return {"Body": BytesIO(memory.values[kwargs["Key"]].body)}
        raise ClientError(
            {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
            "GetObject",
        )

    monkeypatch.setattr(store, "_client", lambda *_: SimpleNamespace(get_object=get_object))
    archives = FixedEdinetArchives(store, fixed.value())
    for _ in range(2):
        with pytest.raises(R2OperationStopped):
            list(
                owner._archive_contents(
                    ByteConnection([], forbidden=True),
                    doc_ids=ids,
                    tickers=None,
                    archives=archives,
                )
            )
        assert len(calls) == 2
        assert stopped.read_text() == f"{status}\n"
