"""Opportunity authoring: prepare a workspace, scaffold packet/review drafts, and
derive a planning-only limit from the previous business day's raw close.

This module is pure logic behind ``baibai-loop-opportunity``. It never submits an
order, never asserts fill probability, and never reads a realtime quote. The
canonical price basis for a limit is the JPX store's latest complete business-day
*raw/unadjusted* close before the target session; adjusted series are for past
comparison only and are never used for a limit.

Design boundaries (Issue #359 Milestone A):

- Investment value is decided before budget rounding. The 20-30万円 guide is a
  sizing annotation, never a hard gate: a single board lot above the guide still
  produces a proposal with a warning rather than an auto-reject.
- ``promote`` is the only command that writes a canonical decision packet/review;
  every other command writes only to the rebuildable ``.cache/opportunity/<asof>/``
  workspace and refuses to overwrite a canonical record.
- ``defer`` and "no actionable bargain" are normal investment judgments and exit 0.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import yaml

from baibai_loop.foundation.filesystem import write_text_atomic
from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import (
    PortfolioSnapshot,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_loop.position.policy import PORTFOLIO_POLICY

from .close_source import PreviousClose, resolve_previous_business_day_close
from .decision_packet import (
    DecisionPacketError,
    decision_packet_core_hash,
    evaluate_decision_packet,
    load_decision_packet,
    load_independent_review,
)
from .execution_policy import ExecutionPolicyError, max_acceptable_price

TOOL_VERSION = "opportunity-v1"
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
    "judgment.ai_value_capture",
)


class OpportunityError(Exception):
    """Base error carrying the CLI exit code for the failure class."""

    exit_code = 3


class OpportunityDataError(OpportunityError):
    """Missing source / checklist / schema / hash makes the request unprocessable."""

    exit_code = 3


class OpportunityConflictError(OpportunityError):
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
        raise OpportunityDataError(f"{label} not found: {path}")
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise OpportunityDataError(f"{label} root must be a mapping: {path}")
    return dict(raw)


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PrepareResult:
    workspace: Path
    actionable: bool
    shortlist_slots: int
    audit_pool_size: int


def prepare_workspace(
    *,
    asof: date,
    selection_output: Path,
    ledger: Path,
    workspace: Path,
    force: bool = False,
) -> PrepareResult:
    """Build the opportunity workspace from a screening selection output and ledger.

    Holdings/reservations are ledger annotations, never hard exclusions: a held or
    reserved ticker stays a comparable candidate. An empty audit pool is a normal
    'no actionable bargain' outcome and still produces a workspace.
    """
    selection = _load_mapping(selection_output, label="selection output")
    audit_pool = _dict_list(selection.get("audit_pool"))
    snapshot = _load_snapshot(ledger)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise OpportunityConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    held = {holding.ticker for holding in snapshot.holdings}
    reserved = {reservation.ticker for reservation in snapshot.active_reservations}
    annotated = [_annotate_candidate(row, held=held, reserved=reserved) for row in audit_pool]
    research_selection_target_max = _research_selection_target_max(selection)
    shortlist_slots = (
        min(research_selection_target_max, len(annotated))
        if research_selection_target_max > 0
        else len(annotated)
    )

    selection_doc = {
        "as_of": asof.isoformat(),
        "audit_pool": annotated,
        "shortlist_slots": shortlist_slots,
        "shortlist": [],
        "actionable": bool(annotated),
    }
    comparison_doc = _research_comparison(asof, annotated)

    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "selection.yaml", selection_doc)
    _write_workspace_file(workspace / "research-comparison.yaml", comparison_doc)

    manifest = {
        "as_of": asof.isoformat(),
        "tool_version": TOOL_VERSION,
        "inputs": {
            "selection_output": {
                "path": selection_output.as_posix(),
                "sha256": _sha256_text(selection_output.read_text(encoding="utf-8")),
            },
            "ledger": {
                "path": ledger.as_posix(),
                "sha256": _sha256_text(ledger.read_text(encoding="utf-8")),
            },
        },
        "rules": {
            "research_selection_target_max": research_selection_target_max,
            "research_selection_playbook_order": _selection_playbook_order(selection),
        },
    }
    _write_workspace_file(manifest_path, manifest)
    _write_status(workspace)
    return PrepareResult(
        workspace=workspace,
        actionable=bool(annotated),
        shortlist_slots=shortlist_slots,
        audit_pool_size=len(annotated),
    )


def _annotate_candidate(
    row: Mapping[str, object], *, held: set[str], reserved: set[str]
) -> dict[str, object]:
    ticker = str(row.get("ticker") or "")
    annotation = _portfolio_annotation(ticker, held=held, reserved=reserved)
    output = dict(row)
    output["portfolio_annotation"] = annotation
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


def _research_comparison(
    asof: date, annotated: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    candidates = [
        {
            "ticker": str(row.get("ticker") or ""),
            "screening_rank": row.get("rank"),
            "portfolio_annotation": row.get("portfolio_annotation"),
            "temporary_mispricing_hypothesis": None,
            "permanent_loss_conclusion": None,
            "permanent_loss_unknown_axes": [],
            "five_year_base_cagr_pct": None,
            "fair_value_yen": None,
            "fv_gap_pct": None,
            "portfolio_marginal_value": None,
            "strongest_countercase": None,
            "source_ids": [],
            "disposition": None,
            "disposition_reason": None,
        }
        for row in annotated
    ]
    return {
        "as_of": asof.isoformat(),
        "candidates": candidates,
        "selected_ticker": None,
        "ranking_rationale": None,
    }


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _write_status(workspace: Path) -> dict[str, object]:
    status = compute_status(workspace)
    write_text_atomic(workspace / "status.yaml", _dump_yaml(status))
    return status


def compute_status(workspace: Path) -> dict[str, object]:
    """Read the workspace and report completion, drift, and the next command.

    External inputs remain bound to their prepare-time hashes. Operator-authored
    drafts are editable, but their structure and lineage must remain consistent.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest)
    _validate_editable_drafts(workspace, manifest)

    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    selected_ticker = _string_or_none(comparison.get("selected_ticker"))

    if selected_ticker is None:
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=None,
            next_command="baibai-loop-opportunity packet-scaffold",
        )

    checklist = _load_checklist(workspace, selected_ticker)
    completed = [item for item in checklist if item.get("status") == "complete"]
    pending = [
        _string_or_none(item.get("check_id"))
        for item in checklist
        if item.get("status") == "pending"
    ]
    blocked = [
        _string_or_none(item.get("check_id"))
        for item in checklist
        if item.get("status") == "blocked"
    ]
    packet_errors = _packet_validation_errors(workspace, selected_ticker)
    review_errors = _review_validation_errors(workspace, selected_ticker)

    workspace_status = _resolve_workspace_status(
        pending=pending, blocked=blocked, packet_errors=packet_errors, review_errors=review_errors
    )
    return _status_payload(
        workspace_status=workspace_status,
        selected_ticker=selected_ticker,
        completed_checks=len(completed),
        pending_checks=[value for value in pending if value is not None],
        blocked_checks=[value for value in blocked if value is not None],
        packet_validation_errors=packet_errors,
        review_validation_errors=review_errors,
        next_command=_next_command(workspace_status, selected_ticker),
    )


