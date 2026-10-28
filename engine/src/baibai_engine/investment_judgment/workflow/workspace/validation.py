"""Assessment・Review・保存済み入力のbindingを照合する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

import yaml

from baibai_engine.primitives.json import canonical_json
from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.selection import ResearchSelectionRecord as ResearchSelection
from baibai_engine.investment_judgment.independent_review.contracts import (
    AssessmentReview,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _load_mapping,
)


def _validate_editable_drafts(
    workspace: Path, manifest: Mapping[str, object], *, gate: ResearchSelection
) -> None:
    ref = manifest.get("domain_inputs_ref")
    path = workspace / "domain-inputs.yaml" if ref is None else workspace / str(ref)
    if ref is not None and (
        ref != "../../domain-inputs.yaml" or manifest.get("holding_ticker") is None
    ):
        raise ResearchWorkspaceConflictError("invalid parent domain input reference")
    actual = _load_mapping(path, label="domain inputs")
    periods = cast("int | None", manifest.get("company_growth_periods", 12))
    if canonical_json(actual) != canonical_json(
        project_research_inputs(gate, company_growth_periods=periods)
    ):
        raise ResearchWorkspaceConflictError(
            "domain inputs differ from the Operation's fixed snapshot"
        )


def _research_ticker_dir(workspace: Path, ticker: str) -> Path:
    """Return a path-confined ticker directory inside the shared research workspace."""

    workspace_root = workspace.resolve()
    ticker_dir = (workspace_root / ticker).resolve()
    if ticker_dir.parent != workspace_root:
        raise ResearchWorkspaceDataError(
            f"research ticker directory must be a direct child of the workspace: {ticker}"
        )
    return ticker_dir


def _review_draft_path(workspace: Path, ticker: str) -> Path:
    """Use the same stable file for scaffolding, status and publication."""
    return _research_ticker_dir(workspace, ticker) / "assessment-review.yaml"


# --------------------------------------------------------------------------- #
# Authored Assessment pair
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _CaseDraft:
    """One read of authored inputs, retaining raw payloads for the canonical writer."""

    assessment_payload: dict[str, object]
    review_payload: dict[str, object]
    assessment: StockAssessmentDocument | None
    review: AssessmentReview | None
    assessment_errors: list[str]
    review_errors: list[str]


def _read_case(workspace: Path, ticker: str, asof: date) -> _CaseDraft:
    directory = _research_ticker_dir(workspace, ticker)
    assessment_payload: dict[str, object] = {}
    review_payload: dict[str, object] = {}
    assessment, review = None, None
    assessment_errors: list[str] = []
    review_errors: list[str] = []
    assessment_path = directory / "assessment-draft.yaml"
    review_path = _review_draft_path(workspace, ticker)
    if not assessment_path.exists():
        assessment_errors.append("assessment draft missing")
    else:
        try:
            assessment_payload = _load_mapping(assessment_path, label="assessment")
            assessment = StockAssessmentDocument.model_validate(assessment_payload)
        except (OSError, yaml.YAMLError, ValueError, ResearchWorkspaceDataError) as error:
            assessment_errors.append(str(error))
    if not review_path.exists():
        review_errors.append("review draft missing")
    else:
        try:
            review_payload = _load_mapping(review_path, label="Assessment Review")
            review = AssessmentReview.model_validate(review_payload)
        except (OSError, yaml.YAMLError, ValueError, ResearchWorkspaceDataError) as error:
            review_errors.append(str(error))
    if assessment is not None:
        if assessment.input_snapshot.ticker != ticker:
            assessment_errors.append(
                f"assessment ticker {assessment.input_snapshot.ticker} does not match case {ticker}"
            )
        if assessment.input_snapshot.as_of < asof:
            assessment_errors.append("assessment predates the fixed Research selection")
        if review is not None and review.reviewed_assessment_sha256 != assessment_core_hash(
            assessment
        ):
            review_errors.append("review is stale for the current assessment core hash")
    return _CaseDraft(
        assessment_payload, review_payload, assessment, review, assessment_errors, review_errors
    )


def _case_eligibility(case: _CaseDraft, *, now: datetime) -> tuple[list[str], list[str]]:
    assessment_errors = list(case.assessment_errors)
    if not assessment_errors:
        assert case.assessment is not None
        result = evaluate_assessment(
            case.assessment,
            review=None if case.review_errors else case.review,
            now=now,
            identity=UnpublishedAssessment.DRAFT,
        )
        if result.assessment_readiness != "ready" and result.assessment_status != "review_required":
            assessment_errors.extend(result.errors)
    return assessment_errors, case.review_errors
