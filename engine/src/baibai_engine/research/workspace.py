"""調査工程の admission、local draft と Reviewed Thesis 公開を産む。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path
from typing import get_args

import yaml
from pydantic import BaseModel, ValidationError

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.repository_layout import ER_LEVEL_CALIBRATION_CONTEXT_PATH
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.operation.models import OperationPayload, OperationSession
from baibai_engine.operation.research_binding import research_binding
from baibai_engine.operation.service import OperationService
from baibai_engine.position.ledger import (
    replay_events_through,
)
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api.er_calibration_context import (
    candidate_er_band_context,
    load_er_calibration_context,
)
from baibai_engine.read_api.research_triage import (
    current_research_triage,
    research_triage_payload_hash,
)

from .market_close_source import (
    UnadjustedCloseObservation,
    read_unadjusted_close,
)
from .thesis import (
    ThesisDocument,
    ThesisError,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    load_thesis,
    thesis_core_hash,
)
from .thesis_store import (
    ResearchConflictError,
    ResearchValidationError,
    ThesisStoreService,
    latest_thesis_id,
    load_reviewed_thesis,
)

_THESIS_DRAFT_HEADER = "# 企業評価 draft。未確認は理由付き unresolved とし、一次資料で補う。\n"
_REVIEW_DRAFT_HEADER = "# 独立検算の値を入力する。作者の値をコピーしない。\n"


class ResearchWorkspaceError(Exception):
    """Base error carrying the CLI exit code for the failure class."""

    exit_code = 3


class ResearchWorkspaceDataError(ResearchWorkspaceError):
    """Missing source / schema / hash makes the request unprocessable."""

    exit_code = 3


class ResearchWorkspaceConflictError(ResearchWorkspaceError):
    """Output collision, input hash drift, or path-confinement violation."""

    exit_code = 4


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump_yaml(payload: object) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _write_workspace_file(path: Path, payload: object) -> str:
    text = _dump_yaml(payload)
    write_text_atomic(path, text)
    return _sha256_text(text)


def _load_mapping(path: Path, *, label: str) -> dict[str, object]:
    if not path.exists():
        raise ResearchWorkspaceDataError(f"{label} not found: {path}")
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ResearchWorkspaceDataError(f"{label} root must be a mapping: {path}")
    return dict(raw)


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResearchPreparation:
    workspace: Path
    actionable: bool
    admissible_count: int
    review_set_size: int
    research_triage_id: str | None = None
    admissible_research_tickers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchSetAdmissionBinding:
    """The canonical ResearchTriage judgment a research workspace is bound to.

    ``admissible_tickers`` is the Research Triage's ``research`` set in judgment order: the exact
    set a human may admit into the Research Set. The human still chooses
    which of those to research; what they cannot do is widen the set from the
    workspace, because a canonical thesis is produced per ticker and the Gate
    already decided this cycle's answer for each one.
    """

    research_triage_id: str
    asof: date
    payload_hash: str
    screening_rules_hash: str
    admissible_tickers: tuple[str, ...]
    triage_decision_by_ticker: dict[str, str]
    review_set_entries: tuple[dict[str, object], ...]


def _research_triage_decisions(
    triage: ResearchTriage,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Read one research_triage payload as a ResearchTriage judgment, or fail closed."""

    decisions: dict[str, str] = {}
    for entry in triage.entries:
        decisions[entry.ticker] = entry.decision
    return triage.admissible_research_tickers(), decisions


def _review_set_entries_from_triage(triage: ResearchTriage) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for entry in sorted(triage.entries, key=lambda item: item.ticker):
        snapshot = entry.candidate_snapshot
        rows.append(
            {
                "ticker": entry.ticker,
                "name": snapshot.name,
                "sector_33": snapshot.sector_33,
                "nominations": [item.model_dump(mode="json") for item in snapshot.nominations],
                "research_priority": entry.priority,
                "analysis": snapshot.analysis.model_dump(mode="json"),
            }
        )
    return tuple(rows)