def _resolve_workspace_status(
    *,
    pending: Sequence[object],
    blocked: Sequence[object],
    packet_errors: Sequence[str],
    review_errors: Sequence[str],
) -> str:
    if blocked:
        return "deferred"
    if pending or packet_errors:
        return "incomplete"
    if review_errors:
        return "ready_for_review"
    return "ready_for_promotion"


def _next_command(workspace_status: str, ticker: str) -> str:
    match workspace_status:
        case "deferred":
            return "resolve blocked checks or defer the candidate"
        case "incomplete":
            return f"baibai-loop-opportunity packet-scaffold --ticker {ticker}"
        case "ready_for_review":
            return f"baibai-loop-opportunity review-scaffold --ticker {ticker}"
        case _:
            return f"baibai-loop-opportunity promote --ticker {ticker}"


def _status_payload(
    *,
    workspace_status: str,
    selected_ticker: str | None,
    completed_checks: int = 0,
    pending_checks: Sequence[str] | None = None,
    blocked_checks: Sequence[str] | None = None,
    packet_validation_errors: Sequence[str] | None = None,
    review_validation_errors: Sequence[str] | None = None,
    next_command: str,
) -> dict[str, object]:
    return {
        "workspace_status": workspace_status,
        "selected_ticker": selected_ticker,
        "completed_checks": completed_checks,
        "pending_checks": list(pending_checks or []),
        "blocked_checks": list(blocked_checks or []),
        "packet_validation_errors": list(packet_validation_errors or []),
        "review_validation_errors": list(review_validation_errors or []),
        "next_command": next_command,
    }


