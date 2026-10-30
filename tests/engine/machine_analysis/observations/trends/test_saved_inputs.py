"""通常保存→通常抽出→固定読取。本文は完全合成、ネットワークなし。"""

import hashlib
from contextlib import closing
from datetime import timedelta

import pytest
from tests.trend_fixtures import NOW, PHARMA

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
    BodyRef,
)
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.generation.runners.trend_inputs import read_trend_inputs
from baibai_engine.machine_analysis.models.industry_trends.calculations import (
    calculate_industry_trends,
)
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.store import save_artifact
from baibai_engine.machine_analysis.observations.trends.extraction import (
    extract_observation,
    extract_observations,
)
from baibai_engine.machine_analysis.observations.trends.validation import decode_artifact
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def retain(
    conn, receipt_id="first", *, received_at=NOW, body_text=PHARMA, heading="合成・医薬品調査"
):
    body = (f'<div class="entry"><h1>{heading}</h1><p>' + body_text + "</p></div>").encode()
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
        assert decode_artifact(first, observation) == decoded
        tampered = first.model_copy(
            update={"payload": {**first.payload, "article_version_id": "wrong-version"}}
        )
        with pytest.raises(ValueError, match="semantic evidence differs"):
            decode_artifact(tampered, observation)
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
    descriptor = result["observations"][0]["exact_evidence"]
    # The month fields belong to the core article contract branch until integration.
    # Validate every field recognized by the contract in the current checkout.
    reference = ArticleEvidenceRef.model_validate(
        {key: value for key, value in descriptor.items() if key in ArticleEvidenceRef.model_fields}
    )
    assert reference.receipt_id == "first"
    assert reference.artifact_id == first.artifact_id
    assert reference.lineage_release_id == "synthetic"
    assert descriptor["receipt_month"] == "2026-10"
    assert descriptor["body_month"] == "2026-10"
    assert descriptor["observation_month"] == "2026-10"
    assert descriptor["artifact_month"] == "2026-10"


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


def retain_index(conn, receipt_id, *, received_at, summary="同じ概要"):
    import json

    item = {
        "url": "/survey",
        "title": "合成・医薬品調査",
        "list_text": summary,
        "viewing_start_ymd": "2026/10/24",
        "sort": receipt_id,
    }
    body = json.dumps([item], ensure_ascii=False).encode()
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
    run, (observation,) = project_receipt(conn, receipt_id, kind="index", generated_at=received_at)
    assert run.status == "parsed"
    extract_observations(conn, (observation.observation_id,), generated_at=received_at)
    return observation


