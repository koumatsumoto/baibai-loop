"""Thesis・Review・保存済み入力のbindingを照合する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from baibai_engine.appdb.json import canonical_json
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    thesis_core_hash,
)
from baibai_engine.research.workspace.admission import (
    ResearchSetAdmissionBinding,
    _verify_research_triage,
)
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _dict_list,
    _load_mapping,
    _string_or_none,
)


def _verify_external_inputs(
    manifest: Mapping[str, object], *, db_path: Path | None = None
) -> ResearchSetAdmissionBinding | None:
    """Re-check the canonical subject for this workspace's purpose.

    Returning the gate rather than reading it later is what keeps the two in step:
    a caller cannot validate drafts without having first proved, against the store,
    which tickers the Gate admits.

    Position Review has no Research Triage — the ledger is its source — so it gets ``None``, and
    its holding subject is re-proved against that ledger here. Both purposes
    therefore prove their subject against a store: without that, declaring
    ``position_review`` in the manifest would be a way to opt out of the Gate.
    """

    purpose = str(manifest.get("purpose") or "fundamental_research")
    if purpose not in {"fundamental_research", "position_review"}:
        raise ResearchWorkspaceDataError("invalid workspace purpose")
    if purpose == "position_review":
        _require_holding_subject(manifest, db_path=db_path)
        return None
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ResearchWorkspaceDataError("manifest is missing external input hashes")
    return _verify_research_triage(manifest, inputs, db_path=db_path)


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
    workspace: Path, manifest: Mapping[str, object], *, gate: ResearchSetAdmissionBinding | None
) -> None:
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    manifest_asof = str(manifest.get("as_of") or "")
    if research_workspace.get("as_of") != manifest_asof:
        raise ResearchWorkspaceDataError("workspace draft as_of does not match manifest")

    purpose = str(manifest.get("purpose") or "fundamental_research")
    if purpose == "position_review":
        _validate_position_review_drafts(research_workspace, manifest)
        return
    if purpose != "fundamental_research":
        raise ResearchWorkspaceDataError(f"manifest purpose is invalid: {purpose}")

    workspace_entries = _dict_list(research_workspace.get("review_set_entries"))
    review_set_tickers = tuple(str(row.get("ticker") or "") for row in workspace_entries)
    expected_entries = list(gate.review_set_entries) if gate is not None else []
    machine_entries = [
        {
            key: row.get(key)
            for key in (
                "ticker",
                "name",
                "sector_33",
                "nominations",
                "research_priority",
                "analysis",
            )
        }
        for row in workspace_entries
    ]
    if canonical_json(machine_entries) != canonical_json(expected_entries):
        raise ResearchWorkspaceConflictError(
            "workspace Review Set Entry snapshot differs from canonical Research Triage; "
            "rebuild the workspace with `research prepare --force`"
        )
    research_set = research_workspace.get("research_set")
    if not isinstance(research_set, list) or not all(
        isinstance(ticker, str) and ticker for ticker in research_set
    ):
        raise ResearchWorkspaceDataError("workspace research_set must be an array of tickers")
    research_set_tickers = [str(ticker) for ticker in research_set]
    if len(research_set_tickers) != len(set(research_set_tickers)) or any(
        ticker not in review_set_tickers for ticker in research_set_tickers
    ):
        raise ResearchWorkspaceDataError("workspace research_set is invalid")
    manifest_research_set = manifest.get("research_set")
    if manifest_research_set != research_set:
        raise ResearchWorkspaceConflictError(
            "workspace Research Set differs from the human-confirmed set; "
            "rebuild the workspace with `research prepare --force`"
        )
    # Narrowing guard, not a reachable state: `_verify_external_inputs` reads purpose
    # from this same manifest and returns None only for Position Review, which left
    # above. A missing binding is refused there, with the command that rebuilds it.
    if gate is None:  # pragma: no cover - unreachable by construction
        raise ResearchWorkspaceDataError(
            "Fundamental Research workspace has no ResearchTriage binding"
        )
    forbidden = [ticker for ticker in research_set_tickers if ticker not in gate.admissible_tickers]
    if forbidden:
        raise ResearchWorkspaceDataError(
            f"workspace Research Set includes {', '.join(forbidden)}, which "
            f"{gate.research_triage_id} did not mark research"
        )


def _validate_position_review_drafts(
    research_workspace: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    ticker = _string_or_none(manifest.get("holding_ticker"))
    if ticker is None:
        raise ResearchWorkspaceDataError("position-review manifest is missing holding_ticker")
    subject_tickers = [
        str(row.get("ticker") or "")
        for row in _dict_list(research_workspace.get("position_review_subject"))
    ]
    research_set = research_workspace.get("research_set")
    if subject_tickers != [ticker] or research_set != [ticker]:
        raise ResearchWorkspaceDataError(
            "position-review workspace must keep its holding subject fixed"
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