def _verify_external_inputs(manifest: Mapping[str, object]) -> None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise OpportunityDataError("manifest is missing external input hashes")
    for name in ("selection_output", "ledger"):
        input_ref = inputs.get(name)
        if not isinstance(input_ref, Mapping):
            raise OpportunityDataError(f"manifest is missing input hash: {name}")
        path_value = input_ref.get("path")
        expected = input_ref.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise OpportunityDataError(f"manifest input ref is invalid: {name}")
        path = Path(path_value)
        if not path.is_file():
            raise OpportunityConflictError(f"workspace external input is missing: {name}")
        actual = _sha256_text(path.read_text(encoding="utf-8"))
        if actual != expected:
            raise OpportunityConflictError(
                f"workspace external input changed since prepare (input hash drift): {name}"
            )


def _validate_editable_drafts(workspace: Path, manifest: Mapping[str, object]) -> None:
    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    manifest_asof = str(manifest.get("as_of") or "")
    if selection.get("as_of") != manifest_asof or comparison.get("as_of") != manifest_asof:
        raise OpportunityDataError("workspace draft as_of does not match manifest")

    audit_pool = _dict_list(selection.get("audit_pool"))
    audit_tickers = [str(row.get("ticker") or "") for row in audit_pool]
    if (not audit_tickers or any(not ticker for ticker in audit_tickers)) and selection.get(
        "actionable"
    ):
        raise OpportunityDataError("workspace audit_pool is invalid")
    shortlist = _dict_list(selection.get("shortlist"))
    shortlist_slots = selection.get("shortlist_slots")
    if not isinstance(shortlist_slots, int) or shortlist_slots < 0:
        raise OpportunityDataError("workspace shortlist_slots is invalid")
    shortlist_tickers = [str(row.get("ticker") or "") for row in shortlist]
    if (
        len(shortlist) > shortlist_slots
        or len(shortlist_tickers) != len(set(shortlist_tickers))
        or any(ticker not in audit_tickers for ticker in shortlist_tickers)
    ):
        raise OpportunityDataError("workspace shortlist is invalid")

    candidates = _dict_list(comparison.get("candidates"))
    comparison_tickers = [str(row.get("ticker") or "") for row in candidates]
    if comparison_tickers != audit_tickers:
        raise OpportunityDataError("research comparison candidates do not match audit_pool")
    selected = _string_or_none(comparison.get("selected_ticker"))
    if selected is not None and selected not in shortlist_tickers:
        raise OpportunityDataError("selected_ticker is not present in shortlist")


# --------------------------------------------------------------------------- #
# packet-scaffold
# --------------------------------------------------------------------------- #


