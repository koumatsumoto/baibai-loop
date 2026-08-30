"""Fundamental Research authoring: prepare a workspace, scaffold thesis/review drafts, and
derive a planning-only limit from the previous business day's raw close.

This module is pure logic behind ``baibai-engine research``. It never submits an
order, never asserts fill probability, and never reads a realtime quote. The
canonical price basis for a limit is the JPX store's latest complete business-day
*raw/unadjusted* close before the target session; adjusted series are for past
comparison only and are never used for a limit.

Design boundaries (Issue #359 Milestone A):

- Investment value is decided before budget rounding. The 20-30万円 guide is a
  sizing annotation for a normal position. Reduced sizing is exactly one board lot.
- ``promote`` is the only command that publishes a canonical thesis/review;
  every other command writes only to a rebuildable workspace chosen by the caller.
  workspace.
- ``defer`` and ``no_allocation`` are normal investment judgments and exit 0.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import get_args

import yaml
from pydantic import BaseModel, ValidationError

from baibai_engine.appdb.paths import database_path
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.repository_layout import ER_LEVEL_CALIBRATION_CONTEXT_PATH
from baibai_engine.foundation.review_set import (
    ReviewSetResolutionError,
    resolve_review_set_entries,
)
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    PortfolioSnapshot,
    reconcile_portfolio,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api.research_triage import research_triage_payloads_for_review_set

from .close_source import (
    PreviousClose,
    resolve_previous_business_day_close,
)
from .decimal_number import decimal_to_number
from .er_distribution_context import (
    HURDLE_ER_ANNUAL,
    valid_er_distribution_context_payload,
)
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
    UnpublishedThesis,
    evaluate_thesis,
    load_independent_review,
    load_thesis,
    thesis_core_hash,
)

TOOL_VERSION = "fundamental-research-v1"
# Research reads the ResearchTriage judgment, so it only accepts the research_triage schema
# that carries the current review-set snapshot.
RESEARCH_TRIAGE_SCHEMA_VERSION = 1
RESEARCH_TRIAGE_CONTRACT_ID = "research-triage-v1"
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
    research_capacity: int
    review_set_size: int
    research_triage_id: str | None = None
    researchable_tickers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ResearchTriageBinding:
    """The canonical ResearchTriage judgment a research workspace is bound to.

    ``researchable`` is the Research Triage's ``research`` set in judgment order: the exact
    set a human may admit into the Research Set. The human still chooses
    which of those to research; what they cannot do is widen the set from the
    workspace, because research capacity and a canonical thesis are spent per
    ticker and the Gate already decided this cycle's answer for each one.
    """

    research_triage_id: str
    review_set_id: str
    researchable: tuple[str, ...]
    decision_by_ticker: dict[str, str]


def _research_triage_decisions(
    payload: Mapping[str, object], *, research_triage_id: str
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Read one research_triage payload as a ResearchTriage judgment, or fail closed."""

    version = payload.get("schema_version")
    if version != RESEARCH_TRIAGE_SCHEMA_VERSION:
        raise OpportunityDataError(
            f"research requires a ResearchTriage v{RESEARCH_TRIAGE_SCHEMA_VERSION} "
            f"ResearchTriage judgment: {research_triage_id} is schema_version {version!r}"
        )
    contract_id = payload.get("triage_contract_id")
    if contract_id != RESEARCH_TRIAGE_CONTRACT_ID:
        raise OpportunityDataError(
            f"unsupported ResearchTriage contract on {research_triage_id}: {contract_id!r} "
            f"(this build admits {RESEARCH_TRIAGE_CONTRACT_ID!r})"
        )
    entries = _dict_list(payload.get("entries"))
    if not entries:
        raise OpportunityDataError(f"{research_triage_id} carries no ResearchTriage entries")
    decisions: dict[str, str] = {}
    ordered: list[str] = []
    for entry in entries:
        ticker = _nonempty_string(entry.get("ticker"), label=f"{research_triage_id} entry ticker")
        decision = entry.get("decision")
        if decision not in {"research", "skip"}:
            raise OpportunityDataError(
                f"{research_triage_id} entry {ticker} has an unknown "
                f"ResearchTriage decision: {decision!r}"
            )
        if ticker in decisions:
            raise OpportunityDataError(f"{research_triage_id} judged {ticker} more than once")
        decisions[ticker] = str(decision)
        if decision == "research":
            ordered.append(ticker)
    return tuple(ordered), decisions