def _resolve_research_triage(
    *,
    db_path: Path | None,
    expected_research_triage_id: str,
) -> ResearchSetAdmissionBinding:
    """Resolve one current judgment directly from the application DB."""

    try:
        triage = current_research_triage(database_path(db_path), expected_research_triage_id)
        if triage is None:
            raise ResearchWorkspaceDataError(
                f"research triage is unavailable: {expected_research_triage_id}"
            )
    except (RuntimeError, ValueError, ValidationError) as exc:
        raise ResearchWorkspaceDataError(str(exc)) from exc
    admissible_tickers, decisions = _research_triage_decisions(triage)
    return ResearchSetAdmissionBinding(
        research_triage_id=triage.research_triage_id,
        asof=triage.as_of,
        payload_hash=research_triage_payload_hash(triage),
        screening_rules_hash=triage.screening_rules_hash,
        admissible_tickers=admissible_tickers,
        triage_decision_by_ticker=decisions,
        review_set_entries=_review_set_entries_from_triage(triage),
    )


def _verify_research_triage(
    manifest: Mapping[str, object], inputs: Mapping[str, object], *, db_path: Path | None
) -> ResearchSetAdmissionBinding:
    """Re-resolve the bound judgment on every workspace gate, from the pinned inputs.

    The manifest names the judgment so an operator can read it, but the identity is
    re-derived from the canonical Research Triage each time, so a hand-written ticker list is never
    what a gate reads. Renaming the bound Research Triage stops matching the judgment;
    an edit cannot admit a ticker that canonical Research Triage marked ``skip``.
    """

    binding = inputs.get("research_triage")
    if not isinstance(binding, Mapping):
        raise ResearchWorkspaceDataError(
            "workspace has no ResearchTriage binding; rebuild it with "
            "`research prepare --research-triage-id <RESEARCH_TRIAGE_ID> --force`"
        )
    recorded_research_triage_id = _nonempty_string(
        binding.get("research_triage_id"),
        label="manifest.inputs.research_triage.research_triage_id",
    )
    recorded_payload_hash = _nonempty_string(
        binding.get("payload_sha256"),
        label="manifest.inputs.research_triage.payload_sha256",
    )
    try:
        gate = _resolve_research_triage(
            db_path=db_path,
            expected_research_triage_id=recorded_research_triage_id,
        )
    except ResearchWorkspaceDataError as error:
        raise ResearchWorkspaceConflictError(
            "workspace ResearchTriage binding does not match the canonical research_triage "
            f"{recorded_research_triage_id}; rebuild the workspace with "
            f"`research prepare --force` ({error})"
        ) from error
    if gate.payload_hash != recorded_payload_hash or gate.asof.isoformat() != str(
        manifest.get("as_of")
    ):
        raise ResearchWorkspaceConflictError(
            "workspace ResearchTriage binding does not match the canonical research_triage "
            f"{gate.research_triage_id}; rebuild the workspace with `research prepare --force`"
        )
    return gate


def prepare_workspace(
    *,
    research_triage_id: str,
    db_path: Path | None,
    workspace: Path,
    research_set: Sequence[str] = (),
    started_at: datetime | None = None,
    force: bool = False,
) -> ResearchPreparation:
    """Build a workspace from one self-contained ResearchTriage and the ledger.

    The workspace keeps the whole Review Set as comparison context but may only
    admit the Research Triage's ``research`` tickers into the Research Set.
    Holdings/reservations stay ledger annotations, never hard exclusions. The human
    selection is fixed at this Research start boundary; an empty set is normal and
    does not create an Operation.
    """
    gate = _resolve_research_triage(
        db_path=db_path,
        expected_research_triage_id=research_triage_id,
    )
    asof = gate.asof
    review_set_entries = list(gate.review_set_entries)
    selected = tuple(research_set)
    if len(selected) != len(set(selected)):
        raise ResearchWorkspaceDataError("Research Set tickers must be unique")
    forbidden = [ticker for ticker in selected if ticker not in gate.admissible_tickers]
    if forbidden:
        raise ResearchWorkspaceDataError(
            f"Research Set includes {', '.join(forbidden)}, which "
            f"{gate.research_triage_id} did not mark research"
        )
    if selected:
        _existing_research_operation(
            gate=gate,
            research_set=selected,
            db_path=db_path,
        )
    ledger, _ = LedgerStoreService(db_path).load_with_head()
    snapshot = replay_events_through(ledger.events, ledger.as_of)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    held = {
        ticker for ticker, lots in snapshot.lots.items() if any(lot.quantity > 0 for lot in lots)
    }
    reserved = {reservation.ticker for reservation in snapshot.active_reservations.values()}
    annotated = [
        _annotate_review_set_entry(
            row, held=held, reserved=reserved, decisions=gate.triage_decision_by_ticker
        )
        for row in review_set_entries
    ]
    admissible_count = len(gate.admissible_tickers)

    workspace_doc: dict[str, object] = {
        "as_of": asof.isoformat(),
        "review_set_entries": annotated,
        "research_set": list(selected),
    }
    er_context = _load_er_distribution_context(
        screening_rules_hash=gate.screening_rules_hash,
        candidates=annotated,
        asof=asof,
    )
    workspace_doc["er_realized_distribution_context"] = er_context

    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "research-workspace.yaml", workspace_doc)

    manifest_inputs: dict[str, object] = {
        # A readable record of the binding, not its authority: every gate re-resolves
        # these tickers from the stored research_triage before trusting them.
        "research_triage": {
            "research_triage_id": gate.research_triage_id,
            "payload_sha256": gate.payload_hash,
        },
    }
    manifest = {
        "as_of": asof.isoformat(),
        "inputs": manifest_inputs,
        "research_set": list(selected),
    }
    _write_workspace_file(manifest_path, manifest)
    if selected:
        _start_research_operation(
            gate=gate,
            research_set=selected,
            db_path=db_path,
            started_at=started_at or datetime.now(JST),
        )
    _write_status(workspace, db_path=db_path)
    return ResearchPreparation(
        workspace=workspace,
        actionable=bool(selected),
        admissible_count=admissible_count,
        review_set_size=len(annotated),
        research_triage_id=gate.research_triage_id,
        admissible_research_tickers=gate.admissible_tickers,
    )


