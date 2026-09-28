"""調査と独立Reviewの下書きを作る。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time
from math import isfinite
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.time import JST
from baibai_engine.position.market_source import read_unadjusted_close
from baibai_engine.research.thesis import (
    ThesisError,
    ThesisReview,
    load_thesis,
    thesis_core_hash,
)
from baibai_engine.research.thesis_store import (
    latest_thesis_id,
    load_reviewed_thesis,
)
from baibai_engine.research.workspace.admission import _require_primary_research_ticker
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    _dump_yaml,
    _load_mapping,
    _parse_date,
)
from baibai_engine.research.workspace.validation import (
    _research_ticker_dir,
    _review_draft_path,
    _validate_editable_drafts,
    _verify_external_inputs,
)

_THESIS_DRAFT_HEADER = "# 企業評価 draft。未確認は理由付き unresolved とし、一次資料で補う。\n"

_REVIEW_DRAFT_HEADER = "# 独立検算の値を入力する。作者の値をコピーしない。\n"

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
