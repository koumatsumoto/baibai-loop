"""構造契約から分離した、現在の提案write policy。"""

from __future__ import annotations

from baibai_engine.research.proposal import InvestmentProposal, ProposalError

MAX_NEW_BUYS = 1


def validate_proposal_policy(proposal: InvestmentProposal) -> None:
    """初期版の新規買付上限を検証する。readの構造上限にはしない。"""

    if sum(item.action == "buy" for item in proposal.items) > MAX_NEW_BUYS:
        raise ProposalError(f"current proposal policy permits at most {MAX_NEW_BUYS} new buy")