def _start_research_operation(
    *,
    gate: ResearchSetAdmissionBinding,
    research_set: tuple[str, ...],
    db_path: Path | None,
    started_at: datetime,
) -> OperationSession:
    """Start one Operation only after the human Research Set is fixed."""

    service = OperationService(db_path)
    active = _existing_research_operation(
        gate=gate,
        research_set=research_set,
        db_path=db_path,
    )
    if active is not None:
        return active
    payload = OperationPayload(
        checkpoint="Human Research Set confirmed; Fundamental Research started",
        artifacts=(
            {
                "kind": "research_triage",
                "ref": gate.research_triage_id,
                "research_set": list(research_set),
            },
        ),
        canonical_refs=(gate.research_triage_id,),
        human_confirmation={
            "request": "confirm the Research Set",
            "result": ",".join(research_set),
        },
        next="perform Fundamental Research for the confirmed Research Set",
    )
    return service.start(
        session_kind="capital-allocation",
        as_of=gate.asof,
        started_at=started_at,
        payload=payload,
    )


def _existing_research_operation(
    *,
    gate: ResearchSetAdmissionBinding,
    research_set: tuple[str, ...],
    db_path: Path | None,
) -> OperationSession | None:
    """Return the exact active Research start, or reject a conflicting session."""

    active = OperationService(db_path).active()
    if active is not None:
        try:
            binding = research_binding(active.payload)
        except ValueError:
            binding = None
        if active.session_kind == "capital-allocation" and binding == (
            gate.research_triage_id,
            frozenset(research_set),
        ):
            return active
        raise ResearchWorkspaceConflictError(
            f"active operation already exists: {active.operation_id}"
        )
    return None


def prepare_holding_workspace(
    *,
    asof: date,
    db_path: Path | None,
    ticker: str,
    workspace: Path,
    force: bool = False,
) -> ResearchPreparation:
    """Build a one-ticker research workspace for an actual open holding.

    Position Review bypasses Review Set publication because the canonical ledger is
    the source of its research target. Each authoring gate confirms that the ticker
    remains held; no Research Triage or portfolio-wide price prerequisite is fabricated.
    """
    ledger, _ = LedgerStoreService(db_path).load_with_head()
    snapshot = replay_events_through(ledger.events, ledger.as_of)
    quantity = sum(lot.quantity for lot in snapshot.lots.get(ticker, []))
    if quantity <= 0:
        raise ResearchWorkspaceDataError("position-prepare requires a current holding")
    sector = snapshot.metadata[ticker][0]

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    subject = [
        {
            "rank": 1,
            "ticker": ticker,
            "sector": sector,
            "quantity": quantity,
            "portfolio_annotation": "held",
        }
    ]
    workspace_doc = {
        "as_of": asof.isoformat(),
        "position_review_subject": subject,
        "research_set": [ticker],
    }
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "research-workspace.yaml", workspace_doc)
    manifest = {
        "purpose": "position_review",
        "holding_ticker": ticker,
        "as_of": asof.isoformat(),
    }
    _write_workspace_file(manifest_path, manifest)
    _write_status(workspace, db_path=db_path)
    return ResearchPreparation(
        workspace=workspace,
        actionable=True,
        admissible_count=1,
        review_set_size=1,
    )