def _resolve_research_triage(
    *,
    db_path: Path | None,
    expected_research_triage_id: str,
    review_set: Mapping[str, object],
    review_set_output: Path,
    asof: date,
    review_tickers: Sequence[str],
) -> _ResearchTriageBinding:
    """Resolve the canonical judgment for this Review Set and verify its binding.

    The lookup is by ``review_set_id``, never by the ID a caller hands in, so
    renaming the bound research_triage cannot hand a workspace some other cycle's Gate:
    the judgment a Review Set carries is a property of the store, not of the
    request. What this does not claim is immutability of the whole binding — the
    Review Set a workspace points at is named in the same editable manifest as its
    hash, so re-pointing both together moves the workspace to that Review Set's
    Triage. The property that holds either way is the one that matters here: a
    workspace can only admit what canonical Research Triage marked ``research``.

    Publication permits only one judgment per Review Set — a second one carries a
    Review Basis that is stale by then — so any other count is a store this must
    not interpret.

    A workspace researching a different cycle than the judgment it names is not a
    lesser form of the same operation: the E[r], the prices, and the rejection
    reasons all belong to another as-of. Every mismatch is fail-close, before any
    research capacity is spent.
    """

    review_set_id = _nonempty_string(review_set.get("review_set_id"), label="review_set_id")
    try:
        payloads = research_triage_payloads_for_review_set(database_path(db_path), review_set_id)
    except ValueError as exc:
        raise OpportunityDataError(str(exc)) from exc
    if not payloads:
        raise OpportunityDataError(
            f"no canonical ResearchTriage judgment for Review Set {review_set_id} "
            f"({review_set_output}); publish the research_triage before starting research"
        )
    if len(payloads) > 1:
        named = ", ".join(sorted(str(payload.get("research_triage_id")) for payload in payloads))
        raise OpportunityDataError(
            f"Review Set {review_set_id} carries more than one canonical judgment: {named}"
        )
    payload = payloads[0]
    research_triage_id = _nonempty_string(
        payload.get("research_triage_id"), label="research_triage_id"
    )
    if research_triage_id != expected_research_triage_id:
        raise OpportunityDataError(
            f"Review Set {review_set_id} was judged by {research_triage_id}, "
            f"not {expected_research_triage_id}"
        )
    researchable, decisions = _research_triage_decisions(
        payload, research_triage_id=research_triage_id
    )

    expected_asof = asof.isoformat()
    if payload.get("as_of") != expected_asof or review_set.get("as_of") != expected_asof:
        raise OpportunityDataError(
            f"{research_triage_id} and its Review Set must both have as_of {expected_asof}"
        )
    if payload.get("run_revision_id") != review_set.get("run_revision_id"):
        raise OpportunityDataError(
            f"{research_triage_id} judged run {payload.get('run_revision_id')!r}, not the "
            f"Review Set run {review_set.get('run_revision_id')!r}"
        )
    # Publication already binds entries to the Review Set; re-checking here keeps a
    # research_triage and a Review Set that disagree from meeting for the first time inside
    # a research workspace.
    if set(decisions) != set(review_tickers):
        missing = sorted(set(review_tickers) - set(decisions))
        extra = sorted(set(decisions) - set(review_tickers))
        raise OpportunityDataError(
            f"{research_triage_id} entries must equal the Review Set; "
            f"missing={missing}, extra={extra}"
        )
    return _ResearchTriageBinding(
        research_triage_id=research_triage_id,
        review_set_id=review_set_id,
        researchable=researchable,
        decision_by_ticker=decisions,
    )


