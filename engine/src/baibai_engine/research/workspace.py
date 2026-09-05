"""Fundamental Research authoring: prepare a workspace, scaffold thesis/review drafts, and
derive a planning-only limit from the previous business day's raw close.

This module is pure logic behind ``baibai-engine research``. It never submits an
order, never asserts fill probability, and never reads a realtime quote. The
canonical price basis for a limit is the JPX store's latest complete business-day
*raw/unadjusted* close before the target session; adjusted series are for past
comparison only and are never used for a limit.

Planning boundary:

- Investment value is decided before budget rounding. The 20-30万円 guide is a
  sizing annotation for a normal position. Reduced sizing is exactly one board lot.
- ``promote`` is the only command that publishes a canonical thesis/review;
  ``prepare`` also starts or resumes the exact human-admitted Research Operation.
  Draft commands write to the caller-selected workspace; status reads canonical publication.
- ``defer`` and ``no_allocation`` are normal investment judgments and exit 0.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
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
from baibai_engine.operation.service import OperationService
from baibai_engine.position.ledger import (
    PortfolioSnapshot,
    reconcile_portfolio,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api.er_calibration_context import (
    candidate_er_band_context,
    load_er_calibration_context,
)
from baibai_engine.read_api.research_triage import (
    current_research_triage,
    research_triage_payload_hash,
)

from .decimal_number import decimal_to_number
from .entry_price_policy import EntryPricePolicyError, maximum_acceptable_entry_price
from .market_close_source import (
    UnadjustedCloseObservation,
    read_prior_session_unadjusted_close,
)
from .portfolio_exposure import (
    calculate_prospective_portfolio_exposure,
    planned_order_cash_warnings,
    portfolio_annotations,
)
from .store import ResearchConflictError, ResearchStoreService, ResearchValidationError
from .thesis import (
    ScreeningEstimate,
    ThesisDocument,
    ThesisError,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    load_thesis,
    load_thesis_review,
    thesis_core_hash,
)

BOARD_LOT: int = PORTFOLIO_POLICY["order_constraints"]["board_lot"]
# 対象 sizing 帯 (20-30万円 / 100株 = ¥2000-3000/株) はちょうど JPX 現物の ¥1 tick 帯。
# max acceptable price の ceiling floor 丸めはこの帯で正確な ¥1 を使う。
PLANNING_TICK_SIZE_YEN = Decimal("1")

# 一次情報の research checklist。scaffold が pending で出し、AI が一次情報を
# 確認して埋める。source.corporate_action は adjustment_factor 未解決なら blocked。
CHECKLIST_IDS: tuple[str, ...] = (
    "source.latest_results",
    "source.financial_position",
    "source.cash_flow",
    "source.share_count_and_dilution",
    "source.customer_concentration",
    "source.structural_decline",
    "source.management_accounting_warning",
    "source.corporate_action",
    "scenario.bear_3y_5y",
    "scenario.base_3y_5y",
    "scenario.bull_3y_5y",
    "valuation.fair_value_and_required_cagr",
    "judgment.strongest_countercase",
)

# 観測 trailing multiple の fact ID。thesis の 5y base break-even check は、この ID
# または `trailing-per-` 前置の valuation_metric / ratio fact だけを読む。
TRAILING_MULTIPLE_FACT_ID = "trailing-per"
# scaffold が観測できない値の sentinel。数値でないので evaluate が必ず拒否し、
# 未記入のまま promote へ抜けない。
TODO_PLACEHOLDER = "TODO"

# draft はそのまま人間 / AI が編集するので、gate の非自明な要求は本文の隣に置く。
# YAML comment なので load 結果にも core hash にも影響しない。
_THESIS_DRAFT_HEADER = """\
# thesis draft — null と TODO を一次情報で埋める。scaffold が置いた構造は変えない。
# - retrieved_at / proposed_at は JST の現在時刻以前。同日でも先の時刻は future-dated
#   として拒否される。source 取得より前の proposed_at も拒否される。
# - facts[trailing-per] は 5y base break-even check が読む観測 trailing multiple。
#   TODO を比率へ置き換え、利益側の一次 source を source_ids へ追加する。
# - independent_review_ref は review-scaffold が書き出す隣接ファイル名。変更しない。
# - 引用符なしの散文（assumption / summary / countercase 等）に「: 」を書かない。
#   YAML が mapping と解釈して load が落ちる。区切りには「 — 」を使う。
"""

_REVIEW_DRAFT_HEADER = """\
# Thesis Review draft — thesis author と別 role が埋める。
# - reviewed_at は JST の現在時刻以前。
# - reviewed_thesis_sha256 は生成時点の thesis core hash に束縛される。thesis を
#   編集したら review-scaffold --force で作り直す（古い hash のままだと promote が拒否）。
# - 引用符なしの散文（strongest_countercase 等）に「: 」を書かない。YAML が mapping と
#   解釈して load が落ちる。区切りには「 — 」を使う。
"""


class ResearchWorkspaceError(Exception):
    """Base error carrying the CLI exit code for the failure class."""

    exit_code = 3


class ResearchWorkspaceDataError(ResearchWorkspaceError):
    """Missing source / checklist / schema / hash makes the request unprocessable."""

    exit_code = 3


class ResearchWorkspaceConflictError(ResearchWorkspaceError):
    """Output collision, input hash drift, or path-confinement violation."""

    exit_code = 4


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    recorded_tickers = binding.get("admissible_research_tickers")
    if not isinstance(recorded_tickers, Sequence) or isinstance(recorded_tickers, str | bytes):
        raise ResearchWorkspaceDataError(
            "manifest.inputs.research_triage.admissible_research_tickers must be an array"
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
    if (
        gate.payload_hash != recorded_payload_hash
        or gate.asof.isoformat() != str(manifest.get("as_of"))
        or list(gate.admissible_tickers) != [str(value) for value in recorded_tickers]
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
    snapshot, append_head = _load_snapshot(db_path)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    held = {holding.ticker for holding in snapshot.holdings}
    reserved = {reservation.ticker for reservation in snapshot.active_reservations}
    annotated = [
        _annotate_review_set_entry(
            row, held=held, reserved=reserved, decisions=gate.triage_decision_by_ticker
        )
        for row in review_set_entries
    ]
    admissible_count = len(gate.admissible_tickers)

    workspace_doc = {
        "as_of": asof.isoformat(),
        "review_set_entries": annotated,
        "admissible_research_tickers": list(gate.admissible_tickers),
        "research_set": list(selected),
        "admissible_count": admissible_count,
    }
    er_context, er_context_ref = _load_er_distribution_context(
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
            "admissible_research_tickers": list(gate.admissible_tickers),
        },
        "ledger": {
            "entity_id": "portfolio-ledger",
            "append_head": append_head,
        },
    }
    if er_context_ref is not None:
        manifest_inputs["er_distribution_context"] = er_context_ref
    manifest = {
        "as_of": asof.isoformat(),
        "inputs": manifest_inputs,
        "research_set": list(selected),
        "rules": {"admissible_count": admissible_count},
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
        selected: tuple[str, ...] | None = None
        for artifact in active.payload.artifacts:
            values = artifact.get("research_set")
            if (
                artifact.get("kind") == "research_triage"
                and artifact.get("ref") == gate.research_triage_id
                and isinstance(values, list)
                and all(isinstance(value, str) for value in values)
            ):
                selected = tuple(str(value) for value in values)
                break
        if active.session_kind == "capital-allocation" and selected == research_set:
            return active
        raise ResearchWorkspaceConflictError(
            f"active operation already exists: {active.operation_id}"
        )
    return None


def _holding_subject_problem(snapshot: PortfolioSnapshot, *, ticker: str, asof: date) -> str | None:
    """Say why ``ticker`` cannot be a position-review subject at ``asof``, or ``None``.

    Position Review has no Research Triage because the ledger is its source, so this is
    the whole of what makes a subject legitimate — and it has to be one statement,
    because ``position-prepare`` and every later gate must not be able to disagree
    about it. The as-of half is not decoration: the canonical Position Review is
    built against the ledger's own price observation, so a review that runs at any
    other as-of cannot become one.
    """

    holding = next((item for item in snapshot.holdings if item.ticker == ticker), None)
    if holding is None:
        return f"{ticker} is not an open holding in the canonical ledger"
    observed_on = holding.market_price_observed_at.date()
    if observed_on != asof:
        return (
            f"the canonical ledger observed {ticker}'s market price on "
            f"{observed_on.isoformat()}, not {asof.isoformat()}"
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
    the source of its research target. The holding ticker and ledger observation as-of
    bind every thesis/review/promotion gate; no Research Triage is fabricated.
    """
    snapshot, append_head = _load_snapshot(db_path)
    problem = _holding_subject_problem(snapshot, ticker=ticker, asof=asof)
    if problem is not None:
        raise ResearchWorkspaceDataError(f"cannot prepare Position Review: {problem}")
    holding = next(item for item in snapshot.holdings if item.ticker == ticker)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    subject = [
        {
            "rank": 1,
            "ticker": holding.ticker,
            "sector": holding.sector,
            "quantity": holding.quantity,
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
        "inputs": {
            "ledger": {
                "entity_id": "portfolio-ledger",
                "append_head": append_head,
            }
        },
        "rules": {},
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
) -> tuple[dict[str, object] | None, dict[str, str] | None]:
    versions: set[str] = set()
    for candidate in candidates:
        analysis = candidate.get("analysis")
        expected_return = analysis.get("expected_return") if isinstance(analysis, Mapping) else None
        if isinstance(expected_return, Mapping) and isinstance(
            expected_return.get("er_model_version"), str
        ):
            versions.add(str(expected_return["er_model_version"]))
    if len(versions) != 1:
        return {"status": "unavailable", "reason": "er_model_identity_mismatch"}, None
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
        return {"status": "unavailable", "reason": loaded.unavailable_reason}, None
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
    return context, {"path": path.as_posix(), "sha256": _sha256_file(path)}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _write_status(workspace: Path, *, db_path: Path | None) -> dict[str, object]:
    status = compute_status(workspace, db_path=db_path)
    write_text_atomic(workspace / "status.yaml", _dump_yaml(status))
    return status


def compute_status(workspace: Path, *, db_path: Path | None = None) -> dict[str, object]:
    """Read per-case publication, remaining work, and prepare-time input drift.

    External inputs remain bound to their prepare-time hashes. Operator-authored
    drafts are editable, but their structure and lineage must remain consistent.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    status = _draft_status(workspace, manifest, db_path=db_path)
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
    workspace: Path, manifest: Mapping[str, object], *, db_path: Path | None
) -> dict[str, object]:
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    document = _load_mapping(workspace / "research-workspace.yaml", label="research workspace")
    research_set = document.get("research_set")
    if not isinstance(research_set, list):
        raise ResearchWorkspaceDataError("workspace research_set must be an array of tickers")
    cases = [_case_status(workspace, str(ticker), asof, db_path=db_path) for ticker in research_set]
    outstanding = [case for case in cases if case["status"] != "published"]
    return {
        "workspace_status": (
            "no_research" if not cases else "published" if not outstanding else "incomplete"
        ),
        "cases": cases,
        "next_action": outstanding[0]["next_action"] if outstanding else None,
    }


def _case_status(
    workspace: Path, ticker: str, asof: date, *, db_path: Path | None
) -> dict[str, object]:
    directory = _research_ticker_dir(workspace, ticker)
    checklist = (
        _load_checklist(workspace, ticker)
        if (directory / "research-checklist.yaml").exists()
        else []
    )
    pending = [
        str(item.get("check_id") or "<missing check_id>")
        for item in checklist
        if item.get("status") not in {"complete", "blocked"}
    ]
    if not checklist:
        pending.append("research checklist missing")
    blocked = [str(item.get("check_id")) for item in checklist if item.get("status") == "blocked"]
    thesis_errors = _thesis_validation_errors(workspace, ticker, asof)
    review_errors = _review_validation_errors(workspace, ticker, asof)
    status = _resolve_workspace_status(
        pending=pending, blocked=blocked, thesis_errors=thesis_errors, review_errors=review_errors
    )
    thesis_id = None
    if status == "ready_for_promotion":
        thesis = load_thesis(directory / "thesis-draft.yaml")
        review = load_thesis_review(_review_draft_path(workspace, ticker, asof))
        thesis_id = _published_case(thesis, review, db_path=db_path)
        if thesis_id is not None:
            status = "published"
    return {
        "ticker": ticker,
        "status": status,
        "thesis_id": thesis_id,
        "completed_checks": sum(item.get("status") == "complete" for item in checklist),
        "pending_checks": pending,
        "blocked_checks": blocked,
        "thesis_validation_errors": thesis_errors,
        "review_validation_errors": review_errors,
        "next_action": None if status == "published" else _next_action(status, ticker),
    }


def _published_case(
    thesis: ThesisDocument, review: ThesisReview, *, db_path: Path | None
) -> str | None:
    """Show Research publication only for this exact thesis core and authored review."""
    with closing(connect_read_only(database_path(db_path))) as connection:
        rows = connection.execute(
            "SELECT t.thesis_id, t.payload AS thesis_payload, r.payload "
            "FROM thesis t JOIN thesis_review r "
            "ON r.thesis_id = t.thesis_id WHERE t.core_sha256 = ? AND r.review_id = ?",
            (thesis_core_hash(thesis), review.review_id),
        ).fetchall()
    for row in rows:
        if (
            json.loads(str(row["payload"])) == review.model_dump(mode="json")
            and ThesisDocument.model_validate(json.loads(str(row["thesis_payload"]))) == thesis
        ):
            return str(row["thesis_id"])
    return None


def _resolve_workspace_status(
    *,
    pending: Sequence[object],
    blocked: Sequence[object],
    thesis_errors: Sequence[str],
    review_errors: Sequence[str],
) -> str:
    if blocked:
        return "deferred"
    if pending or thesis_errors:
        return "incomplete"
    if review_errors:
        return "ready_for_review"
    return "ready_for_promotion"


def _next_action(workspace_status: str, ticker: str) -> str:
    match workspace_status:
        case "deferred":
            return (
                f"{ticker}: resolve blocked evidence or record unknown/defer "
                "and complete the investigation"
            )
        case "incomplete":
            return f"{ticker}: complete the thesis and research checklist"
        case "ready_for_review":
            return f"{ticker}: complete an independent Thesis Review for the current thesis"
        case _:
            return f"{ticker}: publish the reviewed thesis with research promote"


def _verify_external_inputs(
    manifest: Mapping[str, object], *, db_path: Path | None = None
) -> ResearchSetAdmissionBinding | None:
    """Re-check every external input, and return the ResearchTriage that bounds this workspace.

    Returning the gate rather than reading it later is what keeps the two in step:
    a caller cannot validate drafts without having first proved, against the store,
    which tickers the Gate admits.

    Position Review has no Research Triage — the ledger is its source — so it gets ``None``, and
    its subject and as-of are re-proved against that ledger here. Both purposes
    therefore prove their subject against a store: without that, declaring
    ``position_review`` in the manifest would be a way to opt out of the Gate.
    """

    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ResearchWorkspaceDataError("manifest is missing external input hashes")
    purpose = str(manifest.get("purpose") or "fundamental_research")
    required_inputs: tuple[str, ...]
    if purpose == "fundamental_research":
        required_inputs = ("ledger",)
        if "er_distribution_context" in inputs:
            required_inputs += ("er_distribution_context",)
    elif purpose == "position_review":
        required_inputs = ("ledger",)
    else:
        raise ResearchWorkspaceDataError(f"manifest purpose is invalid: {purpose}")
    for name in required_inputs:
        input_ref = inputs.get(name)
        if not isinstance(input_ref, Mapping):
            raise ResearchWorkspaceDataError(f"manifest is missing input hash: {name}")
        if name == "ledger":
            entity_id = input_ref.get("entity_id")
            expected_head = input_ref.get("append_head")
            if entity_id != "portfolio-ledger" or not isinstance(expected_head, int):
                raise ResearchWorkspaceDataError("manifest ledger revision is invalid")
            try:
                current_head = LedgerStoreService(db_path).append_head()
            except (OSError, RuntimeError, ValueError) as error:
                raise ResearchWorkspaceDataError(
                    f"cannot read canonical ledger revision: {error}"
                ) from error
            if current_head != expected_head:
                raise ResearchWorkspaceConflictError(
                    "canonical ledger changed since workspace prepare (append head drift)"
                )
            continue
        path_value = input_ref.get("path")
        expected = input_ref.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise ResearchWorkspaceDataError(f"manifest input ref is invalid: {name}")
        path = Path(path_value)
        if not path.is_file():
            raise ResearchWorkspaceConflictError(f"workspace external input is missing: {name}")
        actual = _sha256_file(path)
        if actual != expected:
            raise ResearchWorkspaceConflictError(
                f"workspace external input changed since prepare (input hash drift): {name}"
            )
    if purpose == "position_review":
        _require_holding_subject(manifest, db_path=db_path)
        return None
    return _verify_research_triage(manifest, inputs, db_path=db_path)


def _require_holding_subject(manifest: Mapping[str, object], *, db_path: Path | None) -> None:
    """Re-prove a position-review workspace's subject against the canonical ledger.

    ``holding-prepare`` proves the subject once, but the manifest recording that
    answer is an editable file, so the purpose would otherwise be a way to research
    an arbitrary ticker at an arbitrary as-of.

    The pinned ``append_head`` does not cover this: it counts ``ledger_event`` rows,
    while market prices live in their own table and are replaced wholesale, so a
    re-applied price draft moves the observation date under an unchanged head. That
    is exactly the drift worth catching — the canonical Position Review is built
    against the ledger's own observation, so a workspace whose price date has moved
    can no longer produce one. Failing here says so before the research is written
    rather than after.
    """

    ticker = _string_or_none(manifest.get("holding_ticker"))
    if ticker is None:
        raise ResearchWorkspaceDataError("position-review manifest is missing holding_ticker")
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    snapshot, _append_head = _load_snapshot(db_path)
    problem = _holding_subject_problem(snapshot, ticker=ticker, asof=asof)
    if problem is not None:
        raise ResearchWorkspaceConflictError(
            f"position-review workspace subject is invalid: {problem}; a Position Review "
            "runs at the as-of its ledger market price was observed, so rebuild at that "
            "date with `research position-prepare --asof <observed> --force` and "
            "regenerate the ticker drafts with `research thesis-scaffold --force` "
            "(market prices only move forward, so the old as-of cannot be restored)"
        )


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
    admissible_count = research_workspace.get("admissible_count")
    if not isinstance(admissible_count, int) or admissible_count < 0:
        raise ResearchWorkspaceDataError("workspace admissible_count is invalid")
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
    if len(research_set_tickers) > admissible_count:
        raise ResearchWorkspaceDataError("workspace Research Set exceeds admissible_count")


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

    The thesis payload carries this name in ``independent_review_ref`` and
    ``plan-limit`` resolves the review by that name from the thesis's own
    directory, so scaffold, status, and promote all address the same path and no
    copy step stands between the draft and the gate.
    """

    return f"{asof:%Y-%m-%d}-{ticker}-decision-review.yaml"


def _review_draft_path(workspace: Path, ticker: str, asof: date) -> Path:
    return _research_ticker_dir(workspace, ticker) / _review_filename(asof=asof, ticker=ticker)


def _require_primary_research_ticker(
    workspace: Path, ticker: str, *, action: str, gate: ResearchSetAdmissionBinding | None
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
) -> dict[str, object]:
    """Write a thesis-draft with observed price facts and a pending checklist.

    AI judgment fields are placeholders; only observed/derived known values are
    filled. A missing raw close is a hard exit_code 3 (no guess from an adjusted
    series). An unresolved corporate action blocks the corporate-action check.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(workspace, ticker, action="scaffold research", gate=gate)
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    purpose = str(manifest.get("purpose") or "fundamental_research")
    screening_estimate: dict[str, object] | None
    transfer_reason: str | None
    if purpose == "position_review":
        screening_estimate, transfer_reason = None, "not_applicable_position_review"
    else:
        screening_estimate, transfer_reason = _screening_estimate_from_triage_snapshot(
            workspace=workspace,
            ticker=ticker,
            asof=asof,
        )
    ticker_dir = _research_ticker_dir(workspace, ticker)

    price = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        raise ResearchWorkspaceDataError(
            f"no raw/unadjusted close available for {ticker} before {target_session.isoformat()}; "
            "an adjusted-only series is not substituted"
        )
    if purpose == "position_review" and price.price_as_of != asof:
        raise ResearchWorkspaceDataError(
            f"raw close date {price.price_as_of.isoformat()} does not match workspace manifest "
            f"as_of {asof.isoformat()}; --target-session must be the next trading session"
        )

    thesis_path = ticker_dir / "thesis-draft.yaml"
    if thesis_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"thesis draft already exists (use --force to regenerate): {thesis_path}"
        )
    ticker_dir.mkdir(parents=True, exist_ok=True)

    thesis_draft = _thesis_draft_skeleton(
        ticker=ticker,
        asof=asof,
        price=price,
        sqlite_path=sqlite_path,
        screening_estimate=screening_estimate,
        screening_retrieved_at=retrieved_at,
    )
    write_text_atomic(thesis_path, _THESIS_DRAFT_HEADER + _dump_yaml(thesis_draft))
    checklist = _checklist_skeleton(price=price)
    write_text_atomic(ticker_dir / "research-checklist.yaml", _dump_yaml(checklist))
    return {
        "thesis_draft": str(thesis_path),
        "price_as_of": price.price_as_of.isoformat(),
        "close_yen": price.close_yen,
        "corporate_action_unresolved": price.corporate_action_unresolved,
        "screening_estimate_transferred": screening_estimate is not None,
        "screening_estimate_transfer_reason": transfer_reason,
    }


def _thesis_draft_skeleton(
    *,
    ticker: str,
    asof: date,
    price: UnadjustedCloseObservation,
    sqlite_path: Path,
    screening_estimate: dict[str, object] | None,
    screening_retrieved_at: datetime,
) -> dict[str, object]:
    # The observed close is the previous business day's raw/unadjusted close, emitted
    # as the thesis's single market_price fact so the draft is schema-valid on load.
    # AI judgment fields are left null so the operator fills them from primary sources;
    # no value is guessed. An adjustment_factor anomaly is surfaced to the corporate
    # action checklist (not the fact), which blocks that check.
    #
    # Every structural slot the evaluation gate requires is laid out here — the
    # market price unit, the trailing-multiple valuation fact, and the review
    # reference — so filling the placeholders is the only work left. Values the
    # scaffold cannot observe stay as the TODO sentinel, which the gate rejects as
    # non-numeric rather than letting an unfilled draft reach promotion.
    del sqlite_path
    observed_at = datetime.combine(price.price_as_of, time(15, 30), tzinfo=JST)
    sources = [
        {
            "source_id": "market_close",
            "ticker": ticker,
            "source_tier": "local_data",
            "provider": "jquants",
            "dataset": "jquants_daily_bars",
            "retrieved_at": observed_at.isoformat(),
            "as_of": price.price_as_of.isoformat(),
            "used_for": "market_price",
        }
    ]
    if screening_estimate is not None:
        sources.append(
            {
                "source_id": "screening_analysis",
                "ticker": ticker,
                "source_tier": "local_data",
                "provider": "baibai-loop",
                "dataset": "security-analysis",
                "retrieved_at": screening_retrieved_at.isoformat(),
                "as_of": asof.isoformat(),
                "used_for": "screening expected return and fair value anchor",
            }
        )
    input_snapshot = {
        "snapshot_version": 1,
        "producer_model_version": "security-analysis-v1",
        "ticker": ticker,
        "company_name": None,
        "sector": None,
        "common_factors": [],
        "as_of": asof.isoformat(),
        "sources": sources,
        "facts": [
            {
                "fact_id": "market_price_close",
                "fact_kind": "market_price",
                "value": price.close_yen,
                "unit": "JPY_per_share",
                "as_of": price.price_as_of.isoformat(),
                "source_ids": ["market_close"],
                "observed_at": observed_at.isoformat(),
                "price_basis": "last_close_unadjusted",
            },
            {
                # The observed trailing multiple the 5y base break-even check reads.
                # Only the price half is local; the earnings half comes from the
                # operator's primary disclosure, so the value stays a sentinel and
                # its source list is extended when the ratio is filled in.
                "fact_id": TRAILING_MULTIPLE_FACT_ID,
                "fact_kind": "valuation_metric",
                "value": TODO_PLACEHOLDER,
                "unit": "ratio",
                "as_of": asof.isoformat(),
                "source_ids": ["market_close"],
            },
        ],
    }
    if screening_estimate is not None:
        input_snapshot["screening_estimate"] = screening_estimate
    return {
        "schema_version": 3,
        "input_snapshot": input_snapshot,
        "derived": {"metrics": []},
        "estimates": None,
        "permanent_loss_risks": [],
        "judgment": None,
        "independent_review_ref": _review_filename(asof=asof, ticker=ticker),
    }


def _screening_estimate_from_triage_snapshot(
    *, workspace: Path, ticker: str, asof: date
) -> tuple[dict[str, object] | None, str | None]:
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    rows = {
        str(row.get("ticker")): row
        for row in _dict_list(research_workspace.get("review_set_entries"))
    }
    row = rows.get(ticker)
    if row is None:
        raise ResearchWorkspaceDataError(f"Review Set must contain ticker exactly once: {ticker}")
    if research_workspace.get("as_of") != asof.isoformat():
        raise ResearchWorkspaceDataError(
            "Review Set Entry snapshot as_of does not match manifest as_of"
        )
    analysis = _required_mapping(row.get("analysis"), label="Review Set analysis")
    estimate = analysis.get("expected_return")
    if not isinstance(estimate, Mapping) or estimate.get("er_annual") is None:
        return None, "screening_estimate_missing"
    annual = _finite_number(estimate.get("er_annual"), label="expected_return.er_annual")
    model_version = _nonempty_string(
        estimate.get("er_model_version"), label="expected_return.er_model_version"
    )
    assumptions = _nonempty_string(
        estimate.get("er_assumptions"), label="expected_return.er_assumptions"
    )
    anchors = [
        _finite_number(value, label=f"expected_return.{key}")
        for key in ("fv_sector_median_yen", "fv_self_range_yen")
        if (value := estimate.get(key)) is not None
    ]
    if any(value <= 0 for value in anchors):
        raise ResearchWorkspaceDataError("Review Set fair-value anchors must be positive")
    fair_value_anchor_yen = min(anchors) if anchors else None
    screening_estimate = {
        "origin": "estimate",
        "model_version": model_version,
        "as_of": asof.isoformat(),
        "expected_return_annual_ratio": annual,
        "expected_return_unit": "annual_ratio",
        "fair_value_anchor_yen": fair_value_anchor_yen,
        "fair_value_unit": "JPY_per_share",
        "assumptions": assumptions,
        # The manifest hash binds the workspace to the immutable Review Set.  The
        # thesis contract, however, requires every source_id to resolve inside
        # input_snapshot.sources and requires this estimate to name a local-data
        # source.  `screening_analysis` is the source emitted by the scaffold for
        # exactly that purpose.
        "source_ids": ["screening_analysis"],
    }
    try:
        ScreeningEstimate.model_validate(screening_estimate)
    except (ValidationError, ValueError) as error:
        raise ResearchWorkspaceDataError(
            f"screening estimate violates thesis contract: {error}"
        ) from error
    return screening_estimate, None


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


def _checklist_skeleton(*, price: UnadjustedCloseObservation) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    for check_id in CHECKLIST_IDS:
        if check_id == "source.corporate_action" and price.corporate_action_unresolved:
            status = "blocked"
            note = (
                "adjustment_factor != 1 on the resolved close; confirm effective date, "
                "share count, and price basis from company/exchange primary disclosure"
            )
        else:
            status = "pending"
            note = None
        checks.append(
            {
                "check_id": check_id,
                "status": status,
                "source_ids": [],
                "as_of": None,
                "note": note,
                "decision_impact": None,
            }
        )
    return {"checks": checks}


# --------------------------------------------------------------------------- #
# review-scaffold
# --------------------------------------------------------------------------- #


def scaffold_review(
    *, workspace: Path, ticker: str, db_path: Path | None = None, force: bool = False
) -> dict[str, object]:
    """Write the Thesis Review draft bound to the current thesis core hash.

    The review author is a distinct role from the thesis author; this scaffold only
    lays out the recalculation slots and never produces the review conclusions. The
    bound ``reviewed_thesis_sha256`` is what lets ``promote`` detect a stale review.
    The file lands under the stable name the thesis already references, so promote
    and plan-limit resolve it without an intervening copy.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(workspace, ticker, action="scaffold review", gate=gate)
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
        "reviewer_run_id": None,
        "reviewed_at": None,
        "reviewed_thesis_sha256": core_hash,
        "primary_source_check": None,
        "checked_source_ids": [],
        "recalculated_scenarios": [
            {"horizon_years": horizon, "name": name, "total_return_cagr_pct": None}
            for horizon in (3, 5)
            for name in ("bear", "base", "bull")
        ],
        "strongest_countercase": None,
        "alternative_candidate_check": None,
        "proposal_changed": False,
        "change_rationale": None,
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

    Gates: no pending/blocked checklist item, thesis evaluates ready against the
    adjacent review, review hash matches the thesis core hash, schema validity, and
    path confinement. Reusing an immutable ID with different content is rejected.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    # Every researched ticker earns a canonical thesis, not only the one being bought.
    # A cycle that buys nothing still produced the judgment that says why, and the
    # Capital Allocation Assessment binds each case to a stored immutable thesis.
    _require_primary_research_ticker(workspace, ticker, action="promote", gate=gate)

    manifest_asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    ticker_dir = _research_ticker_dir(workspace, ticker)
    thesis_path = ticker_dir / "thesis-draft.yaml"
    review_path = _review_draft_path(workspace, ticker, manifest_asof)
    if not thesis_path.exists() or not review_path.exists():
        raise ResearchWorkspaceDataError(f"thesis or review draft missing for {ticker}")

    checklist = _load_checklist(workspace, ticker)
    # Allowlist gate: every check must be explicitly "complete". Any other status
    # (pending / blocked / a missing or typo'd value) counts as unresolved so a
    # hand-edited checklist cannot slip an incomplete item past promotion.
    unresolved = sorted(
        _string_or_none(item.get("check_id")) or "<missing check_id>"
        for item in checklist
        if item.get("status") != "complete"
    )
    if not checklist:
        raise ResearchWorkspaceDataError(f"cannot promote {ticker}: checklist is empty")
    if unresolved:
        raise ResearchWorkspaceDataError(
            f"cannot promote {ticker}: checklist has unresolved checks: {unresolved}"
        )

    try:
        document = load_thesis(thesis_path)
        review = load_thesis_review(review_path)
    except ThesisError as error:
        raise ResearchWorkspaceDataError(f"draft is not schema-valid: {error}") from error

    if document.input_snapshot.ticker != ticker:
        raise ResearchWorkspaceDataError(
            f"cannot promote {ticker}: thesis ticker is {document.input_snapshot.ticker}"
        )
    if document.input_snapshot.as_of != manifest_asof:
        raise ResearchWorkspaceDataError(
            f"cannot promote {ticker}: thesis as_of {document.input_snapshot.as_of.isoformat()} "
            f"does not match workspace manifest as_of {manifest_asof.isoformat()}"
        )

    core_hash = thesis_core_hash(document)
    if review.reviewed_thesis_sha256 != core_hash:
        raise ResearchWorkspaceDataError(
            "review is stale: reviewed_thesis_sha256 does not match the thesis core hash"
        )
    if review.proposal_changed:
        raise ResearchWorkspaceDataError(
            "review changed the proposal; regenerate the thesis and re-review before promotion"
        )

    result = evaluate_thesis(document, review=review, now=now, identity=UnpublishedThesis.DRAFT)
    if result.decision_readiness not in {"ready", "ready_with_warnings"}:
        raise ResearchWorkspaceDataError(f"thesis is not decision-ready: {list(result.errors)}")

    stable_review_name = _review_filename(asof=document.input_snapshot.as_of, ticker=ticker)
    # The ref stays inside the thesis payload and its core hash so a stored thesis
    # still names the review it was decided against; the canonical binding in the DB
    # is the thesis_id FK.
    if document.independent_review_ref != stable_review_name:
        raise ResearchWorkspaceDataError(
            "thesis independent_review_ref must equal the stable review filename "
            f"{stable_review_name!r} before promotion"
        )
    resolved_thesis_id = thesis_id or (
        f"thesis-{document.input_snapshot.as_of:%Y%m%d}-{ticker}-{review.review_id}"
    )
    try:
        ResearchStoreService(db_path, clock=lambda: now).publish_thesis_with_review(
            resolved_thesis_id,
            _load_mapping(thesis_path, label="thesis"),
            _load_mapping(review_path, label="Thesis Review"),
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


def plan_limit(
    *,
    thesis: Path,
    db_path: Path | None,
    sqlite_path: Path,
    target_session: date,
    budget_min_yen: int,
    budget_max_yen: int,
    now: datetime,
) -> dict[str, object]:
    """Derive a planning-only ``limit``/``defer`` from the previous business-day close.

    The primary limit is the legal market close itself; no future price or fill
    probability is asserted. Budget, cash, dry powder, concentration, and reservations
    in other tickers are warnings/annotations only — they never change the investment
    ranking or limit price. An active reservation in the selected ticker defers a new
    order until human-confirmed broker state is recorded. ``defer`` is a normal
    judgment (exit 0).
    """
    document = load_thesis(thesis)
    ticker = document.input_snapshot.ticker
    review_path = _adjacent_review_path(thesis, document.independent_review_ref)
    review = load_thesis_review(review_path) if review_path is not None else None
    defer_reasons: list[str] = []

    result = evaluate_thesis(document, review=review, now=now, identity=UnpublishedThesis.DRAFT)
    if result.decision_readiness != "ready":
        defer_reasons.append("thesis_not_decision_ready")

    price = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        defer_reasons.append("latest_business_day_close_missing")
    elif price.corporate_action_unresolved:
        defer_reasons.append("corporate_action_unresolved")

    try:
        max_price = maximum_acceptable_entry_price(document, tick_size_yen=PLANNING_TICK_SIZE_YEN)
    except EntryPricePolicyError as error:
        raise ResearchWorkspaceDataError(f"cannot derive max acceptable price: {error}") from error

    close_decimal = Decimal(str(price.close_yen)) if price is not None else None
    if close_decimal is not None and close_decimal > max_price:
        defer_reasons.append("close_above_max_acceptable_price")

    snapshot, source_append_head = _load_snapshot(db_path)
    annotations = portfolio_annotations(snapshot, ticker=ticker)
    if any(reservation.ticker == ticker for reservation in snapshot.active_reservations):
        defer_reasons.append("active_reservation_exists")
    expires_at = datetime.combine(target_session, time(15, 30), tzinfo=JST)

    base_output: dict[str, object] = {
        "ticker": ticker,
        "thesis_ref": str(thesis),
        "thesis_sha256": _sha256_text(thesis.read_text(encoding="utf-8")),
        "thesis_core_sha256": thesis_core_hash(document),
        "independent_review_ref": str(review_path) if review_path is not None else None,
        "independent_review_sha256": (
            _sha256_text(review_path.read_text(encoding="utf-8"))
            if review_path is not None
            else None
        ),
        "price_as_of": price.price_as_of.isoformat() if price is not None else None,
        "price_basis": "last_close_unadjusted",
        "source_ref": f"{sqlite_path.as_posix()}:jquants_daily_bars",
        "close_yen": price.close_yen if price is not None else None,
        "max_acceptable_price_yen": decimal_to_number(max_price),
        "board_lot": BOARD_LOT,
        "budget_min_yen": budget_min_yen,
        "budget_max_yen": budget_max_yen,
        "portfolio_annotations": annotations,
        "source_ledger_entity": "portfolio-ledger",
        "source_ledger_append_head": source_append_head,
        "expires_at": expires_at.isoformat(),
    }

    if defer_reasons or close_decimal is None:
        return {
            "status": "defer",
            **base_output,
            "limit_price_yen": None,
            "quantity": 0,
            "notional_yen": 0,
            "warnings": [],
            "defer_reasons": defer_reasons,
        }

    assert price is not None  # close_decimal is derived only from a resolved price
    warnings: list[str] = []
    lot_notional = close_decimal * BOARD_LOT
    if document.judgment.sizing_action == "reduced" and lot_notional > budget_max_yen:
        return {
            "status": "defer",
            **base_output,
            "limit_price_yen": None,
            "quantity": 0,
            "notional_yen": 0,
            "warnings": [],
            "defer_reasons": ["reduced_lot_exceeds_budget"],
        }
    if document.judgment.sizing_action == "reduced":
        quantity = BOARD_LOT
    elif lot_notional <= budget_max_yen:
        # floor(budget_max / lot_notional) on the exact Decimal notional; truncating
        # the notional to int first could select one lot too many and overshoot.
        lots = int(budget_max_yen // lot_notional)
        quantity = max(lots, 1) * BOARD_LOT
    else:
        quantity = BOARD_LOT
        warnings.append("budget_guide_exceeded")
    notional = close_decimal * quantity
    if notional < budget_min_yen:
        warnings.append("budget_guide_under")

    exposure, exposure_warnings, exposure_total_capital_yen = (
        calculate_prospective_portfolio_exposure(
            snapshot,
            sqlite_path=sqlite_path,
            price_as_of=price.price_as_of,
            ticker=ticker,
            sector=document.input_snapshot.sector,
            common_factors=document.input_snapshot.common_factors,
            order_notional_yen=int(notional),
        )
    )
    warnings.extend(
        planned_order_cash_warnings(
            snapshot,
            notional_yen=notional,
            total_capital_yen=exposure_total_capital_yen,
        )
    )
    warnings.extend(exposure_warnings)
    return {
        "status": "planned_limit",
        **base_output,
        "limit_price_yen": decimal_to_number(close_decimal),
        "quantity": quantity,
        "notional_yen": int(notional),
        "portfolio_exposure": exposure,
        "warnings": warnings,
        "defer_reasons": [],
    }


def _load_snapshot(db_path: Path | None) -> tuple[PortfolioSnapshot, int]:
    try:
        service = LedgerStoreService(db_path)
        document, append_head = service.load_with_head()
        return reconcile_portfolio(document), append_head
    except (OSError, ValueError) as error:
        raise ResearchWorkspaceDataError(f"cannot reconcile ledger: {error}") from error


def _load_checklist(workspace: Path, ticker: str) -> list[dict[str, object]]:
    checklist_path = _research_ticker_dir(workspace, ticker) / "research-checklist.yaml"
    payload = _load_mapping(checklist_path, label="research checklist")
    return _dict_list(payload.get("checks"))


def _thesis_validation_errors(workspace: Path, ticker: str, asof: date) -> list[str]:
    """Report what stops this draft from becoming a canonical thesis.

    The as-of comparison belongs here and not only in ``promote``: rebuilding a
    workspace at a new as-of leaves the ticker directory untouched, so a draft
    written for the previous one survives. Without this the workspace would call
    itself ``ready_for_review`` and send the operator to the most expensive step of
    all, and only promote would say the draft was never usable.
    """

    thesis_path = _research_ticker_dir(workspace, ticker) / "thesis-draft.yaml"
    if not thesis_path.exists():
        return ["thesis draft missing"]
    try:
        document = load_thesis(thesis_path)
    except ThesisError as error:
        return [str(error).splitlines()[0]]
    if document.input_snapshot.ticker != ticker:
        return [f"thesis ticker {document.input_snapshot.ticker} does not match case {ticker}"]
    if document.input_snapshot.as_of != asof:
        return [
            (
                f"thesis as_of {document.input_snapshot.as_of.isoformat()} does not match "
                f"workspace as_of {asof.isoformat()}; regenerate it with "
                "`research thesis-scaffold --force`"
            )
        ]
    return []


def _review_validation_errors(workspace: Path, ticker: str, asof: date) -> list[str]:
    review_path = _review_draft_path(workspace, ticker, asof)
    if not review_path.exists():
        return ["review draft missing"]
    try:
        review = load_thesis_review(review_path)
    except ThesisError as error:
        return [str(error).splitlines()[0]]
    thesis_path = _research_ticker_dir(workspace, ticker) / "thesis-draft.yaml"
    core_hash = _thesis_core_hash_if_valid(thesis_path)
    if core_hash is not None and review.reviewed_thesis_sha256 != core_hash:
        return ["review is stale for the current thesis"]
    return []


def _adjacent_review_path(thesis_path: Path, review_ref: str | None) -> Path | None:
    if review_ref is None:
        return None
    root = thesis_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise ResearchWorkspaceDataError("independent_review_ref must stay beside the thesis")
    if not resolved.exists():
        raise ResearchWorkspaceDataError(f"Thesis Review not found beside thesis: {resolved}")
    return resolved


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
    "plan_limit",
    "prepare_workspace",
    "promote",
    "read_prior_session_unadjusted_close",
    "scaffold_review",
    "scaffold_thesis",
]
