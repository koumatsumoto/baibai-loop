"""検証済みのAssessmentとReviewをimmutable publicationへ進める。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.investment_judgment.stock_assessment.store import (
    ResearchConflictError,
    ResearchValidationError,
    publish_reviewed_assessment,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalConflictError
from baibai_engine.investment_judgment.workflow.operation.models import OperationStatus
from baibai_engine.investment_judgment.workflow.operation.service import (
    OperationConflictError,
    OperationService,
)
from baibai_engine.investment_judgment.workflow.workspace.admission import (
    require_authoring_selection,
)
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    ResearchWorkspaceError,
    _load_mapping,
    _parse_date,
)
from baibai_engine.investment_judgment.workflow.workspace.validation import (
    _read_case,
    _research_ticker_dir,
    _validate_editable_drafts,
)
from baibai_engine.storage.sqlite.application.write import connect_existing

# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PromoteResult:
    assessment_id: str
    review_id: str
    assessment_sha256: str


class PromoteBindingError(ResearchWorkspaceError):
    """The exact pair committed, but this invocation did not confirm its Operation binding."""

    def __init__(
        self,
        published: PromoteResult,
        *,
        operation_id: str,
        operation_status: OperationStatus | None,
        cause: Exception,
    ) -> None:
        super().__init__(str(cause))
        self.published = published
        self.operation_id = operation_id
        self.operation_status = operation_status
        self.exit_code = (
            4 if isinstance(cause, (OperationConflictError, ProposalConflictError)) else 3
        )


def promote(
    *,
    workspace: Path,
    ticker: str,
    db_path: Path | None,
    now: datetime,
) -> PromoteResult:
    """Publish the canonical assessment/review only when everything is ready.

    Pair admission and write share the App transaction. Binding the committed pair is a
    separate Operation change; its failure does not erase or replace an immutable publication.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = require_authoring_selection(manifest, ticker=ticker, action="promote", db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    del gate
    # Every researched ticker earns a canonical assessment, not only the one being bought.
    # A cycle that buys nothing still produced the judgment that says why, and the
    # Trade Proposal binds each case to a stored immutable assessment.

    manifest_asof = _parse_date(str(manifest.get("research_as_of")), label="manifest as_of")
    case = _read_case(workspace, ticker, manifest_asof)
    # The writer validates a new pair; an exact retry reads its historical publication.
    assessment_errors, review_errors = case.assessment_errors, case.review_errors
    if assessment_errors or review_errors:
        raise ResearchWorkspaceDataError("; ".join([*assessment_errors, *review_errors]))
    identity_path = _research_ticker_dir(workspace, ticker) / "assessment-publication.yaml"
    identity = _load_mapping(identity_path, label="assessment publication")
    if set(identity) != {"assessment_id", "supersedes_id"}:
        raise ResearchWorkspaceDataError("assessment publication identity fields differ")
    resolved_assessment_id = identity["assessment_id"]
    if not isinstance(resolved_assessment_id, str):
        raise ResearchWorkspaceDataError("assessment publication ID must be a string")
    saved_supersedes = identity["supersedes_id"]
    if saved_supersedes is not None and not isinstance(saved_supersedes, str):
        raise ResearchWorkspaceDataError("assessment supersedes ID must be a string or null")
    try:
        with closing(connect_existing(db_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # The manifest also pins the Selection checked against the local files above.
            # Recheck it and current holding eligibility on the connection that will publish.
            require_authoring_selection(
                manifest, ticker=ticker, action="promote", connection=connection
            )
            pair = publish_reviewed_assessment(
                connection,
                resolved_assessment_id,
                case.assessment_payload,
                case.review_payload,
                published_at=now,
                supersedes_id=saved_supersedes,
            )
    except ResearchConflictError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    except (ResearchValidationError, ValidationError) as error:
        raise ResearchWorkspaceDataError(str(error)) from error
    published = PromoteResult(
        assessment_id=pair.assessment_id,
        review_id=pair.review.review_id,
        assessment_sha256=pair.core_sha256,
    )
    operation_id = str(manifest["operation_id"])
    operations = OperationService(db_path)
    try:
        operations.record_assessment(operation_id, pair.assessment_id, now=now)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        # A late conflict or unavailable read must not hide the committed pair's identity.
        try:
            status = operations.get(operation_id).status
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            status = None
        raise PromoteBindingError(
            published, operation_id=operation_id, operation_status=status, cause=error
        ) from error
    return published
