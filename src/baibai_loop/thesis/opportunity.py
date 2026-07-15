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
from decimal import ROUND_HALF_UP, Decimal
from math import isfinite
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_loop.foundation.filesystem import write_text_atomic
from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import (
    PortfolioSnapshot,
    load_portfolio_ledger_with_sha256,
    reconcile_portfolio,
)
from baibai_loop.position.policy import PORTFOLIO_POLICY

from .close_source import (
    PreviousClose,
    resolve_holding_close_on_basis,
    resolve_previous_business_day_close,
)
from .decision_packet import (
    DecisionPacketError,
    ScreeningEstimate,
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
    _validate_selection_estimate_asof(selection=selection, audit_pool=audit_pool, asof=asof)
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

    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    shortlist = _dict_list(selection.get("shortlist"))
    shortlist_tickers = [str(row.get("ticker") or "") for row in shortlist]
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    selected_ticker = _string_or_none(comparison.get("selected_ticker"))

    if not shortlist_tickers:
        return _status_payload(
            workspace_status="awaiting_primary_research_selection",
            selected_ticker=None,
            next_command="review candidate-report and fill selection.yaml shortlist",
        )

    missing_lanes = [
        ticker
        for ticker in shortlist_tickers
        if not (_research_lane_dir(workspace, ticker) / "packet-draft.yaml").is_file()
    ]
    if missing_lanes:
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            next_command=f"baibai-loop-opportunity packet-scaffold --ticker {missing_lanes[0]}",
        )

    lane_pending: list[str] = []
    lane_blocked: list[str] = []
    lane_packet_errors: list[str] = []
    for ticker in shortlist_tickers:
        checklist = _load_checklist(workspace, ticker)
        lane_pending.extend(
            f"{ticker}:{check_id}"
            for item in checklist
            if item.get("status") == "pending"
            if (check_id := _string_or_none(item.get("check_id"))) is not None
        )
        lane_blocked.extend(
            f"{ticker}:{check_id}"
            for item in checklist
            if item.get("status") == "blocked"
            if (check_id := _string_or_none(item.get("check_id"))) is not None
        )
        lane_packet_errors.extend(
            f"{ticker}:{error}" for error in _packet_validation_errors(workspace, ticker)
        )
    if lane_pending or lane_packet_errors:
        first_ticker = (lane_pending or lane_packet_errors)[0].split(":", maxsplit=1)[0]
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            pending_checks=lane_pending,
            blocked_checks=lane_blocked,
            packet_validation_errors=lane_packet_errors,
            next_command=f"complete primary research lane for {first_ticker}",
        )

    if selected_ticker is None:
        return _status_payload(
            workspace_status="ready_for_comparison",
            selected_ticker=None,
            blocked_checks=lane_blocked,
            next_command=(
                "complete research-comparison.yaml and set selected_ticker, "
                "or record no actionable bargain"
            ),
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


def _research_lane_dir(workspace: Path, ticker: str) -> Path:
    """Return a path-confined ticker lane inside the shared opportunity workspace."""

    workspace_root = workspace.resolve()
    ticker_dir = (workspace_root / ticker).resolve()
    if ticker_dir.parent != workspace_root:
        raise OpportunityDataError(
            f"research lane must be a direct child of the workspace: {ticker}"
        )
    return ticker_dir


def _require_primary_research_ticker(workspace: Path, ticker: str) -> None:
    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    shortlist_tickers = {
        str(row.get("ticker") or "") for row in _dict_list(selection.get("shortlist"))
    }
    if ticker not in shortlist_tickers:
        raise OpportunityDataError(
            f"cannot scaffold research lane for {ticker}: ticker is not in the primary-research set"
        )


# --------------------------------------------------------------------------- #
# packet-scaffold
# --------------------------------------------------------------------------- #


def scaffold_packet(
    *,
    workspace: Path,
    ticker: str,
    sqlite_path: Path,
    target_session: date,
    retrieved_at: datetime,
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
    _require_primary_research_ticker(workspace, ticker)
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    screening_estimate, transfer_reason = _screening_estimate_from_selection_output(
        manifest=manifest,
        ticker=ticker,
        asof=asof,
    )
    ticker_dir = _research_lane_dir(workspace, ticker)

    price = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        raise OpportunityDataError(
            f"no raw/unadjusted close available for {ticker} before {target_session.isoformat()}; "
            "an adjusted-only series is not substituted"
        )

    packet_path = ticker_dir / "packet-draft.yaml"
    if packet_path.exists() and not force:
        raise OpportunityConflictError(
            f"packet draft already exists (use --force to regenerate): {packet_path}"
        )
    ticker_dir.mkdir(parents=True, exist_ok=True)

    packet_draft = _packet_draft_skeleton(
        ticker=ticker,
        asof=asof,
        price=price,
        sqlite_path=sqlite_path,
        screening_estimate=screening_estimate,
        screening_retrieved_at=retrieved_at,
    )
    write_text_atomic(packet_path, _dump_yaml(packet_draft))
    checklist = _checklist_skeleton(price=price)
    write_text_atomic(ticker_dir / "research-checklist.yaml", _dump_yaml(checklist))
    return {
        "packet_draft": str(packet_path),
        "price_as_of": price.price_as_of.isoformat(),
        "close_yen": price.close_yen,
        "corporate_action_unresolved": price.corporate_action_unresolved,
        "screening_estimate_transferred": screening_estimate is not None,
        "screening_estimate_transfer_reason": transfer_reason,
    }


def _packet_draft_skeleton(
    *,
    ticker: str,
    asof: date,
    price: PreviousClose,
    sqlite_path: Path,
    screening_estimate: dict[str, object] | None,
    screening_retrieved_at: datetime,
) -> dict[str, object]:
    # The observed close is the previous business day's raw/unadjusted close, emitted
    # as the packet's single market_price fact so the draft is schema-valid on load.
    # AI judgment fields are left null so the operator fills them from primary sources;
    # no value is guessed. An adjustment_factor anomaly is surfaced to the corporate
    # action checklist (not the fact), which blocks that check.
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
                "source_id": "screening_selection",
                "ticker": ticker,
                "source_tier": "local_data",
                "provider": "baibai-loop",
                "dataset": "screening-selection",
                "retrieved_at": screening_retrieved_at.isoformat(),
                "as_of": asof.isoformat(),
                "used_for": "screening expected return and fair value anchor",
            }
        )
    input_snapshot = {
        "snapshot_version": 1,
        "producer_model_version": "screening-selection-v1",
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
                "unit": "JPY",
                "as_of": price.price_as_of.isoformat(),
                "source_ids": ["market_close"],
                "observed_at": observed_at.isoformat(),
                "price_basis": "last_close_unadjusted",
            }
        ],
    }
    if screening_estimate is not None:
        input_snapshot["screening_estimate"] = screening_estimate
    return {
        "schema_version": 2,
        "input_snapshot": input_snapshot,
        "derived": {"metrics": []},
        "estimates": None,
        "permanent_loss_risks": [],
        "judgment": None,
        "independent_review_ref": None,
    }


