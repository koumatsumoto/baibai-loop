import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.article_handoff import import_handoff
from baibai_jobs.publication.article_verification import capture_delta


def _packet(directory, identity, received_at):
    directory.mkdir()
    body = b"<rss><channel><item><link>https://www.boj.or.jp/synthetic.htm</link><title>Synthetic headline</title><description>Unsupported synthetic summary</description></item></channel></rss>"
    receipt = ArticleReceipt(
        receipt_id=identity,
        source="boj_news",
        requested_url="https://www.boj.or.jp/rss/whatsnew.xml",
        received_at=received_at,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(open_connection(directory / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, receipt, body=body)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "source": "boj_news",
                "receipt_ids": [identity],
                "projection_kinds": {identity: "index"},
                "status": "completed",
            }
        )
    )
    return receipt


def test_same_value_200_reuses_artifact_and_readback_keeps_its_original_closure(tmp_path):
    database = tmp_path / "market.sqlite"
    stamp = datetime(2026, 10, 29, tzinfo=UTC)
    old = _packet(tmp_path / "old", "old-receipt", stamp)
    new = _packet(tmp_path / "new", "new-receipt", stamp + timedelta(hours=4))
    first = import_handoff("boj_news", database, tmp_path / "old")
    second = import_handoff("boj_news", database, tmp_path / "new")
    assert first.complete
    assert second.complete
    assert first.report["artifact_ids"] == second.report["artifact_ids"]
    delta = capture_delta(
        database, second.report["receipt_ids"], artifact_ids=second.report["artifact_ids"]
    )
    assert set(delta["articles.source_receipts"]) == {old.receipt_id, new.receipt_id}
    assert set(delta["articles.extraction_artifacts"]) == set(first.report["artifact_ids"])
    # Explicit late replay adds no new original and cannot replace the newer one.
    assert import_handoff("boj_news", database, tmp_path / "old").complete
    with closing(open_connection(database)) as conn:
        rows = conn.execute(
            "SELECT receipt_json FROM article_source_receipts ORDER BY receipt_id"
        ).fetchall()
    saved = {
        item.receipt_id: item
        for row in rows
        for item in [ArticleReceipt.model_validate_json(row[0])]
    }
    assert saved == {old.receipt_id: old, new.receipt_id: new}


def test_semantic_failure_retains_l1_and_projection_without_success_claim(tmp_path, monkeypatch):
    _packet(tmp_path / "packet", "receipt", datetime(2026, 10, 29, tzinfo=UTC))

    def fail(*_, **__):
        raise ValueError("synthetic identity failure")

    monkeypatch.setattr("baibai_jobs.integration.article_handoff.extract_observations", fail)
    database = tmp_path / "market.sqlite"
    result = import_handoff("boj_news", database, tmp_path / "packet")
    assert not result.complete
    assert result.report["extraction_failures"] == ["receipt"]
    delta = capture_delta(database, result.report["receipt_ids"])
    assert delta["articles.source_receipts"]
    assert delta["articles.observations"]
    assert delta["articles.extraction_artifacts"] == {}


@pytest.mark.parametrize("kind", ["index", "document"])
def test_http_receipt_failure_is_not_misreported_as_parser_failure(tmp_path, kind):
    directory = tmp_path / "packet"
    directory.mkdir()
    receipt = ArticleReceipt(
        receipt_id="failed",
        source="minkabu_news",
        requested_url="https://minkabu.jp/news/1",
        received_at=datetime(2026, 11, 2, tzinfo=UTC),
        status="http_error",
        http_status=403,
        error_code="http_403",
    )
    with closing(open_connection(directory / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, receipt)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "source": "minkabu_news",
                "receipt_ids": ["failed"],
                "projection_kinds": {"failed": kind},
                "status": "partial",
            }
        )
    )
    result = import_handoff("minkabu_news", tmp_path / "market", directory)
    assert result.complete  # 原受領の保存は成功、取得そのものの失敗は別に残す。
    assert result.report["projection_failures"] == []
    assert result.report["observation_ids"] == []
    assert result.report["acquisition_failures"] == [
        {
            "receipt_id": "failed",
            "status": "http_error",
            "http_status": 403,
            "error_code": "http_403",
        }
    ]
    with closing(open_connection(tmp_path / "market")) as conn:
        from baibai_engine.data_collection.receipts.articles.store import read_receipt

        assert read_receipt(conn, receipt.receipt_id) == receipt
        assert conn.execute("SELECT COUNT(*) FROM article_projection_runs").fetchone()[0] == 0


def test_received_200_without_body_region_remains_a_projection_failure(tmp_path):
    directory = tmp_path / "packet"
    directory.mkdir()
    body = b"<html><body>No supported article region</body></html>"
    receipt = ArticleReceipt(
        receipt_id="unsupported",
        source="minkabu_news",
        requested_url="https://minkabu.jp/news/1",
        received_at=datetime(2026, 11, 2, tzinfo=UTC),
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="unsupported", sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(open_connection(directory / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, receipt, body=body)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "source": "minkabu_news",
                "receipt_ids": [receipt.receipt_id],
                "projection_kinds": {receipt.receipt_id: "document"},
                "status": "completed",
            }
        )
    )
    result = import_handoff("minkabu_news", tmp_path / "market", directory)
    assert not result.complete
    assert result.report["acquisition_failures"] == []
    assert result.report["projection_failures"] == [receipt.receipt_id]
