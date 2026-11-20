"""構造契約から分離した、現在の提案write policy。"""

from __future__ import annotations

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposalDraft,
)

MAX_NEW_BUYS = 1


# 旧作業ファイルの既知の執筆指示に限る。原典説明・個別の再調査条件は判定しない。
_RESERVED_CONDITIONS = frozenset(
    {
        "買付では未約定・部分約定・新情報の後に何を再確認するかを記す。",
        (
            "現物台帳の最終事実・終値・実口座を区別する。条件付き回収は注文でなく、"
            "失う将来配当と保有の値下がりリスク、再確認する資料・時点を記す。"
        ),
        (
            "相対強弱が理由なら、自己の絶対騰落と同じ端点の指数代理・自己除外同業を照合する。"
            "市場倍率と地合い、別期間の勢いを分け、相対強弱だけでPmaxや売却を決めない。"
            "全シグナルのコメントは要求しない。"
        ),
    }
)


def _has_reserved_instruction(proposal: TradeProposalDraft) -> bool:
    if (
        proposal.headline == "提案の要点を記述してください。"
        or proposal.forgone == "採らなかった行動とその理由を記述してください。"
        or proposal.comparison.endswith(" 選んだ行動と、それを変える次の価格または企業事実を記す。")
        or (
            proposal.review is not None
            and proposal.review.reviewer_identity == "独立Reviewerのidentityを記述してください。"
        )
    ):
        return True
    return any(
        item.rationale.endswith(
            " モデル観測・一次資料によって企業の利益・普通株に帰属する資金の見通し、"
            "価格条件、または次に確かめる事実の何が変わったかを記す。変わらない場合も区別する。"
        )
        or not _RESERVED_CONDITIONS.isdisjoint(item.review_conditions)
        or (
            item.action == "defer"
            and any(r.code == "proposal_content_requires_review" for r in item.blockers)
        )
        for item in proposal.items
    )


def validate_proposal_policy(proposal: TradeProposalDraft) -> None:
    """初期版の新規買付上限を検証する。readの構造上限にはしない。"""

    if _has_reserved_instruction(proposal):
        raise ProposalError(
            "replace reserved authoring instructions with authored proposal content"
        )
    if sum(item.action == "buy" for item in proposal.items) > MAX_NEW_BUYS:
        raise ProposalError(f"current proposal policy permits at most {MAX_NEW_BUYS} new buy")
