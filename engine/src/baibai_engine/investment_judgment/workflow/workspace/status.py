"""workspaceの進捗と発行可否を読み取る。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.independent_review.contracts import (
    AssessmentReview,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    load_reviewed_assessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.workflow.operation.progress import (
    ResearchOutcome,
    progress_binding,
    require_coverage,
)
from baibai_engine.investment_judgment.workflow.selection import selection_targets
from baibai_engine.investment_judgment.workflow.workspace.admission import (
    read_workspace_operation,
    validate_workspace_selection,
)
from baibai_engine.investment_judgment.workflow.workspace.files import (
    _load_mapping,
)
from baibai_engine.investment_judgment.workflow.workspace.validation import (
    _case_eligibility,
    _read_case,
    _validate_editable_drafts,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only

# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def compute_status(
    workspace: Path, *, db_path: Path | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Read per-case publication and current remaining work without canonical writes.

    The Operation's fixed selection and inputs bind the subject. Current holdings
    gate new authored work; they do not change an exact saved publication's status.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    operation = read_workspace_operation(manifest, db_path=db_path)
    selection = validate_workspace_selection(manifest, operation=operation)
    progress = progress_binding(operation.payload)
    completed_at = operation.completed_at
    operation_status = operation.status
    del operation  # Older Operations carry the whole market; release it before projecting inputs.
    _validate_editable_drafts(workspace, manifest, gate=selection)
    targets = selection_targets(selection)
    require_coverage(targets, progress.outcomes, complete=False)
    recorded = {o.ticker: o for o in progress.outcomes}
    holding = manifest.get("holding_ticker")
    cases = []
    instant = now or datetime.now(JST)
    with closing(connect_read_only(database_path(db_path))) as connection:
        connection.execute("BEGIN")
        for target in targets:
            if holding is not None and holding != target.ticker:
                continue
            case_workspace = (
                workspace / "holdings" / target.ticker
                if target.purpose == "holding" and holding is None
                else workspace
            )
            cases.append(
                _case_status(
                    connection,
                    case_workspace,
                    target.ticker,
                    selection.research_as_of,
                    outcome=recorded.get(target.ticker),
                    now=instant,
                    completed_at=completed_at,
                )
            )
    next_action = next((row["next_action"] for row in cases if row["next_action"]), None)
    return {
        "workspace_status": (
            "no_research"
            if not cases
            else "published"
            if all(row["status"] == "published" for row in cases)
            else "incomplete"
        ),
        "cases": cases,
        "next_action": next_action,
        "operation_status": operation_status,
        "request_scope": selection.request.scope,
        "selection_summary": selection.selection_summary,
    }


def _case_status(
    connection: sqlite3.Connection,
    workspace: Path,
    ticker: str,
    asof: date,
    *,
    outcome: ResearchOutcome | None,
    now: datetime,
    completed_at: datetime | None,
) -> dict[str, object]:
    case = _read_case(workspace, ticker, asof)
    # Local edits still need their own review. Only a clean workspace reads the bound result
    # in lieu of those files; a same-ticker latest pair is not this Operation's decision.
    if not case.has_authored_files and outcome is not None:
        result: dict[str, object] = {
            "ticker": ticker,
            "status_basis": "recorded_outcome",
            "recorded_outcome": outcome.model_dump(mode="json"),
            "assessment_id": None,
            "assessment_validation_errors": [],
            "review_validation_errors": [],
            "next_action": None,
        }
        if outcome.status == "unfinished":
            return {**result, "status": "recorded_unfinished"}
        try:
            pair = require_assessment_ref(connection, outcome.assessment_ref)
            recorded_by = min(now, completed_at) if completed_at else now
            if pair.document.input_snapshot.ticker != ticker or pair.published_at > recorded_by:
                raise ValueError("recorded Assessment differs from the target or postdates status")
        except ValueError as error:
            return {
                **result,
                "status": "recorded_outcome_unreadable",
                "assessment_validation_errors": [str(error)],
                "next_action": f"{ticker}: verify the recorded Assessment and Review reference",
            }
        return {
            **result,
            "status": "published",
            "assessment_id": pair.assessment_id,
            "assessment_as_of": pair.document.input_snapshot.as_of.isoformat(),
        }
    assessment_id = None
    if not case.assessment_errors and not case.review_errors:
        assert case.assessment is not None
        assert case.review is not None
        assessment_id = _published_case(connection, case.assessment, case.review)
    # Publication is a historical fact; current eligibility only guides unpublished work.
    assessment_errors, review_errors = (
        (case.assessment_errors, case.review_errors)
        if assessment_id is not None
        else _case_eligibility(case, now=now)
    )
    status = (
        "incomplete"
        if assessment_errors
        else "ready_for_review"
        if review_errors
        else "ready_for_promotion"
    )
    next_action = None
    if assessment_id is not None:
        status = "published"
        assert case.review is not None
        if (
            outcome is None
            or outcome.status != "reviewed"
            or outcome.assessment_ref.assessment_id != assessment_id
            or outcome.assessment_ref.review_id != case.review.review_id
            or outcome.assessment_ref.core_sha256 != case.review.reviewed_assessment_sha256
        ):
            status = "published_not_bound"
            if completed_at is None:
                next_action = (
                    f"{ticker}: compare the published pair with this Operation's question and "
                    "recorded outcome; if adopting it, bind with operation record-assessment, "
                    "do not republish"
                )
    elif completed_at is None:
        next_action = _next_action(status, ticker)
    return {
        "ticker": ticker,
        "status": status,
        "status_basis": "workspace_files",
        # A local publication can exist without being the Operation's chosen pair.
        "recorded_outcome": outcome.model_dump(mode="json") if outcome is not None else None,
        "assessment_id": assessment_id,
        "assessment_validation_errors": assessment_errors,
        "review_validation_errors": review_errors,
        "next_action": next_action,
    }


def _published_case(
    connection: sqlite3.Connection, assessment: StockAssessmentDocument, review: AssessmentReview
) -> str | None:
    """Show Research publication only for this exact assessment core and authored review."""
    rows = connection.execute(
        "SELECT t.assessment_id FROM stock_assessment t "
        "JOIN stock_assessment_review r ON r.assessment_id=t.assessment_id "
        "WHERE t.core_sha256=? AND r.review_id=?",
        (assessment_core_hash(assessment), review.review_id),
    ).fetchall()
    for row in rows:
        pair = load_reviewed_assessment(connection, str(row["assessment_id"]))
        if pair.document == assessment and pair.review == review:
            return pair.assessment_id
    return None


def _next_action(workspace_status: str, ticker: str) -> str:
    match workspace_status:
        case "incomplete":
            return f"{ticker}: complete the assessment"
        case "ready_for_review":
            return f"{ticker}: complete an independent Assessment Review for the current assessment"
        case _:
            return f"{ticker}: publish with investment-judgment stock-assessment-promote"