def test_unchanged_index_after_document_keeps_fixed_document_claims(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        retain_index(conn, "index-before", received_at=NOW - timedelta(days=2))
        _, document = retain(conn, received_at=NOW - timedelta(days=1))
        (document_artifact,) = extract_observations(
            conn,
            (document.observation_id,),
            generated_at=NOW - timedelta(days=1),
        )
        retain_index(conn, "index-after", received_at=NOW)
    result = calculate_industry_trends(read(path).industries[0])
    assert result["observations"]
    assert result["observations"][0]["artifact"]["artifact_id"] == document_artifact.artifact_id
    assert any(
        item["kind"] == "period_change" and item["delta"] == "-2.0"
        for item in result["comparisons"]
    )


def test_changed_index_marks_unverified_body_and_new_failed_body_stays_current(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        retain_index(conn, "index-before", received_at=NOW - timedelta(days=3))
        _, old = retain(conn, received_at=NOW - timedelta(days=2))
        (old_artifact,) = extract_observations(
            conn,
            (old.observation_id,),
            generated_at=NOW - timedelta(days=2),
        )
        retain_index(
            conn,
            "index-changed",
            received_at=NOW - timedelta(days=1),
            summary="明示的に更新された概要",
        )
    inputs = read(path).industries[0].inputs
    result = calculate_industry_trends(read(path).industries[0])
    assert result["observations"] == []
    assert any("index_changed_document_unverified" in reason for reason in inputs.coverage.reasons)
    assert result["extraction_states"][0]["last_known"]["artifact_id"] == old_artifact.artifact_id
    with closing(open_connection(path)) as conn, conn:
        _, changed = retain(conn, "new-document", body_text="合成・医薬品調査は本文未対応。")
        (failed,) = extract_observations(conn, (changed.observation_id,), generated_at=NOW)
    current = calculate_industry_trends(read(path).industries[0])
    assert current["observations"] == []
    assert current["extraction_states"][0]["artifact"]["artifact_id"] == failed.artifact_id
    assert current["extraction_states"][0]["last_known"]["artifact_id"] == old_artifact.artifact_id


def test_unrelated_recent_boj_receipts_cannot_evict_decision_candidate(tmp_path):
    from tests.trend_fixtures import BOJ

    from baibai_engine.machine_analysis.observations.articles.contracts import (
        ArticleObservation,
        ArticleProjectionRun,
    )
    from baibai_engine.machine_analysis.observations.articles.store import save_projection

    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        for index in range(21):
            policy = index == 0
            url = (
                "https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260918a.pdf"
                if policy
                else f"https://www.boj.or.jp/other/{index}.pdf"
            )
            body = (BOJ if policy else "無関係な記事").encode()
            received = NOW - timedelta(minutes=21 - index)
            identity = f"boj-{index}"
            ref = BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest())
            receipt = ArticleReceipt(
                receipt_id=identity,
                source="boj_news",
                requested_url=url,
                received_at=received,
                status="ok",
                http_status=200,
                body_ref=ref,
            )
            retain_receipt(conn, receipt, body=body)
            observation = ArticleObservation(
                article_id=f"boj_news:{url}",
                source="boj_news",
                receipt_id=identity,
                receipt_sha256=receipt.sha256,
                body_ref=ref,
                native_locator="/pdf",
                original_url=url,
                title="",
                content_scope="partial_text",
                parser_revision="article-native-v1",
                text_revision="article-text-v1",
                text=body.decode(),
            )
            save_projection(
                conn,
                ArticleProjectionRun(
                    receipt_id=identity,
                    receipt_sha256=receipt.sha256,
                    parser_revision="article-native-v1",
                    generated_at=received,
                    status="parsed",
                    observation_ids=(observation.observation_id,),
                ),
                (observation,),
            )
            if policy:
                extract_observations(conn, (observation.observation_id,), generated_at=received)
    inputs = read(path).social
    assert len(inputs.artifacts) == 1
    assert inputs.artifacts[0].current.claims[0].status == "decided"
    assert inputs.coverage.omitted_artifacts == 0


def test_industry_scope_limits_do_not_erase_other_survey(tmp_path):
    from tests.trend_fixtures import INFORMATION

    from baibai_engine.machine_analysis.observations.articles.contracts import (
        ArticleObservation,
        ArticleProjectionRun,
    )
    from baibai_engine.machine_analysis.observations.articles.store import save_projection

    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        for index in range(18):
            information = index == 0
            identity = f"survey-{index}"
            url = f"https://example.invalid/data/detail/{identity}"
            body = (INFORMATION if information else PHARMA).encode()
            received = NOW - timedelta(minutes=18 - index)
            ref = BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest())
            receipt = ArticleReceipt(
                receipt_id=identity,
                source="tsr_data",
                requested_url=url,
                received_at=received,
                status="ok",
                http_status=200,
                body_ref=ref,
            )
            retain_receipt(conn, receipt, body=body)
            observation = ArticleObservation(
                article_id=f"tsr_data:{url}",
                source="tsr_data",
                receipt_id=identity,
                receipt_sha256=receipt.sha256,
                body_ref=ref,
                native_locator="/html/body-region",
                original_url=url,
                title="情報サービス業績調査" if information else "医薬品業績調査",
                content_scope="partial_text",
                parser_revision="article-native-v1",
                text_revision="article-text-v1",
                text=body.decode(),
            )
            save_projection(
                conn,
                ArticleProjectionRun(
                    receipt_id=identity,
                    receipt_sha256=receipt.sha256,
                    parser_revision="article-native-v1",
                    generated_at=received,
                    status="parsed",
                    observation_ids=(observation.observation_id,),
                ),
                (observation,),
            )
            if information:
                extract_observations(conn, (observation.observation_id,), generated_at=received)
    pharma, information = read(path).industries
    assert pharma.inputs.coverage.omitted_artifacts == 1
    assert information.inputs.coverage.state == "available"
    assert information.inputs.coverage.omitted_artifacts == 0
    assert len(information.inputs.artifacts) == 1
    assert (
        information.inputs.artifacts[0].current.claims[0].scope.scope_id
        == "jp-information-services"
    )


