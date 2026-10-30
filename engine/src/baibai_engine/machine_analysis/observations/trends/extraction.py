"""保存済み記事の意味抽出。原本/DDLはarticles owner、claim payloadはこのowner。"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime
from typing import cast

from pydantic import JsonValue

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt
from baibai_engine.data_collection.receipts.articles.store import read_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleExtractionArtifact,
    ArticleObservation,
)
from baibai_engine.machine_analysis.observations.articles.store import (
    read_artifacts,
    read_observations,
    save_artifact,
)
from baibai_engine.machine_analysis.observations.trends.contracts import (
    DocumentText,
    ExtractionArtifact,
    Span,
    TextEvidence,
)
from baibai_engine.machine_analysis.observations.trends.parsers import (
    BOJ_REVISION,
    TSR_REVISION,
    extract_supported_family,
)
from baibai_engine.machine_analysis.observations.trends.validation import (
    PAYLOAD_SCHEMA,
    decode_artifact as decode_artifact,
)

def expected_revision(source: str) -> str:
    return {"tsr_data": TSR_REVISION, "tsr_flash": TSR_REVISION, "boj_news": BOJ_REVISION}.get(
        source, "supported-families-v1"
    )


def _source(source: str) -> str:
    return {"tsr_data": "tsr", "tsr_flash": "tsr", "boj_news": "boj"}.get(source, source)


def extract_observation(
    observation: ArticleObservation,
    receipt: ArticleReceipt,
    *,
    generated_at: datetime,
) -> ArticleExtractionArtifact:
    """NFKC照合のoffsetを元投影へ戻す。正規化した本文を別途永続複写しない。"""
    if (
        observation.receipt_id != receipt.receipt_id
        or observation.receipt_sha256 != receipt.sha256
        or observation.body_ref != receipt.body_ref
    ):
        raise ValueError("semantic extraction receipt differs from observation")
    normalized: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(observation.text):
        for transformed in unicodedata.normalize("NFKC", char):
            if not transformed.isspace():
                normalized.append(transformed)
                offsets.append(index)
    text = "".join(normalized)
    published = None
    if observation.published.precision in ("minute", "second") and observation.published.value:
        value = datetime.fromisoformat(observation.published.value.replace("Z", "+00:00"))
        if value.tzinfo is not None:
            published = value
    evidence = TextEvidence(
        article_id=observation.article_id,
        receipt_id=receipt.receipt_id,
        receipt_sha256=receipt.sha256,
        body_receipt_id=observation.body_ref.receipt_id,
        observation_id=observation.observation_id,
        observation_sha256=observation.sha256,
        source=_source(observation.source),
        source_url=observation.original_url,
        raw_sha256=observation.body_ref.sha256,
        text_sha256=observation.text_sha256,
        text_revision=observation.text_revision,
        published_at=published,
        published_date=date.fromisoformat(observation.published.value)
        if observation.published.precision == "date" and observation.published.value
        else None,
        published_raw=observation.published.raw,
        published_precision=observation.published.precision,
        received_at=receipt.received_at,
        content_scope=observation.content_scope,
    )
    normalized_evidence = evidence.model_copy(
        update={
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "text_revision": "per-codepoint-nfkc-no-whitespace-v1",
        }
    )
    try:
        extracted = extract_supported_family(
            DocumentText(evidence=normalized_evidence, text=text),
            created_at=generated_at,
        )
    except ValueError:
        extracted = ExtractionArtifact(
            evidence=normalized_evidence,
            method="parser",
            method_revision=expected_revision(observation.source),
            family="invalid_supported_family",
            created_at=generated_at,
            status="failed",
            limitations=("invalid_explicit_value",),
        )
    claims = tuple(
        claim.model_copy(
            update={
                "spans": tuple(
                    Span(start=offsets[span.start], end=offsets[span.end - 1] + 1)
                    for span in claim.spans
                )
            }
        )
        for claim in extracted.claims
    )
    extracted = extracted.model_copy(update={"evidence": evidence, "claims": claims})
    extracted.verify_text(DocumentText(evidence=evidence, text=observation.text))
    # Status/method/clock have one persisted authority: the canonical envelope.
    payload = extracted.model_dump(
        mode="json",
        exclude={
            "method",
            "method_revision",
            "created_at",
            "status",
            "assistant_config",
        },
    )
    payload["schema"] = PAYLOAD_SCHEMA
    payload["article_version_id"] = observation.article_version_id
    payload["published"] = observation.published.model_dump(mode="json")
    payload["updated"] = observation.updated.model_dump(mode="json")
    return ArticleExtractionArtifact(
        observation_id=observation.observation_id,
        observation_sha256=observation.sha256,
        text_sha256=observation.text_sha256,
        text_revision=observation.text_revision,
        generated_at=generated_at,
        method="parser",
        extractor_revision=extracted.method_revision,
        status=extracted.status,
        payload=cast(dict[str, JsonValue], payload),
    )


def matching_artifacts(
    conn: sqlite3.Connection,
    observation: ArticleObservation,
    *,
    cutoff: datetime,
) -> tuple[ArticleExtractionArtifact, ...]:
    rows = conn.execute(
        "SELECT a.artifact_id FROM article_extraction_artifacts a "
        "JOIN article_observations o ON o.observation_id=a.observation_id "
        "WHERE o.article_id=? AND json_extract(a.artifact_json,'$.extractor_revision')=? "
        "AND json_extract(a.artifact_json,'$.method')='parser' "
        "AND json_extract(a.artifact_json,'$.payload.schema')=? "
        "AND json_extract(a.artifact_json,'$.payload.article_version_id')=? "
        "AND julianday(json_extract(a.artifact_json,'$.generated_at'))<=julianday(?) "
        "ORDER BY a.artifact_id LIMIT 65",
        (
            observation.article_id,
            expected_revision(observation.source),
            PAYLOAD_SCHEMA,
            observation.article_version_id,
            cutoff.isoformat(),
        ),
    ).fetchall()
    if len(rows) > 64:
        raise ValueError("too many competing extraction artifacts for one representation")
    return read_artifacts(conn, [row[0] for row in rows])


def extract_observations(
    conn: sqlite3.Connection,
    observation_ids: Sequence[str],
    *,
    generated_at: datetime,
) -> tuple[ArticleExtractionArtifact, ...]:
    """通常更新入口。callerがtransactionを所有し、未変更内容は元artifactを返す。"""
    result: list[ArticleExtractionArtifact] = []
    for observation in read_observations(conn, observation_ids):
        previous = matching_artifacts(conn, observation, cutoff=generated_at)
        if previous:
            # Do not decide between different claims here. The generation reader
            # marks conflicting candidates; repetition never retargets evidence.
            result.extend(previous)
            continue
        receipt = read_receipt(
            conn, observation.receipt_id, receipt_sha256=observation.receipt_sha256
        )
        artifact = extract_observation(observation, receipt, generated_at=generated_at)
        save_artifact(conn, artifact)
        result.append(artifact)
    return tuple(result)