def _annotate_review_set_entry(
    row: Mapping[str, object],
    *,
    held: set[str],
    reserved: set[str],
    decisions: Mapping[str, str],
) -> dict[str, object]:
    ticker = str(row.get("ticker") or "")
    annotation = _portfolio_annotation(ticker, held=held, reserved=reserved)
    output = dict(row)
    output["portfolio_annotation"] = annotation
    output["research_triage_decision"] = decisions[ticker]
    return output


def _portfolio_annotation(ticker: str, *, held: set[str], reserved: set[str]) -> str:
    is_held = ticker in held
    is_reserved = ticker in reserved
    if is_held and is_reserved:
        return "held_and_reserved"
    if is_held:
        return "held"
    if is_reserved:
        return "reserved"
    return "unheld"


def _load_er_distribution_context(
    *,
    screening_rules_hash: str,
    candidates: Sequence[Mapping[str, object]],
    asof: date,
) -> dict[str, object]:
    versions: set[str] = set()
    for candidate in candidates:
        analysis = candidate.get("analysis")
        expected_return = analysis.get("expected_return") if isinstance(analysis, Mapping) else None
        if isinstance(expected_return, Mapping) and isinstance(
            expected_return.get("er_model_version"), str
        ):
            versions.add(str(expected_return["er_model_version"]))
    if len(versions) != 1:
        return {"status": "unavailable", "reason": "er_model_identity_mismatch"}
    er_model_version = next(iter(versions))
    path = ER_LEVEL_CALIBRATION_CONTEXT_PATH
    loaded = load_er_calibration_context(
        path,
        expected_rules_hash=screening_rules_hash,
        expected_er_model_version=er_model_version,
        as_of=asof,
    )
    artifact = loaded.artifact
    if artifact is None:
        return {"status": "unavailable", "reason": loaded.unavailable_reason}
    candidate_context: dict[str, object] = {}
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "")
        analysis = candidate.get("analysis")
        expected_return = analysis.get("expected_return") if isinstance(analysis, Mapping) else None
        if not isinstance(expected_return, Mapping):
            candidate_context[ticker] = {"status": "unavailable", "reason": "candidate_er_missing"}
            continue
        if expected_return.get("er_model_version") != artifact.er_model_version:
            candidate_context[ticker] = {
                "status": "unavailable",
                "reason": "er_model_identity_mismatch",
            }
            continue
        er_value = expected_return.get("er_annual")
        if (
            isinstance(er_value, bool)
            or not isinstance(er_value, int | float)
            or not isfinite(float(er_value))
        ):
            candidate_context[ticker] = {"status": "unavailable", "reason": "candidate_er_missing"}
            continue
        er_annual = float(er_value)
        candidate_context[ticker] = {
            "status": "historical_context_only",
            "er_annual": er_annual,
            "horizons": list(candidate_er_band_context(artifact, er_annual)),
        }
    context: dict[str, object] = {
        "status": "historical_context_only",
        "artifact": path.as_posix(),
        "generated_at": artifact.generated_at.isoformat(),
        "valid_through": artifact.valid_through.isoformat(),
        "screening_rules_hash": screening_rules_hash,
        "er_model_version": artifact.er_model_version,
        "primary_realized_basis": artifact.primary_realized_basis,
        "primary_weighting": "ticker_asof_observation_equal",
        "interpretation": "historical distribution; not an individual security forecast",
        "candidates": candidate_context,
    }
    return context


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
    status["research_triage"] = _research_triage_view(gate)
    return status


def _research_triage_view(gate: ResearchSetAdmissionBinding | None) -> dict[str, object]:
    """Name the judgment bounding this workspace and what it lets a human admit."""

    if gate is None:
        return {
            "purpose": "position_review",
            "research_triage_id": None,
            "admissible_research_tickers": [],
        }
    return {
        "purpose": "fundamental_research",
        "research_triage_id": gate.research_triage_id,
        "admissible_research_tickers": list(gate.admissible_tickers),
    }


def _draft_status(
    workspace: Path, manifest: Mapping[str, object], *, db_path: Path | None, now: datetime
) -> dict[str, object]:
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    document = _load_mapping(workspace / "research-workspace.yaml", label="research workspace")
    research_set = document.get("research_set")
    if not isinstance(research_set, list):
        raise ResearchWorkspaceDataError("workspace research_set must be an array of tickers")
    cases = [
        _case_status(workspace, str(ticker), asof, db_path=db_path, now=now)
        for ticker in research_set
    ]
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


