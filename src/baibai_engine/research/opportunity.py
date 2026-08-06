"""Opportunity authoring: prepare a workspace, scaffold thesis/review drafts, and
derive a planning-only limit from the previous business day's raw close.

This module is pure logic behind ``baibai-engine research``. It never submits an
order, never asserts fill probability, and never reads a realtime quote. The
canonical price basis for a limit is the JPX store's latest complete business-day
*raw/unadjusted* close before the target session; adjusted series are for past
comparison only and are never used for a limit.

Design boundaries (Issue #359 Milestone A):

- Investment value is decided before budget rounding. The 20-30万円 guide is a
  sizing annotation, never a hard gate: a single board lot above the guide still
  produces a proposal with a warning rather than an auto-reject.
- ``promote`` is the only command that publishes a canonical thesis/review;
  every other command writes only to the rebuildable ``.cache/opportunity/<asof>/``
  workspace.
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
from typing import get_args

import yaml
from pydantic import BaseModel, ValidationError

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    PortfolioSnapshot,
    reconcile_portfolio,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.store import LedgerStoreService

from .close_source import (
    PreviousClose,
    resolve_previous_business_day_close,
)
from .decimal_number import decimal_to_number
from .execution_policy import ExecutionPolicyError, max_acceptable_price
from .portfolio_exposure import (
    planned_order_cash_warnings,
    portfolio_annotations,
    portfolio_exposure,
)
from .store import ResearchConflictError, ResearchStoreService, ResearchValidationError
from .thesis import (
    IndependentReview,
    ScreeningEstimate,
    ThesisError,
    evaluate_thesis,
    load_independent_review,
    load_thesis,
    thesis_core_hash,
)

TOOL_VERSION = "opportunity-v1"
BOARD_LOT: int = PORTFOLIO_POLICY["order_constraints"]["board_lot"]
STARTER_MAX_ORDER_NOTIONAL_YEN: int = PORTFOLIO_POLICY["starter_band"]["max_order_notional_yen"]
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
# independent review draft — thesis author と別 role が埋める。
# - reviewed_at は JST の現在時刻以前。
# - reviewed_thesis_sha256 は生成時点の thesis core hash に束縛される。thesis を
#   編集したら review-scaffold --force で作り直す（古い hash のままだと promote が拒否）。
# - 引用符なしの散文（strongest_countercase 等）に「: 」を書かない。YAML が mapping と
#   解釈して load が落ちる。区切りには「 — 」を使う。
"""


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
    longlist_size: int


def prepare_workspace(
    *,
    asof: date,
    selection_output: Path,
    db_path: Path | None,
    workspace: Path,
    force: bool = False,
) -> PrepareResult:
    """Build the opportunity workspace from a screening selection output and ledger.

    Holdings/reservations are ledger annotations, never hard exclusions: a held or
    reserved ticker stays a comparable candidate. An empty longlist is a normal
    'no actionable bargain' outcome and still produces a workspace.
    """
    selection = _load_mapping(selection_output, label="selection output")
    longlist = _dict_list(selection.get("longlist"))
    _validate_selection_estimate_asof(selection=selection, longlist=longlist, asof=asof)
    snapshot, append_head = _load_snapshot(db_path)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise OpportunityConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    held = {holding.ticker for holding in snapshot.holdings}
    reserved = {reservation.ticker for reservation in snapshot.active_reservations}
    annotated = [_annotate_candidate(row, held=held, reserved=reserved) for row in longlist]
    research_selection_target_max = _research_selection_target_max(selection)
    shortlist_slots = (
        min(research_selection_target_max, len(annotated))
        if research_selection_target_max > 0
        else len(annotated)
    )

    selection_doc = {
        "as_of": asof.isoformat(),
        "longlist": annotated,
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
                "sha256": _sha256_file(selection_output),
            },
            "ledger": {
                "entity_id": "portfolio-ledger",
                "append_head": append_head,
            },
        },
        "rules": {
            "research_selection_target_max": research_selection_target_max,
            "research_selection_playbook_order": _selection_playbook_order(selection),
        },
    }
    _write_workspace_file(manifest_path, manifest)
    _write_status(workspace, db_path=db_path)
    return PrepareResult(
        workspace=workspace,
        actionable=bool(annotated),
        shortlist_slots=shortlist_slots,
        longlist_size=len(annotated),
    )


