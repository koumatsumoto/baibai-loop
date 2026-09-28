"""選択済みResearch Setと候補のexact bindingを検証する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.appdb.paths import database_path
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.read_api.research_triage import (
    current_research_triage,
    research_triage_payload_hash,
)
from baibai_engine.research.operation.research_binding import research_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _load_mapping,
    _nonempty_string,
)

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
