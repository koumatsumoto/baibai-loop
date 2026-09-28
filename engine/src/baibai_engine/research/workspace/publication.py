"""検証済みのThesisとReviewをimmutable publicationへ進める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.research.thesis import (
    thesis_core_hash,
)
from baibai_engine.research.thesis_store import (
    ResearchConflictError,
    ResearchValidationError,
    ThesisStoreService,
)
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
    _validate_editable_drafts,
    _verify_external_inputs,
)

# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PromoteResult:
    thesis_id: str
    review_id: str
    thesis_sha256: str


def promote(
    *,
    workspace: Path,
    ticker: str,
    db_path: Path | None,
    thesis_id: str | None,
    supersedes_id: str | None,
    now: datetime,
) -> PromoteResult:
    """Publish the canonical thesis/review only when everything is ready.

    Gates: thesis evaluates ready against the
    adjacent review, review hash matches the thesis core hash, schema validity, and
    path confinement. Reusing an immutable ID with different content is rejected.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    # Every researched ticker earns a canonical thesis, not only the one being bought.
    # A cycle that buys nothing still produced the judgment that says why, and the
    # Capital Allocation Assessment binds each case to a stored immutable thesis.
    _require_primary_research_ticker(
        workspace, ticker, action="promote", gate=gate, db_path=db_path
    )

    manifest_asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    case = _read_case(workspace, ticker, manifest_asof)
    thesis_errors, review_errors = _case_eligibility(case, now=now)
    if thesis_errors or review_errors:
        raise ResearchWorkspaceDataError("; ".join([*thesis_errors, *review_errors]))
    document, review = case.thesis, case.review
    assert document is not None
    assert review is not None
    core_hash = thesis_core_hash(document)
    resolved_thesis_id = thesis_id or (
        f"thesis-{document.input_snapshot.as_of:%Y%m%d}-{ticker}-{review.review_id}"
    )
    try:
        ThesisStoreService(db_path, clock=lambda: now).publish_reviewed_thesis(
            resolved_thesis_id,
            case.thesis_payload,
            case.review_payload,
            supersedes_id=supersedes_id,
        )
    except ResearchConflictError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    except (ResearchValidationError, ValidationError) as error:
        raise ResearchWorkspaceDataError(str(error)) from error
    return PromoteResult(
        thesis_id=resolved_thesis_id,
        review_id=review.review_id,
        thesis_sha256=core_hash,
    )