def _verify_research_triage(
    manifest: Mapping[str, object], inputs: Mapping[str, object], *, db_path: Path | None
) -> _ResearchTriageBinding:
    """Re-resolve the bound judgment on every workspace gate, from the pinned inputs.

    The manifest names the judgment so an operator can read it, but the identity is
    re-derived from the Review Set each time, so a hand-written ticker list is never
    what a gate reads. Renaming the bound Research Triage stops matching the judgment;
    an edit cannot admit a ticker that canonical Research Triage marked ``skip``.
    """

    binding = inputs.get("research_triage")
    if not isinstance(binding, Mapping):
        raise OpportunityDataError(
            "workspace has no ResearchTriage binding; rebuild it with "
            "`research prepare --research-triage-id <RESEARCH_TRIAGE_ID> --force`"
        )
    recorded_research_triage_id = _nonempty_string(
        binding.get("research_triage_id"),
        label="manifest.inputs.research_triage.research_triage_id",
    )
    recorded_review_set_id = _nonempty_string(
        binding.get("review_set_id"), label="manifest.inputs.research_triage.review_set_id"
    )
    recorded_tickers = binding.get("researchable_tickers")
    if not isinstance(recorded_tickers, Sequence) or isinstance(recorded_tickers, str | bytes):
        raise OpportunityDataError(
            "manifest.inputs.research_triage.researchable_tickers must be an array"
        )
    review_set_ref = _required_mapping(
        inputs.get("review_set_output"), label="manifest.inputs.review_set_output"
    )
    review_set_output = Path(
        _nonempty_string(review_set_ref.get("path"), label="manifest.inputs.review_set_output.path")
    )
    review_set = _load_mapping(review_set_output, label="Review Set output")
    try:
        review_tickers, _rows = resolve_review_set_entries(review_set)
    except ReviewSetResolutionError as error:
        raise OpportunityDataError(f"Review Set is invalid: {error}") from error
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    try:
        gate = _resolve_research_triage(
            db_path=db_path,
            expected_research_triage_id=recorded_research_triage_id,
            review_set=review_set,
            review_set_output=review_set_output,
            asof=asof,
            review_tickers=review_tickers,
        )
    except OpportunityDataError as error:
        raise OpportunityConflictError(
            "workspace ResearchTriage binding does not match the canonical research_triage "
            f"{recorded_research_triage_id}; rebuild the workspace with "
            f"`research prepare --force` ({error})"
        ) from error
    if gate.review_set_id != recorded_review_set_id or list(gate.researchable) != [
        str(value) for value in recorded_tickers
    ]:
        raise OpportunityConflictError(
            "workspace ResearchTriage binding does not match the canonical research_triage "
            f"{gate.research_triage_id}; rebuild the workspace with `research prepare --force`"
        )
    return gate


