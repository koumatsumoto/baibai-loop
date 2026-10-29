"""企業調査の固定入力に保存済みE[r]較正文脈を添える。候補順位・判断は変えない。"""

from __future__ import annotations

from datetime import date
from math import isfinite
from pathlib import Path

from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    GeneratedResearchSelectionV4,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.machine_analysis.historical_evaluation.context.query import (
    candidate_er_band_context,
    load_er_calibration_context,
)


def research_calibration_context(
    selection: ResearchSelection, *, context_path: Path, as_of: date
) -> dict[str, object]:
    """Read a diagnostic only; never change Selection, candidate order or decisions."""

    if isinstance(selection, (GeneratedResearchSelection, GeneratedResearchSelectionV4)):
        return {
            "selection_sha256": selection_sha256(selection),
            "generation_id": selection.inputs.generation.generation_id,
            "checked_as_of": as_of.isoformat(),
            "status": "not_applicable",
            "unavailable_reason": "generated_models_do_not_emit_legacy_er",
        }
    source = selection.inputs.analysis
    result: dict[str, object] = {
        "selection_sha256": selection_sha256(selection),
        "run_revision_id": source.run_revision_id,
        "analysis_as_of": None
        if source.analysis_as_of is None
        else source.analysis_as_of.isoformat(),
        "checked_as_of": as_of.isoformat(),
        "interpretation": "observed_distribution_only",
    }
    if source.run_revision_id is None:
        return {**result, "status": "unavailable", "unavailable_reason": "missing_run_identity"}
    if source.screening_rules_hash is None or source.er_model_version is None:
        return {**result, "status": "unavailable", "unavailable_reason": "missing_method_identity"}
    loaded = load_er_calibration_context(
        context_path,
        expected_rules_hash=source.screening_rules_hash,
        expected_er_model_version=source.er_model_version,
        as_of=as_of,
    )
    if loaded.artifact is None:
        return {
            **result,
            "status": "unavailable",
            "unavailable_reason": loaded.unavailable_reason,
        }
    artifact = loaded.artifact
    targets: list[dict[str, object]] = []
    for target in selection.targets:
        analysis = source.analyses.get(target.ticker)
        metrics = None if analysis is None else analysis.get("metrics")
        estimate = metrics.get("er_annual") if isinstance(metrics, dict) else None
        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, (int, float))
            or not isfinite(estimate)
        ):
            targets.append({"ticker": target.ticker, "unavailable_reason": "er_unavailable"})
        else:
            targets.append(
                {
                    "ticker": target.ticker,
                    "er_annual": float(estimate),
                    "horizons": candidate_er_band_context(artifact, float(estimate)),
                }
            )
    return {
        **result,
        "status": "available",
        "context": {
            "generated_at": artifact.generated_at.isoformat(),
            "valid_through": artifact.valid_through.isoformat(),
            "screening_rules_hash": artifact.screening_rules_hash,
            "er_model_version": artifact.er_model_version,
            "evidence_complete": artifact.evidence_complete,
            "evidence_blocking_reasons": artifact.evidence_blocking_reasons,
            "reference_horizon": artifact.reference_horizon,
            "primary_realized_basis": artifact.primary_realized_basis,
            "secondary_realized_basis": artifact.secondary_realized_basis,
            "weighting": artifact.weighting.model_dump(mode="json"),
        },
        "targets": targets,
    }