def prepare_holding_workspace(
    *,
    asof: date,
    db_path: Path | None,
    ticker: str,
    workspace: Path,
    force: bool = False,
) -> PrepareResult:
    """Build a one-ticker research workspace for an actual open holding.

    Holding review bypasses screening selection because the canonical ledger is
    the source of its research target. The fixed longlist, shortlist, and
    selected ticker keep the existing thesis/review/promotion gates usable
    without weakening the normal opportunity-selection contract.
    """
    snapshot, append_head = _load_snapshot(db_path)
    holding = next((item for item in snapshot.holdings if item.ticker == ticker), None)
    if holding is None:
        raise OpportunityDataError(
            f"cannot prepare holding review for {ticker}: ticker is not an open holding"
        )
    if holding.market_price_observed_at.date() != asof:
        raise OpportunityDataError(
            f"cannot prepare holding review for {ticker}: holding market price date "
            f"{holding.market_price_observed_at.date().isoformat()} does not match --asof "
            f"{asof.isoformat()}"
        )

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise OpportunityConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    longlist = [
        {
            "rank": 1,
            "ticker": holding.ticker,
            "sector": holding.sector,
            "quantity": holding.quantity,
            "portfolio_annotation": "held",
        }
    ]
    selection_doc = {
        "as_of": asof.isoformat(),
        "longlist": longlist,
        "shortlist_slots": 1,
        "shortlist": [{"ticker": ticker, "reason": "open holding review"}],
        "actionable": True,
    }
    comparison_doc = _research_comparison(asof, longlist)
    comparison_doc["selected_ticker"] = ticker
    comparison_doc["ranking_rationale"] = "research target fixed by the canonical open holding"

    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "selection.yaml", selection_doc)
    _write_workspace_file(workspace / "research-comparison.yaml", comparison_doc)
    manifest = {
        "purpose": "holding_review",
        "holding_ticker": ticker,
        "as_of": asof.isoformat(),
        "tool_version": TOOL_VERSION,
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
    return PrepareResult(
        workspace=workspace,
        actionable=True,
        shortlist_slots=1,
        longlist_size=1,
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


def _write_status(workspace: Path, *, db_path: Path | None) -> dict[str, object]:
    status = compute_status(workspace, db_path=db_path)
    write_text_atomic(workspace / "status.yaml", _dump_yaml(status))
    return status


def compute_status(workspace: Path, *, db_path: Path | None = None) -> dict[str, object]:
    """Read the workspace and report completion, drift, and the next command.

    External inputs remain bound to their prepare-time hashes. Operator-authored
    drafts are editable, but their structure and lineage must remain consistent.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest, db_path=db_path)
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
            next_command="review the /shortlist gate and fill selection.yaml shortlist",
        )

    missing_lanes = [
        ticker
        for ticker in shortlist_tickers
        if not (_research_lane_dir(workspace, ticker) / "thesis-draft.yaml").is_file()
    ]
    if missing_lanes:
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            next_command=f"baibai-engine research thesis-scaffold --ticker {missing_lanes[0]}",
        )

    lane_pending: list[str] = []
    lane_blocked: list[str] = []
    lane_thesis_errors: list[str] = []
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
        lane_thesis_errors.extend(
            f"{ticker}:{error}" for error in _thesis_validation_errors(workspace, ticker)
        )
    if lane_pending or lane_thesis_errors:
        first_ticker = (lane_pending or lane_thesis_errors)[0].split(":", maxsplit=1)[0]
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            pending_checks=lane_pending,
            blocked_checks=lane_blocked,
            thesis_validation_errors=lane_thesis_errors,
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

    checklist_path = _research_lane_dir(workspace, selected_ticker) / "research-checklist.yaml"
    if not checklist_path.exists():
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            next_command=f"baibai-engine research thesis-scaffold --ticker {selected_ticker}",
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
    thesis_errors = _thesis_validation_errors(workspace, selected_ticker)
    review_errors = _review_validation_errors(
        workspace, selected_ticker, _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    )

    workspace_status = _resolve_workspace_status(
        pending=pending, blocked=blocked, thesis_errors=thesis_errors, review_errors=review_errors
    )
    return _status_payload(
        workspace_status=workspace_status,
        selected_ticker=selected_ticker,
        completed_checks=len(completed),
        pending_checks=[value for value in pending if value is not None],
        blocked_checks=[value for value in blocked if value is not None],
        thesis_validation_errors=thesis_errors,
        review_validation_errors=review_errors,
        next_command=_next_command(workspace_status, selected_ticker),
    )


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


def _next_command(workspace_status: str, ticker: str) -> str:
    match workspace_status:
        case "deferred":
            return "resolve blocked checks or defer the candidate"
        case "incomplete":
            return f"baibai-engine research thesis-scaffold --ticker {ticker}"
        case "ready_for_review":
            return f"baibai-engine research review-scaffold --ticker {ticker}"
        case _:
            return f"baibai-engine research promote --ticker {ticker}"


def _status_payload(
    *,
    workspace_status: str,
    selected_ticker: str | None,
    completed_checks: int = 0,
    pending_checks: Sequence[str] | None = None,
    blocked_checks: Sequence[str] | None = None,
    thesis_validation_errors: Sequence[str] | None = None,
    review_validation_errors: Sequence[str] | None = None,
    next_command: str,
) -> dict[str, object]:
    return {
        "workspace_status": workspace_status,
        "selected_ticker": selected_ticker,
        "completed_checks": completed_checks,
        "pending_checks": list(pending_checks or []),
        "blocked_checks": list(blocked_checks or []),
        "thesis_validation_errors": list(thesis_validation_errors or []),
        "review_validation_errors": list(review_validation_errors or []),
        "next_command": next_command,
    }


def _verify_external_inputs(manifest: Mapping[str, object], *, db_path: Path | None = None) -> None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise OpportunityDataError("manifest is missing external input hashes")
    purpose = str(manifest.get("purpose") or "opportunity")
    required_inputs: tuple[str, ...]
    if purpose == "opportunity":
        required_inputs = ("selection_output", "ledger")
    elif purpose == "holding_review":
        required_inputs = ("ledger",)
    else:
        raise OpportunityDataError(f"manifest purpose is invalid: {purpose}")
    for name in required_inputs:
        input_ref = inputs.get(name)
        if not isinstance(input_ref, Mapping):
            raise OpportunityDataError(f"manifest is missing input hash: {name}")
        if name == "ledger":
            entity_id = input_ref.get("entity_id")
            expected_head = input_ref.get("append_head")
            if entity_id != "portfolio-ledger" or not isinstance(expected_head, int):
                raise OpportunityDataError("manifest ledger revision is invalid")
            try:
                current_head = LedgerStoreService(db_path).append_head()
            except (OSError, RuntimeError, ValueError) as error:
                raise OpportunityDataError(
                    f"cannot read canonical ledger revision: {error}"
                ) from error
            if current_head != expected_head:
                raise OpportunityConflictError(
                    "canonical ledger changed since workspace prepare (append head drift)"
                )
            continue
        path_value = input_ref.get("path")
        expected = input_ref.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected, str):
            raise OpportunityDataError(f"manifest input ref is invalid: {name}")
        path = Path(path_value)
        if not path.is_file():
            raise OpportunityConflictError(f"workspace external input is missing: {name}")
        actual = _sha256_file(path)
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

    purpose = str(manifest.get("purpose") or "opportunity")
    if purpose == "holding_review":
        _validate_holding_review_drafts(selection, comparison, manifest)
        return
    if purpose != "opportunity":
        raise OpportunityDataError(f"manifest purpose is invalid: {purpose}")

    longlist = _dict_list(selection.get("longlist"))
    longlist_tickers = [str(row.get("ticker") or "") for row in longlist]
    if (not longlist_tickers or any(not ticker for ticker in longlist_tickers)) and selection.get(
        "actionable"
    ):
        raise OpportunityDataError("workspace longlist is invalid")
    shortlist = _dict_list(selection.get("shortlist"))
    shortlist_slots = selection.get("shortlist_slots")
    if not isinstance(shortlist_slots, int) or shortlist_slots < 0:
        raise OpportunityDataError("workspace shortlist_slots is invalid")
    shortlist_tickers = [str(row.get("ticker") or "") for row in shortlist]
    if (
        len(shortlist) > shortlist_slots
        or len(shortlist_tickers) != len(set(shortlist_tickers))
        or any(ticker not in longlist_tickers for ticker in shortlist_tickers)
    ):
        raise OpportunityDataError("workspace shortlist is invalid")

    candidates = _dict_list(comparison.get("candidates"))
    comparison_tickers = [str(row.get("ticker") or "") for row in candidates]
    if comparison_tickers != longlist_tickers:
        raise OpportunityDataError("research comparison candidates do not match longlist")
    selected = _string_or_none(comparison.get("selected_ticker"))
    if selected is not None and selected not in shortlist_tickers:
        raise OpportunityDataError("selected_ticker is not present in shortlist")


def _validate_holding_review_drafts(
    selection: Mapping[str, object],
    comparison: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    ticker = _string_or_none(manifest.get("holding_ticker"))
    if ticker is None:
        raise OpportunityDataError("holding-review manifest is missing holding_ticker")
    longlist_tickers = [
        str(row.get("ticker") or "") for row in _dict_list(selection.get("longlist"))
    ]
    shortlist_tickers = [
        str(row.get("ticker") or "") for row in _dict_list(selection.get("shortlist"))
    ]
    comparison_tickers = [
        str(row.get("ticker") or "") for row in _dict_list(comparison.get("candidates"))
    ]
    if (
        longlist_tickers != [ticker]
        or shortlist_tickers != [ticker]
        or comparison_tickers != [ticker]
        or selection.get("shortlist_slots") != 1
        or selection.get("actionable") is not True
        or comparison.get("selected_ticker") != ticker
    ):
        raise OpportunityDataError(
            "holding-review workspace must keep its longlist, shortlist, and selected ticker fixed"
        )


def _research_lane_dir(workspace: Path, ticker: str) -> Path:
    """Return a path-confined ticker lane inside the shared opportunity workspace."""

    workspace_root = workspace.resolve()
    ticker_dir = (workspace_root / ticker).resolve()
    if ticker_dir.parent != workspace_root:
        raise OpportunityDataError(
            f"research lane must be a direct child of the workspace: {ticker}"
        )
    return ticker_dir


def _review_filename(*, asof: date, ticker: str) -> str:
    """Return the stable independent-review filename for a lane.

    The thesis payload carries this name in ``independent_review_ref`` and
    ``plan-limit`` resolves the review by that name from the thesis's own
    directory, so scaffold, status, and promote all address the same path and no
    copy step stands between the draft and the gate.
    """

    return f"{asof:%Y-%m-%d}-{ticker}-decision-review.yaml"


def _review_draft_path(workspace: Path, ticker: str, asof: date) -> Path:
    return _research_lane_dir(workspace, ticker) / _review_filename(asof=asof, ticker=ticker)


def _require_primary_research_ticker(workspace: Path, ticker: str, *, action: str) -> None:
    selection = _load_mapping(workspace / "selection.yaml", label="workspace selection")
    shortlist_tickers = {
        str(row.get("ticker") or "") for row in _dict_list(selection.get("shortlist"))
    }
    if ticker not in shortlist_tickers:
        raise OpportunityDataError(
            f"cannot {action} for {ticker}: ticker is not in the primary-research set"
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
    _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest)
    _require_primary_research_ticker(workspace, ticker, action="scaffold research lane")
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    purpose = str(manifest.get("purpose") or "opportunity")
    screening_estimate: dict[str, object] | None
    transfer_reason: str | None
    if purpose == "holding_review":
        screening_estimate, transfer_reason = None, "not_applicable_holding_review"
    else:
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
    if purpose == "holding_review" and price.price_as_of != asof:
        raise OpportunityDataError(
            f"raw close date {price.price_as_of.isoformat()} does not match workspace manifest "
            f"as_of {asof.isoformat()}; --target-session must be the next trading session"
        )

    thesis_path = ticker_dir / "thesis-draft.yaml"
    if thesis_path.exists() and not force:
        raise OpportunityConflictError(
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
    price: PreviousClose,
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
        "schema_version": 2,
        "input_snapshot": input_snapshot,
        "derived": {"metrics": []},
        "estimates": None,
        "permanent_loss_risks": [],
        "judgment": None,
        "independent_review_ref": _review_filename(asof=asof, ticker=ticker),
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
    longlist = _dict_list(selection.get("longlist"))
    longlist_tickers = [str(row.get("ticker") or "") for row in longlist]
    if len(longlist_tickers) != len(set(longlist_tickers)):
        raise OpportunityDataError("selection output longlist tickers must be unique")
    matching_rows = [row for row in longlist if str(row.get("ticker") or "") == ticker]
    if len(matching_rows) != 1:
        raise OpportunityDataError(
            f"selection output longlist must contain ticker exactly once: {ticker}"
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
        row.get("expected_return_pct"), label="longlist.expected_return_pct"
    )
    if displayed_er_pct != round(annual * 100, 4):
        raise OpportunityDataError("estimate_snapshot expected return does not match longlist row")

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
            raise OpportunityDataError("null estimate anchors do not match longlist row fair value")
    else:
        displayed_anchor = _finite_number(
            displayed_fair_value, label="longlist.fair_value_anchor_yen"
        )
        if displayed_anchor != round(raw_fair_value_anchor_yen, 4):
            raise OpportunityDataError("estimate_snapshot fair value does not match longlist row")

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
            f"screening estimate violates thesis contract: {error}"
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
    *, selection: Mapping[str, object], longlist: Sequence[Mapping[str, object]], asof: date
) -> None:
    snapshots = [row["estimate_snapshot"] for row in longlist if "estimate_snapshot" in row]
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


def scaffold_review(
    *, workspace: Path, ticker: str, db_path: Path | None = None, force: bool = False
) -> dict[str, object]:
    """Write the independent review draft bound to the current thesis core hash.

    The review author is a distinct role from the thesis author; this scaffold only
    lays out the recalculation slots and never produces the review conclusions. The
    bound ``reviewed_thesis_sha256`` is what lets ``promote`` detect a stale review.
    The file lands under the stable name the thesis already references, so promote
    and plan-limit resolve it without an intervening copy.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest)
    _require_primary_research_ticker(workspace, ticker, action="scaffold review")
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    ticker_dir = _research_lane_dir(workspace, ticker)
    thesis_path = ticker_dir / "thesis-draft.yaml"
    if not thesis_path.exists():
        raise OpportunityDataError(f"thesis draft not found for {ticker}: {thesis_path}")

    core_hash = _thesis_core_hash_if_valid(thesis_path)
    review_path = _review_draft_path(workspace, ticker, asof)
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
    header = _REVIEW_DRAFT_HEADER + _enum_field_header(IndependentReview)
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
    _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest)
    # Every researched lane earns a canonical thesis, not only the one being bought.
    # A cycle that buys nothing still produced the judgment that says why, and the
    # bargain assessment binds each lane's machine values to a stored thesis.
    _require_primary_research_ticker(workspace, ticker, action="promote")

    manifest_asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    ticker_dir = _research_lane_dir(workspace, ticker)
    thesis_path = ticker_dir / "thesis-draft.yaml"
    review_path = _review_draft_path(workspace, ticker, manifest_asof)
    if not thesis_path.exists() or not review_path.exists():
        raise OpportunityDataError(f"thesis or review draft missing for {ticker}")

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
        document = load_thesis(thesis_path)
        review = load_independent_review(review_path)
    except ThesisError as error:
        raise OpportunityDataError(f"draft is not schema-valid: {error}") from error

    if document.input_snapshot.ticker != ticker:
        raise OpportunityDataError(
            f"cannot promote {ticker}: thesis ticker is {document.input_snapshot.ticker}"
        )
    if document.input_snapshot.as_of != manifest_asof:
        raise OpportunityDataError(
            f"cannot promote {ticker}: thesis as_of {document.input_snapshot.as_of.isoformat()} "
            f"does not match workspace manifest as_of {manifest_asof.isoformat()}"
        )

    core_hash = thesis_core_hash(document)
    if review.reviewed_thesis_sha256 != core_hash:
        raise OpportunityDataError(
            "review is stale: reviewed_thesis_sha256 does not match the thesis core hash"
        )
    if review.proposal_changed:
        raise OpportunityDataError(
            "review changed the proposal; regenerate the thesis and re-review before promotion"
        )

    result = evaluate_thesis(document, review=review, now=now)
    if result.decision_readiness != "ready":
        raise OpportunityDataError(f"thesis is not decision-ready: {list(result.errors)}")

    stable_review_name = _review_filename(asof=document.input_snapshot.as_of, ticker=ticker)
    # The ref stays inside the thesis payload and its core hash so a stored thesis
    # still names the review it was decided against; the canonical binding in the DB
    # is the thesis_id FK.
    if document.independent_review_ref != stable_review_name:
        raise OpportunityDataError(
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
            _load_mapping(review_path, label="independent review"),
            supersedes_id=supersedes_id,
        )
    except ResearchConflictError as error:
        raise OpportunityConflictError(str(error)) from error
    except (ResearchValidationError, ValidationError) as error:
        raise OpportunityDataError(str(error)) from error
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
    review = load_independent_review(review_path) if review_path is not None else None
    defer_reasons: list[str] = []

    result = evaluate_thesis(document, review=review, now=now)
    if result.decision_readiness != "ready":
        defer_reasons.append("thesis_not_decision_ready")

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
    if document.judgment.position_intent == "starter":
        # starter は「観測をゼロから非ゼロにする」ための枠なので、1 単元が上限を超える
        # 銘柄は 1 単元へ切り上げず defer にする。切り上げると縮小 lot の意味が消える。
        if lot_notional > STARTER_MAX_ORDER_NOTIONAL_YEN:
            return {
                "status": "defer",
                **base_output,
                "limit_price_yen": None,
                "quantity": 0,
                "notional_yen": 0,
                "warnings": [],
                "defer_reasons": ["starter_lot_exceeds_notional_cap"],
            }
        budget_max_yen = min(budget_max_yen, STARTER_MAX_ORDER_NOTIONAL_YEN)
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

    exposure, exposure_warnings, exposure_total_capital_yen = portfolio_exposure(
        snapshot,
        sqlite_path=sqlite_path,
        price_as_of=price.price_as_of,
        ticker=ticker,
        sector=document.input_snapshot.sector,
        common_factors=document.input_snapshot.common_factors,
        order_notional_yen=int(notional),
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
        raise OpportunityDataError(f"cannot reconcile ledger: {error}") from error


def _load_checklist(workspace: Path, ticker: str) -> list[dict[str, object]]:
    checklist_path = _research_lane_dir(workspace, ticker) / "research-checklist.yaml"
    payload = _load_mapping(checklist_path, label="research checklist")
    return _dict_list(payload.get("checks"))


def _thesis_validation_errors(workspace: Path, ticker: str) -> list[str]:
    thesis_path = _research_lane_dir(workspace, ticker) / "thesis-draft.yaml"
    if not thesis_path.exists():
        return ["thesis draft missing"]
    try:
        load_thesis(thesis_path)
    except ThesisError as error:
        return [str(error).splitlines()[0]]
    return []


def _review_validation_errors(workspace: Path, ticker: str, asof: date) -> list[str]:
    review_path = _review_draft_path(workspace, ticker, asof)
    if not review_path.exists():
        return ["review draft missing"]
    try:
        review = load_independent_review(review_path)
    except ThesisError as error:
        return [str(error).splitlines()[0]]
    thesis_path = _research_lane_dir(workspace, ticker) / "thesis-draft.yaml"
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
        raise OpportunityDataError("independent_review_ref must stay beside the thesis")
    if not resolved.exists():
        raise OpportunityDataError(f"independent review not found beside thesis: {resolved}")
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
    "scaffold_review",
    "scaffold_thesis",
]