def prepare_workspace(
    *,
    asof: date,
    review_set_output: Path,
    research_triage_id: str,
    db_path: Path | None,
    workspace: Path,
    force: bool = False,
) -> PrepareResult:
    """Build a workspace from a Review Set, ResearchTriage, and the ledger.

    The workspace keeps the whole Review Set as comparison context but may only
    admit the Research Triage's ``research`` tickers into the Research Set.
    Holdings/reservations stay ledger annotations, never hard exclusions. A triage
    with no ``research`` entries is normal and still produces a workspace.
    """
    if review_set_output.resolve().is_relative_to(workspace.resolve()):
        raise OpportunityDataError(
            "--review-set-output must be outside --workspace; prepare writes generated "
            "review-set.yaml into the workspace"
        )
    review_set = _load_mapping(review_set_output, label="Review Set output")
    try:
        review_tickers, review_rows = resolve_review_set_entries(review_set)
    except ReviewSetResolutionError as error:
        raise OpportunityDataError(f"Review Set is invalid: {error}") from error
    review_set_entries = [dict(review_rows[ticker]) for ticker in review_tickers]
    _validate_review_set_estimate_asof(
        review_set=review_set, review_set_entries=review_set_entries, asof=asof
    )
    gate = _resolve_research_triage(
        db_path=db_path,
        expected_research_triage_id=research_triage_id,
        review_set=review_set,
        review_set_output=review_set_output,
        asof=asof,
        review_tickers=review_tickers,
    )
    snapshot, append_head = _load_snapshot(db_path)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise OpportunityConflictError(
            f"workspace already prepared (use --force to rebuild): {workspace}"
        )

    held = {holding.ticker for holding in snapshot.holdings}
    reserved = {reservation.ticker for reservation in snapshot.active_reservations}
    annotated = [
        _annotate_candidate(row, held=held, reserved=reserved, decisions=gate.decision_by_ticker)
        for row in review_set_entries
    ]
    research_capacity = len(gate.researchable)

    workspace_doc = {
        "as_of": asof.isoformat(),
        "review_set": review_set,
        "researchable_tickers": list(gate.researchable),
        "research_set": [],
        "research_capacity": research_capacity,
    }
    er_context, er_context_ref = _load_er_distribution_context(
        review_set=review_set,
        candidates=annotated,
        asof=asof,
    )
    comparison_doc = _research_comparison(asof, annotated, er_context=er_context)

    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "research-workspace.yaml", workspace_doc)
    _write_workspace_file(workspace / "research-comparison.yaml", comparison_doc)

    manifest_inputs: dict[str, object] = {
        "review_set_output": {
            "path": review_set_output.as_posix(),
            "sha256": _sha256_file(review_set_output),
        },
        # A readable record of the binding, not its authority: every gate re-resolves
        # these tickers from the stored research_triage before trusting them.
        "research_triage": {
            "research_triage_id": gate.research_triage_id,
            "review_set_id": gate.review_set_id,
            "researchable_tickers": list(gate.researchable),
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
        "tool_version": TOOL_VERSION,
        "inputs": manifest_inputs,
        "rules": {"research_capacity": research_capacity},
    }
    _write_workspace_file(manifest_path, manifest)
    _write_status(workspace, db_path=db_path)
    return PrepareResult(
        workspace=workspace,
        actionable=bool(gate.researchable),
        research_capacity=research_capacity,
        review_set_size=len(annotated),
        research_triage_id=gate.research_triage_id,
        researchable_tickers=gate.researchable,
    )


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
) -> PrepareResult:
    """Build a one-ticker research workspace for an actual open holding.

    Position Review bypasses Review Set publication because the canonical ledger is
    the source of its research target. The fixed Review Set entries, Research Triage,
    and subject ticker keep the thesis/review/promotion gates usable without weakening
    the normal Research Set boundary.
    """
    snapshot, append_head = _load_snapshot(db_path)
    problem = _holding_subject_problem(snapshot, ticker=ticker, asof=asof)
    if problem is not None:
        raise OpportunityDataError(f"cannot prepare Position Review: {problem}")
    holding = next(item for item in snapshot.holdings if item.ticker == ticker)

    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists() and not force:
        raise OpportunityConflictError(
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
    comparison_doc = _research_comparison(asof, subject)
    comparison_doc["selected_ticker"] = ticker
    comparison_doc["ranking_rationale"] = "research target fixed by the canonical open holding"

    workspace.mkdir(parents=True, exist_ok=True)
    _write_workspace_file(workspace / "research-workspace.yaml", workspace_doc)
    _write_workspace_file(workspace / "research-comparison.yaml", comparison_doc)
    manifest = {
        "purpose": "position_review",
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
        research_capacity=1,
        review_set_size=1,
    )


def _annotate_candidate(
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


def _research_comparison(
    asof: date,
    annotated: Sequence[Mapping[str, object]],
    *,
    er_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    context_by_ticker = er_context.get("candidates") if isinstance(er_context, Mapping) else None
    if not isinstance(context_by_ticker, Mapping):
        context_by_ticker = {}
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
            "er_realized_distribution_context": context_by_ticker.get(str(row.get("ticker") or "")),
        }
        for row in annotated
    ]
    return {
        "as_of": asof.isoformat(),
        "candidates": candidates,
        "selected_ticker": None,
        "ranking_rationale": None,
        "er_realized_distribution_context": er_context,
    }


def _load_er_distribution_context(
    *,
    review_set: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    asof: date,
) -> tuple[dict[str, object] | None, dict[str, str] | None]:
    metadata = review_set.get("method")
    if not isinstance(metadata, Mapping):
        return None, None
    rules_hash = metadata.get("screening_rules_hash")
    er_model_version = metadata.get("er_model_version")
    if not isinstance(rules_hash, str) or not isinstance(er_model_version, str):
        return None, None
    path = ER_LEVEL_CALIBRATION_CONTEXT_PATH
    if not path.is_file():
        return None, None
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None, None
    if (
        not isinstance(raw, Mapping)
        or not valid_er_distribution_context_payload(raw)
        or raw.get("kind") != "er-level-calibration-context"
        or raw.get("schema_version") != 2
        or raw.get("screening_rules_hash") != rules_hash
        or raw.get("er_model_version") != er_model_version
    ):
        return None, None
    generated_at = raw.get("generated_at")
    valid_through = raw.get("valid_through")
    if not isinstance(generated_at, str) or not isinstance(valid_through, str):
        return None, None
    try:
        generated_on = datetime.fromisoformat(generated_at).date()
        expires_on = date.fromisoformat(valid_through)
    except ValueError:
        return None, None
    if generated_on > asof or expires_on < asof:
        return None, None
    horizons = _dict_list(raw.get("horizons"))
    candidate_context: dict[str, object] = {}
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "")
        er_pct = candidate.get("expected_return_pct")
        if isinstance(er_pct, bool) or not isinstance(er_pct, int | float):
            continue
        er_annual = float(er_pct) / 100.0
        matched_horizons: list[dict[str, object]] = []
        for horizon in horizons:
            bands = _dict_list(horizon.get("bands"))
            quintile = next(
                (
                    band
                    for band in bands
                    if band.get("quintile") is not None and _quintile_contains(er_annual, band)
                ),
                None,
            )
            hurdle = next(
                (
                    band
                    for band in bands
                    if band.get("band_id") == "er_gte_8_5pct" and er_annual >= HURDLE_ER_ANNUAL
                ),
                None,
            )
            matched = [
                _context_band_summary(band) for band in (quintile, hurdle) if band is not None
            ]
            matched = [band for band in matched if band is not None]
            matched_horizons.append(
                {
                    "horizon": horizon.get("horizon"),
                    "cohort_count": horizon.get("cohort_count"),
                    "bands": matched,
                }
            )
        candidate_context[ticker] = {
            "er_annual": er_annual,
            "horizons": matched_horizons,
        }
    context = {
        "status": "historical_context_only",
        "artifact": path.as_posix(),
        "generated_at": generated_at,
        "valid_through": valid_through,
        "screening_rules_hash": rules_hash,
        "er_model_version": er_model_version,
        "primary_realized_basis": raw.get("primary_realized_basis"),
        "primary_weighting": "ticker_asof_observation_equal",
        "interpretation": "historical distribution; not an individual security forecast",
        "candidates": candidate_context,
    }
    return context, {"path": path.as_posix(), "sha256": _sha256_file(path)}


