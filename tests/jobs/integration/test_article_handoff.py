import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.article_handoff import import_handoff
from baibai_jobs.publication.article_verification import capture_delta


def _packet(directory, identity, received_at, *, source="boj_news"):
    directory.mkdir()
    body = {
        "boj_news": b"<rss><channel><item><link>https://www.boj.or.jp/synthetic.htm</link><title>Synthetic headline</title><description>Unsupported synthetic summary</description></item></channel></rss>",
        "minkabu_news": b'<a href="/news/1">Synthetic headline</a>',
        "rakuten_news": b'<a href="https://www.rakuten-sec.co.jp/Direct_Login/?story=1">Synthetic headline</a>',
    }[source]
    receipt = ArticleReceipt(
        receipt_id=identity,
        source=source,
        requested_url=SOURCES[source].entry_url,
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
                "source": source,
                "receipt_ids": [identity],
                "projection_kinds": {identity: "index"},
                "status": "completed",
            }
        )
    )
    return receipt


@pytest.mark.parametrize("source", ["boj_news", "minkabu_news", "rakuten_news"])
def test_same_value_200_reuses_artifact_and_readback_keeps_its_original_closure(tmp_path, source):
    database = tmp_path / "market.sqlite"
    stamp = datetime(2026, 10, 29, tzinfo=UTC)
    old = _packet(tmp_path / "old", "old-receipt", stamp, source=source)
    new = _packet(tmp_path / "new", "new-receipt", stamp + timedelta(hours=4), source=source)
    first = import_handoff(source, database, tmp_path / "old")
    second = import_handoff(source, database, tmp_path / "new")
    assert first.complete
    assert second.complete
    assert first.report["artifact_ids"] == second.report["artifact_ids"]
    delta = capture_delta(
        database, second.report["receipt_ids"], artifact_ids=second.report["artifact_ids"]
    )
    assert set(delta["articles.source_receipts"]) == {old.receipt_id, new.receipt_id}
    assert set(delta["articles.extraction_artifacts"]) == set(first.report["artifact_ids"])
    # Explicit late replay adds no new original and cannot replace the newer one.
    assert import_handoff(source, database, tmp_path / "old").complete
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


def test_common_integration_accepts_article_shape_from_old_market_version(tmp_path, monkeypatch):
    import sqlite3

    from tests.helpers.r2_store import MemoryR2Store

    from baibai_jobs.acquisition.article_evidence import exact_entry
    from baibai_jobs.acquisition.economic_staging import save_handoff
    from baibai_jobs.integration import sources

    packet = tmp_path / "legacy"
    original = _packet(packet, "legacy-original", datetime(2026, 10, 29, tzinfo=UTC))
    with closing(sqlite3.connect(packet / "receipts.sqlite")) as conn:
        conn.execute("PRAGMA user_version=49")
    report = json.loads((packet / "result.json").read_text())
    report.update(started_at=original.received_at.isoformat(), asof="2026-10-29")
    (packet / "result.json").write_text(json.dumps(report))
    before = (packet / "receipts.sqlite").read_bytes()
    remote = MemoryR2Store()
    save_handoff(remote, "legacy-article", packet, namespace="article-sources")
    manifest = remote.values["coordination/article-sources/legacy-article/handoff.json"].body
    entry = exact_entry(
        remote,
        source="boj_news",
        stage_id="legacy-article",
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps(
            {
                "handoffs": [entry],
                "edinet_input": empty_edinet_input(remote, tmp_path).model_dump(mode="json"),
            }
        )
    )
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    monkeypatch.setattr(sources, "extract_saved_edinet", lambda *_, **__: {})
    from tests.helpers.master_calendar_native import seed_calendar, seed_master

    seed_calendar(remote, market, tmp_path / "native-calendar", as_of=original.received_at.date())
    seed_master(remote, market, tmp_path / "native-master", as_of=original.received_at.date())
    puts = list(remote.put_keys)
    applied = sources.apply(remote, work, market=market, asof=original.received_at.date())
    outcome = applied["handoffs"][0]
    assert outcome["applied"] is True
    assert outcome["failed"] is False
    assert outcome["article_delta"]["articles.source_receipts"]
    assert (packet / "receipts.sqlite").read_bytes() == before
    assert remote.put_keys[: len(puts)] == puts
    assert all(key.startswith("fixed-inputs/") for key in remote.put_keys[len(puts) :])


