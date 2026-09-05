"""Compose a canonical Position Review from decision and portfolio contracts.

The position package owns ledger replay and position-review arithmetic.  This
module belongs to research because it assembles their output with the current
thesis, without introducing a reverse position-to-thesis dependency.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import PortfolioLedgerDocument, reconcile_portfolio
from baibai_engine.position.position_review import (
    PositionReviewDocument,
    PositionReviewError,
    evaluate_position_review,
)
from baibai_engine.position.store import load_ledger_in_transaction
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    _classify_current_thesis_eligibility,
    evaluate_thesis,
    require_recorded_identity,
)


def build_position_review_from_db(
    *,
    db_path: Path | None,
    holding_thesis_id: str,
    position_id: str,
    replacement_thesis_id: str | None = None,
    now: datetime | None = None,
) -> PositionReviewDocument:
    """Build a draft from canonical thesis revisions and the current ledger head."""
    operation_now = _operation_instant(now)
    initialize_database(db_path)
    with closing(connect_rw(db_path)) as connection:
        connection.execute("BEGIN")
        return _build_position_review_in_transaction(
            connection,
            holding_thesis_id=holding_thesis_id,
            replacement_thesis_id=replacement_thesis_id,
            position_id=position_id,
            now=operation_now,
        )


def _build_position_review_in_transaction(
    connection: sqlite3.Connection,
    *,
    holding_thesis_id: str,
    position_id: str,
    replacement_thesis_id: str | None,
    now: datetime,
) -> PositionReviewDocument:
    ledger, ledger_append_head = load_ledger_in_transaction(connection)
    holding_thesis = _load_db_thesis(
        connection,
        holding_thesis_id,
        now=now,
        allow_expired_override=True,
    )
    replacement_thesis = (
        None
        if replacement_thesis_id is None
        else _load_db_thesis(
            connection,
            replacement_thesis_id,
            now=now,
            allow_expired_override=False,
        )
    )
    if not holding_thesis.current_ready and replacement_thesis is not None:
        raise PositionReviewError(
            "expired holding thesis cannot be compared with a replacement Thesis"
        )
    return _compose_position_review(
        ledger=ledger,
        thesis=holding_thesis.document,
        holding_current_ready=holding_thesis.current_ready,
        replacement_thesis=(None if replacement_thesis is None else replacement_thesis.document),
        position_id=position_id,
        now=now,
        sources={
            "ledger": {
                "entity_id": "portfolio-ledger",
                "append_head": ledger_append_head,
            },
            "holding_thesis": {"entity_id": holding_thesis_id, "append_head": None},
            "candidate_thesis": (
                None
                if replacement_thesis_id is None
                else {"entity_id": replacement_thesis_id, "append_head": None}
            ),
        },
    )


def _compose_position_review(
    *,
    ledger: PortfolioLedgerDocument,
    thesis: ThesisDocument,
    holding_current_ready: bool,
    replacement_thesis: ThesisDocument | None,
    position_id: str,
    now: datetime,
    sources: dict[str, object],
) -> PositionReviewDocument:
    snapshot = reconcile_portfolio(ledger)
    holding = next(
        (item for item in snapshot.holdings if item.ticker == thesis.input_snapshot.ticker),
        None,
    )
    if holding is None:
        raise PositionReviewError("holding thesis ticker has no open ledger holding")
    as_of = holding.market_price_observed_at.date()
    if thesis.input_snapshot.as_of != as_of:
        raise PositionReviewError(
            "thesis as_of must equal the holding market-price observation date"
        )
    thesis_price = _thesis_market_price(thesis)
    if thesis_price != holding.market_price_yen or holding.market_price_observed_at.date() != as_of:
        raise PositionReviewError(
            "holding thesis market price does not match ledger unadjusted close"
        )
    current_cagr = _base_5y_cagr(thesis, now=now) if holding_current_ready else None
    replacement: dict[str, object] = {"status": "no_candidate"}
    if replacement_thesis is not None:
        if replacement_thesis.input_snapshot.as_of != as_of:
            raise PositionReviewError(
                "replacement Thesis as_of must equal the holding market-price observation date"
            )
        if replacement_thesis.input_snapshot.ticker == holding.ticker:
            raise PositionReviewError("replacement Thesis ticker must differ from holding ticker")
        exit_tax: dict[str, object]
        if ledger.estimated_exit_tax_rate_bps is None:
            exit_tax = {"tax_basis": "unknown"}
        else:
            exit_tax = {
                "tax_basis": "estimated",
                "rate_bps": ledger.estimated_exit_tax_rate_bps,
                "estimated_exit_tax_basis": ledger.estimated_exit_tax_basis,
            }
        replacement = {
            "status": "evaluated",
            "hold": {
                "market_value_yen": holding.market_value_yen,
                "deployed_cost_yen": holding.deployed_cost_yen,
                "forward_5y_cagr_pct": current_cagr,
            },
            "candidate": {
                "ticker": replacement_thesis.input_snapshot.ticker,
                "forward_5y_cagr_pct": _base_5y_cagr(replacement_thesis, now=now),
            },
            "exit_tax": exit_tax,
        }
    latest_evidence = max(risk.as_of for risk in thesis.permanent_loss_risks)
    raw: dict[str, object] = {
        "schema_version": 2,
        "as_of": as_of.isoformat(),
        "position_id": position_id,
        "ticker": holding.ticker,
        "sources": sources,
        "thesis_health": {
            "invalidation_status": (
                "broken" if thesis.judgment.permanent_loss_conclusion == "elevated" else "intact"
            ),
            "permanent_loss_axes": [
                {
                    "axis": risk.axis,
                    "assessment": risk.assessment,
                    "evidence_status": risk.evidence_status,
                }
                for risk in thesis.permanent_loss_risks
            ],
            "evidence_freshness": {
                "latest_source_as_of": latest_evidence.isoformat(),
                "age_days": (as_of - latest_evidence).days,
            },
            "current_5y_estimate": {
                "status": "resolved" if holding_current_ready else "unresolved",
                **({"forward_5y_cagr_pct": current_cagr} if holding_current_ready else {}),
            },
        },
        "valuation_review": (
            {
                "status": "resolved",
                "current_price_yen": _whole_yen(holding.market_price_yen),
                "fair_value_yen": _whole_yen(thesis.estimates.current_fair_value_yen),
                "review_trigger": (
                    holding.market_price_yen >= thesis.estimates.current_fair_value_yen
                ),
            }
            if holding_current_ready
            else {"status": "unresolved"}
        ),
        "replacement_comparison": replacement,
        "action": "hold",
    }
    draft = PositionReviewDocument.model_validate(raw)
    return draft.model_copy(update={"action": evaluate_position_review(draft).computed_action})


def validate_position_review_scalars_from_db(
    document: PositionReviewDocument,
    *,
    db_path: Path | None,
    now: datetime,
) -> None:
    operation_now = _operation_instant(now)
    initialize_database(db_path)
    with closing(connect_rw(db_path)) as connection:
        connection.execute("BEGIN")
        _validate_position_review_scalars_in_transaction(
            document,
            connection=connection,
            now=operation_now,
        )


def _validate_position_review_scalars_in_transaction(
    document: PositionReviewDocument,
    *,
    connection: sqlite3.Connection,
    now: datetime,
) -> None:
    operation_now = _operation_instant(now)
    ledger_source = document.sources.ledger
    thesis_source = document.sources.holding_thesis
    replacement_source = document.sources.candidate_thesis
    if ledger_source.entity_id != "portfolio-ledger" or ledger_source.append_head is None:
        raise PositionReviewError("Position Review ledger binding is incomplete")
    rebuilt = _build_position_review_in_transaction(
        connection,
        holding_thesis_id=thesis_source.entity_id,
        replacement_thesis_id=(
            None if replacement_source is None else replacement_source.entity_id
        ),
        position_id=document.position_id,
        now=operation_now,
    )
    if rebuilt.sources.ledger.append_head != ledger_source.append_head:
        raise PositionReviewError("Position Review ledger source changed after draft build")
    fields = {
        "as_of",
        "ticker",
        "thesis_health",
        "valuation_review",
        "replacement_comparison",
        "action",
    }
    if document.model_dump(mode="json", include=fields) != rebuilt.model_dump(
        mode="json", include=fields
    ):
        raise PositionReviewError("Position Review load-bearing values differ from DB rebuild")


@dataclass(frozen=True, slots=True)
class _LoadedThesis:
    document: ThesisDocument
    current_ready: bool


def _load_db_thesis(
    connection: sqlite3.Connection,
    thesis_id: str,
    *,
    now: datetime,
    allow_expired_override: bool,
) -> _LoadedThesis:
    thesis_row = connection.execute(
        "SELECT core_sha256, payload FROM thesis WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if thesis_row is None:
        raise PositionReviewError(f"unknown research thesis: {thesis_id}")
    review_rows = connection.execute(
        "SELECT payload FROM thesis_review WHERE thesis_id = ? ORDER BY reviewed_at DESC",
        (thesis_id,),
    ).fetchall()
    if len(review_rows) != 1:
        raise PositionReviewError("Position Review requires exactly one Thesis Review")
    thesis = ThesisDocument.model_validate(json.loads(str(thesis_row["payload"])))
    review = ThesisReview.model_validate(json.loads(str(review_rows[0]["payload"])))
    eligibility = _classify_current_thesis_eligibility(
        thesis,
        review=review,
        now=now,
        identity=require_recorded_identity(thesis_row["core_sha256"], thesis_id),
    )
    if eligibility.status == "current_ready":
        return _LoadedThesis(thesis, current_ready=True)
    if allow_expired_override and eligibility.status == "expired_override_only":
        return _LoadedThesis(thesis, current_ready=False)
    detail = "; ".join(eligibility.evaluation.errors) or eligibility.evaluation.decision_readiness
    raise PositionReviewError(f"thesis is not ready for Position Review: {detail}")


def _thesis_market_price(thesis: ThesisDocument) -> Decimal:
    fact = next(
        item
        for item in thesis.input_snapshot.facts
        if item.fact_id == thesis.estimates.market_price_fact_id
    )
    if fact.price_basis != "last_close_unadjusted" or fact.as_of != thesis.input_snapshot.as_of:
        raise PositionReviewError("thesis must use a same-date unadjusted close")
    if isinstance(fact.value, bool | str):
        raise PositionReviewError("thesis market price must be numeric")
    return Decimal(str(fact.value))


def _base_5y_cagr(thesis: ThesisDocument, *, now: datetime) -> float:
    # Only the scenarios are read here; the identity never leaves this call.
    evaluation = evaluate_thesis(thesis, now=now, identity=UnpublishedThesis.DRAFT)
    scenario = next(
        (item for item in evaluation.scenarios if item.horizon_years == 5 and item.name == "base"),
        None,
    )
    if scenario is None:
        raise PositionReviewError("thesis lacks a 5y base scenario")
    return scenario.total_return_cagr_pct


def _whole_yen(value: Decimal) -> int:
    """Bring a price onto the whole-yen grid the review compares against.

    The documented way to set `estimates.current_fair_value_yen` is to discount the 5y
    base path with `scenario_arithmetic --required-cagr-pct`, which prints four decimals,
    so refusing a fractional value made the documented research path unusable for any
    holding. The purchase path already resolves the same question by flooring the ceiling
    it derives (`entry_price_policy.maximum_acceptable_entry_price`), and this follows it: sub-yen
    precision is spurious against a market that trades in whole yen, and flooring keeps a
    fair value from being rounded up into an upside the thesis did not claim.
    """

    floored = int(value.to_integral_value(rounding=ROUND_FLOOR))
    if floored <= 0:
        raise PositionReviewError("Position Review requires a positive whole-yen price")
    return floored


def _operation_instant(now: datetime | None) -> datetime:
    resolved = now or datetime.now(JST)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise PositionReviewError("operation clock must be timezone-aware")
    return resolved
