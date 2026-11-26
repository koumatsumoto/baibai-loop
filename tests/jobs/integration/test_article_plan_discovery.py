"""The retired inbox is invisible to regular integration but exact evidence still opens."""

import hashlib
import json
from contextlib import closing

from tests.helpers.article_evidence import SLOT, make_packet
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs import cli
from baibai_jobs.acquisition import article_evidence
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.integration import sources


def test_daily_plan_does_not_list_or_read_retained_article_stages(tmp_path, monkeypatch, capsys):
    store = MemoryR2Store()
    packet = make_packet(tmp_path)
    body = b"synthetic retired article"
    original = ArticleReceipt(
        receipt_id="exact-original",
        source="boj_news",
        requested_url="https://www.boj.or.jp/example.htm",
        received_at=SLOT,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="exact-original", sha256=hashlib.sha256(body).hexdigest()),
    )
    with closing(open_connection(packet / "receipts.sqlite")) as connection, connection:
        retain_receipt(connection, original, body=body)
    report_path = packet / "result.json"
    old_report = json.loads(report_path.read_text())
    old_report.update(
        receipt_ids=[original.receipt_id], projection_kinds={original.receipt_id: "document"}
    )
    report_path.write_text(json.dumps(old_report))
    stage = "boj_news-retained"
    key = f"coordination/article-sources/{stage}/handoff.json"
    save_handoff(store, stage, packet, namespace="article-sources")
    digest = hashlib.sha256(store.values[key].body).hexdigest()
    # A missing/invalid older ancestry used to be an error in each day. Data
    # from the exact fixed original remains readable without opening that chain.
    planning_key = "coordination/article-planning/boj_news.json"
    store.store_object(planning_key, b"unusable retired planning root")
    empty_edinet_input(store, tmp_path)
    before = dict(store.values)
    calls = []
    iter_objects = store.iter_objects

    def trace(prefix):
        calls.append(prefix)
        return iter_objects(prefix)

    monkeypatch.setattr(store, "iter_objects", trace)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    directory = tmp_path / "integration"
    assert sources.main(["plan", "--directory", str(directory)]) == 0
    plan = json.loads((directory / "plan.json").read_text())
    assert plan["handoffs"] == []
    assert plan["edinet_input"]
    assert "coordination/market-sources/" in calls
    retired = ("coordination/article-sources/", "coordination/article-planning/")
    assert not any(
        key.startswith(retired) for key in calls + store.get_keys + store.head_keys + store.put_keys
    )
    assert all(store.values[key] == val for key, val in before.items())
    assert not store.delete_keys

    evidence_dir = tmp_path / "private-evidence"
    monkeypatch.setattr(article_evidence, "Boto3R2Store", lambda **_: store)
    assert (
        cli.main(
            [
                "article-evidence",
                "--source",
                "boj_news",
                "--stage-id",
                stage,
                "--manifest-sha256",
                digest,
                "--directory",
                str(evidence_dir),
                "--receipt",
                original.receipt_id,
                original.sha256,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["receipts"] == 1
    assert (evidence_dir / "body-0.bin").read_bytes() == body
    record = json.loads((evidence_dir / "evidence.json").read_text())
    assert record["receipts"][0]["receipt_sha256"] == original.sha256
    assert record["stage"]["manifest_sha256"] == digest
    assert planning_key not in store.get_keys
    assert all(store.values[key] == val for key, val in before.items())
