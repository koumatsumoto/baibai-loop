"""共有結果の有限な表示契約。本文は含めず、固定artifactの詳細へ戻る。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from baibai_engine.machine_analysis.observations.trends.contracts import Claim, ExtractionArtifact
from baibai_engine.machine_analysis.observations.trends.inputs import FixedTrendInputs
from baibai_engine.primitives.json import canonical_json

MAX_RECORDS = 24
MAX_COMPARISONS = 12
MAX_BYTES = 65536


def claim_record(claim: Claim, artifact: ExtractionArtifact) -> dict[str, object]:
    return {
        "claim": claim.model_dump(mode="json"),
        "artifact": artifact.ref.model_dump(mode="json"),
        "evidence": artifact.evidence.model_dump(mode="json"),
        "method_revision": artifact.method_revision,
    }


def finish_payload(
    fixed: FixedTrendInputs,
    *,
    records: list[dict[str, object]],
    comparisons: list[dict[str, object]],
    context: dict[str, object],
) -> dict[str, object]:
    artifacts = {
        artifact.ref.artifact_id: artifact
        for selection in fixed.artifacts
        for artifact in (selection.current, selection.baseline)
        if artifact is not None
    }
    for record in records:
        _pin_reference(record, fixed, artifacts)
    for comparison in comparisons:
        for key in ("current", "baseline"):
            compared_record = comparison.get(key)
            if isinstance(compared_record, dict):
                _pin_reference(compared_record, fixed, artifacts)
    records.sort(key=canonical_json)
    comparisons.sort(key=canonical_json)
    shown_records = records[:MAX_RECORDS]
    shown_comparisons = comparisons[:MAX_COMPARISONS]
    details = []
    for identity in sorted(artifacts):
        artifact = artifacts[identity]
        reference: dict[str, object] = {
            "evidence": artifact.evidence.model_dump(mode="json"),
            "artifact": artifact.ref.model_dump(mode="json"),
        }
        _pin_reference(reference, fixed, artifacts)
        details.append(
            {"artifact": reference["artifact"], "exact_evidence": reference["exact_evidence"]}
        )
    payload: dict[str, object] = {
        **context,
        "input_manifest": fixed.manifest.model_dump(mode="json"),
        "input_manifest_sha256": fixed.manifest.sha256,
        "coverage": fixed.coverage.model_dump(mode="json"),
        "extraction_states": [
            {
                "artifact": item.current.ref.model_dump(mode="json"),
                "status": item.current.status,
                "limitations": list(item.current.limitations),
                "last_known": None
                if item.baseline is None
                else item.baseline.ref.model_dump(mode="json"),
            }
            for item in fixed.artifacts
        ],
        "observations": shown_records,
        "comparisons": shown_comparisons,
        "display": {},
        "detail": {
            "release_id": fixed.manifest.release_id,
            "release_sha256": fixed.manifest.release_sha256,
            "input_manifest_sha256": fixed.manifest.sha256,
            "reader": "article-exact-evidence",
            "reference_kind": "artifact_id_and_sha256",
            "artifacts": details,
        },
    }
    while True:
        payload["display"] = {
            "ordering": "canonical_json_ascending",
            "max_observations": MAX_RECORDS,
            "max_comparisons": MAX_COMPARISONS,
            "max_bytes": MAX_BYTES,
            "omitted_observations": len(records) - len(shown_records),
            "omitted_comparisons": len(comparisons) - len(shown_comparisons),
            "continuation": "detail.input_manifest_sha256_and_selected_artifact_refs",
        }
        if len(canonical_json(payload).encode()) <= MAX_BYTES:
            return payload
        if shown_comparisons:
            shown_comparisons.pop()
        elif shown_records:
            shown_records.pop()
        else:
            raise ValueError("trend manifest or status metadata exceeds the bounded payload")


def _month(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m")


def _pin_reference(
    record: dict[str, object],
    fixed: FixedTrendInputs,
    artifacts: Mapping[str, ExtractionArtifact],
) -> None:
    evidence = cast(dict[str, object], record["evidence"])
    artifact = cast(dict[str, object], record["artifact"])
    record["exact_evidence"] = None
    if not all(
        evidence.get(key)
        for key in (
            "receipt_sha256",
            "body_receipt_id",
            "observation_id",
            "observation_sha256",
        )
    ):
        return  # Pure synthetic inputs need no invented storage identity.
    selected = artifacts[str(artifact["artifact_id"])]
    receipt_month = _month(selected.evidence.received_at)
    record["exact_evidence"] = {
        "lineage_release_id": fixed.manifest.release_id,
        "lineage_manifest_sha256": fixed.manifest.release_sha256,
        "receipt_id": evidence["receipt_id"],
        "receipt_sha256": evidence["receipt_sha256"],
        "receipt_month": receipt_month,
        "body_ref": {"receipt_id": evidence["body_receipt_id"], "sha256": evidence["raw_sha256"]},
        "body_month": receipt_month
        if evidence["body_receipt_id"] == evidence["receipt_id"]
        else None,
        "original_url": evidence["source_url"],
        "observation_id": evidence["observation_id"],
        "observation_sha256": evidence["observation_sha256"],
        "observation_month": receipt_month,
        "artifact_id": artifact["artifact_id"],
        "artifact_sha256": artifact["sha256"],
        "artifact_month": _month(selected.created_at),
    }
