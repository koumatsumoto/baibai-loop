"""Build a source-bound holding review from a ledger and current decision packets."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

from baibai_loop.position.holding_review import (
    HoldingReviewDocument,
    HoldingReviewError,
    evaluate_holding_review,
)
from baibai_loop.position.ledger import load_portfolio_ledger, reconcile_portfolio
from baibai_loop.thesis.decision_packet import (
    DecisionPacketDocument,
    evaluate_decision_packet,
    load_decision_packet,
)


def build_holding_review(
    *,
    root: Path,
    ledger_ref: Path,
    holding_packet_ref: Path,
    position_id: str,
    candidate_packet_ref: Path | None = None,
) -> HoldingReviewDocument:
    """Derive every load-bearing review scalar from immutable source artifacts.

    A review only accepts a current packet whose decision-time unadjusted close
    is the same price and date as the ledger valuation. This prevents an entry
    packet or adjusted series from being copied into a current holding decision.
    """

    ledger_path = _resolve(root, ledger_ref)
    holding_path = _resolve(root, holding_packet_ref)
    ledger = load_portfolio_ledger(ledger_path)
    packet = load_decision_packet(holding_path)
    as_of = ledger.as_of.date()
    _require_current_packet(packet, as_of=as_of)
    snapshot = reconcile_portfolio(ledger)
    holding = next(
        (item for item in snapshot.holdings if item.ticker == packet.input_snapshot.ticker),
        None,
    )
    if holding is None:
        raise HoldingReviewError("holding packet ticker has no open ledger holding")
    packet_price = _packet_market_price(packet)
    if packet_price != holding.market_price_yen or holding.market_price_observed_at.date() != as_of:
        raise HoldingReviewError(
            "holding packet market price does not match ledger unadjusted close"
        )
    current_cagr = _base_5y_cagr(packet)
    sources: dict[str, object] = {
        "ledger": _source_ref(root, ledger_path),
        "holding_packet": _source_ref(root, holding_path),
        "candidate_packet": None,
    }
    replacement: dict[str, object] = {"status": "no_candidate"}
    if candidate_packet_ref is not None:
        candidate_path = _resolve(root, candidate_packet_ref)
        candidate = load_decision_packet(candidate_path)
        _require_current_packet(candidate, as_of=as_of)
        if candidate.input_snapshot.ticker == holding.ticker:
            raise HoldingReviewError("replacement candidate ticker must differ from holding ticker")
        sources["candidate_packet"] = _source_ref(root, candidate_path)
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
    latest_evidence = max(risk.as_of for risk in packet.permanent_loss_risks)
    raw: dict[str, object] = {
        "schema_version": 2,
        "as_of": as_of.isoformat(),
        "position_id": position_id,
        "ticker": holding.ticker,
        "sources": sources,
        "thesis_health": {
            "invalidation_status": (
                "broken" if packet.judgment.permanent_loss_conclusion == "elevated" else "intact"
            ),
            "permanent_loss_axes": [
                {
                    "axis": risk.axis,
                    "assessment": risk.assessment,
                    "evidence_status": risk.evidence_status,
                }
                for risk in packet.permanent_loss_risks
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
            "fair_value_yen": _whole_yen(packet.estimates.current_fair_value_yen),
            "review_trigger": holding.market_price_yen >= packet.estimates.current_fair_value_yen,
        },
        "replacement_comparison": replacement,
        "action": "hold",
    }
    draft = HoldingReviewDocument.model_validate(raw)
    return draft.model_copy(update={"action": evaluate_holding_review(draft).computed_action})


def validate_holding_review_scalars(document: HoldingReviewDocument, *, root: Path) -> None:
    """Ensure persisted load-bearing review fields equal a fresh source rebuild."""

    rebuilt = build_holding_review(
        root=root,
        ledger_ref=Path(document.sources.ledger.ref),
        holding_packet_ref=Path(document.sources.holding_packet.ref),
        candidate_packet_ref=(
            None
            if document.sources.candidate_packet is None
            else Path(document.sources.candidate_packet.ref)
        ),
        position_id=document.position_id,
    )
    fields = (
        "as_of",
        "ticker",
        "thesis_health",
        "valuation_review",
        "replacement_comparison",
        "action",
    )
    expected = rebuilt.model_dump(mode="json", include=set(fields))
    actual = document.model_dump(mode="json", include=set(fields))
    if actual != expected:
        raise HoldingReviewError("holding review load-bearing values differ from source rebuild")


def _require_current_packet(packet: DecisionPacketDocument, *, as_of: date) -> None:
    if packet.input_snapshot.as_of != as_of:
        raise HoldingReviewError("decision packet as_of must equal ledger valuation date")
    result = evaluate_decision_packet(packet)
    if result.errors or result.decision_readiness != "ready":
        raise HoldingReviewError(
            "decision packet is not ready for holding review: " + "; ".join(result.errors)
        )


def _packet_market_price(packet: DecisionPacketDocument) -> Decimal:
    fact = next(
        item
        for item in packet.input_snapshot.facts
        if item.fact_id == packet.estimates.market_price_fact_id
    )
    if fact.price_basis != "last_close_unadjusted" or fact.as_of != packet.input_snapshot.as_of:
        raise HoldingReviewError("decision packet must use a same-date unadjusted close")
    if isinstance(fact.value, bool | str):
        raise HoldingReviewError("decision packet market price must be numeric")
    return Decimal(str(fact.value))


def _base_5y_cagr(packet: DecisionPacketDocument) -> float:
    result = evaluate_decision_packet(packet)
    scenario = next(
        (item for item in result.scenarios if item.horizon_years == 5 and item.name == "base"), None
    )
    if scenario is None:
        raise HoldingReviewError("decision packet lacks a 5y base scenario")
    return scenario.total_return_cagr_pct


def _whole_yen(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise HoldingReviewError("holding review requires whole-yen current price and fair value")
    return int(value)


def _source_ref(root: Path, path: Path) -> dict[str, str]:
    return {
        "ref": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _resolve(root: Path, reference: Path) -> Path:
    path = reference if reference.is_absolute() else root / reference
    if not path.is_file():
        raise HoldingReviewError(f"source is missing: {reference}")
    return path