def _require_primary_research_ticker(
    workspace: Path,
    ticker: str,
    *,
    action: str,
    gate: ResearchSetAdmissionBinding | None,
    db_path: Path | None,
) -> None:
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    research_set = research_workspace.get("research_set")
    if not isinstance(research_set, list) or ticker not in research_set:
        raise ResearchWorkspaceDataError(
            f"cannot {action} for {ticker}: ticker is not in the Research Set"
        )
    if gate is not None and ticker not in gate.admissible_tickers:
        raise ResearchWorkspaceDataError(
            f"cannot {action} for {ticker}: {gate.research_triage_id} did not mark it research"
        )
    if gate is not None:
        active = OperationService(db_path).active()
        try:
            if active is None or active.session_kind != "capital-allocation":
                raise ValueError("active capital-allocation Operation is required")
            if research_binding(active.payload) != (
                gate.research_triage_id,
                frozenset(research_set),
            ):
                raise ValueError("workspace Research Set differs from the active Operation binding")
        except ValueError as error:
            raise ResearchWorkspaceConflictError(str(error)) from error


# --------------------------------------------------------------------------- #
# thesis-scaffold
# --------------------------------------------------------------------------- #


def scaffold_thesis(
    *,
    workspace: Path,
    ticker: str,
    sqlite_path: Path,
    target_session: date,
    retrieved_at: datetime,
    db_path: Path | None = None,
    force: bool = False,
    from_thesis_id: str | None = None,
) -> dict[str, object]:
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(
        workspace, ticker, action="scaffold research", gate=gate, db_path=db_path
    )
    path = _research_ticker_dir(workspace, ticker) / "thesis-draft.yaml"
    if path.exists() and not force:
        raise ResearchWorkspaceConflictError("thesis draft exists; use --force")
    quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=retrieved_at)
    if from_thesis_id is not None:
        with closing(connect_read_only(database_path(db_path))) as connection:
            pair = load_reviewed_thesis(connection, from_thesis_id)
            if (
                pair.document.input_snapshot.ticker != ticker
                or latest_thesis_id(connection, ticker) != from_thesis_id
            ):
                raise ResearchWorkspaceConflictError(
                    "refresh must use latest revision of this ticker"
                )
        payload = pair.document.model_dump(mode="json")
        payload["input_snapshot"]["as_of"] = target_session.isoformat()
        payload["judgment"]["proposed_at"] = retrieved_at.isoformat()
        # Keep original sources, facts and projections; a fresh independent hash review is required.
    else:
        sources: list[dict[str, object]] = []
        facts: list[dict[str, object]] = []
        if quote is not None:
            sources.append(
                {
                    "source_id": "market_close",
                    "ticker": ticker,
                    "source_tier": "local_data",
                    "provider": "jquants",
                    "dataset": "jquants_daily_bars",
                    "retrieved_at": retrieved_at.isoformat(),
                    "as_of": quote.price_as_of.isoformat(),
                    "used_for": "market_price",
                }
            )
            facts.append(
                {
                    "fact_id": "market_price_close",
                    "fact_kind": "market_price",
                    "value": quote.close_yen,
                    "unit": "JPY_per_share",
                    "as_of": quote.price_as_of.isoformat(),
                    "source_ids": ["market_close"],
                    "observed_at": datetime.combine(
                        quote.price_as_of, time(15, 30), tzinfo=JST
                    ).isoformat(),
                    "price_basis": "last_close_unadjusted",
                }
            )
        payload = {
            "schema_version": 4,
            "input_snapshot": {
                "snapshot_version": 1,
                "producer_model_version": "research-v4",
                "ticker": ticker,
                "company_name": None,
                "sector": None,
                "common_factors": [],
                "as_of": target_session.isoformat(),
                "sources": sources,
                "facts": facts,
            },
            "derived": {"metrics": []},
            "valuation": {
                "status": "unresolved",
                "market_price_fact_id": "market_price_close" if quote else None,
                "horizon_months": None,
                "required_annual_return_pct": None,
                "base": None,
                "downside": None,
                "unresolved_reason": "企業価値未評価" if quote else "quote未取得・企業価値未評価",
            },
            "investment_case": {
                "explanation": None,
                "invalidation_conditions": [],
                "status": "uncertain",
                "status_reason": None,
                "source_ids": [],
            },
            "permanent_loss_risks": [],
            "judgment": {
                "disposition": "defer",
                "proposed_at": retrieved_at.isoformat(),
                "strongest_countercase": None,
            },
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, _THESIS_DRAFT_HEADER + _dump_yaml(payload))
    return {
        "thesis_draft": str(path),
        "price_as_of": None if quote is None else quote.price_as_of.isoformat(),
        "close_yen": None if quote is None else quote.close_yen,
        "from_thesis_id": from_thesis_id,
    }


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ResearchWorkspaceDataError(f"{label} must be a mapping")
    return value


