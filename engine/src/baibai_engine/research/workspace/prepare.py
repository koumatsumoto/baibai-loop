"""企業・保有の調査入力を揃え、workspaceを準備する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path

from baibai_engine.appdb.paths import database_path
from baibai_engine.foundation.repository_layout import (
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    MACRO_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
)
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api.er_calibration_context import (
    candidate_er_band_context,
    load_er_calibration_context,
)
from baibai_engine.research.inputs import research_inputs
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.progress import (
    ResearchSelection,
    ResearchTarget,
    selection_binding,
    selection_sha256,
)
from baibai_engine.research.operation.research_binding import research_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace.admission import (
    ResearchPreparation,
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
    research_triage_id: str | None,
    db_path: Path | None,
    workspace: Path,
    request: str,
    research_set: Sequence[str] = (),
    holding_tickers: Sequence[str] = (),
    asof: date | None = None,
    started_at: datetime | None = None,
    sqlite_path: Path = MARKET_DB_PATH,
    runs_path: Path = RUNS_DB_PATH,
    macro_path: Path = MACRO_DB_PATH,
    force: bool = False,
) -> ResearchPreparation:
    """Explicitly start/reuse one fixed combined selection, including a normal empty set."""
    now = started_at or datetime.now(JST)
    gate = (
        None
        if research_triage_id is None
        else _resolve_research_triage(
            db_path=db_path, expected_research_triage_id=research_triage_id
        )
    )
    resolved_asof = gate.asof if gate is not None else asof
    if resolved_asof is None:
        raise ResearchWorkspaceDataError("--asof is required without Research Triage")
    selected = tuple(research_set)
    if selected and (gate is None or not set(selected) <= set(gate.admissible_tickers)):
        raise ResearchWorkspaceDataError(
            "Research Triage did not mark research for one of the new targets"
        )
    selection = ResearchSelection(
        request=request,
        research_triage_id=research_triage_id,
        targets=tuple(ResearchTarget(ticker=ticker, purpose="new") for ticker in selected)
        + tuple(ResearchTarget(ticker=ticker, purpose="holding") for ticker in holding_tickers),
    )
    service = OperationService(db_path, sqlite_path=sqlite_path)
    active = service.active()
    if active is not None:
        recorded = selection_binding(active.payload)
        if recorded is None:
            old_triage, old_tickers = research_binding(active.payload)
            if old_triage != selection.research_triage_id or old_tickers != {
                item.ticker for item in selection.targets
            }:
                raise ResearchWorkspaceConflictError(
                    f"active Operation has a different fixed selection: {active.operation_id}"
                )
            legacy_confirmation = (
                ()
                if active.payload.human_confirmation is None
                else (
                    {
                        "kind": "legacy_human_confirmation",
                        "record": active.payload.human_confirmation.model_dump(mode="json"),
                    },
                )
            )
            payload = active.payload.model_copy(
                update={
                    "artifacts": (
                        *active.payload.artifacts,
                        *legacy_confirmation,
                        selection.model_dump(mode="json"),
                    ),
                    "human_confirmation": None,
                }
            )
            active = service.checkpoint(active.operation_id, payload)
        elif recorded.research_triage_id != selection.research_triage_id or set(
            recorded.targets
        ) != set(selection.targets):
            raise ResearchWorkspaceConflictError(
                f"active operation already exists: {active.operation_id}"
            )
        else:
            selection = recorded
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )
    ledger, _ = LedgerStoreService(db_path).load_with_head()
    snapshot = replay_events_through(ledger.events, ledger.as_of)
    held = {
        ticker for ticker, lots in snapshot.lots.items() if any(lot.quantity > 0 for lot in lots)
    }
    reserved = {reservation.ticker for reservation in snapshot.active_reservations.values()}
    if set(selected) & held or not set(holding_tickers) <= held:
        raise ResearchWorkspaceDataError("ticker purposes must agree with current holdings")
    operation = active or service.start(
        session_kind="capital-allocation",
        as_of=resolved_asof,
        started_at=now,
        payload=OperationPayload(
            checkpoint="Explicit research started; fixed AI selection recorded",
            artifacts=(selection.model_dump(mode="json"),),
            canonical_refs=() if research_triage_id is None else (research_triage_id,),
            next="research only the fixed targets; record completed or unfinished outcomes",
        ),
    )
    annotated = (
        []
        if gate is None
        else [
            _annotate_review_set_entry(
                row, held=held, reserved=reserved, decisions=gate.triage_decision_by_ticker
            )
            for row in gate.review_set_entries
        ]
    )
    workspace_doc: dict[str, object] = {
        "as_of": resolved_asof.isoformat(),
        "review_set_entries": annotated,
        "research_set": list(selected),
    }
    if gate is not None:
        workspace_doc["er_realized_distribution_context"] = _load_er_distribution_context(
            screening_rules_hash=gate.screening_rules_hash, candidates=annotated, asof=resolved_asof
        )
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "research-workspace.yaml", workspace_doc)
    inputs = (
        {}
        if gate is None
        else {
            "research_triage": {
                "research_triage_id": gate.research_triage_id,
                "payload_sha256": gate.payload_hash,
            }
        }
    )
    _write_workspace_file(
        manifest_path,
        {
            "as_of": resolved_asof.isoformat(),
            "inputs": inputs,
            "research_set": list(selected),
            "operation_id": operation.operation_id,
            "selection_sha256": selection_sha256(selection),
        },
    )
    cutoff = min(now, datetime.combine(resolved_asof, time.max, JST))
    _write_workspace_file(
        workspace / "domain-inputs.yaml",
        research_inputs(
            tickers=tuple(item.ticker for item in selection.targets),
            as_of=resolved_asof,
            cutoff=cutoff,
            market_path=sqlite_path,
            runs_path=runs_path,
            macro_path=macro_path,
            app_path=database_path(db_path),
            run_revision_id=None if gate is None else gate.run_revision_id,
        ),
    )
    for ticker in holding_tickers:
        prepare_holding_workspace(
            asof=resolved_asof,
            db_path=db_path,
            ticker=ticker,
            workspace=workspace / "holdings" / ticker,
            started_at=now,
            force=force,
            sqlite_path=sqlite_path,
            runs_path=runs_path,
            macro_path=macro_path,
        )
    _write_status(workspace, db_path=db_path)
    return ResearchPreparation(
        workspace=workspace,
        actionable=bool(selection.targets),
        admissible_count=0 if gate is None else len(gate.admissible_tickers),
        review_set_size=len(annotated),
        research_triage_id=research_triage_id,
        admissible_research_tickers=() if gate is None else gate.admissible_tickers,
        operation_id=operation.operation_id,
    )


def prepare_holding_workspace(
    *,
    asof: date,
    db_path: Path | None,
    ticker: str,
    workspace: Path,
    request: str | None = None,
    started_at: datetime | None = None,
    sqlite_path: Path = MARKET_DB_PATH,
    runs_path: Path = RUNS_DB_PATH,
    macro_path: Path = MACRO_DB_PATH,
    force: bool = False,
) -> ResearchPreparation:
    """Attach a holding workspace to the same finite Operation; never use new admission."""
    now = started_at or datetime.now(JST)
    ledger, _ = LedgerStoreService(db_path).load_with_head()
    snapshot = replay_events_through(ledger.events, ledger.as_of)
    quantity = sum(lot.quantity for lot in snapshot.lots.get(ticker, []))
    if quantity <= 0:
        raise ResearchWorkspaceDataError("position-prepare requires a current holding")
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )
    service = OperationService(db_path, sqlite_path=sqlite_path)
    operation = service.active()
    selection = None if operation is None else selection_binding(operation.payload)
    if operation is not None:
        if (
            selection is None
            and operation.session_kind == "position-review"
            and operation.ticker == ticker
            and request is not None
        ):
            selection = ResearchSelection(
                request=request, targets=(ResearchTarget(ticker=ticker, purpose="holding"),)
            )
            operation = service.checkpoint(
                operation.operation_id,
                operation.payload.model_copy(
                    update={
                        "artifacts": (
                            *operation.payload.artifacts,
                            selection.model_dump(mode="json"),
                        )
                    }
                ),
            )
        if (
            selection is None
            or ResearchTarget(ticker=ticker, purpose="holding") not in selection.targets
        ):
            raise ResearchWorkspaceConflictError(
                "holding ticker is outside the fixed Operation selection"
            )
    else:
        if request is None:
            raise ResearchWorkspaceDataError("position-prepare requires an explicit start request")
        selection = ResearchSelection(
            request=request, targets=(ResearchTarget(ticker=ticker, purpose="holding"),)
        )
        operation = service.start(
            session_kind="position-review",
            as_of=asof,
            started_at=now,
            ticker=ticker,
            payload=OperationPayload(
                checkpoint="Explicit holding research started",
                artifacts=(selection.model_dump(mode="json"),),
                next="validate a holding proposal or record unfinished research",
            ),
        )
    assert selection is not None
    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(
        workspace / "research-workspace.yaml",
        {
            "as_of": asof.isoformat(),
            "position_review_subject": [
                {
                    "rank": 1,
                    "ticker": ticker,
                    "sector": snapshot.metadata[ticker][0],
                    "quantity": quantity,
                    "portfolio_annotation": "held",
                }
            ],
            "research_set": [ticker],
        },
    )
    _write_workspace_file(
        manifest_path,
        {
            "purpose": "position_review",
            "holding_ticker": ticker,
            "as_of": asof.isoformat(),
            "operation_id": operation.operation_id,
            "selection_sha256": selection_sha256(selection),
        },
    )
    _write_workspace_file(
        workspace / "domain-inputs.yaml",
        research_inputs(
            tickers=(ticker,),
            as_of=asof,
            cutoff=min(now, datetime.combine(asof, time.max, JST)),
            market_path=sqlite_path,
            runs_path=runs_path,
            macro_path=macro_path,
            app_path=database_path(db_path),
        ),
    )
    _write_status(workspace, db_path=db_path)
    return ResearchPreparation(
        workspace=workspace,
        actionable=True,
        admissible_count=1,
        review_set_size=1,
        operation_id=operation.operation_id,
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