def test_prior_matching_index_keeps_family_when_new_document_title_changes(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        retain_index(conn, "index-before", received_at=NOW - timedelta(days=1))
        _, updated = retain(conn, heading="訂正後の見出し")
        extract_observations(conn, (updated.observation_id,), generated_at=NOW)
    result = calculate_industry_trends(read(path).industries[0])
    assert result["observations"]
    assert result["observations"][0]["evidence"]["observation_id"] == updated.observation_id


def test_historical_as_of_excludes_later_publication_but_keeps_future_plan(tmp_path):
    from datetime import date

    from tests.trend_fixtures import BOJ

    from baibai_engine.machine_analysis.observations.articles.contracts import (
        ArticleObservation,
        ArticleProjectionRun,
    )
    from baibai_engine.machine_analysis.observations.articles.store import save_projection

    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        for identity, published, received in (
            ("earlier", "2026年9月18日", NOW),
            ("later", "2026年10月28日", NOW + timedelta(days=4)),
        ):
            url = f"https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/{identity}.pdf"
            text = BOJ.replace("2026年9月18日", published)
            body = text.encode()
            ref = BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest())
            receipt = ArticleReceipt(
                receipt_id=identity,
                source="boj_news",
                requested_url=url,
                received_at=received,
                status="ok",
                http_status=200,
                body_ref=ref,
            )
            retain_receipt(conn, receipt, body=body)
            observation = ArticleObservation(
                article_id=f"boj_news:{url}",
                source="boj_news",
                receipt_id=identity,
                receipt_sha256=receipt.sha256,
                body_ref=ref,
                native_locator="/pdf",
                original_url=url,
                title="",
                content_scope="partial_text",
                parser_revision="article-native-v1",
                text_revision="article-text-v1",
                text=text,
            )
            save_projection(
                conn,
                ArticleProjectionRun(
                    receipt_id=identity,
                    receipt_sha256=receipt.sha256,
                    parser_revision="article-native-v1",
                    generated_at=received,
                    status="parsed",
                    observation_ids=(observation.observation_id,),
                ),
                (observation,),
            )
            extract_observations(conn, (observation.observation_id,), generated_at=received)
    historical = read_trend_inputs(
        path,
        release_id="synthetic",
        release_sha256="a" * 64,
        cutoff=NOW + timedelta(days=5),
        as_of=date(2026, 9, 20),
    ).social
    assert len(historical.artifacts) == 1
    assert {c.occurred_on for c in historical.artifacts[0].current.claims} == {date(2026, 9, 18)}
    assert any(c.scheduled_for == date(2026, 9, 24) for c in historical.artifacts[0].current.claims)


def test_old_index_date_does_not_backdate_later_document_revision(tmp_path):
    from datetime import date

    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as conn, conn:
        retain_index(conn, "index-before", received_at=NOW - timedelta(days=1))
        _, document = retain(conn, received_at=NOW)
        extract_observations(conn, (document.observation_id,), generated_at=NOW)
    inputs = (
        read_trend_inputs(
            path,
            release_id="synthetic",
            release_sha256="a" * 64,
            cutoff=NOW,
            as_of=date(2026, 10, 24),
        )
        .industries[0]
        .inputs
    )
    assert inputs.artifacts == ()
    assert any(
        "document_version_after_as_of_unverified" in reason for reason in inputs.coverage.reasons
    )
