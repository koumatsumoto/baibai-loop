"""workspaceの進捗と発行可否を読み取る。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.foundation.time import JST
from baibai_engine.research.assessment import (
    AssessmentReview,
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.research.assessment_store import (
    load_reviewed_assessment,
)
from baibai_engine.research.workspace.admission import (
    workspace_selection,
)
from baibai_engine.research.workspace.files import (
    _load_mapping,
    _parse_date,
)
from baibai_engine.research.workspace.validation import (
    _case_eligibility,
    _read_case,
    _validate_editable_drafts,
    _verify_external_inputs,
)
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only

# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def compute_status(
    workspace: Path, *, db_path: Path | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Read per-case publication and current remaining work without canonical writes.

    The Operation's fixed selection binds the subject. Holding workspaces recheck
    the current ledger subject without replacing the saved Research inputs.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    status = _draft_status(workspace, manifest, db_path=db_path, now=now or datetime.now(JST))
    selection = gate
    status["request_scope"] = selection.request.scope
    status["selection_summary"] = selection.selection_summary
    return status


def _draft_status(
    workspace: Path, manifest: Mapping[str, object], *, db_path: Path | None, now: datetime
) -> dict[str, object]:
    asof = _parse_date(str(manifest.get("research_as_of")), label="manifest research_as_of")
    selection = workspace_selection(manifest, db_path=db_path)
    holding = manifest.get("holding_ticker")
    cases = []
    for target in selection.targets:
        if holding is not None and holding != target.ticker:
            continue
        case_workspace = (
            workspace / "holdings" / target.ticker
            if target.purpose == "holding" and holding is None
            else workspace
        )
        cases.append(_case_status(case_workspace, target.ticker, asof, db_path=db_path, now=now))
    outstanding = [case for case in cases if case["status"] != "published"]
    return {
        "workspace_status": (
            "no_research" if not cases else "published" if not outstanding else "incomplete"
        ),
        "cases": cases,
        "next_action": outstanding[0]["next_action"] if outstanding else None,
    }


def _case_status(
    workspace: Path, ticker: str, asof: date, *, db_path: Path | None, now: datetime
) -> dict[str, object]:
    case = _read_case(workspace, ticker, asof)
    assessment_id = None
    if not case.assessment_errors and not case.review_errors:
        assert case.assessment is not None
        assert case.review is not None
        assessment_id = _published_case(case.assessment, case.review, db_path=db_path)
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
    if assessment_id is not None:
        status = "published"
    return {
        "ticker": ticker,
        "status": status,
        "assessment_id": assessment_id,
        "assessment_validation_errors": assessment_errors,
        "review_validation_errors": review_errors,
        "next_action": None if status == "published" else _next_action(status, ticker),
    }


def _published_case(
    assessment: StockAssessmentDocument, review: AssessmentReview, *, db_path: Path | None
) -> str | None:
    """Show Research publication only for this exact assessment core and authored review."""
    with closing(connect_read_only(database_path(db_path))) as connection:
        connection.execute("BEGIN")
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
            return f"{ticker}: publish the reviewed assessment with research assessment-promote"
