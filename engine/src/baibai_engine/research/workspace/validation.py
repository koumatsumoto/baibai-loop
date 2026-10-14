"""Thesis・Review・保存済み入力のbindingを照合する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.inputs import project_research_inputs
from baibai_engine.research.selection import ResearchSelection
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    thesis_core_hash,
)
from baibai_engine.research.workspace.admission import (
    workspace_selection,
)
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _load_mapping,
    _string_or_none,
)
from baibai_engine.storage.application.json import canonical_json


def _verify_external_inputs(
    manifest: Mapping[str, object], *, db_path: Path | None = None
) -> ResearchSelection:
    selection = workspace_selection(manifest, db_path=db_path)
    if manifest.get("holding_ticker") is not None:
        _require_holding_subject(manifest, db_path=db_path)
    return selection


def _require_holding_subject(manifest: Mapping[str, object], *, db_path: Path | None) -> None:
    """Confirm that the editable workspace still names an actual holding."""

    ticker = _string_or_none(manifest.get("holding_ticker"))
    if ticker is None:
        raise ResearchWorkspaceDataError("position-review manifest is missing holding_ticker")
    ledger, _ = LedgerStoreService(db_path).load_with_head()
    state = replay_events_through(ledger.events, ledger.as_of)
    if sum(lot.quantity for lot in state.lots.get(ticker, [])) <= 0:
        raise ResearchWorkspaceConflictError("position workspace requires a current holding")


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
    if canonical_json(actual) != canonical_json(project_research_inputs(gate)):
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


def _review_filename(*, asof: date, ticker: str) -> str:
    """Return the stable Thesis Review filename for a research ticker.

    The local draft uses a stable review filename, so
    scaffold, status, and promote all address the same path and no
    copy step stands between the draft and the gate.
    """

    return f"{asof:%Y-%m-%d}-{ticker}-decision-review.yaml"


def _review_draft_path(workspace: Path, ticker: str, asof: date) -> Path:
    return _research_ticker_dir(workspace, ticker) / _review_filename(asof=asof, ticker=ticker)


# --------------------------------------------------------------------------- #
# plan-limit
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _CaseDraft:
    """One read of authored inputs, retaining raw payloads for the canonical writer."""

    thesis_payload: dict[str, object]
    review_payload: dict[str, object]
    thesis: ThesisDocument | None
    review: ThesisReview | None
    thesis_errors: list[str]
    review_errors: list[str]


def _read_case(workspace: Path, ticker: str, asof: date) -> _CaseDraft:
    directory = _research_ticker_dir(workspace, ticker)
    thesis_payload: dict[str, object] = {}
    review_payload: dict[str, object] = {}
    thesis, review = None, None
    thesis_errors: list[str] = []
    review_errors: list[str] = []
    thesis_path = directory / "thesis-draft.yaml"
    review_path = _review_draft_path(workspace, ticker, asof)
    if not thesis_path.exists():
        thesis_errors.append("thesis draft missing")
    else:
        try:
            thesis_payload = _load_mapping(thesis_path, label="thesis")
            thesis = ThesisDocument.model_validate(thesis_payload)
        except (OSError, yaml.YAMLError, ValueError, ResearchWorkspaceDataError) as error:
            thesis_errors.append(str(error))
    if not review_path.exists():
        review_errors.append("review draft missing")
    else:
        try:
            review_payload = _load_mapping(review_path, label="Thesis Review")
            review = ThesisReview.model_validate(review_payload)
        except (OSError, yaml.YAMLError, ValueError, ResearchWorkspaceDataError) as error:
            review_errors.append(str(error))
    if thesis is not None:
        if thesis.input_snapshot.ticker != ticker:
            thesis_errors.append(
                f"thesis ticker {thesis.input_snapshot.ticker} does not match case {ticker}"
            )
        if thesis.input_snapshot.as_of < asof:
            thesis_errors.append("thesis predates admitted Research Set")
        if review is not None and review.reviewed_thesis_sha256 != thesis_core_hash(thesis):
            review_errors.append("review is stale for the current thesis core hash")
    return _CaseDraft(thesis_payload, review_payload, thesis, review, thesis_errors, review_errors)


def _case_eligibility(case: _CaseDraft, *, now: datetime) -> tuple[list[str], list[str]]:
    thesis_errors = list(case.thesis_errors)
    if not thesis_errors:
        assert case.thesis is not None
        result = evaluate_thesis(
            case.thesis,
            review=None if case.review_errors else case.review,
            now=now,
            identity=UnpublishedThesis.DRAFT,
        )
        if result.decision_readiness != "ready" and result.thesis_status != "review_required":
            thesis_errors.extend(result.errors)
    return thesis_errors, case.review_errors
