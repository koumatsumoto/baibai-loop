"""調査と独立Reviewの下書きを作る。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path
from typing import get_args
from uuid import uuid4

from pydantic import BaseModel

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.position.market_source import read_unadjusted_close
from baibai_engine.research.assessment import (
    AssessmentError,
    AssessmentReview,
    assessment_core_hash,
    load_assessment,
)
from baibai_engine.research.assessment_refresh import scaffold_assessment_refresh
from baibai_engine.research.assessment_store import (
    latest_assessment_id,
    load_reviewed_assessment,
)
from baibai_engine.research.workspace.admission import _require_primary_research_ticker
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _dump_yaml,
    _load_mapping,
)
from baibai_engine.research.workspace.validation import (
    _research_ticker_dir,
    _review_draft_path,
    _validate_editable_drafts,
    _verify_external_inputs,
)
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only

_ASSESSMENT_DRAFT_HEADER = "# 企業評価 draft。未確認は理由付き unresolved とし、一次資料で補う。\n"

_REVIEW_DRAFT_HEADER = "# 独立検算の値を入力する。作者の値をコピーしない。\n"

# --------------------------------------------------------------------------- #
# assessment-scaffold
# --------------------------------------------------------------------------- #


def scaffold_assessment(
    *,
    workspace: Path,
    ticker: str,
    sqlite_path: Path,
    as_of: date,
    retrieved_at: datetime,
    db_path: Path | None = None,
    force: bool = False,
    from_assessment_id: str | None = None,
    out: Path | None = None,
) -> dict[str, object]:
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(
        workspace, ticker, action="scaffold research", gate=gate, db_path=db_path
    )
    directory = _research_ticker_dir(workspace, ticker)
    path = directory / "assessment-draft.yaml"
    if path.exists() and not force:
        raise ResearchWorkspaceConflictError("assessment draft exists; use --force")
    if out is not None and out.exists() and out.resolve() != path and not force:
        raise ResearchWorkspaceConflictError("assessment export exists; use --force")
    if retrieved_at.utcoffset() is None or as_of > retrieved_at.astimezone(JST).date():
        raise ResearchWorkspaceDataError(
            "assessment date must not follow its aware assessment time"
        )
    if as_of < gate.research_as_of:
        raise ResearchWorkspaceDataError("assessment predates the fixed Research selection")
    # The refresh path preserves saved source/fact dates and does not read a new quote.
    quote = None
    with closing(connect_read_only(database_path(db_path))) as connection:
        connection.execute("BEGIN")
        supersedes_id = latest_assessment_id(connection, ticker)
        if from_assessment_id is not None:
            pair = load_reviewed_assessment(connection, from_assessment_id)
            if pair.document.input_snapshot.ticker != ticker:
                raise ResearchWorkspaceConflictError("refresh material must belong to this ticker")
    if from_assessment_id is not None:
        payload = scaffold_assessment_refresh(pair, as_of=as_of, assessed_at=retrieved_at)
    else:
        quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=retrieved_at)
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
                    "value": _finite_number(quote.close_yen, label="cached close"),
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
            "kind": "stock_assessment",
            "schema_version": 1,
            "input_snapshot": {
                "snapshot_version": 1,
                "producer_model_version": "stock-assessment-v1",
                "ticker": ticker,
                "company_name": None,
                "sector": None,
                "common_factors": [],
                "as_of": as_of.isoformat(),
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
            "findings": {
                "business_understanding": None,
                "value_gap_explanation": None,
                "realization": {
                    "mechanism": None,
                    "timing": "uncertain",
                    "timing_reason": None,
                    "delay_risk": None,
                },
                "hypotheses": [],
                "unresolved_questions": [],
                "source_ids": [],
            },
            "conclusion": {
                "stance": "uncertain",
                "assessed_at": retrieved_at.isoformat(),
                "strongest_countercase": None,
            },
            "refresh": None,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    identity_path = directory / "assessment-publication.yaml"
    identity = (
        _load_mapping(identity_path, label="assessment publication")
        if identity_path.exists()
        else {}
    )
    same_material = True
    if path.exists():
        previous = _load_mapping(path, label="assessment draft")
        previous_refresh = previous.get("refresh")
        previous_ref = (
            previous_refresh.get("prior_assessment_ref")
            if isinstance(previous_refresh, dict)
            else None
        )
        refresh = payload.get("refresh")
        current_ref = refresh.get("prior_assessment_ref") if isinstance(refresh, dict) else None
        same_material = previous_ref == current_ref
    if not identity or identity.get("supersedes_id") != supersedes_id or not same_material:
        identity = {
            "assessment_id": f"stock-assessment-{uuid4()}",
            "supersedes_id": supersedes_id,
        }
    write_text_atomic(identity_path, _dump_yaml(identity))
    content = _ASSESSMENT_DRAFT_HEADER + _dump_yaml(payload)
    write_text_atomic(path, content)
    if out is not None and out.resolve() != path:
        write_text_atomic(out, content)
    return {
        "assessment_draft": str(path),
        "price_as_of": None if quote is None else quote.price_as_of.isoformat(),
        "close_yen": None if quote is None else quote.close_yen,
        "from_assessment_id": from_assessment_id,
        "assessment_id": identity["assessment_id"],
        "exported_draft": None if out is None else str(out),
    }


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
    """Write the Assessment Review draft bound to the current assessment core hash.

    The review author is a distinct role from the assessment author; this scaffold only
    lays out the recalculation slots and never produces the review conclusions. The
    bound ``reviewed_assessment_sha256`` is what lets ``promote`` detect a stale review.
    The file uses the stable assessment-review.yaml name that promote reads.
    """
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    gate = _verify_external_inputs(manifest, db_path=db_path)
    _validate_editable_drafts(workspace, manifest, gate=gate)
    _require_primary_research_ticker(
        workspace, ticker, action="scaffold review", gate=gate, db_path=db_path
    )
    ticker_dir = _research_ticker_dir(workspace, ticker)
    assessment_path = ticker_dir / "assessment-draft.yaml"
    if not assessment_path.exists():
        raise ResearchWorkspaceDataError(
            f"assessment draft not found for {ticker}: {assessment_path}"
        )

    core_hash = _assessment_core_hash_if_valid(assessment_path)
    review_path = _review_draft_path(workspace, ticker)
    if review_path.exists() and not force:
        raise ResearchWorkspaceConflictError(
            f"review draft already exists (use --force to regenerate): {review_path}"
        )

    review_draft = {
        "review_id": f"assessment-review-{uuid4()}",
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": None,
        "reviewed_at": None,
        "reviewed_assessment_sha256": core_hash,
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
        if load_assessment(assessment_path).valuation.status == "resolved"
        else [],
        "strongest_countercase": None,
        "nonmaterial_unknown_reason": None,
        "review_scope": None,
    }
    header = _REVIEW_DRAFT_HEADER + _enum_field_header(AssessmentReview)
    write_text_atomic(review_path, header + _dump_yaml(review_draft))
    return {"review_draft": str(review_path), "reviewed_assessment_sha256": core_hash}


def _enum_field_header(model: type[BaseModel]) -> str:
    """List the draft's closed-vocabulary fields and their allowed values.

    A scaffolded `null` carries no type, so a reviewer filling in `primary_source_check`
    cannot tell a three-way verdict from free prose
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


def _assessment_core_hash_if_valid(assessment_path: Path) -> str | None:
    # A fully-filled draft hashes to its core; an incomplete draft cannot be hashed
    # yet, so the review is bound once the assessment is complete.
    try:
        document = load_assessment(assessment_path)
    except AssessmentError:
        return None
    return assessment_core_hash(document)
