"""構造契約から分離した、現在の提案write policy。"""

from __future__ import annotations

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposalDraft,
)

MAX_NEW_BUYS = 1


def validate_proposal_policy(proposal: TradeProposalDraft) -> None:
    """初期版の新規買付上限を検証する。readの構造上限にはしない。"""

    if sum(item.action == "buy" for item in proposal.items) > MAX_NEW_BUYS:
        raise ProposalError(f"current proposal policy permits at most {MAX_NEW_BUYS} new buy")
    # 初回の新形式を発行する前に、現在稼働中のexact reader・Web・Case完了を確認する。
    for item in proposal.items:
        if item.action == "buy" and any(
            reason.code
            not in {"price_grid_rule_unavailable", "security_classification_unavailable"}
            for reason in item.terms_unavailable_reasons
        ):
            raise ProposalError(
                "buy terms may be unavailable only for price grid or classification"
            )