def test_explicit_semantic_recovery_keeps_selected_native_version_and_publication_closure(
    tmp_path, monkeypatch
):
    from tests.helpers.trends import BOJ, NOW

    from baibai_engine.data_collection.providers.articles import native
    from baibai_engine.data_collection.receipts.articles.store import read_body
    from baibai_engine.machine_analysis.generation.runners.trend_inputs import read_trend_inputs
    from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
    from baibai_engine.machine_analysis.observations.articles.store import (
        read_artifacts,
        save_artifact,
    )
    from baibai_engine.machine_analysis.observations.trends.extraction import (
        extract_observation,
        extract_observations,
    )

    database = tmp_path / "market.sqlite"
    body = f"<main><h1>当面の金融政策運営について</h1><p>{BOJ}</p></main>".encode()
    original = ArticleReceipt(
        receipt_id="original",
        source="boj_news",
        requested_url="https://www.boj.or.jp/mopo/synthetic.htm",
        received_at=NOW - timedelta(days=2),
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id="original", sha256=hashlib.sha256(body).hexdigest()),
    )
    confirmed = original.model_copy(
        update={
            "receipt_id": "later-304",
            "received_at": NOW - timedelta(days=1),
            "status": "not_modified",
            "http_status": 304,
        }
    )

    def social(cutoff):
        return read_trend_inputs(
            database,
            release_id="synthetic",
            release_sha256="a" * 64,
            cutoff=cutoff,
            as_of=NOW.date(),
        ).social

    with closing(open_connection(database)) as conn, conn:
        retain_receipt(conn, original, body=body)
        retain_receipt(conn, confirmed)
        with monkeypatch.context() as old_native:
            old_native.setattr(native, "BOJ_PARSER_REVISION", "article-native-boj-v2")
            _, (first,) = project_receipt(
                conn, original.receipt_id, kind="document", generated_at=original.received_at
            )
            _, (selected,) = project_receipt(
                conn, confirmed.receipt_id, kind="document", generated_at=confirmed.received_at
            )
        old_artifact = extract_observation(
            first, original, generated_at=original.received_at
        ).model_copy(update={"extractor_revision": "boj-policy-decision-v1"})
        save_artifact(conn, old_artifact)
        # Native replay creates another version, not an extraction for the later 304's version.
        _, (reprojected,) = project_receipt(
            conn, original.receipt_id, kind="document", generated_at=NOW
        )
        (new_native,) = extract_observations(conn, [reprojected.observation_id], generated_at=NOW)
        assert reprojected.text == selected.text
        assert reprojected.article_version_id != selected.article_version_id
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "article_source_receipts",
                "article_observations",
                "article_projection_runs",
            )
        }
    assert social(NOW).coverage.state == "extraction_pending"
    with closing(open_connection(database)) as conn, conn:
        (recovered,) = extract_observations(conn, [selected.observation_id], generated_at=NOW)
        assert recovered.payload["article_version_id"] == selected.article_version_id
        assert recovered.extractor_revision == "boj-policy-decision-v2"
        assert recovered.status == "extracted"
        assert recovered.generated_at == NOW
        assert read_artifacts(conn, [old_artifact.artifact_id]) == (old_artifact,)
        assert read_body(conn, original.body_ref) == body
        for table, rows in before.items():
            assert conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert extract_observations(
            conn, [selected.observation_id], generated_at=NOW + timedelta(hours=1)
        ) == (recovered,)
    assert social(NOW - timedelta(seconds=1)).artifacts == ()
    (selection,) = social(NOW).artifacts
    assert selection.current.ref.artifact_id == recovered.artifact_id
    assert selection.current.evidence.receipt_id == confirmed.receipt_id
    delta = capture_delta(database, [], artifact_ids=[recovered.artifact_id])
    assert set(delta["articles.source_receipts"]) == {original.receipt_id, confirmed.receipt_id}
    assert set(delta["articles.observations"]) == {
        first.observation_id,
        selected.observation_id,
        reprojected.observation_id,
    }
    assert set(delta["articles.extraction_artifacts"]) == {
        old_artifact.artifact_id,
        new_native.artifact_id,
        recovered.artifact_id,
    }
