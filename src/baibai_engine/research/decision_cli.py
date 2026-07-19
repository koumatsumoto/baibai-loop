"""Read-only CLI for deterministic decision-packet evaluation."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from baibai_engine.position.ledger import (
    PortfolioLedgerError,
    reconcile_portfolio,
)
from baibai_engine.position.store import (
    LedgerConflictError,
    LedgerSchemaError,
    LedgerStoreService,
)

from .decision_packet import (
    DecisionPacketError,
    evaluate_decision_packet,
    load_decision_packet,
    load_independent_review,
    result_to_payload,
)
from .execution_policy import (
    ExecutionPolicyError,
    evaluate_execution_policy,
    execution_proposal_to_payload,
    load_execution_policy_input,
    portfolio_input_from_snapshot,
    require_current_execution_input,
)


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baibai-engine research evaluate")
    parser.add_argument("packet", type=Path)
    parser.add_argument(
        "--execution-input",
        type=Path,
        help="provider-neutral quote, portfolio, quantity, and expiry input YAML",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="application DB path; BAIBAI_DB is used when omitted",
    )
    args = parser.parse_args(argv)
    try:
        document = load_decision_packet(args.packet)
        review_path = _review_path(args.packet, document.independent_review_ref)
        review = load_independent_review(review_path) if review_path is not None else None
        result = evaluate_decision_packet(document, review=review)
        payload = result_to_payload(result)
        if args.execution_input is not None:
            policy_input = load_execution_policy_input(args.execution_input)
            require_current_execution_input(
                policy_input,
                now=now or datetime.now(tz=policy_input.evaluated_at.tzinfo),
            )
            snapshot = reconcile_portfolio(LedgerStoreService(args.db).load())
            canonical_portfolio = portfolio_input_from_snapshot(
                snapshot,
                ticker=policy_input.ticker,
                sector=policy_input.sector,
                common_factors=policy_input.common_factors,
                spread_warning_bps=policy_input.portfolio.spread_warning_bps,
            )
            if policy_input.portfolio != canonical_portfolio:
                raise ExecutionPolicyError(
                    "execution input portfolio must match the canonical ledger snapshot"
                )
            policy_input = policy_input.model_copy(update={"portfolio": canonical_portfolio})
            proposal = evaluate_execution_policy(document, result, policy_input)
            payload["execution_proposal"] = execution_proposal_to_payload(proposal)
    except (
        DecisionPacketError,
        ExecutionPolicyError,
        LedgerConflictError,
        LedgerSchemaError,
        PortfolioLedgerError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    yaml.safe_dump(
        payload,
        sys.stdout,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return 0 if result.decision_readiness == "ready" else 2


def _review_path(packet_path: Path, review_ref: str | None) -> Path | None:
    if review_ref is None:
        return None
    root = packet_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise DecisionPacketError("independent_review_ref must stay beside the packet")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
