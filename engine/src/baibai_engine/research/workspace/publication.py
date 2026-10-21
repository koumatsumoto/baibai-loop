"""検証済みのAssessmentとReviewをimmutable publicationへ進める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.research.assessment import (
    assessment_core_hash,
)
from baibai_engine.research.assessment_store import (
    AssessmentStoreService,
    ResearchConflictError,
    ResearchValidationError,
)
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace.admission import _require_primary_research_ticker
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _load_mapping,
    _parse_date,
)
from baibai_engine.research.workspace.validation import (
    _case_eligibility,
    _read_case,
    _research_ticker_dir,
    _validate_editable_drafts,
    _verify_external_inputs,
)

# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PromoteResult:
    assessment_id: str
    review_id: str
    assessment_sha256: str


def promote(
    *,
    workspace: Path,
    ticker: str,
    db_path: Path | None,
    assessment_id: str | None = None,
    supersedes_id: str | None = None,
    now: datetime,
) -> PromoteResult:
    """Publish the canonical assessment/review only when everything is ready.

    Gates: assessment evaluates ready against the
    adjacent review, review hash matches the assessment core hash, schema validity, and
    path confinement. Reusing an immutable ID with different content is rejected.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    # Every researched ticker earns a canonical assessment, not only the one being bought.
    # A cycle that buys nothing still produced the judgment that says why, and the
    # Investment Proposal binds each case to a stored immutable assessment.
    _require_primary_research_ticker(
        workspace, ticker, action="promote", gate=gate, db_path=db_path
    )

    manifest_asof = _parse_date(str(manifest.get("research_as_of")), label="manifest as_of")
    case = _read_case(workspace, ticker, manifest_asof)
    assessment_errors, review_errors = _case_eligibility(case, now=now)
    if assessment_errors or review_errors:
        raise ResearchWorkspaceDataError("; ".join([*assessment_errors, *review_errors]))
    document, review = case.assessment, case.review
    assert document is not None
    assert review is not None
    core_hash = assessment_core_hash(document)
    identity_path = _research_ticker_dir(workspace, ticker) / "assessment-publication.yaml"
    identity = _load_mapping(identity_path, label="assessment publication")
    if set(identity) != {"assessment_id", "supersedes_id"}:
        raise ResearchWorkspaceDataError("assessment publication identity fields differ")
    resolved_assessment_id = identity["assessment_id"]
    if not isinstance(resolved_assessment_id, str):
        raise ResearchWorkspaceDataError("assessment publication ID must be a string")
    if assessment_id is not None and assessment_id != resolved_assessment_id:
        raise ResearchWorkspaceConflictError("assessment ID differs from the scaffolded identity")
    saved_supersedes = identity["supersedes_id"]
    if saved_supersedes is not None and not isinstance(saved_supersedes, str):
        raise ResearchWorkspaceDataError("assessment supersedes ID must be a string or null")
    if supersedes_id is not None and supersedes_id != saved_supersedes:
        raise ResearchWorkspaceConflictError("supersedes ID differs from the scaffolded identity")
    try:
        AssessmentStoreService(db_path, clock=lambda: now).publish_reviewed_assessment(
            resolved_assessment_id,
            case.assessment_payload,
            case.review_payload,
            supersedes_id=saved_supersedes,
        )
    except ResearchConflictError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    except (ResearchValidationError, ValidationError) as error:
        raise ResearchWorkspaceDataError(str(error)) from error
    OperationService(db_path).record_assessment(
        str(manifest["operation_id"]), resolved_assessment_id, now=now
    )
    return PromoteResult(
        assessment_id=resolved_assessment_id,
        review_id=review.review_id,
        assessment_sha256=core_hash,
    )
