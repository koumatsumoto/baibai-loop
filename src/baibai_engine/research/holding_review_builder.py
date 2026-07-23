"""Compose a canonical holding review from decision and portfolio contracts.

The position package owns ledger replay and holding-review arithmetic.  This
module belongs to research because it assembles their output with the current
thesis, without introducing a reverse position-to-thesis dependency.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.position.holding_review import (
    HoldingReviewDocument,
    HoldingReviewError,
    evaluate_holding_review,
)
from baibai_engine.position.ledger import PortfolioLedgerDocument, reconcile_portfolio
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    evaluate_thesis,
)


def build_holding_review_from_db(
    *,
    db_path: Path | None,
    holding_thesis_id: str,
    position_id: str,
    candidate_thesis_id: str | None = None,
) -> HoldingReviewDocument:
    """Build a draft from canonical thesis revisions and the current ledger head."""
    initialize_database(db_path)
    ledger_service = LedgerStoreService(db_path)
    ledger, ledger_append_head = ledger_service.load_with_head()
    with closing(connect_rw(db_path)) as connection:
        thesis = _load_ready_db_thesis(connection, holding_thesis_id)
        candidate = (
            None
            if candidate_thesis_id is None
            else _load_ready_db_thesis(connection, candidate_thesis_id)
        )
    return _compose_holding_review(
        ledger=ledger,
        thesis=thesis,
        candidate=candidate,
        position_id=position_id,
        sources={
            "ledger": {
                "entity_id": "portfolio-ledger",
                "append_head": ledger_append_head,
            },
            "holding_thesis": {"entity_id": holding_thesis_id, "append_head": None},
            "candidate_thesis": (
                None
                if candidate_thesis_id is None
                else {"entity_id": candidate_thesis_id, "append_head": None}
            ),
        },
    )


def _compose_holding_review(
    *,
    ledger: PortfolioLedgerDocument,
    thesis: ThesisDocument,
    candidate: ThesisDocument | None,
    position_id: str,
    sources: dict[str, object],
) -> HoldingReviewDocument:
    snapshot = reconcile_portfolio(ledger)
    holding = next(
        (item for item in snapshot.holdings if item.ticker == thesis.input_snapshot.ticker),
        None,
    )
    if holding is None:
        raise HoldingReviewError("holding thesis ticker has no open ledger holding")
    as_of = holding.market_price_observed_at.date()
    if thesis.input_snapshot.as_of != as_of:
        raise HoldingReviewError(
            "thesis as_of must equal the holding market-price observation date"
        )
    thesis_price = _thesis_market_price(thesis)
    if thesis_price != holding.market_price_yen or holding.market_price_observed_at.date() != as_of:
        raise HoldingReviewError(
            "holding thesis market price does not match ledger unadjusted close"
        )
    current_cagr = _base_5y_cagr(thesis)
    replacement: dict[str, object] = {"status": "no_candidate"}
    if candidate is not None:
        if candidate.input_snapshot.as_of != as_of:
            raise HoldingReviewError(
                "candidate thesis as_of must equal the holding market-price observation date"
            )
        if candidate.input_snapshot.ticker == holding.ticker:
            raise HoldingReviewError("replacement candidate ticker must differ from holding ticker")
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
                "ticker": candidate.input_snapshot.ticker,
                "forward_5y_cagr_pct": _base_5y_cagr(candidate),
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
                "status": "resolved",
                "forward_5y_cagr_pct": current_cagr,
            },
        },
        "valuation_review": {
            "status": "resolved",
            "current_price_yen": _whole_yen(holding.market_price_yen),
            "fair_value_yen": _whole_yen(thesis.estimates.current_fair_value_yen),
            "review_trigger": holding.market_price_yen >= thesis.estimates.current_fair_value_yen,
        },
        "replacement_comparison": replacement,
        "action": "hold",
    }
    draft = HoldingReviewDocument.model_validate(raw)
    return draft.model_copy(update={"action": evaluate_holding_review(draft).computed_action})


def validate_holding_review_scalars_from_db(
    document: HoldingReviewDocument,
    *,
    db_path: Path | None,
) -> None:
    ledger_source = document.sources.ledger
    thesis_source = document.sources.holding_thesis
    candidate_source = document.sources.candidate_thesis
    if ledger_source.entity_id != "portfolio-ledger" or ledger_source.append_head is None:
        raise HoldingReviewError("holding review ledger binding is incomplete")
    if LedgerStoreService(db_path).append_head() != ledger_source.append_head:
        raise HoldingReviewError("holding review ledger source changed after draft build")
    rebuilt = build_holding_review_from_db(
        db_path=db_path,
        holding_thesis_id=thesis_source.entity_id,
        candidate_thesis_id=(None if candidate_source is None else candidate_source.entity_id),
        position_id=document.position_id,
    )
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
        raise HoldingReviewError("holding review load-bearing values differ from DB rebuild")


def _load_ready_db_thesis(connection: sqlite3.Connection, thesis_id: str) -> ThesisDocument:
    thesis_row = connection.execute(
        "SELECT payload FROM thesis WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if thesis_row is None:
        raise HoldingReviewError(f"unknown research thesis: {thesis_id}")
    review_rows = connection.execute(
        "SELECT payload FROM thesis_review WHERE thesis_id = ? ORDER BY reviewed_at DESC",
        (thesis_id,),
    ).fetchall()
    if len(review_rows) != 1:
        raise HoldingReviewError("holding review requires exactly one independent review")
    thesis = ThesisDocument.model_validate(json.loads(str(thesis_row["payload"])))
    review = IndependentReview.model_validate(json.loads(str(review_rows[0]["payload"])))
    result = evaluate_thesis(thesis, review=review)
    if result.errors or result.decision_readiness != "ready":
        raise HoldingReviewError(
            "thesis is not ready for holding review: " + "; ".join(result.errors)
        )
    return thesis


def _thesis_market_price(thesis: ThesisDocument) -> Decimal:
    fact = next(
        item
        for item in thesis.input_snapshot.facts
        if item.fact_id == thesis.estimates.market_price_fact_id
    )
    if fact.price_basis != "last_close_unadjusted" or fact.as_of != thesis.input_snapshot.as_of:
        raise HoldingReviewError("thesis must use a same-date unadjusted close")
    if isinstance(fact.value, bool | str):
        raise HoldingReviewError("thesis market price must be numeric")
    return Decimal(str(fact.value))


def _base_5y_cagr(thesis: ThesisDocument) -> float:
    result = evaluate_thesis(thesis)
    scenario = next(
        (item for item in result.scenarios if item.horizon_years == 5 and item.name == "base"), None
    )
    if scenario is None:
        raise HoldingReviewError("thesis lacks a 5y base scenario")
    return scenario.total_return_cagr_pct


def _whole_yen(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise HoldingReviewError("holding review requires whole-yen current price and fair value")
    return int(value)