def _quintile_contains(er_annual: float, band: Mapping[str, object]) -> bool:
    upper = band.get("upper_er_annual")
    return upper is None or (
        not isinstance(upper, bool) and isinstance(upper, int | float) and er_annual <= float(upper)
    )


def _context_band_summary(band: Mapping[str, object]) -> dict[str, object] | None:
    primary = next(
        (
            basis
            for basis in _dict_list(band.get("bases"))
            if basis.get("basis") == "fy_actual_dividend_total_return"
        ),
        None,
    )
    if primary is None:
        return None
    stats = primary.get("ticker_equal")
    if not isinstance(stats, Mapping):
        return None
    return {
        "band_id": band.get("band_id"),
        "lower_er_annual": band.get("lower_er_annual"),
        "upper_er_annual": band.get("upper_er_annual"),
        "median_predicted_er_annual": band.get("median_predicted_er_annual"),
        "cohort_count": band.get("cohort_count"),
        "median_n": band.get("median_n"),
        "realized_total_return_ticker_equal": {
            key: stats.get(key) for key in ("median", "q25", "q10", "trap_rate", "n")
        },
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
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    status = _draft_status(workspace, manifest)
    status["research_triage"] = _research_triage_view(gate)
    return status


def _research_triage_view(gate: _ResearchTriageBinding | None) -> dict[str, object]:
    """Name the judgment bounding this workspace and what it lets a human admit."""

    if gate is None:
        return {
            "purpose": "position_review",
            "research_triage_id": None,
            "researchable_tickers": [],
        }
    return {
        "purpose": "fundamental_research",
        "research_triage_id": gate.research_triage_id,
        "researchable_tickers": list(gate.researchable),
    }


def _draft_status(workspace: Path, manifest: Mapping[str, object]) -> dict[str, object]:
    manifest_asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    research_set = research_workspace.get("research_set")
    if not isinstance(research_set, list) or not all(
        isinstance(ticker, str) for ticker in research_set
    ):
        raise OpportunityDataError("workspace research_set must be an array of tickers")
    research_set_tickers = [str(ticker) for ticker in research_set]
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    selected_ticker = _string_or_none(comparison.get("selected_ticker"))

    if not research_set_tickers:
        return _status_payload(
            workspace_status="awaiting_research_set_admission",
            selected_ticker=None,
            next_command="review the ResearchTriage and fill research-workspace.yaml research_set",
        )

    missing_research = [
        ticker
        for ticker in research_set_tickers
        if not (_research_ticker_dir(workspace, ticker) / "thesis-draft.yaml").is_file()
    ]
    if missing_research:
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            next_command=f"baibai-engine research thesis-scaffold --ticker {missing_research[0]}",
        )

    research_pending: list[str] = []
    research_blocked: list[str] = []
    research_thesis_errors: list[str] = []
    for ticker in research_set_tickers:
        checklist = _load_checklist(workspace, ticker)
        research_pending.extend(
            f"{ticker}:{check_id}"
            for item in checklist
            if item.get("status") == "pending"
            if (check_id := _string_or_none(item.get("check_id"))) is not None
        )
        research_blocked.extend(
            f"{ticker}:{check_id}"
            for item in checklist
            if item.get("status") == "blocked"
            if (check_id := _string_or_none(item.get("check_id"))) is not None
        )
        research_thesis_errors.extend(
            f"{ticker}:{error}"
            for error in _thesis_validation_errors(workspace, ticker, manifest_asof)
        )
    if research_pending or research_thesis_errors:
        first_ticker = (research_pending or research_thesis_errors)[0].split(":", maxsplit=1)[0]
        return _status_payload(
            workspace_status="incomplete",
            selected_ticker=selected_ticker,
            pending_checks=research_pending,
            blocked_checks=research_blocked,
            thesis_validation_errors=research_thesis_errors,
            next_command=f"complete Fundamental Research for {first_ticker}",
        )

    if selected_ticker is None:
        return _status_payload(
            workspace_status="ready_for_comparison",
            selected_ticker=None,
            blocked_checks=research_blocked,
            next_command=(
                "complete research-comparison.yaml and set selected_ticker, or record no_allocation"
            ),
        )

    checklist_path = _research_ticker_dir(workspace, selected_ticker) / "research-checklist.yaml"
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
    thesis_errors = _thesis_validation_errors(workspace, selected_ticker, manifest_asof)
    review_errors = _review_validation_errors(workspace, selected_ticker, manifest_asof)

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
            return (
                "resolve the blocked investigation, or record its evidence as unknown and "
                "mark the completed investigation complete before promotion"
            )
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


