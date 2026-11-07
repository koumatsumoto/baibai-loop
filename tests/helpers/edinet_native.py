"""手動取得用の合成native原本。HTTPやSQL writerの代用はしない。"""

import json
from datetime import UTC, datetime

from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_jobs.maintenance import edinet_extraction
from baibai_jobs.maintenance.source_refresh import edinet
from tests.helpers.r2_store import MemoryR2Store


def native_edinet_store(tmp_path, monkeypatch, documents_by_day=None):
    store = MemoryR2Store()
    store.close = lambda: None
    monkeypatch.setattr(edinet_extraction, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(edinet, "Boto3R2Store", lambda **_: store)
    lists = EdinetListL1(store, tmp_path / "fixture-lists")
    lists.freeze(False)
    receipts = []
    for day, documents in (documents_by_day or {}).items():
        rows = [{"seqNumber": number, **row} for number, row in enumerate(documents, 1)]
        body = json.dumps(
            {"metadata": {"resultset": {"count": len(rows)}}, "results": rows}
        ).encode()
        receipts.append(
            NativeListReceipt.from_response(
                day, EdinetResponse(datetime.combine(day, datetime.min.time(), tzinfo=UTC), body)
            )
        )
    lists.publish(receipts)
    archives = EdinetArchiveL1(store, tmp_path / "fixture-archives")
    archives.initialize()
    return store, lists, archives
