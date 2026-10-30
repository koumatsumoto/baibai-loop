"""通常保存→通常抽出→固定読取。本文は完全合成、ネットワークなし。"""

import hashlib
from contextlib import closing
from datetime import timedelta

from tests.trend_fixtures import NOW, PHARMA

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
    BodyRef,
)
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.models.industry_trends.calculations import (
    calculate_industry_trends,
)
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.store import save_artifact
from baibai_engine.machine_analysis.observations.trends.extraction import (
    decode_artifact,
    extract_observation,
    extract_observations,
)
from baibai_engine.machine_analysis.observations.trends.reader import read_trend_inputs
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def retain(conn, receipt_id="first", *, received_at=NOW, body_text=PHARMA):
    body = ('<div class="entry"><h1>合成・医薬品調査</h1><p>' + body_text + "</p></div>").encode()
    receipt = ArticleReceipt(
        receipt_id=receipt_id,
        source="tsr_data",
        requested_url="https://example.invalid/survey",
        received_at=received_at,
        status="ok",
        http_status=200,
        body_ref=BodyRef(receipt_id=receipt_id, sha256=hashlib.sha256(body).hexdigest()),
    )
    retain_receipt(conn, receipt, body=body)
    run, observations = project_receipt(conn, receipt_id, kind="document", generated_at=received_at)
    assert run.status == "parsed"
    return receipt, observations[0]


def read(path):
    return read_trend_inputs(
        path, release_id="synthetic", release_sha256="a" * 64, cutoff=NOW, as_of=NOW.date()
    )


def test_normal_projection_extracts_and_reuses_unchanged_representation_without_retargeting(
    tmp_path,
):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        receipt, observation = retain(conn, received_at=NOW - timedelta(days=1))
        (first,) = extract_observations(conn, (observation.observation_id,), generated_at=NOW)
        second_receipt, second_observation = retain(conn, "second")
        (repeated,) = extract_observations(
            conn, (second_observation.observation_id,), generated_at=NOW
        )
        assert observation.observation_id != second_observation.observation_id
        assert first == repeated
        assert decode_artifact(repeated).evidence.receipt_id == receipt.receipt_id
        assert decode_artifact(repeated).evidence.receipt_id != second_receipt.receipt_id
        assert conn.execute("SELECT count(*) FROM article_extraction_artifacts").fetchone()[0] == 1
        decoded = decode_artifact(first)
        assert decoded.evidence.text_sha256 == observation.text_sha256
        assert all(
            span.end <= len(observation.text) for claim in decoded.claims for span in claim.spans
        )
        assert (
            "2兆3,400億円"
            in observation.text[decoded.claims[0].spans[1].start : decoded.claims[0].spans[1].end]
        )
    inputs = read(path)
    path.unlink()  # calculation consumes only fixed values, after the reader is closed
    result = calculate_industry_trends(inputs.industries[0])
    reference = ArticleEvidenceRef.model_validate(result["observations"][0]["exact_evidence"])
    assert reference.receipt_id == "first"
    assert reference.artifact_id == first.artifact_id
    assert reference.lineage_release_id == "synthetic"


def test_new_text_without_extraction_is_pending_and_not_an_empty_world(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        _, observation = retain(conn)
    inputs = read(path)
    assert inputs.industries[0].inputs.coverage.state == "extraction_pending"
    assert observation.observation_id in inputs.industries[0].inputs.coverage.reasons[0]
    assert inputs.industries[0].inputs.artifacts == ()
    assert len(inputs.industries) == 2


def test_conflicting_same_revision_artifacts_are_not_resolved_by_arrival_time(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        receipt, observation = retain(conn)
        first = extract_observation(observation, receipt, generated_at=NOW)
        save_artifact(conn, first)
        payload = dict(first.payload)
        payload["limitations"] = ["a different extraction interpretation"]
        conflict = first.model_copy(update={"payload": payload})
        save_artifact(conn, conflict)
    inputs = read(path)
    assert inputs.industries[0].inputs.coverage.state == "extraction_pending"
    assert "competing_extraction_artifacts" in inputs.industries[0].inputs.coverage.reasons[0]


def test_new_text_revision_failed_extraction_does_not_promote_old_claims(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        _, old = retain(conn, received_at=NOW - timedelta(days=1))
        extract_observations(conn, (old.observation_id,), generated_at=NOW - timedelta(days=1))
        _, changed = retain(conn, "changed", body_text="合成・医薬品調査は本文未対応。")
        (new,) = extract_observations(conn, (changed.observation_id,), generated_at=NOW)
    inputs = read(path)
    result = calculate_industry_trends(inputs.industries[0])
    assert result["observations"] == []
    assert new.status == "unsupported"
    assert result["extraction_states"][0]["last_known"] is not None