def _verify_external_inputs(
    manifest: Mapping[str, object], *, db_path: Path | None = None
) -> _ResearchTriageBinding | None:
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
        raise OpportunityDataError("manifest is missing external input hashes")
    purpose = str(manifest.get("purpose") or "fundamental_research")
    required_inputs: tuple[str, ...]
    if purpose == "fundamental_research":
        required_inputs = ("review_set_output", "ledger")
        if "er_distribution_context" in inputs:
            required_inputs += ("er_distribution_context",)
    elif purpose == "position_review":
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
        raise OpportunityDataError("position-review manifest is missing holding_ticker")
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    snapshot, _append_head = _load_snapshot(db_path)
    problem = _holding_subject_problem(snapshot, ticker=ticker, asof=asof)
    if problem is not None:
        raise OpportunityConflictError(
            f"position-review workspace subject is invalid: {problem}; a Position Review "
            "runs at the as-of its ledger market price was observed, so rebuild at that "
            "date with `research position-prepare --asof <observed> --force` and "
            "regenerate the ticker drafts with `research thesis-scaffold --force` "
            "(market prices only move forward, so the old as-of cannot be restored)"
        )


def _validate_editable_drafts(
    workspace: Path, manifest: Mapping[str, object], *, gate: _ResearchTriageBinding | None
) -> None:
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    comparison = _load_mapping(workspace / "research-comparison.yaml", label="research comparison")
    manifest_asof = str(manifest.get("as_of") or "")
    if research_workspace.get("as_of") != manifest_asof or comparison.get("as_of") != manifest_asof:
        raise OpportunityDataError("workspace draft as_of does not match manifest")

    purpose = str(manifest.get("purpose") or "fundamental_research")
    if purpose == "position_review":
        _validate_position_review_drafts(research_workspace, comparison, manifest)
        return
    if purpose != "fundamental_research":
        raise OpportunityDataError(f"manifest purpose is invalid: {purpose}")

    review_set = _required_mapping(
        research_workspace.get("review_set"), label="workspace.review_set"
    )
    try:
        review_set_tickers, _ = resolve_review_set_entries(review_set)
    except ReviewSetResolutionError as error:
        raise OpportunityDataError(f"workspace Review Set is invalid: {error}") from error
    research_set = research_workspace.get("research_set")
    research_capacity = research_workspace.get("research_capacity")
    if not isinstance(research_capacity, int) or research_capacity < 0:
        raise OpportunityDataError("workspace research_capacity is invalid")
    if not isinstance(research_set, list) or not all(
        isinstance(ticker, str) and ticker for ticker in research_set
    ):
        raise OpportunityDataError("workspace research_set must be an array of tickers")
    research_set_tickers = [str(ticker) for ticker in research_set]
    if len(research_set_tickers) != len(set(research_set_tickers)) or any(
        ticker not in review_set_tickers for ticker in research_set_tickers
    ):
        raise OpportunityDataError("workspace research_set is invalid")
    # Narrowing guard, not a reachable state: `_verify_external_inputs` reads purpose
    # from this same manifest and returns None only for Position Review, which left
    # above. A missing binding is refused there, with the command that rebuilds it.
    if gate is None:  # pragma: no cover - unreachable by construction
        raise OpportunityDataError("Fundamental Research workspace has no ResearchTriage binding")
    # Named before the slot count: over-filling and reaching past the Gate both show
    # up as "too many tickers", and only one of them is a capacity question.
    forbidden = [ticker for ticker in research_set_tickers if ticker not in gate.researchable]
    if forbidden:
        raise OpportunityDataError(
            f"workspace Research Set includes {', '.join(forbidden)}, which "
            f"{gate.research_triage_id} did not mark research"
        )
    if len(research_set_tickers) > research_capacity:
        raise OpportunityDataError("workspace Research Set exceeds research_capacity")

    candidates = _dict_list(comparison.get("candidates"))
    comparison_tickers = [str(row.get("ticker") or "") for row in candidates]
    if comparison_tickers != list(review_set_tickers):
        raise OpportunityDataError("research comparison candidates do not match Review Set")
    selected = _string_or_none(comparison.get("selected_ticker"))
    if selected is not None and selected not in research_set_tickers:
        raise OpportunityDataError("selected_ticker is not present in Research Set")


