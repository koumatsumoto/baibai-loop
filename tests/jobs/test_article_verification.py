import hashlib
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import article_verification as verification


def test_readback_includes_parse_failed_raw_and_checks_remote_rows(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    body = b"synthetic unparseable raw"
    receipt = ArticleReceipt(
        receipt_id="raw-failed-parse",
        source="boj_news",
        requested_url="https://www.boj.or.jp/synthetic.xml",
        received_at=datetime(2026, 10, 29, tzinfo=UTC),
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="raw-failed-parse", sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(open_connection(database)) as conn, conn:
        retain_receipt(conn, receipt, body=body)
    delta = verification.capture_delta(database, [receipt.receipt_id])
    assert delta["articles.source_receipts"]
    assert delta["articles.extraction_artifacts"] == {}
    dataset = verification.ARTICLE_RECEIPTS
    columns = [column.name for column in dataset.columns]
    with closing(open_connection(database)) as conn:
        rows = conn.execute("SELECT * FROM article_source_receipts").fetchall()
    parquet = tmp_path / "remote.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [dict(zip(columns, rows[0], strict=True))], schema=dataset.arrow_schema
        ),
        parquet,
    )
    part = SimpleNamespace(values={"year": 2026, "month": 10}, objects=[])
    release = SimpleNamespace(dataset_manifests={dataset.name: SimpleNamespace(partitions=(part,))})
    monkeypatch.setattr(
        verification,
        "_serving_pointer",
        lambda _: SimpleNamespace(
            pointer=SimpleNamespace(release_id="fixed", manifest_sha256="a" * 64)
        ),
    )
    monkeypatch.setattr(verification, "_resolve_serving_release", lambda *_: release)
    monkeypatch.setattr(verification, "accepted_dataset", lambda *_: dataset)
    monkeypatch.setattr(verification, "materialize_partitions", lambda *_, **__: [parquet])
    kwargs = dict(
        delta=delta,
        directory=tmp_path / "readback",
        generations={"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64},
    )
    assert verification.verify_delta(MemoryR2Store(), **kwargs)[dataset.name] == 1
    # Different actual downloaded body cannot be sealed by matching receipt ID only.
    wrong = dict(zip(columns, rows[0], strict=True))
    wrong["body_gzip"] = b"wrong saved body"
    pq.write_table(pa.Table.from_pylist([wrong], schema=dataset.arrow_schema), parquet)
    with pytest.raises(LakePublishError, match="readback differs"):
        verification.verify_delta(MemoryR2Store(), **kwargs)
