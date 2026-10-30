"""既知のtrend claim schemaを原観測へ照合する純粋な検証。"""

from __future__ import annotations

from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleExtractionArtifact,
    ArticleObservation,
)
from baibai_engine.machine_analysis.observations.trends.contracts import (
    ArtifactRef,
    DocumentText,
    ExtractionArtifact,
)

PAYLOAD_SCHEMA = "trend-claims-v1"


def decode_artifact(
    artifact: ArticleExtractionArtifact,
    observation: ArticleObservation | None = None,
) -> ExtractionArtifact:
    """Recognized trend schema only; pass its observation to verify provenance and spans."""
    if artifact.payload.get("schema") != PAYLOAD_SCHEMA:
        raise ValueError("unsupported semantic artifact schema")
    payload = {
        key: value
        for key, value in artifact.payload.items()
        if key not in {"schema", "article_version_id", "published", "updated"}
    }
    decoded = ExtractionArtifact.model_validate(
        {
            **payload,
            "method": artifact.method,
            "method_revision": artifact.extractor_revision,
            "created_at": artifact.generated_at,
            "status": artifact.status,
            "assistant_config": None
            if artifact.method == "parser"
            else f"{artifact.model_id}:{artifact.prompt_sha256}:{artifact.config_sha256}",
            "storage_ref": ArtifactRef(artifact_id=artifact.artifact_id, sha256=artifact.sha256),
        }
    )
    if (
        decoded.evidence.observation_id != artifact.observation_id
        or decoded.evidence.observation_sha256 != artifact.observation_sha256
        or decoded.evidence.text_sha256 != artifact.text_sha256
        or decoded.evidence.text_revision != artifact.text_revision
    ):
        raise ValueError("semantic payload differs from canonical artifact envelope")
    if observation is not None:
        evidence = decoded.evidence
        if (
            observation.observation_id != artifact.observation_id
            or observation.sha256 != artifact.observation_sha256
            or artifact.payload.get("article_version_id") != observation.article_version_id
            or artifact.payload.get("published") != observation.published.model_dump(mode="json")
            or artifact.payload.get("updated") != observation.updated.model_dump(mode="json")
            or evidence.receipt_id != observation.receipt_id
            or evidence.receipt_sha256 != observation.receipt_sha256
            or evidence.body_receipt_id != observation.body_ref.receipt_id
            or evidence.raw_sha256 != observation.body_ref.sha256
            or evidence.source_url != observation.original_url
            or evidence.article_id != observation.article_id
            or evidence.content_scope != observation.content_scope
        ):
            raise ValueError("semantic evidence differs from retained observation")
        decoded.verify_text(DocumentText(evidence=evidence, text=observation.text))
    return decoded