def _validate_position_review_drafts(
    research_workspace: Mapping[str, object],
    comparison: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    ticker = _string_or_none(manifest.get("holding_ticker"))
    if ticker is None:
        raise OpportunityDataError("position-review manifest is missing holding_ticker")
    subject_tickers = [
        str(row.get("ticker") or "")
        for row in _dict_list(research_workspace.get("position_review_subject"))
    ]
    research_set = research_workspace.get("research_set")
    comparison_tickers = [
        str(row.get("ticker") or "") for row in _dict_list(comparison.get("candidates"))
    ]
    if (
        subject_tickers != [ticker]
        or research_set != [ticker]
        or comparison_tickers != [ticker]
        or comparison.get("selected_ticker") != ticker
    ):
        raise OpportunityDataError(
            "position-review workspace must keep its subject and comparison fixed"
        )


def _research_ticker_dir(workspace: Path, ticker: str) -> Path:
    """Return a path-confined ticker directory inside the shared research workspace."""

    workspace_root = workspace.resolve()
    ticker_dir = (workspace_root / ticker).resolve()
    if ticker_dir.parent != workspace_root:
        raise OpportunityDataError(
            f"research ticker directory must be a direct child of the workspace: {ticker}"
        )
    return ticker_dir


def _review_filename(*, asof: date, ticker: str) -> str:
    """Return the stable independent-review filename for a research ticker.

    The thesis payload carries this name in ``independent_review_ref`` and
    ``plan-limit`` resolves the review by that name from the thesis's own
    directory, so scaffold, status, and promote all address the same path and no
    copy step stands between the draft and the gate.
    """

    return f"{asof:%Y-%m-%d}-{ticker}-decision-review.yaml"


def _review_draft_path(workspace: Path, ticker: str, asof: date) -> Path:
    return _research_ticker_dir(workspace, ticker) / _review_filename(asof=asof, ticker=ticker)


def _require_primary_research_ticker(
    workspace: Path, ticker: str, *, action: str, gate: _ResearchTriageBinding | None
) -> None:
    research_workspace = _load_mapping(
        workspace / "research-workspace.yaml", label="research workspace"
    )
    research_set = research_workspace.get("research_set")
    if not isinstance(research_set, list) or ticker not in research_set:
        raise OpportunityDataError(
            f"cannot {action} for {ticker}: ticker is not in the Research Set"
        )
    if gate is not None and ticker not in gate.researchable:
        raise OpportunityDataError(
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
        screening_estimate, transfer_reason = _screening_estimate_from_review_set_output(
            manifest=manifest,
            ticker=ticker,
            asof=asof,
        )
    ticker_dir = _research_ticker_dir(workspace, ticker)

    price = resolve_previous_business_day_close(
        sqlite_path=sqlite_path, ticker=ticker, target_session=target_session
    )
    if price is None:
        raise OpportunityDataError(
            f"no raw/unadjusted close available for {ticker} before {target_session.isoformat()}; "
            "an adjusted-only series is not substituted"
        )
    if purpose == "position_review" and price.price_as_of != asof:
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
        "schema_version": 2,
        "input_snapshot": input_snapshot,
        "derived": {"metrics": []},
        "estimates": None,
        "permanent_loss_risks": [],
        "judgment": None,
        "independent_review_ref": _review_filename(asof=asof, ticker=ticker),
    }


def _screening_estimate_from_review_set_output(
    *, manifest: Mapping[str, object], ticker: str, asof: date
) -> tuple[dict[str, object] | None, str | None]:
    inputs = _required_mapping(manifest.get("inputs"), label="manifest.inputs")
    review_set_ref = _required_mapping(
        inputs.get("review_set_output"), label="manifest.inputs.review_set_output"
    )
    review_set_path = _nonempty_string(
        review_set_ref.get("path"), label="manifest.inputs.review_set_output.path"
    )
    review_set = _load_mapping(Path(review_set_path), label="Review Set output")
    try:
        _tickers, rows = resolve_review_set_entries(review_set)
    except ReviewSetResolutionError as error:
        raise OpportunityDataError(f"Review Set is invalid: {error}") from error
    row = rows.get(ticker)
    if row is None:
        raise OpportunityDataError(f"Review Set must contain ticker exactly once: {ticker}")
    if review_set.get("as_of") != asof.isoformat():
        raise OpportunityDataError("Review Set as_of does not match manifest as_of")
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
        raise OpportunityDataError("Review Set fair-value anchors must be positive")
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
        "source_ids": [f"review-set:{review_set.get('review_set_id')}"],
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


def _validate_review_set_estimate_asof(
    *,
    review_set: Mapping[str, object],
    review_set_entries: Sequence[Mapping[str, object]],
    asof: date,
) -> None:
    expected_asof = asof.isoformat()
    if review_set.get("as_of") != expected_asof:
        raise OpportunityDataError("Review Set as_of does not match prepare as_of")
    if len(review_set_entries) > 20:
        raise OpportunityDataError("Review Set exceeds capacity 20")


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
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(workspace, ticker, action="scaffold review", gate=gate)
    asof = _parse_date(str(manifest.get("as_of")), label="manifest as_of")
    ticker_dir = _research_ticker_dir(workspace, ticker)
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

    result = evaluate_thesis(document, review=review, now=now, identity=UnpublishedThesis.DRAFT)
    if result.decision_readiness not in {"ready", "ready_with_warnings"}:
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

    result = evaluate_thesis(document, review=review, now=now, identity=UnpublishedThesis.DRAFT)
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
        review = load_independent_review(review_path)
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
