"""企業・保有の調査入力を揃え、workspaceを準備する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from math import isfinite
from pathlib import Path

from baibai_engine.foundation.repository_layout import ER_LEVEL_CALIBRATION_CONTEXT_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api.er_calibration_context import (
    candidate_er_band_context,
    load_er_calibration_context,
)
from baibai_engine.research.operation.models import OperationPayload, OperationSession
from baibai_engine.research.operation.research_binding import research_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace.admission import (
    ResearchPreparation,
    ResearchSetAdmissionBinding,
    _resolve_research_triage,
)
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _write_workspace_file,
)
from baibai_engine.research.workspace.status import _write_status


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