def scaffold_packet(
    *,
    workspace: Path,
    ticker: str,
    sqlite_path: Path,
    target_session: date,
    force: bool = False,
) -> dict[str, object]:
    """Write a packet-draft with observed price facts and a pending checklist.

    AI judgment fields are placeholders; only observed/derived known values are
    filled. A missing raw close is a hard exit_code 3 (no guess from an adjusted
    series). An unresolved corporate action blocks the corporate-action check.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest)
    _validate_editable_drafts(workspace, manifest)
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")

    price = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        raise OpportunityDataError(
            f"no raw/unadjusted close available for {ticker} before {target_session.isoformat()}; "
            "an adjusted-only series is not substituted"
        )

    ticker_dir = workspace / ticker
    packet_path = ticker_dir / "packet-draft.yaml"
    if packet_path.exists() and not force:
        raise OpportunityConflictError(
            f"packet draft already exists (use --force to regenerate): {packet_path}"
        )
    ticker_dir.mkdir(parents=True, exist_ok=True)

    packet_draft = _packet_draft_skeleton(
        ticker=ticker, asof=asof, price=price, sqlite_path=sqlite_path
    )
    write_text_atomic(packet_path, _dump_yaml(packet_draft))
    checklist = _checklist_skeleton(price=price)
    write_text_atomic(ticker_dir / "research-checklist.yaml", _dump_yaml(checklist))
    return {
        "packet_draft": str(packet_path),
        "price_as_of": price.price_as_of.isoformat(),
        "close_yen": price.close_yen,
        "corporate_action_unresolved": price.corporate_action_unresolved,
    }


def _packet_draft_skeleton(
    *, ticker: str, asof: date, price: PreviousClose, sqlite_path: Path
) -> dict[str, object]:
    # The observed close is the previous business day's raw/unadjusted close, emitted
    # as the packet's single market_price fact so the draft is schema-valid on load.
    # AI judgment fields are left null so the operator fills them from primary sources;
    # no value is guessed. An adjustment_factor anomaly is surfaced to the corporate
    # action checklist (not the fact), which blocks that check.
    del sqlite_path
    observed_at = datetime.combine(price.price_as_of, time(15, 30), tzinfo=JST)
    return {
        "schema_version": 2,
        "input_snapshot": {
            "snapshot_version": 1,
            "producer_model_version": "screening-selection-v1",
            "ticker": ticker,
            "company_name": None,
            "sector": None,
            "common_factors": [],
            "as_of": asof.isoformat(),
            "sources": [
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
            ],
            "facts": [
                {
                    "fact_id": "market_price_close",
                    "fact_kind": "market_price",
                    "value": price.close_yen,
                    "unit": "JPY",
                    "as_of": price.price_as_of.isoformat(),
                    "source_ids": ["market_close"],
                    "observed_at": observed_at.isoformat(),
                    "price_basis": "last_close_unadjusted",
                }
            ],
        },
        "derived": {"metrics": []},
        "estimates": None,
        "permanent_loss_risks": [],
        "judgment": None,
        "independent_review_ref": None,
    }


def _checklist_skeleton(*, price: PreviousClose) -> dict[str, object]:
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


def scaffold_review(*, workspace: Path, ticker: str, force: bool = False) -> dict[str, object]:
    """Write a review-draft bound to the current packet core hash.

    The review author is a distinct role from the packet author; this scaffold only
    lays out the recalculation slots and never produces the review conclusions. The
    bound ``reviewed_packet_sha256`` is what lets ``promote`` detect a stale review.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest)
    _validate_editable_drafts(workspace, manifest)
    ticker_dir = workspace / ticker
    packet_path = ticker_dir / "packet-draft.yaml"
    if not packet_path.exists():
        raise OpportunityDataError(f"packet draft not found for {ticker}: {packet_path}")

    core_hash = _packet_core_hash_if_valid(packet_path)
    review_path = ticker_dir / "review-draft.yaml"
    if review_path.exists() and not force:
        raise OpportunityConflictError(
            f"review draft already exists (use --force to regenerate): {review_path}"
        )

    review_draft = {
        "review_id": None,
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": None,
        "reviewer_run_id": None,
        "reviewed_at": None,
        "reviewed_packet_sha256": core_hash,
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
    write_text_atomic(review_path, _dump_yaml(review_draft))
    return {"review_draft": str(review_path), "reviewed_packet_sha256": core_hash}


def _packet_core_hash_if_valid(packet_path: Path) -> str | None:
    # A fully-filled draft hashes to its core; an incomplete draft cannot be hashed
    # yet, so the review is bound once the packet is complete.
    try:
        document = load_decision_packet(packet_path)
    except DecisionPacketError:
        return None
    return decision_packet_core_hash(document)


# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PromoteResult:
    packet_path: Path
    review_path: Path
    packet_sha256: str


def promote(
    *,
    workspace: Path,
    ticker: str,
    output_dir: Path,
    now: datetime,
) -> PromoteResult:
    """Persist the canonical packet/review only when everything is ready.

    Gates: no pending/blocked checklist item, packet evaluates ready against the
    adjacent review, review hash matches the packet core hash, schema validity, and
    path confinement. A canonical file that already exists is never overwritten.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest)
    _validate_editable_drafts(workspace, manifest)
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    if _string_or_none(comparison.get("selected_ticker")) != ticker:
        raise OpportunityDataError(f"cannot promote {ticker}: it is not the selected_ticker")

    ticker_dir = workspace / ticker
    packet_path = ticker_dir / "packet-draft.yaml"
    review_path = ticker_dir / "review-draft.yaml"
    if not packet_path.exists() or not review_path.exists():
        raise OpportunityDataError(f"packet or review draft missing for {ticker}")

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
        raise OpportunityDataError(f"cannot promote {ticker}: checklist is empty")
    if unresolved:
        raise OpportunityDataError(
            f"cannot promote {ticker}: checklist has unresolved checks: {unresolved}"
        )

    try:
        document = load_decision_packet(packet_path)
        review = load_independent_review(review_path)
    except DecisionPacketError as error:
        raise OpportunityDataError(f"draft is not schema-valid: {error}") from error

    core_hash = decision_packet_core_hash(document)
    if review.reviewed_packet_sha256 != core_hash:
        raise OpportunityDataError(
            "review is stale: reviewed_packet_sha256 does not match the packet core hash"
        )
    if review.proposal_changed:
        raise OpportunityDataError(
            "review changed the proposal; regenerate the packet and re-review before promotion"
        )

    result = evaluate_decision_packet(document, review=review, now=now)
    if result.decision_readiness != "ready":
        raise OpportunityDataError(f"packet is not decision-ready: {list(result.errors)}")

    output_dir = output_dir.resolve()
    packet_out = output_dir / f"{document.input_snapshot.as_of:%Y-%m-%d}-{ticker}-decision.yaml"
    review_out = (
        output_dir / f"{document.input_snapshot.as_of:%Y-%m-%d}-{ticker}-decision-review.yaml"
    )
    # The packet's independent_review_ref is part of its core hash, so promotion
    # must not rewrite it. The packet must already point at the canonical review
    # filename so the review resolves path-confined beside it without changing the
    # hash the review and any override are bound to.
    if document.independent_review_ref != review_out.name:
        raise OpportunityDataError(
            f"packet independent_review_ref must equal the canonical review filename "
            f"{review_out.name!r} before promotion"
        )
    for target in (packet_out, review_out):
        if not target.resolve().is_relative_to(output_dir):
            raise OpportunityConflictError("promotion target escapes the output directory")
        if target.exists():
            raise OpportunityConflictError(
                f"canonical file already exists and is never overwritten: {target}"
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(review_out, review_path.read_text(encoding="utf-8"))
    write_text_atomic(packet_out, packet_path.read_text(encoding="utf-8"))
    return PromoteResult(packet_path=packet_out, review_path=review_out, packet_sha256=core_hash)


# --------------------------------------------------------------------------- #
# plan-limit
# --------------------------------------------------------------------------- #


def plan_limit(
    *,
    packet: Path,
    ledger: Path,
    sqlite_path: Path,
    target_session: date,
    budget_min_yen: int,
    budget_max_yen: int,
    now: datetime,
) -> dict[str, object]:
    """Derive a planning-only ``limit``/``defer`` from the previous business-day close.

    The primary limit is the legal market close itself; no future price or fill
    probability is asserted. Budget, cash, dry powder, concentration, and existing
    reservations are warnings/annotations only — they never change the investment
    ranking or the ``planned_limit``. ``defer`` is a normal judgment (exit 0).
    """
    document = load_decision_packet(packet)
    ticker = document.input_snapshot.ticker
    review_path = _adjacent_review_path(packet, document.independent_review_ref)
    review = load_independent_review(review_path) if review_path is not None else None
    defer_reasons: list[str] = []

    result = evaluate_decision_packet(document, review=review, now=now)
    if result.decision_readiness != "ready":
        defer_reasons.append("packet_not_decision_ready")

    price = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        defer_reasons.append("latest_business_day_close_missing")
    elif price.corporate_action_unresolved:
        defer_reasons.append("corporate_action_unresolved")

    try:
        max_price = max_acceptable_price(document, tick_size_yen=PLANNING_TICK_SIZE_YEN)
    except ExecutionPolicyError as error:
        raise OpportunityDataError(f"cannot derive max acceptable price: {error}") from error

    close_decimal = Decimal(str(price.close_yen)) if price is not None else None
    if close_decimal is not None and close_decimal > max_price:
        defer_reasons.append("close_above_max_acceptable_price")

    snapshot = _load_snapshot(ledger)
    portfolio_annotations = _portfolio_annotations(snapshot, ticker=ticker)
    expires_at = datetime.combine(target_session, time(15, 30), tzinfo=JST)

    base_output: dict[str, object] = {
        "ticker": ticker,
        "price_as_of": price.price_as_of.isoformat() if price is not None else None,
        "price_basis": "last_close_unadjusted",
        "source_ref": f"{sqlite_path.as_posix()}:jquants_daily_bars",
        "close_yen": price.close_yen if price is not None else None,
        "max_acceptable_price_yen": _decimal_to_number(max_price),
        "board_lot": BOARD_LOT,
        "budget_min_yen": budget_min_yen,
        "budget_max_yen": budget_max_yen,
        "portfolio_annotations": portfolio_annotations,
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

    warnings: list[str] = []
    lot_notional = close_decimal * BOARD_LOT
    if lot_notional <= budget_max_yen:
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

    warnings.extend(_portfolio_warnings(snapshot, notional_yen=notional))
    return {
        "status": "planned_limit",
        **base_output,
        "limit_price_yen": _decimal_to_number(close_decimal),
        "quantity": quantity,
        "notional_yen": int(notional),
        "warnings": warnings,
        "defer_reasons": [],
    }


def _portfolio_annotations(snapshot: PortfolioSnapshot, *, ticker: str) -> list[str]:
    annotations: list[str] = []
    if any(holding.ticker == ticker for holding in snapshot.holdings):
        annotations.append("already_held")
    if any(reservation.ticker == ticker for reservation in snapshot.active_reservations):
        annotations.append("active_reservation")
    for warning in snapshot.warnings:
        annotations.append(f"ledger_warning:{warning.code}")
    return annotations


def _portfolio_warnings(snapshot: PortfolioSnapshot, *, notional_yen: Decimal) -> list[str]:
    # Cash / dry powder shortfalls are human-decision warnings only; they never
    # downgrade the investment ranking or auto-switch to a cheaper next candidate.
    warnings: list[str] = []
    if notional_yen > snapshot.available_cash_yen:
        warnings.append("available_cash_below_notional")
    dry_powder_pct = Decimal(str(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"]))
    dry_powder_floor = Decimal(snapshot.total_capital_yen) * dry_powder_pct / 100
    if Decimal(snapshot.available_cash_yen) - notional_yen < dry_powder_floor:
        warnings.append("dry_powder_below_floor")
    return warnings


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _load_snapshot(ledger: Path) -> PortfolioSnapshot:
    try:
        return reconcile_portfolio(load_portfolio_ledger(ledger))
    except (OSError, ValueError) as error:
        raise OpportunityDataError(f"cannot reconcile ledger: {error}") from error


def _load_checklist(workspace: Path, ticker: str) -> list[dict[str, object]]:
    checklist_path = workspace / ticker / "research-checklist.yaml"
    payload = _load_mapping(checklist_path, label="research checklist")
    return _dict_list(payload.get("checks"))


def _packet_validation_errors(workspace: Path, ticker: str) -> list[str]:
    packet_path = workspace / ticker / "packet-draft.yaml"
    if not packet_path.exists():
        return ["packet draft missing"]
    try:
        load_decision_packet(packet_path)
    except DecisionPacketError as error:
        return [str(error).splitlines()[0]]
    return []


def _review_validation_errors(workspace: Path, ticker: str) -> list[str]:
    review_path = workspace / ticker / "review-draft.yaml"
    if not review_path.exists():
        return ["review draft missing"]
    try:
        review = load_independent_review(review_path)
    except DecisionPacketError as error:
        return [str(error).splitlines()[0]]
    packet_path = workspace / ticker / "packet-draft.yaml"
    core_hash = _packet_core_hash_if_valid(packet_path)
    if core_hash is not None and review.reviewed_packet_sha256 != core_hash:
        return ["review is stale for the current packet"]
    return []


def _adjacent_review_path(packet_path: Path, review_ref: str | None) -> Path | None:
    if review_ref is None:
        return None
    root = packet_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise OpportunityDataError("independent_review_ref must stay beside the packet")
    if not resolved.exists():
        raise OpportunityDataError(f"independent review not found beside packet: {resolved}")
    return resolved


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _research_selection_target_max(selection: Mapping[str, object]) -> int:
    block = selection.get("selection")
    value = block.get("research_selection_target_max") if isinstance(block, Mapping) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OpportunityDataError(
            "selection output research_selection_target_max must be a non-negative integer"
        )
    return value


def _selection_playbook_order(selection: Mapping[str, object]) -> object:
    block = selection.get("selection")
    if isinstance(block, Mapping):
        return block.get("research_selection_playbook_order")
    return None


def _parse_date(value: str, *, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise OpportunityDataError(f"invalid {label}: {value}") from error


def _decimal_to_number(value: Decimal) -> float | int:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


__all__ = [
    "OpportunityConflictError",
    "OpportunityDataError",
    "OpportunityError",
    "PrepareResult",
    "PreviousClose",
    "PromoteResult",
    "compute_status",
    "plan_limit",
    "prepare_workspace",
    "promote",
    "resolve_previous_business_day_close",
    "scaffold_packet",
    "scaffold_review",
]