def _screening_estimate_from_selection_output(
    *, manifest: Mapping[str, object], ticker: str, asof: date
) -> tuple[dict[str, object] | None, str | None]:
    inputs = _required_mapping(manifest.get("inputs"), label="manifest.inputs")
    selection_ref = _required_mapping(
        inputs.get("selection_output"), label="manifest.inputs.selection_output"
    )
    selection_path = _nonempty_string(
        selection_ref.get("path"), label="manifest.inputs.selection_output.path"
    )
    selection = _load_mapping(Path(selection_path), label="selection output")
    audit_pool = _dict_list(selection.get("audit_pool"))
    audit_tickers = [str(row.get("ticker") or "") for row in audit_pool]
    if len(audit_tickers) != len(set(audit_tickers)):
        raise OpportunityDataError("selection output audit_pool tickers must be unique")
    matching_rows = [row for row in audit_pool if str(row.get("ticker") or "") == ticker]
    if len(matching_rows) != 1:
        raise OpportunityDataError(
            f"selection output audit_pool must contain ticker exactly once: {ticker}"
        )
    row = matching_rows[0]
    if "estimate_snapshot" not in row:
        return None, "estimate_snapshot_missing"

    snapshot = _required_mapping(row["estimate_snapshot"], label="estimate_snapshot")
    selection_metadata = _required_mapping(selection.get("selection"), label="selection.selection")
    expected_asof = asof.isoformat()
    if selection_metadata.get("asof") != expected_asof or snapshot.get("as_of") != expected_asof:
        raise OpportunityDataError("selection, estimate snapshot, and manifest as_of must match")
    expected_return = _required_mapping(
        snapshot.get("expected_return"), label="estimate_snapshot.expected_return"
    )
    fair_value = _required_mapping(snapshot.get("fair_value"), label="estimate_snapshot.fair_value")
    annual = _finite_number(
        expected_return.get("annual"), label="estimate_snapshot.expected_return.annual"
    )
    if expected_return.get("origin") != "estimate" or fair_value.get("origin") != "estimate":
        raise OpportunityDataError("estimate_snapshot origin must be estimate")
    if expected_return.get("unit") != "annual_ratio":
        raise OpportunityDataError("estimate_snapshot expected return unit must be annual_ratio")
    if fair_value.get("unit") != "JPY_per_share":
        raise OpportunityDataError("estimate_snapshot fair value unit must be JPY_per_share")
    model_version = _nonempty_string(
        expected_return.get("model_version"),
        label="estimate_snapshot.expected_return.model_version",
    )
    fair_value_model_version = _nonempty_string(
        fair_value.get("model_version"), label="estimate_snapshot.fair_value.model_version"
    )
    assumptions = _nonempty_string(
        expected_return.get("assumptions"), label="estimate_snapshot.expected_return.assumptions"
    )
    fair_value_assumptions = _nonempty_string(
        fair_value.get("assumptions"), label="estimate_snapshot.fair_value.assumptions"
    )
    if model_version != fair_value_model_version or assumptions != fair_value_assumptions:
        raise OpportunityDataError("estimate_snapshot model version and assumptions must agree")

    displayed_er_pct = _finite_number(
        row.get("expected_return_pct"), label="audit_pool.expected_return_pct"
    )
    if displayed_er_pct != round(annual * 100, 4):
        raise OpportunityDataError("estimate_snapshot expected return does not match audit row")

    anchors = _required_mapping(
        fair_value.get("anchors"), label="estimate_snapshot.fair_value.anchors"
    )
    positive_anchors: list[float] = []
    for key in ("fv_sector_median_yen", "fv_self_range_yen"):
        value = anchors.get(key)
        if value is None:
            continue
        number = _finite_number(value, label=f"estimate_snapshot.fair_value.anchors.{key}")
        if number <= 0:
            raise OpportunityDataError(
                f"estimate_snapshot fair value anchor must be positive: {key}"
            )
        positive_anchors.append(number)
    raw_fair_value_anchor_yen = min(positive_anchors) if positive_anchors else None
    displayed_fair_value = row.get("fair_value_anchor_yen")
    if raw_fair_value_anchor_yen is None:
        if displayed_fair_value is not None:
            raise OpportunityDataError("null estimate anchors do not match audit row fair value")
    else:
        displayed_anchor = _finite_number(
            displayed_fair_value, label="audit_pool.fair_value_anchor_yen"
        )
        if displayed_anchor != round(raw_fair_value_anchor_yen, 4):
            raise OpportunityDataError("estimate_snapshot fair value does not match audit row")

    fair_value_anchor_yen = (
        None
        if raw_fair_value_anchor_yen is None
        else float(
            Decimal(str(raw_fair_value_anchor_yen)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )
    )
    screening_estimate = {
        "origin": "estimate",
        "model_version": model_version,
        "as_of": asof.isoformat(),
        "expected_return_annual_ratio": annual,
        "expected_return_unit": "annual_ratio",
        "fair_value_anchor_yen": fair_value_anchor_yen,
        "fair_value_unit": "JPY_per_share",
        "assumptions": assumptions,
        "source_ids": ["screening_selection"],
    }
    try:
        ScreeningEstimate.model_validate(screening_estimate)
    except (ValidationError, ValueError) as error:
        raise OpportunityDataError(
            f"screening estimate violates packet contract: {error}"
        ) from error
    return screening_estimate, None


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OpportunityDataError(f"{label} must be a mapping")
    return value


def _nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpportunityDataError(f"{label} must be a non-empty string")
    return value


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OpportunityDataError(f"{label} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise OpportunityDataError(f"{label} must be finite") from error
    if not isfinite(number):
        raise OpportunityDataError(f"{label} must be finite")
    return number


def _validate_selection_estimate_asof(
    *, selection: Mapping[str, object], audit_pool: Sequence[Mapping[str, object]], asof: date
) -> None:
    snapshots = [row["estimate_snapshot"] for row in audit_pool if "estimate_snapshot" in row]
    if not snapshots:
        return
    selection_metadata = _required_mapping(selection.get("selection"), label="selection.selection")
    expected_asof = asof.isoformat()
    if selection_metadata.get("asof") != expected_asof:
        raise OpportunityDataError("selection as_of does not match prepare as_of")
    for snapshot_value in snapshots:
        snapshot = _required_mapping(snapshot_value, label="estimate_snapshot")
        if snapshot.get("as_of") != expected_asof:
            raise OpportunityDataError("estimate_snapshot as_of does not match prepare as_of")


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
    _require_primary_research_ticker(workspace, ticker)
    ticker_dir = _research_lane_dir(workspace, ticker)
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

    ticker_dir = _research_lane_dir(workspace, ticker)
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
    probability is asserted. Budget, cash, dry powder, concentration, and reservations
    in other tickers are warnings/annotations only — they never change the investment
    ranking or limit price. An active reservation in the selected ticker defers a new
    order until human-confirmed broker state is recorded. ``defer`` is a normal
    judgment (exit 0).
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

    snapshot, source_ledger_sha256 = _load_snapshot_with_sha256(ledger)
    portfolio_annotations = _portfolio_annotations(snapshot, ticker=ticker)
    if any(reservation.ticker == ticker for reservation in snapshot.active_reservations):
        defer_reasons.append("active_reservation_exists")
    expires_at = datetime.combine(target_session, time(15, 30), tzinfo=JST)

    base_output: dict[str, object] = {
        "ticker": ticker,
        "decision_packet_ref": str(packet),
        "decision_packet_sha256": _sha256_text(packet.read_text(encoding="utf-8")),
        "decision_packet_core_sha256": decision_packet_core_hash(document),
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
        "max_acceptable_price_yen": _decimal_to_number(max_price),
        "board_lot": BOARD_LOT,
        "budget_min_yen": budget_min_yen,
        "budget_max_yen": budget_max_yen,
        "portfolio_annotations": portfolio_annotations,
        "source_ledger_sha256": source_ledger_sha256,
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

    portfolio_exposure, exposure_warnings, exposure_total_capital_yen = _portfolio_exposure(
        snapshot,
        sqlite_path=sqlite_path,
        price_as_of=price.price_as_of,
        ticker=ticker,
        sector=document.input_snapshot.sector,
        common_factors=document.input_snapshot.common_factors,
        order_notional_yen=int(notional),
    )
    warnings.extend(
        _portfolio_warnings(
            snapshot,
            notional_yen=notional,
            total_capital_yen=exposure_total_capital_yen,
        )
    )
    warnings.extend(exposure_warnings)
    return {
        "status": "planned_limit",
        **base_output,
        "limit_price_yen": _decimal_to_number(close_decimal),
        "quantity": quantity,
        "notional_yen": int(notional),
        "portfolio_exposure": portfolio_exposure,
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


def _portfolio_warnings(
    snapshot: PortfolioSnapshot, *, notional_yen: Decimal, total_capital_yen: int
) -> list[str]:
    # Cash / dry powder shortfalls are human-decision warnings only; they never
    # downgrade the investment ranking or auto-switch to a cheaper next candidate.
    warnings: list[str] = []
    if notional_yen > snapshot.available_cash_yen:
        warnings.append("available_cash_below_notional")
    dry_powder_pct = Decimal(str(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"]))
    dry_powder_floor = Decimal(total_capital_yen) * dry_powder_pct / 100
    if Decimal(snapshot.available_cash_yen) - notional_yen < dry_powder_floor:
        warnings.append("dry_powder_below_floor")
    return warnings


def _portfolio_exposure(
    snapshot: PortfolioSnapshot,
    *,
    sqlite_path: Path,
    price_as_of: date,
    ticker: str,
    sector: str,
    common_factors: Sequence[str],
    order_notional_yen: int,
) -> tuple[dict[str, object], list[str], int]:
    """Derive prospective concentration with disclosed common-factor coverage.

    Candidate holdings/reservations use the packet's current factor classification.
    Other tickers retain ledger classifications; empty classifications are reported,
    so common-factor exposure remains an explicit lower bound rather than a silent
    claim of complete portfolio coverage.
    """
    holding_values: dict[str, int] = {}
    fallback_tickers: list[str] = []
    for holding in snapshot.holdings:
        resolved = resolve_holding_close_on_basis(
            sqlite_path=sqlite_path,
            ticker=holding.ticker,
            ledger_price_observed_on=holding.market_price_observed_at.date(),
            basis_as_of=price_as_of,
        )
        market_value = (
            Decimal(str(resolved.close_yen)) * holding.quantity if resolved is not None else None
        )
        if market_value is None or market_value != market_value.to_integral_value():
            holding_values[holding.ticker] = holding.market_value_yen
            fallback_tickers.append(holding.ticker)
        else:
            holding_values[holding.ticker] = int(market_value)

    total_capital_yen = (
        snapshot.available_cash_yen + snapshot.reserved_cash_yen + sum(holding_values.values())
    )
    risk_policy = PORTFOLIO_POLICY["risk_budget"]
    ticker_warning_pct = Decimal(str(risk_policy["max_ticker_concentration_pct"]))
    sector_warning_pct = Decimal(str(risk_policy["max_sector_concentration_pct"]))
    factor_warning_pct = Decimal(str(risk_policy["max_common_factor_concentration_pct"]))

    def current_exposure(*, scope: str, key: str) -> int:
        holding_yen = sum(
            holding_values[holding.ticker]
            for holding in snapshot.holdings
            if (
                (scope == "ticker" and holding.ticker == key)
                or (scope == "sector" and holding.sector == key)
                or (
                    scope == "common_factor"
                    and (
                        key in holding.common_factors
                        or (holding.ticker == ticker and key in common_factors)
                    )
                )
            )
        )
        reservation_yen = sum(
            reservation.reserved_yen
            for reservation in snapshot.active_reservations
            if (
                (scope == "ticker" and reservation.ticker == key)
                or (scope == "sector" and reservation.sector == key)
                or (
                    scope == "common_factor"
                    and (
                        key in reservation.common_factors
                        or (reservation.ticker == ticker and key in common_factors)
                    )
                )
            )
        )
        return holding_yen + reservation_yen

    def exposure_row(*, scope: str, key: str, warning_pct: Decimal) -> dict[str, object]:
        current_yen = current_exposure(scope=scope, key=key)
        prospective_yen = current_yen + order_notional_yen
        prospective_pct = (Decimal(prospective_yen) * 100 / Decimal(total_capital_yen)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return {
            "key": key,
            "current_and_reserved_yen": current_yen,
            "prospective_yen": prospective_yen,
            "prospective_pct": float(prospective_pct),
            "warning_pct": _decimal_to_number(warning_pct),
        }

    ticker_current_yen = current_exposure(scope="ticker", key=ticker)
    sector_current_yen = current_exposure(scope="sector", key=sector)
    factor_current_yen = {
        factor: current_exposure(scope="common_factor", key=factor) for factor in common_factors
    }
    ticker_row = exposure_row(scope="ticker", key=ticker, warning_pct=ticker_warning_pct)
    sector_row = exposure_row(scope="sector", key=sector, warning_pct=sector_warning_pct)
    factor_rows = [
        exposure_row(scope="common_factor", key=factor, warning_pct=factor_warning_pct)
        for factor in common_factors
    ]
    common_factor_empty_tickers = sorted(
        {
            holding.ticker
            for holding in snapshot.holdings
            if holding.ticker != ticker and not holding.common_factors
        }
        | {
            reservation.ticker
            for reservation in snapshot.active_reservations
            if reservation.ticker != ticker and not reservation.common_factors
        }
    )
    warnings = [
        f"portfolio_exposure_ledger_fallback:{fallback_ticker}"
        for fallback_ticker in sorted(fallback_tickers)
    ]
    if common_factor_empty_tickers:
        warnings.append("portfolio_exposure_common_factor_coverage_incomplete")
    if (
        Decimal(ticker_current_yen + order_notional_yen) * 100 / Decimal(total_capital_yen)
        > ticker_warning_pct
    ):
        warnings.append("prospective_ticker_concentration_exceeds_warning")
    if (
        Decimal(sector_current_yen + order_notional_yen) * 100 / Decimal(total_capital_yen)
        > sector_warning_pct
    ):
        warnings.append("prospective_sector_concentration_exceeds_warning")
    for factor in common_factors:
        if Decimal(factor_current_yen[factor] + order_notional_yen) * 100 / Decimal(
            total_capital_yen
        ) > (factor_warning_pct):
            warnings.append(f"prospective_common_factor_concentration_exceeds_warning:{factor}")
    return (
        {
            "price_as_of": price_as_of.isoformat(),
            "price_basis": "last_close_unadjusted",
            "total_capital_yen": total_capital_yen,
            "holding_valuation_status": (
                "mixed_with_ledger_fallback" if fallback_tickers else "same_asof_raw_close"
            ),
            "ledger_fallback_tickers": sorted(fallback_tickers),
            "common_factor_empty_tickers": common_factor_empty_tickers,
            "ticker": ticker_row,
            "sector": sector_row,
            "common_factors": factor_rows,
        },
        warnings,
        total_capital_yen,
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _load_snapshot(ledger: Path) -> PortfolioSnapshot:
    snapshot, _source_sha256 = _load_snapshot_with_sha256(ledger)
    return snapshot


def _load_snapshot_with_sha256(ledger: Path) -> tuple[PortfolioSnapshot, str]:
    try:
        document, source_sha256 = load_portfolio_ledger_with_sha256(ledger)
        return reconcile_portfolio(document), source_sha256
    except (OSError, ValueError) as error:
        raise OpportunityDataError(f"cannot reconcile ledger: {error}") from error


def _load_checklist(workspace: Path, ticker: str) -> list[dict[str, object]]:
    checklist_path = _research_lane_dir(workspace, ticker) / "research-checklist.yaml"
    payload = _load_mapping(checklist_path, label="research checklist")
    return _dict_list(payload.get("checks"))


def _packet_validation_errors(workspace: Path, ticker: str) -> list[str]:
    packet_path = _research_lane_dir(workspace, ticker) / "packet-draft.yaml"
    if not packet_path.exists():
        return ["packet draft missing"]
    try:
        load_decision_packet(packet_path)
    except DecisionPacketError as error:
        return [str(error).splitlines()[0]]
    return []


def _review_validation_errors(workspace: Path, ticker: str) -> list[str]:
    review_path = _research_lane_dir(workspace, ticker) / "review-draft.yaml"
    if not review_path.exists():
        return ["review draft missing"]
    try:
        review = load_independent_review(review_path)
    except DecisionPacketError as error:
        return [str(error).splitlines()[0]]
    packet_path = _research_lane_dir(workspace, ticker) / "packet-draft.yaml"
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
