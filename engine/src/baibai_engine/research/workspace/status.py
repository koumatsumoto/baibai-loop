"""workspaceの進捗と発行可否を読み取る。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    thesis_core_hash,
)
from baibai_engine.research.thesis_store import (
    load_reviewed_thesis,
)
from baibai_engine.research.workspace.admission import (
    workspace_selection,
)
from baibai_engine.research.workspace.files import (
    _dump_yaml,
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


def _write_status(workspace: Path, *, db_path: Path | None) -> dict[str, object]:
    status = compute_status(workspace, db_path=db_path)
    write_text_atomic(workspace / "status.yaml", _dump_yaml(status))
    return status


def compute_status(
    workspace: Path, *, db_path: Path | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Read per-case publication and current remaining work without canonical writes.

    Research Triage binds the subject. Ledger annotations and calibration context
    describe prepare time; Position Review rechecks its current ledger subject.
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
    thesis_id = None
    if not case.thesis_errors and not case.review_errors:
        assert case.thesis is not None
        assert case.review is not None
        thesis_id = _published_case(case.thesis, case.review, db_path=db_path)
    # Publication is a historical fact; current eligibility only guides unpublished work.
    thesis_errors, review_errors = (
        (case.thesis_errors, case.review_errors)
        if thesis_id is not None
        else _case_eligibility(case, now=now)
    )
    status = (
        "incomplete"
        if thesis_errors
        else "ready_for_review"
        if review_errors
        else "ready_for_promotion"
    )
    if thesis_id is not None:
        status = "published"
    return {
        "ticker": ticker,
        "status": status,
        "thesis_id": thesis_id,
        "thesis_validation_errors": thesis_errors,
        "review_validation_errors": review_errors,
        "next_action": None if status == "published" else _next_action(status, ticker),
    }


def _published_case(
    thesis: ThesisDocument, review: ThesisReview, *, db_path: Path | None
) -> str | None:
    """Show Research publication only for this exact thesis core and authored review."""
    with closing(connect_read_only(database_path(db_path))) as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT t.thesis_id FROM thesis t JOIN thesis_review r ON r.thesis_id=t.thesis_id "
            "WHERE t.core_sha256=? AND r.review_id=?",
            (thesis_core_hash(thesis), review.review_id),
        ).fetchall()
        for row in rows:
            pair = load_reviewed_thesis(connection, str(row["thesis_id"]))
            if pair.document == thesis and pair.review == review:
                return pair.thesis_id
    return None


def _next_action(workspace_status: str, ticker: str) -> str:
    match workspace_status:
        case "incomplete":
            return f"{ticker}: complete the thesis"
        case "ready_for_review":
            return f"{ticker}: complete an independent Thesis Review for the current thesis"
        case _:
            return f"{ticker}: publish the reviewed thesis with research promote"