def _nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchWorkspaceDataError(f"{label} must be a non-empty string")
    return value


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ResearchWorkspaceDataError(f"{label} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ResearchWorkspaceDataError(f"{label} must be finite") from error
    if not isfinite(number):
        raise ResearchWorkspaceDataError(f"{label} must be finite")
    return number


def scaffold_review(
    *, workspace: Path, ticker: str, db_path: Path | None = None, force: bool = False
) -> dict[str, object]:
    """Write the Thesis Review draft bound to the current thesis core hash.

    The review author is a distinct role from the thesis author; this scaffold only
    lays out the recalculation slots and never produces the review conclusions. The
    bound ``reviewed_thesis_sha256`` is what lets ``promote`` detect a stale review.
    The file lands under the stable name the thesis already references, so promote
    resolves it without an intervening copy.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(
        workspace, ticker, action="scaffold review", gate=gate, db_path=db_path
    )
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    ticker_dir = _research_ticker_dir(workspace, ticker)
    thesis_path = ticker_dir / "thesis-draft.yaml"
    if not thesis_path.exists():
        raise ResearchWorkspaceDataError(f"thesis draft not found for {ticker}: {thesis_path}")

    core_hash = _thesis_core_hash_if_valid(thesis_path)
    review_path = _review_draft_path(workspace, ticker, asof)
    if review_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"review draft already exists (use --force to regenerate): {review_path}"
        )

    review_draft = {
        "review_id": None,
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": None,
        "reviewed_at": None,
        "reviewed_thesis_sha256": core_hash,
        "primary_source_check": None,
        "checked_source_ids": [],
        "recalculated_projections": [
            {
                "name": name,
                "terminal_value_per_share_yen": None,
                "cash_distribution_per_share_yen": None,
            }
            for name in ("base", "downside")
        ]
        if load_thesis(thesis_path).valuation.status == "resolved"
        else [],
        "strongest_countercase": None,
        "nonmaterial_unknown_reason": None,
    }
    header = _REVIEW_DRAFT_HEADER + _enum_field_header(ThesisReview)
    write_text_atomic(review_path, header + _dump_yaml(review_draft))
    return {"review_draft": str(review_path), "reviewed_thesis_sha256": core_hash}


def _enum_field_header(model: type[BaseModel]) -> str:
    """List the draft's closed-vocabulary fields and their allowed values.

    A scaffolded `null` carries no type, so a reviewer filling in `primary_source_check`
    or `alternative_candidate_check` cannot tell a three-way verdict from free prose
    until validation rejects the draft. The values are read off the model rather than
    written down, so a vocabulary change reaches the draft without a second edit.
    """

    lines = []
    for name, field in model.model_fields.items():
        choices = get_args(field.annotation)
        # A single-valued Literal is not a choice — the scaffold already writes it.
        if len(choices) < 2 or not all(isinstance(choice, str) for choice in choices):
            continue
        lines.append(f"# - {name}: {' | '.join(str(choice) for choice in choices)}\n")
    return "".join(lines)


def _thesis_core_hash_if_valid(thesis_path: Path) -> str | None:
    # A fully-filled draft hashes to its core; an incomplete draft cannot be hashed
    # yet, so the review is bound once the thesis is complete.
    try:
        document = load_thesis(thesis_path)
    except ThesisError:
        return None
    return thesis_core_hash(document)


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


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_date(value: str, *, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ResearchWorkspaceDataError(f"invalid {label}: {value}") from error


__all__ = [
    "PromoteResult",
    "ResearchPreparation",
    "ResearchWorkspaceConflictError",
    "ResearchWorkspaceDataError",
    "ResearchWorkspaceError",
    "UnadjustedCloseObservation",
    "compute_status",
    "prepare_workspace",
    "promote",
    "read_unadjusted_close",
    "scaffold_review",
    "scaffold_thesis",
]
